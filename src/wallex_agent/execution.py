"""Order execution safety core.

Guarantees (each enforced here and tested with ``SimExchange`` fault injection):

* persist-before-send: ``OrderRecord(INTENT)`` -> ``SENDING`` (+ a pre-send balance/trade snapshot)
  are committed in ONE transaction before ``create_order``; a ``client_id`` is POSTed at most once, ever.
* a POST whose outcome is unknown (timeout, 5xx, undecodable, rate limit, ...) is never retried: it is
  resolved from exchange facts (order lookups, open orders, trades, balance delta vs the snapshot);
  "not found anywhere and nothing moved" => ``ABANDONED`` + quarantine of that symbol/side; any
  contradiction => bot ``STOPPED`` (``needs_manual_reconcile``) with a loud audit record.
* an ABANDONED order stays on a permanent watch list (derived from the persisted orders): on EVERY
  poll, before every new order and at startup it is looked up again (by id, in the open orders, in
  the trades); if it ever shows up (an exchange that accepted it late) => ALERT, cancel it (cancel
  only), ``STOPPED`` (``late_accept``). Its quarantine is never lifted on absence alone: only after
  ``QUARANTINE_MIN_CHECKS`` consecutive absent re-checks spanning ``QUARANTINE_MIN_CHECKS`` x
  ``unknown_resolve_window_s``.
* in-run ledger vs exchange: a balance shortfall beyond tolerance with no order of ours in flight
  blocks new orders at once and, confirmed on a later check, STOPS the bot with the numbers.
* fills are recorded only from observed ``executed_qty`` increases; fees come from matching
  ``get_trades`` rows, else are estimated and flagged ``fee_source='estimated'``.
* bot state transitions follow observed facts; ``*_PENDING`` only while an order is in flight/active.
* kill switch (``<data_dir>/KILL`` or in-process) is checked before every send; ``kill`` only cancels.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol

from .clock import Clock
from .config import Config
from .errors import ExchangeError, InvariantError, KillSwitchActive, OrderNotFound, RejectedError
from .portfolio import Portfolio
from .risk import RiskContext, RiskGate
from .state import StateMachine, derive_state_explained
from .store import FillRecord, OrderRecord
from .types import (BPS, ONE, ORDER_TRANSITIONS, ZERO, Balance, BotState, MarketRules, OrderBook,
                    OrderInfo, OrderLifecycle as L, OrderRequest, OrderStatus, Side, Trade, TradeIntent,
                    plain, quantize_down, quantize_up)

CORRECTABLE_CODES = frozenset({1001, 1002, 1004, 1006})
# 'canary': one tiny, deliberately non-fillable USDTTMN BUY used to verify API behaviour before
# live trading (see canary.py). It goes through the same persist-before-send / kill / unknown-outcome
# path as every other order; only the strategy/edge gates (risk.py) are not involved.
PURPOSES = ("convert", "entry", "exit", "flatten", "canary")
K_RUN = "exec.run_id"
K_SEQ = "exec.seq"
K_QUARANTINE = "exec.quarantine"
K_ATTRIBUTED = "exec.attributed_trades"
K_PRESEND = "exec.presend."
K_MISSING = "exec.not_found."
K_STOP = "exec.stop"
K_SHORTFALL = "exec.shortfall_suspect"
DEFAULT_TOLERANCE: dict[str, Decimal] = {"BTC": Decimal("0.0000001"), "USDT": Decimal("0.01"),
                                         "TMN": Decimal("10")}
TRADE_TIME_SKEW_S = 120.0
MAX_NOT_FOUND = 3
MAX_ATTRIBUTED_KEPT = 5000
QUARANTINE_MIN_CHECKS = 3          # absent re-checks (each >= unknown_resolve_window_s apart) to lift
SHORTFALL_CONFIRM_S = 15.0         # a balance shortfall must persist this long (2nd check) to STOP
StopCheck = Callable[[], "str | None"]   # returns why to stop now (shutdown, deadline), or None


def drift_text(rr: Any) -> str:
    """``TMN ledger 5000000 exchange 4999989 short 11`` for every SHORT asset of a ReconcileReport."""
    parts = []
    for a, d in sorted(getattr(rr, "drifts", {}).items()):
        if d.direction != "short":
            continue
        actual = d.actual_value + (d.actual_locked if d.interpretation == "value+locked" else ZERO)
        parts.append(f"{a} ledger {plain(d.expected)} exchange {plain(actual)} short {plain(-d.drift)}")
    if not parts:
        parts = [str(r) for r in getattr(rr, "reasons", [])]
    return "; ".join(parts)


def _brief(evidence: Mapping[str, Any], limit: int = 240) -> str:
    """Compact one-line rendering of contradiction evidence for the human-readable stop reason."""
    text = "; ".join(f"{k}={v}" for k, v in jsonable(dict(evidence)).items()
                     if k not in ("info", "found") and v not in (None, "", [], {}))
    return text if len(text) <= limit else text[: limit - 3] + "..."

_STATUS = {L.FILLED: "filled", L.PARTIAL: "partial", L.OPEN: "open", L.CANCELED: "canceled",
           L.REJECTED: "rejected", L.ABANDONED: "abandoned", L.UNKNOWN_OUTCOME: "unknown",
           L.SENDING: "unknown", L.INTENT: "unknown"}


# --------------------------------------------------------------------------- audit sink
class AuditSink(Protocol):
    def write(self, kind: str, payload: dict[str, Any]) -> None: ...


class NullAudit:
    def write(self, kind: str, payload: dict[str, Any]) -> None:
        return None


class ListAudit:
    """In-memory sink for tests."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append((kind, payload))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.records]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [p for k, p in self.records if k == kind]


def jsonable(o: Any) -> Any:
    """Decimals -> plain strings, enums -> values, dataclasses -> dicts (audit/status payloads)."""
    if isinstance(o, Decimal):
        return plain(o)
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, Mapping):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in o]
    if is_dataclass(o) and not isinstance(o, type):
        return {f.name: jsonable(getattr(o, f.name)) for f in fields(o)}
    return o


class SimulatedCrash(BaseException):
    """Test hook: raised at a named stage to abandon the engine like a process kill would.
    BaseException so that no ``except Exception`` in the engine can swallow it."""


# --------------------------------------------------------------------------- results
@dataclass
class ExecutionResult:
    order_ids: list[str] = field(default_factory=list)
    filled_qty: Decimal = ZERO
    status: str = "declined"  # filled|partial|open|canceled|rejected|declined|blocked|abandoned|unknown|stopped
    notes: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass
class ConversionResult:
    status: str     # done|already_funded|insufficient|nothing_to_convert|waiting|declined|blocked|stopped|interrupted
    rounds: int = 0
    order_ids: list[str] = field(default_factory=list)
    tmn_spent: Decimal = ZERO
    usdt_received: Decimal = ZERO
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


class _Decline(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class _Adjust:
    """Corrections applied after a definitive, correctable rejection (new client_id each time)."""

    price_cut: int = 0          # 1001: send one fewer price decimal
    qty_cut: int = 0            # 1002: send one fewer quantity decimal
    extra_margin: Decimal = ZERO  # 1006: more balance headroom

    def correct(self, code: int | None) -> "_Adjust":
        if code == 1001:
            return _Adjust(self.price_cut + 1, self.qty_cut, self.extra_margin)
        if code == 1002:
            return _Adjust(self.price_cut, self.qty_cut + 1, self.extra_margin)
        if code == 1006:
            return _Adjust(self.price_cut, self.qty_cut, self.extra_margin + Decimal("0.005"))
        return self   # 1004: rules are re-fetched; an unchanged plan means give up


@dataclass(frozen=True)
class _Plan:
    price: Decimal
    qty: Decimal
    fee_rate: Decimal
    rules: MarketRules
    notes: tuple[str, ...]


def trade_keys(trades: Iterable[Trade]) -> list[tuple[str, Trade]]:
    """Stable identities for ``get_trades`` rows (they carry no id): content + occurrence index,
    oldest first."""
    seen: Counter[str] = Counter()
    out: list[tuple[str, Trade]] = []
    for t in sorted(trades, key=lambda t: (t.ts_epoch, t.timestamp, t.price, t.quantity, t.fee)):
        base = (f"{t.symbol}|{t.side.value}|{t.timestamp}|{plain(t.price)}|{plain(t.quantity)}|"
                f"{plain(t.fee)}|{t.fee_asset}")
        out.append((f"{base}#{seen[base]}", t))
        seen[base] += 1
    return out


def _info_dict(info: OrderInfo) -> dict[str, Any]:
    return jsonable(info)


# --------------------------------------------------------------------------- engine
class ExecutionEngine:
    def __init__(self, exchange: Any, store: Any, portfolio: Portfolio, state_machine: StateMachine,
                 config: Config, clock: Clock, audit: AuditSink | None = None, *,
                 tolerance: Mapping[str, Decimal] | None = None, rng: random.Random | None = None) -> None:
        self.exchange = exchange
        self.store = store
        self.portfolio = portfolio
        self.sm = state_machine
        self.config = config
        self.clock = clock
        self.audit = audit or NullAudit()
        self.tolerance = dict(DEFAULT_TOLERANCE if tolerance is None else tolerance)
        self.rng = rng or random.Random()
        self.crash_at: set[str] = set()        # test hook (see SimulatedCrash)
        self.posts = 0                          # create_order calls made by THIS engine object
        self.last_cancel_error: str | None = None   # exception type of the last cancel_order call
        self._killed = False
        self._rules_cache: dict[str, MarketRules] | None = None
        self._fees_cache: dict[str, Any] | None = None
        self._last_poll_ts: float | None = None
        run = store.kv_get(K_RUN)
        if run is None:
            run = f"{int(clock.now()) % 0xFFFFFFFF:08x}{self.rng.getrandbits(16):04x}"
            store.kv_set(K_RUN, run)
        self.run_id: str = run

    # ------------------------------------------------------------------ small helpers
    def _now(self) -> float:
        return float(self.clock.now())

    def _audit(self, kind: str, payload: Mapping[str, Any]) -> None:
        self.audit.write(kind, jsonable(dict(payload)))

    def _alert(self, kind: str, payload: Mapping[str, Any]) -> None:
        self._audit("ALERT", {"level": "critical", "alert": kind, **payload})

    def _maybe_crash(self, stage: str) -> None:
        if stage in self.crash_at:
            raise SimulatedCrash(stage)

    def _new_client_id(self) -> str:
        seq = int(self.store.kv_get(K_SEQ, 0)) + 1
        self.store.kv_set(K_SEQ, seq)
        return f"wa-{self.run_id}-{seq}-{self.rng.getrandbits(24):06x}"

    def _pair(self, symbol: str) -> tuple[str, str]:
        r = (self._rules_cache or {}).get(symbol)
        if r is not None:
            return r.base, r.quote
        pf = self.portfolio
        if symbol == pf.btc_symbol:
            return pf.base, pf.quote
        if symbol == pf.usdt_symbol:
            return pf.quote, pf.fiat
        raise _Decline("rules_unknown", symbol)

    # ------------------------------------------------------------------ kill switch
    def kill_requested(self) -> bool:
        return self._killed or self.config.kill_file.exists()

    def _assert_not_killed(self) -> None:
        if self.kill_requested():
            self._audit("send_refused", {"reason": "kill_switch"})
            raise KillSwitchActive("kill switch engaged: no new orders")

    def kill(self, cancel_open: bool = True, reason: str = "operator") -> dict[str, Any]:
        """Engage the kill switch: optionally cancel (never sell) our open orders, then STOPPED."""
        self._killed = True
        out: dict[str, Any] = {"cancelled": [], "still_open": [], "unresolved": []}
        if cancel_open:
            for rec in self.store.unresolved_orders():
                if rec.lifecycle in (L.SENDING, L.UNKNOWN_OUTCOME, L.INTENT):
                    self._resolve_one(rec)
                rec = self.store.get_order(rec.client_id)
                if rec.lifecycle in (L.OPEN, L.PARTIAL):
                    rec = self._cancel_and_verify(rec)
                    (out["still_open"] if rec.lifecycle in (L.OPEN, L.PARTIAL) else out["cancelled"]).append(
                        rec.client_id)
                elif not rec.is_terminal:
                    out["unresolved"].append(rec.client_id)
        if self.sm.state is not BotState.STOPPED:
            self.sm.transition(BotState.STOPPED, f"kill: {reason}", {"cancel_open": cancel_open})
        self._alert("kill", {"reason": reason, **out})
        return out

    # ------------------------------------------------------------------ market knowledge
    def _rules(self, symbol: str, *, refresh: bool = False) -> MarketRules:
        if refresh or self._rules_cache is None:
            try:
                self._rules_cache = dict(self.exchange.get_markets())
            except Exception as exc:
                raise _Decline("rules_unavailable", type(exc).__name__) from exc
        r = self._rules_cache.get(symbol)
        if r is None:
            raise _Decline("rules_unknown", symbol)
        return r

    def _fee_rate(self, symbol: str, *, refresh: bool = False) -> Decimal:
        if refresh or self._fees_cache is None or symbol not in self._fees_cache:
            try:
                self._fees_cache = dict(self.exchange.get_fees())
            except Exception as exc:
                raise _Decline("fee_unavailable", type(exc).__name__) from exc
        f = self._fees_cache.get(symbol)
        if f is None:
            raise _Decline("fee_unknown", symbol)
        rate = f.taker
        if not (ZERO <= rate < Decimal("0.05")):
            raise _Decline("fee_implausible", f"{symbol} taker {rate}")
        return rate

    def _fresh_book(self, symbol: str) -> OrderBook:
        try:
            book = self.exchange.get_order_book(symbol)
        except Exception as exc:
            raise _Decline("book_unavailable", type(exc).__name__) from exc
        if not book.bids or not book.asks:
            raise _Decline("book_missing", symbol)
        if book.best_bid >= book.best_ask:
            raise _Decline("book_crossed", symbol)
        age = book.age(self._now())
        if age > self.config.max_book_age_s:
            raise _Decline("book_stale", f"{symbol} age {age:.1f}s > {self.config.max_book_age_s}s")
        return book

    def _balances(self) -> dict[str, Balance]:
        try:
            return dict(self.exchange.get_balances())
        except Exception as exc:
            raise _Decline("balances_unavailable", type(exc).__name__) from exc

    # ------------------------------------------------------------------ quarantine
    def quarantined(self) -> frozenset[tuple[str, Side]]:
        q = self.store.kv_get(K_QUARANTINE, {}) or {}
        return frozenset((v["symbol"], Side(v["side"])) for v in q.values())

    def _quarantine_add(self, rec: OrderRecord) -> None:
        q = dict(self.store.kv_get(K_QUARANTINE, {}) or {})
        q[f"{rec.symbol}:{rec.side.value}"] = {
            "symbol": rec.symbol, "side": rec.side.value, "client_id": rec.client_id,
            "ids": [i for i in (rec.client_id, rec.exchange_client_order_id) if i], "since": self._now(),
            "absent_checks": 0, "last_check_ts": None}
        self.store.kv_set(K_QUARANTINE, q)

    # ------------------------------------------------------------------ abandoned-order watch list
    def watched_abandoned(self) -> list[OrderRecord]:
        """Every ABANDONED order that was ever handed to the exchange (it has a pre-send snapshot).
        Derived from the persisted orders, so the list is permanent and survives restarts."""
        return [r for r in self.store.list_orders([L.ABANDONED])
                if self.store.kv_get(K_PRESEND + r.client_id) is not None]

    def _unexplained_trades(self, rec: OrderRecord) -> tuple[str, dict[str, Any]]:
        """('ok'|'moved'|'unknown'|'skipped', numbers): trades on the order's symbol/side since its
        pre-send snapshot vs the fills we recorded since then."""
        if any(o.symbol == rec.symbol and o.side is rec.side for o in self.store.unresolved_orders()):
            return "skipped", {}          # one of OUR orders is in flight there: its fills are not final
        snap = self.store.kv_get(K_PRESEND + rec.client_id) or {}
        try:
            trades = self.exchange.get_trades(rec.symbol, rec.side)
        except Exception:
            return "unknown", {}
        pre = set(snap.get("trade_keys", []))
        new_qty = sum((t.quantity for k, t in trade_keys(trades) if k not in pre), ZERO)
        since = float(snap.get("ts", rec.created_ts or 0.0))
        recorded = sum((f.quantity for f in self.store.list_fills()
                        if f.symbol == rec.symbol and f.side is rec.side and f.ts >= since), ZERO)
        if new_qty > recorded:
            return "moved", {"new_trade_qty": new_qty, "recorded_fill_qty": recorded}
        return "ok", {}

    def check_abandoned(self, *, stage: str) -> bool | None:
        """Re-check every watched ABANDONED order. True: all still absent; False: one appeared or
        something unexplained happened (the bot is now STOPPED); None: could not verify (a read
        failed) - callers must not send a new order then. Also counts quarantine re-checks."""
        watched = self.watched_abandoned()
        if not watched:
            return True
        unverified: set[str] = set()
        for rec in watched:
            info, ok = self._find_order([rec.client_id, rec.exchange_client_order_id or ""], rec.symbol)
            if info is not None:
                self._late_accept(rec, info, stage=stage)
                return False
            status, numbers = self._unexplained_trades(rec)
            if status == "moved":
                self._contradiction(
                    f"unexplained {rec.side.value} trades on {rec.symbol} after ABANDONED order {rec.client_id}: "
                    f"{plain(numbers['new_trade_qty'])} traded vs {plain(numbers['recorded_fill_qty'])} recorded "
                    "(possible late accept of that order)", {"client_id": rec.client_id, **numbers, "stage": stage})
                return False
            if not ok or status == "unknown":
                unverified.add(rec.client_id)
        for sym in sorted({r.symbol for r in watched}):
            try:
                opens = list(self.exchange.list_open_orders(sym))
            except Exception:
                unverified |= {r.client_id for r in watched if r.symbol == sym}
                continue
            for o in opens:
                known = self.store.find_order_by_exchange_id(o.client_order_id)
                if known is None or known.is_terminal:
                    cids = ",".join(r.client_id for r in watched if r.symbol == sym)
                    self._contradiction(
                        f"open order {o.client_order_id} ({o.side.value} {plain(o.orig_qty)} {o.symbol} @ "
                        f"{plain(o.price)}) is unknown to the store while ABANDONED order(s) {cids} are watched "
                        "(possible late accept: if it is ours, cancel it in the Wallex app)",
                        {"exchange_id": o.client_order_id, "stage": stage, "info": _info_dict(o)})
                    return False
        self._count_quarantine_checks(unverified)
        if unverified:
            self._audit("abandoned_watch_unverified", {"stage": stage, "client_ids": sorted(unverified)})
            return None
        return True

    def _count_quarantine_checks(self, unverified: set[str]) -> None:
        """A quarantine is lifted only after QUARANTINE_MIN_CHECKS consecutive verified-absent
        re-checks, each >= unknown_resolve_window_s after the previous one (and after the abandon),
        i.e. never on one look and never sooner than QUARANTINE_MIN_CHECKS x the window."""
        q = dict(self.store.kv_get(K_QUARANTINE, {}) or {})
        if not q:
            return
        now, window = self._now(), float(self.config.unknown_resolve_window_s)
        changed = False
        for key, e in list(q.items()):
            if e.get("client_id") in unverified:
                if e.get("absent_checks"):
                    e["absent_checks"], changed = 0, True       # not consecutive any more
                continue
            last = e.get("last_check_ts")
            if now - float(e["since"]) < window or (last is not None and now - float(last) < window):
                continue
            e["absent_checks"] = int(e.get("absent_checks") or 0) + 1
            e["last_check_ts"] = now
            changed = True
            if e["absent_checks"] >= QUARANTINE_MIN_CHECKS and now - float(e["since"]) >= QUARANTINE_MIN_CHECKS * window:
                q.pop(key)
                self._audit("quarantine_lifted", {"key": key, "client_id": e["client_id"],
                                                  "absent_checks": e["absent_checks"],
                                                  "span_s": round(now - float(e["since"]), 3),
                                                  "still_watched": True})
        if changed:
            self.store.kv_set(K_QUARANTINE, q)

    def _late_accept(self, rec: OrderRecord, info: OrderInfo, *, stage: str) -> None:
        """An ABANDONED order exists on the exchange after all: cancel it (cancel only) and STOP with
        the ledger/exchange numbers it explains. Its fills cannot be adopted (the record is final)."""
        oid = info.client_order_id or rec.client_id
        cancel = "not needed (not active)"
        if info.active:
            try:
                self.exchange.cancel_order(oid)
                cancel = "requested"
            except Exception as exc:  # noqa: BLE001 - reported, the stop happens anyway
                cancel = f"FAILED ({type(exc).__name__}): cancel it in the Wallex app"
            try:
                after = self.exchange.get_order(oid)
                info = after
                cancel += f"; now {'ACTIVE' if after.active else 'inactive'}"
            except Exception as exc:  # noqa: BLE001
                cancel += f"; not verified ({type(exc).__name__})"
        base, quote = self._pair(rec.symbol)
        numbers = self._ledger_vs_exchange((base, quote))
        reason = (f"late_accept: ABANDONED order {rec.client_id} ({rec.side.value} {plain(rec.quantity)} {rec.symbol} "
                  f"@ {plain(rec.price)}) exists on the exchange ({info.status.value}, executed "
                  f"{plain(info.executed_qty)} {base} for {plain(info.executed_sum)} {quote}); cancel {cancel}; "
                  f"ledger vs exchange: {numbers}. The difference is that order's execution (fee not included)")
        evidence = {"client_id": rec.client_id, "stage": stage, "found": _info_dict(info), "cancel": cancel}
        self._alert("late_accept", {"reason": reason, "evidence": jsonable(evidence)})
        self._stop(reason, evidence)

    def _ledger_vs_exchange(self, assets: Iterable[str]) -> str:
        ledger = self.portfolio.expected_balances()
        try:
            bals = dict(self.exchange.get_balances())
        except Exception as exc:  # noqa: BLE001
            return f"exchange balances unavailable ({type(exc).__name__}); ledger " + ", ".join(
                f"{a} {plain(ledger.get(a, ZERO))}" for a in assets)
        parts = []
        for a in assets:
            b = bals.get(a)
            parts.append(f"{a} ledger {plain(ledger.get(a, ZERO))} exchange {plain(b.value if b else ZERO)}"
                         + (f" (locked {plain(b.locked)})" if b and b.locked else ""))
        return "; ".join(parts)

    # ------------------------------------------------------------------ in-run balance check
    def check_balances(self, *, stage: str) -> dict[str, Any]:
        """Ledger vs exchange during the run. With no order of ours in flight, a shortfall beyond
        tolerance blocks every new order at once and - still there on a check at least
        ``SHORTFALL_CONFIRM_S`` later (balances may settle a moment after a fill) - STOPS the bot
        (``needs_manual_reconcile`` with the numbers), exactly like the startup check."""
        if self.sm.state is BotState.STOPPED:
            return {"ok": None, "skipped": "bot STOPPED"}
        if self.store.unresolved_orders():
            return {"ok": None, "skipped": "orders in flight"}
        try:
            bals = self._balances()
        except _Decline as d:
            return {"ok": None, "error": str(d)}
        rr = self.portfolio.reconcile(bals, self.tolerance, locked_semantics="unknown")
        out: dict[str, Any] = {"ok": rr.ok, "reasons": rr.reasons, "excess": rr.excess, "stage": stage}
        suspect = self.store.kv_get(K_SHORTFALL)
        if rr.ok:
            if suspect:
                self.store.kv_set(K_SHORTFALL, None)
                self._audit("balance_shortfall_cleared", {"stage": stage, "was": suspect})
            return out
        text = drift_text(rr)
        now = self._now()
        if not suspect:
            self.store.kv_set(K_SHORTFALL, {"since": now, "text": text, "stage": stage})
            self._alert("balance_shortfall_suspected", {"reason": text, "stage": stage,
                                                        "note": "no new order until re-checked"})
            out["confirmed"] = False
            return out
        if now - float(suspect.get("since", now)) < SHORTFALL_CONFIRM_S:
            out["confirmed"] = False
            return out
        self._contradiction(f"ledger/exchange shortfall: {text}",
                            {"reasons": rr.reasons, "stage": stage, "first_seen": suspect.get("since")})
        out["confirmed"] = True
        return out

    # ------------------------------------------------------------------ contradictions
    def _stop(self, reason: str, evidence: Mapping[str, Any]) -> None:
        ev = jsonable(dict(evidence))
        self.store.kv_set(K_STOP, {"reason": reason, "ts": self._now(), "evidence": ev})
        if self.sm.state is not BotState.STOPPED:
            self.sm.transition(BotState.STOPPED, reason[:500], ev)

    def _contradiction(self, reason: str, evidence: Mapping[str, Any]) -> None:
        ev = jsonable(dict(evidence))
        self._alert("needs_manual_reconcile", {"reason": reason, "evidence": ev})
        self._stop(f"needs_manual_reconcile: {reason}", ev)

    # ------------------------------------------------------------------ planning
    def _state_problem(self, intent: TradeIntent) -> str | None:
        st = self.sm.state
        btc, usd = self.config.btc_symbol, self.config.usdt_symbol
        ok = {
            "entry": intent.side is Side.BUY and intent.symbol == btc and st is BotState.READY,
            "exit": intent.side is Side.SELL and intent.symbol == btc and st is BotState.BTC_HELD,
            "convert": intent.side is Side.BUY and intent.symbol == usd
            and st in (BotState.UNFUNDED, BotState.CONVERTING, BotState.READY),
            "flatten": intent.side is Side.SELL and (
                (intent.symbol == btc and st is BotState.BTC_HELD)
                or (intent.symbol == usd and st is BotState.READY)),
            "canary": intent.side is Side.BUY and intent.symbol == usd
            and intent.style == "passive_limit" and st in (BotState.UNFUNDED, BotState.READY),
        }.get(intent.purpose, False)
        return None if ok else f"state_mismatch: {intent.purpose} {intent.side.value} {intent.symbol} in {st.value}"

    def _pending_state(self, intent: TradeIntent) -> BotState | None:
        if intent.purpose == "entry":
            return BotState.BUY_PENDING
        if intent.symbol == self.config.btc_symbol and intent.side is Side.SELL:
            return BotState.SELL_PENDING
        if intent.purpose == "convert":
            return BotState.CONVERTING
        return None

    def _blocker(self, intent: TradeIntent) -> str | None:
        if self.sm.state is BotState.STOPPED:
            return "bot_stopped"
        if self.store.unresolved_orders():
            return "unresolved_orders"
        watch = self.check_abandoned(stage="pre_send")      # a late accept must never meet a new order
        if self.sm.state is BotState.STOPPED:
            return f"bot_stopped: {str((self.store.kv_get(K_STOP) or {}).get('reason', ''))[:200]}"
        if watch is None:
            return ("abandoned_watch_unverified: an ABANDONED order could not be re-checked (exchange read "
                    "failed); no new order until it can be")
        if (intent.symbol, intent.side) in self.quarantined():
            return f"quarantined: {intent.symbol} {intent.side.value} after an unknown outcome"
        suspect = self.store.kv_get(K_SHORTFALL)
        if suspect:
            return f"balance_shortfall_unconfirmed: {suspect.get('text')}"
        return self._state_problem(intent)

    def _plan(self, intent: TradeIntent, adj: _Adjust) -> _Plan:
        cfg = self.config
        if intent.purpose not in PURPOSES:
            raise _Decline("invalid_intent", f"purpose {intent.purpose!r}")
        if intent.symbol not in (cfg.btc_symbol, cfg.usdt_symbol):
            raise _Decline("symbol_not_allowed", intent.symbol)
        if intent.quantity <= 0 or intent.reference_price <= 0 or intent.max_slippage_bps < 0:
            raise _Decline("invalid_intent", "quantity/reference_price must be > 0, slippage >= 0")
        cap_bps = cfg.conversion_slippage_bps if intent.symbol == cfg.usdt_symbol else cfg.max_slippage_bps
        if intent.max_slippage_bps > cap_bps:
            raise _Decline("slippage_cap_too_high", f"{intent.max_slippage_bps} > {cap_bps} bps")
        rules = self._rules(intent.symbol, refresh=True)
        rate = self._fee_rate(intent.symbol, refresh=True)
        book = self._fresh_book(intent.symbol)
        bals = self._balances()
        ledger = self.portfolio.expected_balances()
        pdec = max(0, rules.price_decimals - adj.price_cut)
        qdec = max(0, rules.qty_decimals - adj.qty_cut)
        side = intent.side
        slip = intent.max_slippage_bps / BPS
        notes: list[str] = []

        def spendable(asset: str) -> Decimal:
            b = bals.get(asset)
            return max(ZERO, min(b.spendable if b else ZERO, ledger.get(asset, ZERO)))

        if side is Side.BUY:
            cap = quantize_down(intent.reference_price * (ONE + slip), pdec)
            budget = spendable(rules.quote)
            target = intent.quantity
            held = ZERO
        else:
            cap = quantize_up(intent.reference_price * (ONE - slip), pdec)
            held = spendable(rules.base)
            if held <= 0:
                raise _Decline("nothing_to_sell", f"{rules.base} held (ledger/spendable) is 0")
            target = min(intent.quantity, held)
            budget = ZERO
            if target < intent.quantity:
                notes.append(f"sell clamped {intent.quantity} -> {target} (min of ledger and spendable)")
        if cap <= 0:
            raise _Decline("invalid_intent", "slippage cap price <= 0")
        headroom = ONE + rate + cfg.fee_safety_margin + adj.extra_margin

        if intent.style == "passive_limit":
            if side is Side.BUY:
                price = quantize_down(min(book.best_bid, cap), pdec)
                qty = min(target, budget / (price * headroom)) if price > 0 else ZERO
            else:
                price = quantize_up(max(book.best_ask, cap), pdec)
                qty = target
        elif intent.style == "aggressive_limit":
            levels = book.asks if side is Side.BUY else book.bids
            within = [lv for lv in levels if (lv.price <= cap if side is Side.BUY else lv.price >= cap)]
            if not within:
                best = book.best_ask if side is Side.BUY else book.best_bid
                raise _Decline("no_liquidity_within_slippage_cap",
                               f"best {best} beyond cap {cap} ({intent.max_slippage_bps} bps from "
                               f"{intent.reference_price})")

            def limit_for(q: Decimal) -> Decimal:
                cum = ZERO
                for lv in within:
                    cum += lv.quantity
                    if cum >= q:
                        return lv.price
                return within[-1].price

            qty = min(target, sum((lv.quantity for lv in within), ZERO))
            price = limit_for(qty)
            if side is Side.BUY:
                for _ in range(3):
                    afford = budget / (price * headroom)
                    if qty <= afford:
                        break
                    qty = afford
                    price = limit_for(qty)
                price = quantize_down(price, pdec)
            else:
                price = quantize_up(price, pdec)
            if qty < target:
                notes.append(f"size reduced {target} -> {qty}: visible liquidity within the "
                             f"{intent.max_slippage_bps} bps cap and/or budget")
        else:
            raise _Decline("invalid_intent", f"style {intent.style!r}")

        qty = quantize_down(qty, qdec)
        if side is Side.BUY and qty * price * headroom > budget:
            qty = quantize_down(budget / (price * headroom), qdec)
        if side is Side.SELL and qty > held:
            raise InvariantError(f"planned sell {qty} > held {held}")   # unreachable by construction
        viol = rules.violations(price, qty)
        if viol:
            raise _Decline("below_minimum", f"{','.join(viol)} (price {price}, qty {qty}); not sent")
        return _Plan(price, qty, rate, rules, tuple(notes))

    def _snapshot(self, symbol: str, side: Side) -> dict[str, Any]:
        base, quote = self._pair(symbol)
        bals = self._balances()
        try:
            trades = self.exchange.get_trades(symbol, side)
        except Exception as exc:
            raise _Decline("trades_unavailable", type(exc).__name__) from exc

        def bv(a: str) -> list[str]:
            b = bals.get(a)
            return [str(b.value if b else ZERO), str(b.locked if b else ZERO)]

        return {"ts": self._now(), "assets": [base, quote], "balances": {base: bv(base), quote: bv(quote)},
                "trade_keys": [k for k, _ in trade_keys(trades)]}

    # ------------------------------------------------------------------ submit
    def submit(self, intent: TradeIntent, decision_id: str | None = None) -> ExecutionResult:
        """Execute one intent. Raises ``KillSwitchActive`` if the kill switch is engaged."""
        self._assert_not_killed()
        res = ExecutionResult(status="declined")
        block = self._blocker(intent)
        if block:
            res.status, res.reason = "blocked", block
            self._audit("submit_blocked", {"intent": intent, "reason": block, "decision_id": decision_id})
            return res
        adj = _Adjust()
        last_terms: tuple[Decimal, Decimal] | None = None
        attempts = max(1, int(self.config.max_orders_per_intent))
        for attempt in range(1, attempts + 1):
            try:
                plan = self._plan(intent, adj)
                if last_terms == (plan.price, plan.qty):
                    res.status, res.reason = "rejected", "rejected: correction did not change the order; giving up"
                    break
                res.notes.extend(plan.notes)
                kind, rec, err = self._send(intent, plan, decision_id)
            except _Decline as d:
                res.status = "declined" if not res.order_ids else "rejected"
                res.reason = str(d)
                self._audit("submit_declined", {"intent": intent, "reason": str(d), "decision_id": decision_id})
                break
            res.order_ids.append(rec.client_id)
            if kind == "rejected":
                assert err is not None
                self._settle_state()
                res.notes.append(f"attempt {attempt}: rejected code={err.error_code}")
                if err.error_code in CORRECTABLE_CODES and attempt < attempts:
                    adj, last_terms = adj.correct(err.error_code), (plan.price, plan.qty)
                    continue
                res.status = "rejected"
                res.reason = (f"rejected code={err.error_code}: {err.message[:120]}"
                              + (f" (gave up after {attempt} attempts)" if err.error_code in CORRECTABLE_CODES else ""))
                break
            res.status = _STATUS[rec.lifecycle]
            if self.sm.state is BotState.STOPPED and not rec.is_terminal:
                res.status = "stopped"
            break
        self._settle_state()
        res.filled_qty = sum((self.store.get_order(i).executed_qty for i in res.order_ids), ZERO)
        self._audit("submit_result", {"intent": intent, "decision_id": decision_id, "result": res})
        return res

    def _send(self, intent: TradeIntent, plan: _Plan, decision_id: str | None
              ) -> tuple[str, OrderRecord, RejectedError | None]:
        symbol, side = intent.symbol, intent.side
        snapshot = self._snapshot(symbol, side)
        self._assert_not_killed()
        to_state = self._pending_state(intent)
        with self.store.transaction():
            cid = self._new_client_id()
            self.store.insert_order_intent(OrderRecord(
                client_id=cid, symbol=symbol, side=side, price=plan.price, quantity=plan.qty,
                purpose=intent.purpose, decision_id=decision_id, note=intent.note[:200]))
            self._maybe_crash("after_intent")
            self.store.update_order(cid, lifecycle=L.SENDING)
            self.store.kv_set(K_PRESEND + cid, snapshot)
            if to_state is not None and self.sm.state is not to_state:
                self.sm.transition(to_state, f"order {cid} in flight ({intent.purpose})",
                                   {"client_id": cid, "price": plan.price, "qty": plan.qty})
        self._audit("order_sending", {"client_id": cid, "symbol": symbol, "side": side, "price": plan.price,
                                      "qty": plan.qty, "purpose": intent.purpose, "decision_id": decision_id})
        self._maybe_crash("after_sending")
        req = OrderRequest(cid, symbol, side, plan.price, plan.qty)
        self.posts += 1
        try:
            info = self.exchange.create_order(req)       # the ONE AND ONLY POST for this client_id
        except RejectedError as exc:
            rec = self.store.update_order(cid, lifecycle=L.REJECTED,
                                          note=f"rejected code={exc.error_code}: {exc.message[:200]}")
            self._audit("order_rejected", {"client_id": cid, "code": exc.error_code, "message": exc.message[:200]})
            return "rejected", rec, exc
        except Exception as exc:   # anything else on a write: outcome UNKNOWN, never re-sent
            self._maybe_crash("after_post")
            self.store.update_order(cid, lifecycle=L.UNKNOWN_OUTCOME,
                                    note=f"POST outcome unknown: {type(exc).__name__}: {str(exc)[:160]}")
            self._alert("unknown_outcome", {"client_id": cid, "error": type(exc).__name__})
            self.resolve_unknown(self.store.get_order(cid))
            return "resolved", self.store.get_order(cid), None
        self._maybe_crash("after_post")
        rec = self._apply_info(cid, info, source="ack")
        self._maybe_crash("after_ack")
        return "ack", rec, None

    # ------------------------------------------------------------------ applying exchange facts
    @staticmethod
    def _target_lifecycle(rec: OrderRecord, info: OrderInfo) -> L:
        if info.executed_qty >= rec.quantity:
            return L.FILLED
        if info.status is OrderStatus.CANCELED or not info.active:
            return L.CANCELED
        return L.PARTIAL if info.executed_qty > 0 else L.OPEN

    def _apply_info(self, cid: str, info: OrderInfo, *, source: str) -> OrderRecord:
        rec = self.store.get_order(cid)
        problems: list[str] = []
        if info.symbol != rec.symbol or info.side is not rec.side:
            problems.append(f"symbol/side {info.symbol}/{info.side.value} != {rec.symbol}/{rec.side.value}")
        if info.orig_qty != rec.quantity or info.price != rec.price:
            problems.append(f"terms {info.price}x{info.orig_qty} != {rec.price}x{rec.quantity}")
        if info.executed_qty < 0 or info.executed_sum < 0 or info.executed_qty > rec.quantity:
            problems.append(f"executed {info.executed_qty} out of range")
        if info.executed_qty < rec.executed_qty or info.executed_sum < rec.executed_sum:
            problems.append("executed amounts went backwards")
        if info.status is OrderStatus.FILLED and info.executed_qty < rec.quantity:
            problems.append("status FILLED with executed < quantity")
        if rec.lifecycle in (L.INTENT, L.REJECTED, L.ABANDONED):
            problems.append(f"order exists on exchange but is locally {rec.lifecycle.value}")
        if (rec.exchange_client_order_id is not None and info.client_order_id not in
                (rec.exchange_client_order_id, rec.client_id)):
            problems.append(f"exchange id {info.client_order_id} != {rec.exchange_client_order_id}")
        delta_q = info.executed_qty - rec.executed_qty
        delta_s = info.executed_sum - rec.executed_sum
        if not problems and ((delta_q > 0 and delta_s <= 0) or (delta_q == 0 and delta_s != 0)):
            problems.append(f"inconsistent execution delta qty={delta_q} sum={delta_s}")
        target = self._target_lifecycle(rec, info)
        if not problems and rec.is_terminal:
            if delta_q != 0 or target is not rec.lifecycle:
                problems.append(f"terminal {rec.lifecycle.value} order changed on exchange -> {target.value}")
            else:
                return rec
        if not problems and target is not rec.lifecycle and target not in ORDER_TRANSITIONS[rec.lifecycle]:
            problems.append(f"illegal lifecycle {rec.lifecycle.value} -> {target.value}")
        if problems:
            self._contradiction(f"exchange order {cid} contradicts the local record ({source}): "
                                + "; ".join(problems),
                                {"client_id": cid, "source": source, "problems": problems,
                                 "info": _info_dict(info)})
            return rec
        if info.status is OrderStatus.UNKNOWN:
            self._audit("order_status_unrecognised", {"client_id": cid, "raw_status": info.raw_status})

        fee = ZERO
        fee_asset: str | None = None
        fee_source: str | None = None
        keys: list[str] = []
        base, quote = self._pair(rec.symbol)
        if delta_q > 0:
            m = self._match_trades(rec, delta_q, self._safe_trades(rec.symbol, rec.side))
            if m is not None:
                fee, fee_asset, keys = m
                fee_source = "exchange"
            else:
                try:
                    rate = self._fee_rate(rec.symbol)
                except _Decline as d:
                    self._audit("fill_deferred", {"client_id": cid, "reason": str(d)})
                    return rec       # nothing recorded; retried on the next poll
                fee = (delta_q if rec.side is Side.BUY else delta_s) * rate
                fee_asset = base if rec.side is Side.BUY else quote   # received asset (CONTRACT §1)
                fee_source = "estimated"
        try:
            with self.store.transaction():
                if delta_q > 0:
                    assert fee_asset is not None and fee_source is not None
                    self.portfolio.apply_fill(FillRecord(
                        fill_key=f"{cid}:{plain(info.executed_qty)}", client_id=cid, symbol=rec.symbol,
                        side=rec.side, price=delta_s / delta_q, quantity=delta_q, quote_sum=delta_s,
                        fee=fee, fee_asset=fee_asset, fee_source=fee_source, ts=self._now()))
                    if keys:
                        att = list(self.store.kv_get(K_ATTRIBUTED, []) or []) + keys
                        self.store.kv_set(K_ATTRIBUTED, att[-MAX_ATTRIBUTED_KEPT:])
                changes: dict[str, Any] = {"executed_qty": info.executed_qty,
                                           "executed_sum": info.executed_sum, "last_raw": _info_dict(info)}
                if delta_q > 0:
                    changes.update(fee_total=rec.fee_total + fee, fee_asset=fee_asset,
                                   fee_source="estimated" if "estimated" in (fee_source, rec.fee_source)
                                   else "exchange")
                if rec.exchange_client_order_id is None and info.client_order_id:
                    changes["exchange_client_order_id"] = info.client_order_id
                self.store.update_order(cid, lifecycle=None if target is rec.lifecycle else target, **changes)
                if self.store.kv_get(K_MISSING + cid):
                    self.store.kv_set(K_MISSING + cid, 0)
        except InvariantError as exc:
            self._contradiction(f"recording exchange facts for {cid} violated an invariant: {str(exc)[:200]}",
                                {"client_id": cid, "error": str(exc), "info": _info_dict(info)})
            return self.store.get_order(cid)
        new = self.store.get_order(cid)
        self._audit("order_update", {"client_id": cid, "source": source, "lifecycle": new.lifecycle,
                                     "executed_qty": new.executed_qty, "delta_qty": delta_q,
                                     "fee": fee, "fee_asset": fee_asset, "fee_source": fee_source})
        return new

    def _safe_trades(self, symbol: str, side: Side) -> list[Trade] | None:
        try:
            return list(self.exchange.get_trades(symbol, side))
        except Exception:
            return None

    def _match_trades(self, rec: OrderRecord, delta_q: Decimal, trades: list[Trade] | None
                      ) -> tuple[Decimal, str, list[str]] | None:
        """Attribute un-attributed trade rows (symbol, side, time window, limit price, quantity) to an
        execution delta. None => fee must be estimated."""
        if not trades:
            return None
        attributed = set(self.store.kv_get(K_ATTRIBUTED, []) or [])
        snap = self.store.kv_get(K_PRESEND + rec.client_id) or {}
        pre = set(snap.get("trade_keys", []))
        lo = float(rec.created_ts or 0) - TRADE_TIME_SKEW_S
        hi = self._now() + TRADE_TIME_SKEW_S
        remaining = delta_q
        chosen: list[tuple[str, Trade]] = []
        for k, t in trade_keys(trades):
            if k in attributed or k in pre or t.symbol != rec.symbol or t.side is not rec.side:
                continue
            if not (lo <= t.ts_epoch <= hi):
                continue
            if (rec.side is Side.BUY and t.price > rec.price) or (rec.side is Side.SELL and t.price < rec.price):
                continue
            if t.quantity <= remaining:
                chosen.append((k, t))
                remaining -= t.quantity
                if remaining == 0:
                    break
        if remaining != 0 or not chosen or len({t.fee_asset for _, t in chosen}) != 1:
            return None
        return sum((t.fee for _, t in chosen), ZERO), chosen[0][1].fee_asset, [k for k, _ in chosen]

    # ------------------------------------------------------------------ unknown outcomes
    def _find_order(self, ids: Iterable[str], symbol: str) -> tuple[OrderInfo | None, bool]:
        """(info, all_reads_ok). ``OrderNotFound`` is a successful read that found nothing."""
        ids = [i for i in dict.fromkeys(ids) if i]
        ok = True
        for i in ids:
            try:
                return self.exchange.get_order(i), True
            except OrderNotFound:
                pass
            except Exception:
                ok = False
        try:
            for o in self.exchange.list_open_orders(symbol):
                if o.client_order_id in ids:
                    return o, True
        except Exception:
            ok = False
        return None, ok

    def _evidence(self, snap: Mapping[str, Any], rec: OrderRecord) -> tuple[bool, dict[str, Any]]:
        try:
            bals = self.exchange.get_balances()
            trades = self.exchange.get_trades(rec.symbol, rec.side)
        except Exception:
            return False, {}
        changes: dict[str, Any] = {}
        for a in snap.get("assets", []):
            b = bals.get(a)
            cur = [str(b.value if b else ZERO), str(b.locked if b else ZERO)]
            was = snap["balances"].get(a, ["0", "0"])
            if [Decimal(x) for x in cur] != [Decimal(x) for x in was]:
                changes[a] = {"before": was, "after": cur}
        pre = set(snap.get("trade_keys", []))
        attributed = set(self.store.kv_get(K_ATTRIBUTED, []) or [])
        new = [t for k, t in trade_keys(trades) if k not in pre and k not in attributed]
        if new:
            changes["new_trades"] = {"count": len(new), "qty": sum((t.quantity for t in new), ZERO)}
        return True, changes

    def resolve_unknown(self, rec: OrderRecord, *, window_s: float | None = None,
                        adopt_only: bool = False) -> str:
        """Resolve a SENDING/UNKNOWN_OUTCOME order WITHOUT re-sending it. Returns 'adopted',
        'abandoned', 'stopped' (contradiction) or 'unresolved' (exchange unreachable; stays blocking).
        ``adopt_only``: a single lookup that can only adopt (used once the bot is already STOPPED)."""
        cid = rec.client_id
        rec = self.store.get_order(cid)
        if rec.lifecycle is L.SENDING:
            rec = self.store.update_order(cid, lifecycle=L.UNKNOWN_OUTCOME,
                                          note="found SENDING: crashed between persist and acknowledgement")
        if rec.lifecycle is not L.UNKNOWN_OUTCOME:
            return rec.lifecycle.value
        snap = self.store.kv_get(K_PRESEND + cid)
        ids = [cid, rec.exchange_client_order_id or ""]
        window = 0.0 if adopt_only else (
            self.config.unknown_resolve_window_s if window_s is None else float(window_s))
        interval = max(1.0, window / 4)
        start = self.clock.monotonic()
        consistent = 0
        changed: dict[str, Any] | None = None
        while True:
            info, ok = self._find_order(ids, rec.symbol)
            if info is not None:
                new = self._apply_info(cid, info, source="resolve_unknown")
                if new.lifecycle is L.UNKNOWN_OUTCOME:
                    return "stopped" if self.sm.state is BotState.STOPPED else "unresolved"
                self._audit("unknown_resolved", {"client_id": cid, "outcome": "adopted",
                                                 "lifecycle": new.lifecycle})
                return "adopted"
            if ok and snap is not None and not adopt_only:
                ok2, ch = self._evidence(snap, rec)
                if ok2:
                    if ch:
                        changed = ch
                    else:
                        consistent += 1
            elapsed = self.clock.monotonic() - start
            if elapsed >= window:
                break
            self.clock.sleep(min(interval, window - elapsed))
        if adopt_only:
            return "unresolved"
        if snap is None:
            self._contradiction(f"unknown outcome of {cid} without a pre-send snapshot", {"client_id": cid})
            return "stopped"
        if changed is not None:
            self._contradiction(f"order {cid} not found but balances/trades moved: {_brief(changed)}",
                                {"client_id": cid, "changes": changed})
            return "stopped"
        if consistent >= (2 if window > 0 else 1):
            with self.store.transaction():
                self.store.update_order(cid, lifecycle=L.ABANDONED,
                                        note=f"not found after {consistent} consistent checks; balances unchanged")
                self._quarantine_add(self.store.get_order(cid))
            self._audit("unknown_resolved", {"client_id": cid, "outcome": "abandoned", "checks": consistent,
                                             "quarantine": f"{rec.symbol}:{rec.side.value}"})
            return "abandoned"
        self._alert("unknown_unresolved", {"client_id": cid, "consistent_checks": consistent})
        return "unresolved"

    # ------------------------------------------------------------------ polling
    def _resolve_one(self, rec: OrderRecord) -> None:
        if rec.lifecycle is L.INTENT:   # cannot persist alone (INTENT+SENDING commit together); defensive
            self.store.update_order(rec.client_id, lifecycle=L.ABANDONED, note="never sent (INTENT only)")
        elif rec.lifecycle in (L.SENDING, L.UNKNOWN_OUTCOME):
            self.resolve_unknown(rec, adopt_only=self.sm.state is BotState.STOPPED)

    def _refresh(self, rec: OrderRecord) -> OrderRecord:
        oid = rec.exchange_client_order_id or rec.client_id
        try:
            info = self.exchange.get_order(oid)
        except OrderNotFound:
            n = int(self.store.kv_get(K_MISSING + rec.client_id, 0) or 0) + 1
            self.store.kv_set(K_MISSING + rec.client_id, n)
            try:
                info = next((o for o in self.exchange.list_open_orders(rec.symbol)
                             if o.client_order_id in (oid, rec.client_id)), None)
            except Exception:
                info = None
            if info is not None:
                return self._apply_info(rec.client_id, info, source="open_orders")
            self._audit("order_not_found", {"client_id": rec.client_id, "count": n})
            if n >= MAX_NOT_FOUND:
                self._contradiction(f"acknowledged order {rec.client_id} ({rec.side.value} {plain(rec.quantity)} "
                                    f"{rec.symbol}) not found {n} times in a row",
                                    {"client_id": rec.client_id, "count": n})
            return rec
        except Exception as exc:
            self._audit("read_failed", {"client_id": rec.client_id, "op": "get_order", "error": type(exc).__name__})
            return rec
        return self._apply_info(rec.client_id, info, source="poll")

    def _cancel_and_verify(self, rec: OrderRecord) -> OrderRecord:
        oid = rec.exchange_client_order_id or rec.client_id
        err = None
        try:
            self.exchange.cancel_order(oid)
        except Exception as exc:     # failed, already done, or unknown: the GET below decides
            err = type(exc).__name__
        self.last_cancel_error = err
        try:
            info = self.exchange.get_order(oid)
        except Exception as exc:
            self._audit("cancel_unverified", {"client_id": rec.client_id, "cancel_error": err,
                                              "verify_error": type(exc).__name__})
            return self.store.get_order(rec.client_id)
        new = self._apply_info(rec.client_id, info, source="cancel_verify")
        if new.lifecycle in (L.OPEN, L.PARTIAL):
            self._audit("cancel_failed", {"client_id": rec.client_id, "cancel_error": err})
        elif new.lifecycle is L.FILLED:
            self._audit("filled_during_cancel", {"client_id": rec.client_id, "executed_qty": new.executed_qty})
        else:
            self._audit("order_cancelled", {"client_id": rec.client_id, "executed_qty": new.executed_qty})
        return new

    def cancel_and_verify(self, client_id: str) -> OrderRecord:
        """Cancel ONE of our OPEN/PARTIAL orders and verify the result with ``get_order`` (cancel
        only; a fill that races the cancel is recorded through the normal fill path)."""
        rec = self.store.get_order(client_id)
        if rec is None:
            raise InvariantError(f"unknown order {client_id!r}")
        if rec.lifecycle not in (L.OPEN, L.PARTIAL):
            return rec
        new = self._cancel_and_verify(rec)
        self._settle_state()
        return new

    def cancel_open_orders(self, reason: str = "operator") -> dict[str, Any]:
        """Cancel (never sell) every OPEN/PARTIAL order of ours and verify each, WITHOUT engaging the
        kill switch or stopping the bot: used for graceful shutdown and at the deadline, so the run
        stays resumable."""
        out: dict[str, Any] = {"reason": reason, "cancelled": [], "still_open": []}
        for rec in self.store.unresolved_orders():
            if rec.lifecycle not in (L.OPEN, L.PARTIAL):
                continue
            new = self._cancel_and_verify(rec)
            (out["still_open"] if new.lifecycle in (L.OPEN, L.PARTIAL) else out["cancelled"]).append(
                new.client_id)
        self._settle_state()
        self._audit("cancel_open_orders", out)
        return out

    def poll_and_manage(self, *, check_balances: bool = True) -> dict[str, Any]:
        """Refresh every unresolved order from the exchange, record fills, cancel timed-out orders
        (and verify), re-check every watched ABANDONED order (and count quarantine re-checks), settle
        the bot state from the facts, then (``check_balances``) compare the ledger with the exchange."""
        fills_before = self.store.count_fills()
        report: dict[str, Any] = {"checked": 0, "cancel_attempts": [], "fills": 0}
        now = self._now()
        for rec in self.store.unresolved_orders():
            rec = self.store.get_order(rec.client_id)
            report["checked"] += 1
            if rec.lifecycle in (L.INTENT, L.SENDING, L.UNKNOWN_OUTCOME):
                self._resolve_one(rec)
                continue
            rec = self._refresh(rec)
            if rec.lifecycle in (L.OPEN, L.PARTIAL) and now - float(rec.created_ts or now) >= self.config.order_timeout_s:
                report["cancel_attempts"].append(rec.client_id)
                self._cancel_and_verify(rec)
        if self.sm.state is not BotState.STOPPED:
            report["abandoned_watch"] = self.check_abandoned(stage="poll")
        self._settle_state()
        if check_balances:
            report["balances"] = self.check_balances(stage="poll")
        report["fills"] = self.store.count_fills() - fills_before
        report["state"] = self.sm.state.value
        self._last_poll_ts = now
        return report

    def _base_sellable(self) -> bool:
        btc = self.config.btc_symbol
        base, _ = self._pair(btc)
        qty = self.portfolio.expected_balances().get(base, ZERO)
        try:
            r = self._rules(btc)
        except _Decline:
            return qty > 0
        q = r.round_qty(qty) if qty > 0 else ZERO
        if q <= 0 or q < r.min_qty:
            return False
        try:
            price = self.exchange.get_order_book(btc).best_bid
        except Exception:
            last = [o for o in self.store.list_orders() if o.symbol == btc]
            price = last[-1].price if last else ZERO
        return q * price >= r.min_notional if price > 0 else True

    def _settle_state(self) -> None:
        st = self.sm.state
        if st not in (BotState.BUY_PENDING, BotState.SELL_PENDING):
            return
        btc = self.config.btc_symbol
        if any(o.symbol == btc for o in self.store.unresolved_orders()):
            return
        held = self._base_sellable()
        target = BotState.BTC_HELD if held else BotState.READY
        base, _ = self._pair(btc)
        self.sm.transition(target, f"{btc} orders resolved; sellable {base} held: {held}",
                           {"ledger_base": self.portfolio.expected_balances().get(base, ZERO)})

    # ------------------------------------------------------------------ conversion stage
    def _unresolved_on(self, symbol: str) -> bool:
        return any(o.symbol == symbol for o in self.store.unresolved_orders())

    def _wait_for_orders(self, symbol: str, should_stop: StopCheck | None = None) -> None:
        cfg = self.config
        step = max(1.0, cfg.order_timeout_s / 4)
        limit = cfg.order_timeout_s + cfg.unknown_resolve_window_s + 4 * step
        waited = 0.0
        self.poll_and_manage()
        while self._unresolved_on(symbol) and waited < limit and self.sm.state is not BotState.STOPPED:
            if should_stop is not None and should_stop():
                return                # the caller's shutdown cancels/settles whatever is still open
            self.clock.sleep(step)
            waited += step
            self.poll_and_manage()

    def _conversion_verdict(self, intent: TradeIntent, book: OrderBook, bals: Mapping[str, Balance]) -> Any:
        """The SAME RiskGate check the runner applies to the agent's conversion decision (state, kill,
        quarantine, rules, fee, book freshness/crossing, slippage cap, spread, depth within the cap,
        rule violations, balance), re-run on fresh facts before EVERY conversion round."""
        ctx = RiskContext(state=self.sm.state, balances=bals, fees=self._fees_cache, rules=self._rules_cache,
                          books={intent.symbol: book}, now=self._now(), ledger=self.portfolio.expected_balances(),
                          kill=self.kill_requested(), unresolved_orders=bool(self.store.unresolved_orders()),
                          quarantined=self.quarantined())
        return RiskGate(self.config, self.clock).evaluate(intent, ctx)

    def convert_tmn_to_usdt(self, *, max_rounds: int = 8, decision_id: str | None = None,
                            should_stop: StopCheck | None = None) -> ConversionResult:
        """Resumable UNFUNDED -> CONVERTING -> READY stage. Inspects ACTUAL balances (never assumes
        the funds are TMN or USDT), converts spendable TMN (bounded by the ledger) with aggressive
        limits, cancels unfilled remainders and repeats until the residual is below the minimum.

        Before EVERY round (not only the first, which the runner gated): ``should_stop()`` (a shutdown
        request or the deadline: returns a reason) ends the stage, the kill switch raises
        ``KillSwitchActive``, and the round must pass the same RiskGate check as the agent's decision
        (a spread blow-out between rounds is never bought into). Orders carry ``decision_id``."""
        cfg = self.config
        usd, btc = cfg.usdt_symbol, cfg.btc_symbol
        out = ConversionResult(status="nothing_to_convert")
        self._assert_not_killed()
        for rnd in range(1, max_rounds + 1):
            self._wait_for_orders(usd, should_stop)
            if self.sm.state is BotState.STOPPED:
                out.status = "stopped"
                return self._conversion_totals(out)
            if self._unresolved_on(usd):
                out.status = "waiting"
                return self._conversion_totals(out)
            if self.sm.state not in (BotState.UNFUNDED, BotState.CONVERTING, BotState.READY):
                out.status = "blocked"
                out.notes.append(f"state {self.sm.state.value}")
                return self._conversion_totals(out)
            why_stop = should_stop() if should_stop is not None else None
            if why_stop:
                out.status = "interrupted"
                out.notes.append(f"round {rnd} not sent: {why_stop}")
                return self._conversion_totals(out)
            self._assert_not_killed()
            try:
                ur = self._rules(usd, refresh=True)
                rate = self._fee_rate(usd, refresh=True)
                book = self._fresh_book(usd)
                bals = self._balances()
            except _Decline as d:
                out.status = "declined"
                out.notes.append(str(d))
                return self._conversion_totals(out)
            ledger = self.portfolio.expected_balances()
            tb = bals.get(ur.quote)
            tmn = max(ZERO, min(tb.spendable if tb else ZERO, ledger.get(ur.quote, ZERO)))
            ask = book.best_ask
            headroom = ONE + rate + cfg.fee_safety_margin
            est_qty = ur.round_qty(tmn / (ask * (ONE + cfg.conversion_slippage_bps / BPS) * headroom))
            if est_qty <= 0 or ur.violations(ur.round_price(ask, Side.BUY), est_qty):
                out.notes.append(f"residual {ur.quote} {tmn} below conversion minimum")
                break
            intent = TradeIntent(purpose="convert", side=Side.BUY, symbol=usd, quantity=tmn / ask,
                                 reference_price=ask, max_slippage_bps=cfg.conversion_slippage_bps,
                                 note=f"{ur.quote}->{ur.base} conversion round {rnd}")
            verdict = self._conversion_verdict(intent, book, bals)
            if not verdict.approved:
                out.status = "declined"
                out.notes.append(f"round {rnd} refused by the risk gate: {'; '.join(verdict.reasons)[:300]}")
                self._audit("conversion_round_refused", {"round": rnd, "reasons": verdict.reasons,
                                                         "metrics": verdict.metrics, "decision_id": decision_id})
                return self._conversion_totals(out)
            res = self.submit(intent, decision_id)
            if res.order_ids:
                out.rounds += 1
            out.order_ids.extend(res.order_ids)
            out.notes.extend(res.notes)
            if res.status in ("declined", "blocked", "rejected"):
                out.notes.append(f"round stopped: {res.status} {res.reason}")
                break
            if res.status in ("stopped", "unknown", "abandoned"):
                out.status = res.status
                return self._conversion_totals(out)
        self._wait_for_orders(usd, should_stop)
        if self._unresolved_on(usd):
            out.status = "waiting"
            return self._conversion_totals(out)
        if self.sm.state is BotState.STOPPED:
            out.status = "stopped"
            return self._conversion_totals(out)
        ledger = self.portfolio.expected_balances()
        base_usd, _ = self._pair(usd)
        usdt = ledger.get(base_usd, ZERO)
        try:
            min_btc_notional = self._rules(btc).min_notional
        except _Decline as d:
            out.status = "declined"
            out.notes.append(str(d))
            return self._conversion_totals(out)
        st = self.sm.state
        if usdt >= min_btc_notional:
            if st in (BotState.UNFUNDED, BotState.CONVERTING):
                self.sm.transition(BotState.READY, f"conversion complete: {base_usd} {usdt}",
                                   {"rounds": out.rounds, "orders": out.order_ids})
            out.status = "done" if out.rounds else "already_funded"
        else:
            if st is BotState.CONVERTING:
                self.sm.transition(BotState.UNFUNDED, f"conversion left only {base_usd} {usdt}",
                                   {"orders": out.order_ids})
            out.status = "insufficient" if out.rounds else "nothing_to_convert"
        return self._conversion_totals(out)

    def _conversion_totals(self, out: ConversionResult) -> ConversionResult:
        usd = self.config.usdt_symbol
        base, quote = self._pair(usd)
        tmn = usdt = ZERO
        for cid in out.order_ids:
            for f in self.store.list_fills(cid):
                if f.symbol != usd or f.side is not Side.BUY:
                    continue
                tmn += f.quote_sum + (f.fee if f.fee_asset == quote else ZERO)
                usdt += f.quantity - (f.fee if f.fee_asset == base else ZERO)
        out.tmn_spent, out.usdt_received = tmn, usdt
        if tmn > 0 and usdt > 0:
            self.portfolio.record_conversion(tmn, usdt, note=f"orders {','.join(out.order_ids)}")
        self._audit("conversion", {"result": out})
        return out

    # ------------------------------------------------------------------ restart recovery
    def reconcile_startup(self) -> dict[str, Any]:
        """Resolve every unresolved order against the exchange, look for unknown open orders, compare
        the fill ledger with real balances and confirm the bot state. Unexplained drift => STOPPED."""
        report: dict[str, Any] = {"ok": False, "actions": [], "drift": None}
        report["poll"] = self.poll_and_manage(check_balances=False)    # the strict check is below
        cfg = self.config
        if report["poll"].get("abandoned_watch") is None and self.sm.state is not BotState.STOPPED:
            report["actions"].append("abandoned_watch_unverified: an ABANDONED order could not be re-checked")
            return report
        try:
            opens = [o for o in self.exchange.list_open_orders()
                     if o.symbol in (cfg.btc_symbol, cfg.usdt_symbol)]
        except Exception as exc:
            report["actions"].append(f"open_orders_unavailable: {type(exc).__name__}")
            return report
        for o in opens:
            rec = self.store.find_order_by_exchange_id(o.client_order_id)
            if rec is None or rec.is_terminal:
                self._contradiction(
                    f"open order {o.client_order_id} ({o.side.value} {plain(o.orig_qty)} {o.symbol} @ "
                    f"{plain(o.price)}) on the exchange is unknown to (or closed in) the store",
                    {"exchange_id": o.client_order_id, "info": _info_dict(o)})
        try:
            bals = self._balances()
        except _Decline as d:
            report["actions"].append(str(d))
            return report
        rr = self.portfolio.reconcile(bals, self.tolerance, locked_semantics="unknown")
        report["drift"] = {"ok": rr.ok, "reasons": rr.reasons, "excess": rr.excess, "anomalies": rr.anomalies,
                           "text": drift_text(rr) if not rr.ok else ""}
        if not rr.ok:
            self._contradiction(f"ledger/exchange shortfall at startup: {drift_text(rr)}",
                                {"reasons": rr.reasons})
        if self.sm.state is not BotState.STOPPED:
            try:
                rules = {s: self._rules(s, refresh=True) for s in (cfg.btc_symbol, cfg.usdt_symbol)}
            except _Decline as d:
                report["actions"].append(str(d))
                report["state"] = self.sm.state.value
                return report
            books: dict[str, OrderBook] = {}
            try:
                books[cfg.btc_symbol] = self.exchange.get_order_book(cfg.btc_symbol)
            except Exception:
                pass
            ledger = {a: Balance(a, v) for a, v in self.portfolio.expected_balances().items() if v >= 0}
            derived, why = derive_state_explained(ledger, self.store.unresolved_orders(), rules, books,
                                                  btc_symbol=cfg.btc_symbol, usdt_symbol=cfg.usdt_symbol)
            cur = self.sm.state
            report["derived"] = {"state": derived.value, "why": why}
            if derived is not cur:
                if cur is BotState.CONVERTING and derived in (BotState.UNFUNDED, BotState.READY):
                    report["actions"].append("conversion stage to be resumed by convert_tmn_to_usdt")
                elif cur is BotState.UNFUNDED and derived is BotState.READY:
                    self.sm.transition(BotState.READY, f"restart: {why}", {"derived": derived.value})
                    report["actions"].append("UNFUNDED -> READY")
                else:
                    self._contradiction(f"persisted bot state {cur.value} disagrees with the ledger-derived "
                                        f"state {derived.value} ({why})",
                                        {"persisted": cur.value, "derived": derived.value, "why": why})
        report["state"] = self.sm.state.value
        report["ok"] = self.sm.state is not BotState.STOPPED and rr.ok
        self._audit("reconcile_startup", report)
        return report

    # ------------------------------------------------------------------ reporting
    def status(self) -> dict[str, Any]:
        now = self._now()
        orders = self.store.list_orders()
        counts = Counter(o.lifecycle.value for o in orders)
        unresolved = [{"client_id": o.client_id, "exchange_id": o.exchange_client_order_id,
                       "symbol": o.symbol, "side": o.side.value, "lifecycle": o.lifecycle.value,
                       "price": plain(o.price), "quantity": plain(o.quantity),
                       "executed_qty": plain(o.executed_qty), "age_s": now - float(o.created_ts or now)}
                      for o in orders if not o.is_terminal]
        fills = self.store.list_fills()
        q = self.store.kv_get(K_QUARANTINE, {}) or {}
        return jsonable({
            "state": self.sm.state.value, "kill_requested": self.kill_requested(),
            "stop": self.store.kv_get(K_STOP), "run_id": self.run_id,
            "orders_by_lifecycle": dict(counts), "unresolved": unresolved,
            "quarantine": sorted(q.keys()), "fills": len(fills),
            "abandoned_watch": [r.client_id for r in self.watched_abandoned()],
            "balance_shortfall_suspect": self.store.kv_get(K_SHORTFALL),
            "estimated_fee_fills": sum(1 for f in fills if f.fee_source == "estimated"),
            "ledger": self.portfolio.expected_balances(), "fees_paid": self.portfolio.fees_paid(),
            "posts_this_process": self.posts, "last_poll_ts": self._last_poll_ts})
