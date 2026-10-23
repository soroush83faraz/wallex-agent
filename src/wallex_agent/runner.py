"""AgentRunner: one cycle = observe -> reason/plan -> tool/action -> verify -> reflect.

Per cycle (every stage writes audit records, also when it fails):

1. kill check (``<data_dir>/KILL`` or in-process) -> cancel our open orders (never sell), STOPPED.
2. ``engine.poll_and_manage()``: refresh unresolved orders, record fills, cancel timed-out orders.
3. observe: rules, fees, balances, books (live: the account's REAL fee via ``get_fees``). Anything
   unknown or stale => no order this cycle, and the reason is audited; the loop keeps going.
4. deadline: cancel resting orders, apply ``final_policy`` (``report_only`` sells NOTHING; the
   ``flatten_*`` policies only through ``RiskGate.forced_exit`` + the normal engine path), STOPPED,
   ``final_report.json`` + ``trace.txt``.
5. ``RiskGate.forced_exit`` (hard stop-loss / drawdown) then ``brain.decide``; the decision is ALWAYS
   recorded (``brain.record_decision``) with the risk verdict attached.
6. ``RiskGate.evaluate`` -> ``engine.submit`` or ``engine.convert_tmn_to_usdt`` -> verify by re-reading
   orders and balances through the engine -> ``brain.on_order_result``; a closed BTC round trip in the
   ledger -> ``brain.on_round_trip_closed``.
7. ``brain.reflect_pending``; equity (net of exit costs) -> ``portfolio.record_equity``.

Unexpected exceptions in a cycle are caught, audited as ``alert`` and counted; after
``MAX_CONSECUTIVE_FAILURES`` in a row the runner cancels open orders (cancel only) and goes STOPPED.
The competition start is stored in the store's kv the first time the runner may trade and is never
reset by a restart; the deadline is ``start + competition_hours``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent import Observation
from .errors import KillSwitchActive, error_label
from .market_data import StaleQuote, ensure_fresh
from .reporting import Scrubber, build_final_report, render_trace, write_final_report
from .risk import RiskContext
from .types import ZERO, BotState, Decision, FeeRates, OrderBook, Side, TradeIntent, plain

MAX_CONSECUTIVE_FAILURES = 5
DEADLINE_GRACE_CYCLES = 10
K_START = "runner.competition_start"
K_TRIP = "runner.trip"
K_REPORTED = "runner.reported_fills"
K_LAST_LOSS = "runner.last_loss_ts"
K_DEADLINE = "runner.deadline"
K_BOOKS = "runner.last_books"       # last observed books/fees: lets `report` value a run offline
K_FEES = "runner.last_fees"
K_LABELS = "runner.labels"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_GATE = 3
EXIT_KILLED = 4
EXIT_STOPPED = 5
EXIT_REFUSED = 6
EXIT_UNAVAILABLE = 7
EXIT_INTERRUPTED = 130

STATUS_EXIT = {"deadline": EXIT_OK, "data_end": EXIT_OK, "session_end": EXIT_OK, "stage_done": EXIT_OK,
               "max_cycles": EXIT_OK, "killed": EXIT_KILLED, "stopped": EXIT_STOPPED,
               "unavailable": EXIT_UNAVAILABLE, "interrupted": EXIT_INTERRUPTED}
FINAL_STATUSES = frozenset({"deadline", "data_end", "session_end", "killed", "stopped"})


@dataclass
class RunResult:
    status: str
    exit_code: int
    cycles: int
    reason: str
    report_path: Path | None = None
    trace_path: Path | None = None
    report: dict[str, Any] = field(default_factory=dict)


@dataclass
class _View:
    """What one cycle observed (exchange facts + ledger)."""

    now: float
    rules: dict[str, Any] | None
    fees: dict[str, Any] | None
    balances: dict[str, Any]
    books: dict[str, OrderBook]
    unknown: list[str]
    ledger: dict[str, Decimal]
    position: Any
    equity: Decimal | None
    peak: Decimal


def _dec(x: Any) -> Decimal:
    return Decimal(str(x))


def books_to_kv(books: Mapping[str, OrderBook]) -> dict[str, Any]:
    return {s: {"ts": b.ts, "bids": [[plain(lv.price), plain(lv.quantity)] for lv in b.bids],
                "asks": [[plain(lv.price), plain(lv.quantity)] for lv in b.asks]} for s, b in books.items()}


def books_from_kv(doc: Any) -> tuple[dict[str, OrderBook], float | None]:
    """Inverse of ``books_to_kv`` -> (books, oldest ts). Unusable entries are skipped."""
    out: dict[str, OrderBook] = {}
    ts: float | None = None
    for s, d in (doc or {}).items():
        try:
            b = OrderBook.build(s, [(_dec(p), _dec(q)) for p, q in d["bids"]],
                                [(_dec(p), _dec(q)) for p, q in d["asks"]], float(d["ts"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        out[s] = b
        ts = b.ts if ts is None else min(ts, b.ts)
    return out, ts


def fees_from_kv(doc: Any) -> dict[str, FeeRates] | None:
    if not doc:
        return None
    try:
        return {s: FeeRates(s, _dec(v["maker"]), _dec(v["taker"])) for s, v in doc.items()}
    except (KeyError, TypeError, ArithmeticError):
        return None


class AgentRunner:
    def __init__(self, rt: Any, *, interval_s: float | None = None, stage: str = "full",
                 max_failures: int = MAX_CONSECUTIVE_FAILURES) -> None:
        if stage not in ("full", "convert"):
            raise ValueError("stage must be 'full' or 'convert'")
        self.rt = rt
        self.cfg = rt.config
        self.clock = rt.clock
        self.engine = rt.engine
        self.store = rt.store
        self.sm = rt.sm
        self.brain = rt.brain
        self.interval_s = float(self.cfg.loop_interval_s if interval_s is None else interval_s)
        self.stage = stage
        self.max_failures = int(max_failures)
        self.cycle = 0
        self.failures = 0
        self.start_ts: float | None = None
        self.deadline_ts: float | None = None
        self.reason = ""
        self._stop_requested: str | None = None
        self._stop_announced = False
        self.notify: Callable[[str], None] | None = None    # main-loop messages (the CLI prints them)
        self._scrub = Scrubber(rt.secrets)
        self._seen_transitions = sum(1 for _ in self.store.iter_events("state_transition"))
        self._seen_fills = self.store.count_fills()
        self._last_view: _View | None = None

    # ------------------------------------------------------------------ small helpers
    def audit(self, kind: str, payload: Mapping[str, Any]) -> None:
        self.rt.audit.write(kind, dict(payload))

    def request_stop(self, reason: str = "signal") -> None:
        """Signal handlers call this and NOTHING else (no I/O there): the current step finishes, then
        the main loop announces the stop and shuts down cleanly."""
        self._stop_requested = reason

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested is not None

    def _announce_stop(self) -> None:
        if self._stop_announced or not self.stop_requested:
            return
        self._stop_announced = True
        if self.notify is not None:
            try:
                self.notify(f"{self._stop_requested} received: finishing the current step, cancelling resting "
                            "orders (cancel only), writing a partial report...")
            except Exception:  # noqa: BLE001 - a broken terminal must not block the shutdown
                pass

    def _flush_store_events(self) -> None:
        """Mirror new state transitions and fills from the store into the audit log."""
        evs = list(self.store.iter_events("state_transition"))
        for ev in evs[self._seen_transitions:]:
            p = ev.payload
            self.audit("state_transition", {"from": p.get("from"), "to": p.get("to"), "reason": p.get("reason"),
                                            "forced": p.get("forced"), "evidence": p.get("evidence")})
        self._seen_transitions = len(evs)
        n = self.store.count_fills()
        if n > self._seen_fills:
            for f in self.store.list_fills()[self._seen_fills:]:
                self.audit("fill", {"client_id": f.client_id, "symbol": f.symbol, "side": f.side,
                                    "price": f.price, "quantity": f.quantity, "quote_sum": f.quote_sum,
                                    "fee": f.fee, "fee_asset": f.fee_asset, "fee_source": f.fee_source})
        self._seen_fills = n

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> str | None:
        """Restart reconciliation first, then (live: ``engine.require_preflight``) the connectivity
        preflight (rules for both symbols, both books fresh and non-crossed). Returns None if trading may
        proceed, else a terminal status (a failed preflight is ``unavailable``: exit 7, nothing sent, the
        competition clock is not started)."""
        try:
            if self.engine.kill_requested():
                self.reason = "KILL switch present at start"
                self.audit("alert", {"alert": "kill_switch", "level": "critical", "reason": self.reason})
                self.engine.kill(cancel_open=True, reason=self.reason)
                return "killed"
            rep = self.engine.reconcile_startup()
            self.audit("reconcile", {"stage": "startup", "ok": rep.get("ok"), "state": rep.get("state"),
                                     "actions": rep.get("actions"), "drift": rep.get("drift"),
                                     "derived": rep.get("derived")})
            if self.sm.state is BotState.STOPPED:
                stop = self.store.kv_get("exec.stop") or {}
                hint = "" if stop.get("deadline") else (
                    " | to continue after checking the account: delete KILL (if present), then "
                    "`wallex-agent unstop`")
                self.reason = f"bot is STOPPED: {stop.get('reason') or self._last_stop_reason()}{hint}"
                return "stopped"
            if not rep.get("ok"):
                self.reason = f"startup reconciliation incomplete: {'; '.join(map(str, rep.get('actions') or []))}"
                return "unavailable"
            if getattr(self.engine, "require_preflight", False):
                # live: no order of this process before rules (both symbols), both books (fresh,
                # non-crossed) and the reconcile above are confirmed (the engine enforces it too)
                pf = self.engine.preflight()
                if not pf.get("ok"):
                    self.reason = ("connectivity/reconcile preflight failed, no order was sent: "
                                   + "; ".join(map(str, pf.get("problems") or [])))
                    self.audit("alert", {"alert": "preflight_failed", "level": "error", "reason": self.reason[:500]})
                    return "unavailable"
            self.store.kv_set(K_LABELS, {"synthetic": bool(self.rt.synthetic), "labels": list(self.rt.labels)})
            start = self.store.kv_get(K_START)
            if start is None:
                start = float(self.clock.now())
                self.store.kv_set(K_START, start)
                self.audit("outcome", {"stage": "competition_start", "start_ts": start,
                                       "competition_hours": self.cfg.competition_hours})
            self.start_ts = float(start)
            self.deadline_ts = self.start_ts + float(self.cfg.competition_hours) * 3600.0
            return None
        finally:
            self._flush_store_events()

    def _last_stop_reason(self) -> str:
        hist = self.sm.history()
        return str(hist[-1].get("reason")) if hist else "unknown"

    def run(self, *, max_cycles: int | None = None, seconds: float | None = None) -> RunResult:
        status = self.start()
        t0 = self.clock.monotonic()
        while status is None:
            if self.stop_requested:
                self._announce_stop()
                status = "interrupted"
                break
            status = self.run_once()
            if status is not None:
                break
            if max_cycles is not None and self.cycle >= max_cycles:
                status, self.reason = "max_cycles", f"{self.cycle} cycles done"
                break
            if self.rt.advance is not None:
                if not self.rt.advance():
                    status, self.reason = "data_end", "market data exhausted before the deadline"
                    break
            else:
                self._sleep(self.interval_s)
                if seconds is not None and self.clock.monotonic() - t0 >= seconds:
                    status, self.reason = "session_end", f"session of {seconds:.0f}s finished"
                    break
        if status == "interrupted" and not self.reason:
            self.reason = f"stop requested ({self._stop_requested}); state is resumable"
        return self.finish(status)

    def _sleep(self, seconds: float) -> None:
        end = self.clock.monotonic() + max(0.0, seconds)
        while not self.stop_requested:
            left = end - self.clock.monotonic()
            if left <= 0 or self.engine.kill_requested():
                return
            self.clock.sleep(min(1.0, left))

    # ------------------------------------------------------------------ one cycle
    def run_once(self) -> str | None:
        """One full cycle. Returns a terminal status or None. Never raises an ``Exception``."""
        self.cycle += 1
        status: str | None = None
        error: str | None = None
        try:
            status = self._cycle()
            self.failures = 0
        except Exception as exc:  # noqa: BLE001 - a cycle failure is audited and counted, not fatal
            self.failures += 1
            error = type(exc).__name__
            self._safe_audit("alert", {"alert": "cycle_exception", "level": "error", "error": error,
                                       "detail": self._scrub.text(str(exc))[:300], "cycle": self.cycle,
                                       "consecutive": self.failures})
            if self.failures >= self.max_failures:
                status = "stopped"
                self.reason = f"{self.failures} consecutive cycle failures (last: {error})"
                self._safety_stop(self.reason)
        finally:
            try:
                self._flush_store_events()
            except Exception as exc:  # noqa: BLE001
                self._safe_audit("alert", {"alert": "audit_flush_failed", "error": type(exc).__name__})
            self._safe_audit("outcome", {"stage": "cycle_end", "cycle": self.cycle,
                                         "status": status or "continue", "error": error,
                                         "state": self._state_value()})
        return status

    def _safe_audit(self, kind: str, payload: Mapping[str, Any]) -> None:
        try:
            self.audit(kind, payload)
        except Exception:  # noqa: BLE001 - nothing else can be done; the audit file reports its own damage
            pass

    def _state_value(self) -> str:
        try:
            return self.sm.state.value
        except Exception:  # noqa: BLE001
            return "unknown"

    def _safety_stop(self, reason: str) -> None:
        """Adopt-or-leave unknown orders, cancel our open orders (cancel only, never sell), STOPPED."""
        try:
            self.engine.settle_for_shutdown(f"safety stop: {reason}")
        except Exception as exc:  # noqa: BLE001
            self._safe_audit("alert", {"alert": "cancel_failed", "error": type(exc).__name__})
        try:
            if self.sm.state is not BotState.STOPPED:
                self.store.kv_set("exec.stop", {"reason": f"safety stop: {reason}", "ts": float(self.clock.now())})
                self.sm.transition(BotState.STOPPED, f"safety stop: {reason}")
        except Exception as exc:  # noqa: BLE001
            self._safe_audit("alert", {"alert": "stop_transition_failed", "error": type(exc).__name__})
        self._safe_audit("alert", {"alert": "safety_stop", "level": "critical", "reason": reason})

    def _cycle(self) -> str | None:
        eng = self.engine
        # 1. kill switch
        if eng.kill_requested():
            self.reason = "kill switch engaged"
            out = eng.kill(cancel_open=True, reason="kill switch file/flag")
            self.audit("tool_call", {"name": "engine.kill", "result": out})
            return "killed"
        if self.sm.state is BotState.STOPPED:
            self.reason = f"bot is STOPPED: {self._last_stop_reason()}"
            return "stopped"
        # 2. poll / reconcile in-flight orders
        poll = eng.poll_and_manage()
        self.audit("reconcile", {"stage": "poll", **poll})
        self._report_order_progress()
        self._track_round_trips()
        if self.sm.state is BotState.STOPPED:       # late accept, confirmed shortfall, contradiction
            stop = self.store.kv_get("exec.stop") or {}
            self.reason = f"engine stopped the bot: {stop.get('reason') or self._last_stop_reason()}"
            return "stopped"
        # 3. observe
        view = self._observe()
        self._last_view = view
        # 4. deadline
        if self.deadline_ts is not None and view.now >= self.deadline_ts:
            return self._deadline(view)
        # a conversion stage interrupted after its last fill (crash/restart) is closed here: only a
        # residual below the market minimum is left, so no order can be sent and none is needed
        if self.sm.state is BotState.CONVERTING and not view.unknown and self._conversion_residual_only(view):
            did = f"resume-convert-{self.cycle}"
            self.audit("tool_call", {"name": "engine.convert_tmn_to_usdt", "decision_id": did,
                                     "reason": "resume/close the conversion stage (TMN residual below minimum)"})
            # any round it might still send passes the same per-round risk gate and stop check
            cres = eng.convert_tmn_to_usdt(decision_id=did, should_stop=self._conversion_stop_reason)
            self.audit("tool_call", {"name": "engine.convert_tmn_to_usdt", "status": cres.status, "decision_id": did,
                                     "order_ids": cres.order_ids, "ok": cres.status in ("done", "already_funded")})
        # 5. forced exits, then the agent's decision (always recorded)
        ctx0 = self._risk_ctx(view)
        forced = self.rt.risk.forced_exit(ctx0)
        obs = Observation(now=view.now, books=view.books, balances=view.balances, state=self.sm.state,
                          fees=view.fees, rules=view.rules, position=view.position, ledger=view.ledger,
                          equity_tmn=view.equity, peak_equity_tmn=view.peak,
                          round_trips_done=view.position.round_trips,
                          time_left_s=None if self.deadline_ts is None else self.deadline_ts - view.now,
                          total_s=float(self.cfg.competition_hours) * 3600.0,
                          unresolved_orders=bool(self.store.unresolved_orders()))
        d = self.brain.decide(obs)
        intent: TradeIntent | None = forced or d.intent
        source = "forced_exit" if forced is not None else "agent"
        verdict = None
        blocked: str | None = None
        if intent is not None:
            ctx = replace(ctx0, **self.brain.risk_inputs(d)) if forced is None else ctx0
            verdict = self.rt.risk.evaluate(intent, ctx)
            if view.unknown:
                blocked = f"unknown/stale market facts: {', '.join(view.unknown)}"
            elif self.stage == "convert" and intent.purpose != "convert":
                blocked = "stage 'convert': only the TMN->USDT conversion may run"
            elif self.stop_requested:
                blocked = "shutdown requested"
        d.risk_verdict = None if verdict is None else {**verdict.to_dict(), "intent_source": source,
                                                       "blocked": blocked}
        self.brain.record_decision(d, context=self._decision_context(view, source if intent is not None else None))
        self._audit_decision(d, forced, verdict, blocked, view)
        # 6. act -> verify
        if verdict is not None and verdict.approved and blocked is None and verdict.adjusted is not None:
            did = d.decision_id if forced is None else f"{d.decision_id}:forced"
            st = self._act(verdict.adjusted, did, report_to_brain=True)
            if st is not None:
                return st
        # drawdown breach while flat => exits only were possible; now stop
        if self._drawdown_breached(view) and self.sm.state is BotState.READY:
            dd = (view.peak - view.equity) / view.peak if view.equity is not None and view.peak > 0 else None
            self.reason = (f"drawdown {plain((dd or ZERO) * 100)}% >= {plain(self.cfg.max_drawdown_pct * 100)}% "
                           f"(equity {plain(view.equity) if view.equity is not None else '?'} TMN net vs peak "
                           f"{plain(view.peak)} TMN) with no position: STOPPED")
            self.store.kv_set("exec.stop", {"reason": self.reason, "ts": view.now})
            self.sm.transition(BotState.STOPPED, self.reason)
            return "stopped"
        # 7. reflect + equity
        for refl in self.brain.reflect_pending(view.now, view.books):
            self.audit("reflection", refl.to_dict() if hasattr(refl, "to_dict") else dict(refl))
        self._record_equity()
        if self.stage == "convert" and self.sm.state is BotState.READY and not self.store.unresolved_orders():
            self.reason = "conversion stage complete: state READY (run `live --stage full` to trade)"
            return "stage_done"
        return None

    # ------------------------------------------------------------------ observe
    def _observe(self) -> _View:
        ex = self.rt.exchange
        cfg = self.cfg
        now = float(self.clock.now())
        unknown: list[str] = []
        errors: dict[str, str] = {}
        rules = fees = None
        balances: dict[str, Any] = {}
        books: dict[str, OrderBook] = {}
        syms = (cfg.btc_symbol, cfg.usdt_symbol)
        try:
            rules = dict(ex.get_markets())
            unknown += [f"rules:{s}" for s in syms if s not in rules]
        except Exception as exc:  # noqa: BLE001
            unknown.append("rules")
            errors["rules"] = error_label(exc)
        try:
            fees = dict(ex.get_fees())
            unknown += [f"fee:{s}" for s in syms if s not in fees]
        except Exception as exc:  # noqa: BLE001
            unknown.append("fees")
            errors["fees"] = error_label(exc)
        try:
            balances = dict(ex.get_balances())
        except Exception as exc:  # noqa: BLE001
            unknown.append("balances")
            errors["balances"] = error_label(exc)
        for s in syms:
            try:
                b = ex.get_order_book(s)
                # freshness is judged at receipt: a slow proxy/network must not make a book 'future'
                ensure_fresh(b, float(self.clock.now()), cfg.max_book_age_s)
                books[s] = b
            except StaleQuote as exc:
                unknown.append(f"book_stale:{s}")
                errors[f"book:{s}"] = str(exc)[:120]
            except Exception as exc:  # noqa: BLE001
                unknown.append(f"book:{s}")
                errors[f"book:{s}"] = error_label(exc)
        now = float(self.clock.now())   # re-read AFTER the (possibly slow) fetches
        ledger = self.rt.portfolio.expected_balances()
        position = self.rt.portfolio.btc_position()
        equity = None
        if fees is not None:
            try:
                equity = self.rt.portfolio.liquidation_value(books, fees, rules).equity_tmn_net
            except Exception as exc:  # noqa: BLE001
                errors["equity"] = type(exc).__name__
        view = _View(now, rules, fees, balances, books, unknown, ledger, position, equity,
                     self.rt.portfolio.peak_equity())
        if books:
            prev, _ = books_from_kv(self.store.kv_get(K_BOOKS))
            prev.update(books)
            self.store.kv_set(K_BOOKS, books_to_kv(prev))
        if fees:
            self.store.kv_set(K_FEES, {s: {"maker": plain(f.maker), "taker": plain(f.taker)}
                                       for s, f in fees.items() if s in syms})
        self.audit("observe", {
            "cycle": self.cycle, "state": self.sm.state.value,
            "time_left_s": None if self.deadline_ts is None else round(self.deadline_ts - now, 3),
            "prices": {s: {"bid": b.best_bid, "ask": b.best_ask, "mid": b.mid,
                           "spread_bps": b.spread_bps.quantize(Decimal("0.01")), "age_s": round(b.age(now), 3)}
                       for s, b in books.items()},
            "fees_taker": {s: f.taker for s, f in (fees or {}).items() if s in syms},
            "ledger": ledger, "exchange_balances": {a: {"value": b.value, "locked": b.locked}
                                                    for a, b in balances.items() if a in ("TMN", "USDT", "BTC")},
            "equity_tmn_net": equity, "unknown": unknown, "errors": errors,
            "unresolved_orders": len(self.store.unresolved_orders())})
        if unknown:
            self.audit("alert", {"alert": "market_facts_unknown", "level": "warning", "unknown": unknown,
                                 "reason": "no order will be sent this cycle"})
        return view

    def _conversion_residual_only(self, view: _View) -> bool:
        """True when no conversion order is possible any more (same criterion as the engine)."""
        cfg = self.cfg
        if self.store.unresolved_orders() or not view.rules or not view.fees:
            return False
        ur, fee, book = view.rules.get(cfg.usdt_symbol), view.fees.get(cfg.usdt_symbol), view.books.get(cfg.usdt_symbol)
        if ur is None or fee is None or book is None:
            return False
        b = view.balances.get(ur.quote)
        tmn = max(ZERO, min(b.spendable if b else ZERO, view.ledger.get(ur.quote, ZERO)))
        ask = book.best_ask
        headroom = 1 + fee.taker + cfg.fee_safety_margin
        qty = ur.round_qty(tmn / (ask * (1 + cfg.conversion_slippage_bps / 10000) * headroom))
        return qty <= 0 or bool(ur.violations(ur.round_price(ask, Side.BUY), qty))

    def _risk_ctx(self, view: _View) -> RiskContext:
        fills = self.store.list_fills()
        last_loss = self.store.kv_get(K_LAST_LOSS)
        return RiskContext(
            state=self.sm.state, balances=view.balances, fees=view.fees, rules=view.rules, books=view.books,
            now=view.now, ledger=view.ledger, position=view.position, equity_tmn=view.equity,
            peak_equity_tmn=view.peak, round_trips_done=view.position.round_trips,
            last_trade_ts=max((f.ts for f in fills), default=None),
            last_loss_ts=None if last_loss is None else float(last_loss), deadline_ts=self.deadline_ts,
            kill=self.engine.kill_requested(), unresolved_orders=bool(self.store.unresolved_orders()),
            quarantined=self.engine.quarantined())

    def _decision_context(self, view: _View, intent_source: str | None) -> dict[str, Any]:
        """Balances, equity and drawdown at decision time (F1: reported with every decision)."""
        dd = None
        if view.equity is not None and view.peak > 0:
            dd = max(ZERO, (view.peak - view.equity) / view.peak * 100).quantize(Decimal("0.0001"))
        return {"state": self.sm.state.value, "ledger": view.ledger,
                "exchange_balances": {a: {"value": b.value, "locked": b.locked}
                                      for a, b in view.balances.items() if a in ("TMN", "USDT", "BTC")},
                "equity_tmn_net": view.equity, "peak_equity_tmn": view.peak, "drawdown_pct": dd,
                "unknown_facts": list(view.unknown), "intent_source": intent_source}

    def _audit_decision(self, d: Decision, forced: TradeIntent | None, verdict: Any, blocked: str | None,
                        view: _View) -> None:
        dd = d.to_dict()
        tools = dd.pop("tool_calls", []) or []
        self.audit("decision", dd)
        if tools:   # one record per decision: which tools the agent used (or skipped) and why
            self.audit("tool_call", {"decision_id": d.decision_id,
                                     "name": ",".join(str(t.get("tool")) for t in tools if t.get("called")),
                                     "calls": tools})
        if forced is not None:
            self.audit("forced_exit", {"decision_id": d.decision_id, "intent": forced, "reason": forced.note,
                                       "overrides_action": d.action})
        if verdict is not None:
            self.audit("risk_verdict", {"decision_id": d.decision_id, "approved": verdict.approved,
                                        "reasons": verdict.reasons, "notes": verdict.notes,
                                        "metrics": verdict.metrics, "blocked": blocked,
                                        "intent_source": "forced_exit" if forced is not None else "agent"})

    # ------------------------------------------------------------------ act / verify
    def _act(self, intent: TradeIntent, decision_id: str, *, report_to_brain: bool) -> str | None:
        eng = self.engine
        name = "engine.convert_tmn_to_usdt" if intent.purpose == "convert" else "engine.submit"
        self.audit("tool_call", {"decision_id": decision_id, "name": name, "intent": intent})
        try:
            if intent.purpose == "convert":
                cres = eng.convert_tmn_to_usdt(decision_id=decision_id, should_stop=self._conversion_stop_reason)
                result: dict[str, Any] = cres.to_dict()
                ids, status, reason = list(cres.order_ids), cres.status, "; ".join(cres.notes)[-300:]
            else:
                res = eng.submit(intent, decision_id)
                result = res.to_dict()
                ids, status, reason = list(res.order_ids), res.status, res.reason
        except KillSwitchActive:
            self.reason = "kill switch engaged before send"
            out = eng.kill(cancel_open=True, reason=self.reason)
            self.audit("tool_call", {"name": "engine.kill", "result": out})
            return "killed"
        self.audit("tool_call", {"decision_id": decision_id, "name": name, "ok": status not in
                                 ("declined", "blocked", "rejected", "stopped", "interrupted"), "status": status,
                                 "order_ids": ids, "reason": reason})
        link = {"decision_id": decision_id, "order_ids": ids, "status": status, "tool": name}
        self.store.append("decision_orders", link)        # F1: decision -> orders, for the report
        self.audit("decision_orders", link)
        # verify: re-read every order and the balances through the engine / exchange. A shortfall
        # blocks new orders at once and, confirmed on a later check, STOPS the bot (engine).
        poll = eng.poll_and_manage(check_balances=False)
        recs = [self.store.get_order(i) for i in ids]
        try:
            recon: dict[str, Any] = eng.check_balances(stage="verify")
        except Exception as exc:  # noqa: BLE001
            recon = {"ok": None, "error": type(exc).__name__}
        filled = sum((r.executed_qty for r in recs if r), ZERO)
        spent = sum((r.executed_sum for r in recs if r), ZERO)
        summary = {"status": status, "reason": reason, "order_ids": ids, "filled_qty": filled,
                   "avg_price": (spent / filled) if filled > 0 else None,
                   "lifecycles": {r.client_id: r.lifecycle.value for r in recs if r}}
        self.audit("verify", {"decision_id": decision_id, "orders": [
            {"client_id": r.client_id, "exchange_id": r.exchange_client_order_id, "lifecycle": r.lifecycle,
             "price": r.price, "quantity": r.quantity, "executed_qty": r.executed_qty,
             "fee_total": r.fee_total, "fee_asset": r.fee_asset, "fee_source": r.fee_source}
            for r in recs if r], "reconcile": recon, "poll_state": poll.get("state"), "state": self.sm.state})
        if recon.get("ok") is False:
            self.audit("alert", {"alert": "ledger_exchange_mismatch", "level": "warning",
                                 "reason": "; ".join(recon.get("reasons") or [])[:300]})
        if report_to_brain:
            verify = self.brain.on_order_result(decision_id, summary)
            self.audit("outcome", {"decision_id": decision_id, "stage": "order_result", "result": summary,
                                   "verify": verify})
        self._mark_reported(ids)
        self._track_round_trips()
        if self.sm.state is BotState.STOPPED:
            stop = self.store.kv_get("exec.stop") or {}
            self.reason = f"engine stopped the bot: {stop.get('reason') or self._last_stop_reason()}"
            return "stopped"
        return None

    def _conversion_stop_reason(self) -> str | None:
        """Checked by the conversion stage before EVERY round: a signal (SIGINT/SIGTERM/SIGBREAK)
        or the competition deadline ends it (the kill switch raises inside the engine)."""
        if self.stop_requested:
            return f"shutdown requested ({self._stop_requested})"
        if self.deadline_ts is not None and float(self.clock.now()) >= self.deadline_ts:
            return "competition deadline reached"
        return None

    def _mark_reported(self, ids: list[str]) -> None:
        rep = dict(self.store.kv_get(K_REPORTED, {}) or {})
        for i in ids:
            r = self.store.get_order(i)
            if r is not None:
                rep[i] = plain(r.executed_qty)
        self.store.kv_set(K_REPORTED, rep)

    def _report_order_progress(self) -> None:
        """Fills that arrive after the submit cycle (resting orders) are reported to the brain too."""
        rep = dict(self.store.kv_get(K_REPORTED, {}) or {})
        changed = False
        for r in self.store.list_orders():
            if not r.decision_id or r.executed_qty <= 0:
                continue
            before = _dec(rep.get(r.client_id, "0"))
            if r.executed_qty > before:
                delta = r.executed_qty - before
                res = {"status": r.lifecycle.value, "filled_qty": delta, "client_id": r.client_id,
                       "late_fill": True, "avg_price": r.executed_sum / r.executed_qty}
                verify = self.brain.on_order_result(r.decision_id, res)
                self.audit("outcome", {"decision_id": r.decision_id, "stage": "late_fill", "result": res,
                                       "verify": verify})
                rep[r.client_id] = plain(r.executed_qty)
                changed = True
        if changed:
            self.store.kv_set(K_REPORTED, rep)

    def _track_round_trips(self) -> None:
        """When the fill ledger closes a BTC round trip, tell the brain (net P&L incl. all fees)."""
        pos = self.rt.portfolio.btc_position()
        t = self.store.kv_get(K_TRIP) or {"round_trips": pos.round_trips,
                                          "realized_base": plain(pos.realized_pnl_usdt), "cost": "0"}
        cost = _dec(t["cost"])
        if pos.qty > 0 and pos.cost_usdt > cost:
            cost = pos.cost_usdt
        if pos.round_trips > int(t["round_trips"]):
            pnl = pos.realized_pnl_usdt - _dec(t["realized_base"])
            now = float(self.clock.now())
            if pnl < 0:
                self.store.kv_set(K_LAST_LOSS, now)
            sells = [o for o in self.store.list_orders() if o.symbol == self.cfg.btc_symbol
                     and o.side is Side.SELL and o.executed_qty > 0]
            last = sells[-1] if sells else None
            exit_price = (last.executed_sum / last.executed_qty) if last is not None else None
            try:
                refl = self.brain.on_round_trip_closed(
                    pnl_usdt=pnl, cost_usdt=cost, exit_ts=now, exit_price=exit_price,
                    exit_decision_id=last.decision_id if last is not None else None,
                    reason=(last.purpose + (f": {last.note}" if last.note else "")) if last is not None else "")
                self.audit("outcome", {"stage": "round_trip_closed", "pnl_usdt": pnl, "cost_usdt": cost,
                                       "exit_price": exit_price, "round_trips": pos.round_trips})
                self.audit("reflection", refl.to_dict() if hasattr(refl, "to_dict") else dict(refl))
            except ValueError as exc:
                self.audit("alert", {"alert": "round_trip_without_entry_decision", "level": "warning",
                                     "detail": str(exc)[:200]})
            t = {"round_trips": pos.round_trips, "realized_base": plain(pos.realized_pnl_usdt), "cost": "0"}
            cost = ZERO
        t["cost"] = plain(cost)
        self.store.kv_set(K_TRIP, t)

    def _record_equity(self) -> None:
        v = self._last_view
        if v is None or v.fees is None:
            return
        eq = self.rt.portfolio.liquidation_value(v.books, v.fees, v.rules)
        if eq.equity_tmn_net is not None:
            self.rt.portfolio.record_equity(v.now, eq.equity_tmn_net)
        self.audit("outcome", {"stage": "equity", "equity_tmn_net": eq.equity_tmn_net,
                               "equity_tmn_mid": eq.equity_tmn_mid, "flags": list(eq.flags),
                               "peak_tmn": self.rt.portfolio.peak_equity(), "state": self.sm.state})

    def _drawdown_breached(self, view: _View) -> bool:
        if view.equity is None or view.peak <= 0:
            return False
        return (view.peak - view.equity) / view.peak >= self.cfg.max_drawdown_pct

    # ------------------------------------------------------------------ deadline
    def _deadline(self, view: _View) -> str | None:
        eng = self.engine
        st = dict(self.store.kv_get(K_DEADLINE) or {"cancelled": False, "cycles": 0})
        if not st["cancelled"]:
            out = eng.cancel_open_orders("competition deadline")
            self.audit("tool_call", {"name": "engine.cancel_open_orders", "reason": "deadline", "result": out})
            st["cancelled"] = True
        st["cycles"] = int(st["cycles"]) + 1
        self.store.kv_set(K_DEADLINE, st)
        policy = self.cfg.final_policy
        self.audit("outcome", {"stage": "deadline", "final_policy": policy, "deadline_cycle": st["cycles"],
                               "state": self.sm.state})
        if policy != "report_only" and st["cycles"] <= DEADLINE_GRACE_CYCLES:
            # flatten ONLY through the normal risk + engine path (exits are allowed after the deadline)
            ctx = self._risk_ctx(view)
            forced = self.rt.risk.forced_exit(ctx)
            if forced is not None:
                verdict = self.rt.risk.evaluate(forced, ctx)
                did = f"deadline-{st['cycles']}"
                self.audit("forced_exit", {"decision_id": did, "intent": forced, "reason": forced.note})
                self.audit("risk_verdict", {"decision_id": did, "approved": verdict.approved,
                                            "reasons": verdict.reasons, "notes": verdict.notes,
                                            "intent_source": "deadline_policy",
                                            "blocked": "unknown market facts" if view.unknown else None})
                if verdict.approved and verdict.adjusted is not None and not view.unknown:
                    s = self._act(verdict.adjusted, did, report_to_brain=False)
                    if s is not None:
                        return s
                    self._record_equity()
                    return None          # re-check next cycle (e.g. USDT -> TMN after BTC -> USDT)
                if view.unknown:
                    self._record_equity()
                    return None          # wait for the market facts to come back (bounded by grace)
            if self.store.unresolved_orders():
                return None
        self._record_equity()
        held = {a: v for a, v in self.rt.portfolio.expected_balances().items() if v}
        self.reason = (f"competition deadline reached (final_policy={policy}); holdings: "
                       + ", ".join(f"{plain(v)} {a}" for a, v in sorted(held.items())))
        if self.sm.state is not BotState.STOPPED:
            self.store.kv_set("exec.stop", {"reason": self.reason, "ts": view.now, "deadline": True})
            self.sm.transition(BotState.STOPPED, f"competition deadline reached; final_policy={policy}")
        return "deadline"

    # ------------------------------------------------------------------ finish / report
    def finish(self, status: str) -> RunResult:
        self._announce_stop()
        if status in ("interrupted", "stopped", "session_end", "max_cycles"):
            try:
                if self.store.unresolved_orders():     # incl. SENDING / UNKNOWN_OUTCOME: adopt, then cancel
                    out = self.engine.settle_for_shutdown(f"shutdown: {status}")
                    self.audit("tool_call", {"name": "engine.settle_for_shutdown", "reason": status, "result": out})
            except Exception as exc:  # noqa: BLE001
                self._safe_audit("alert", {"alert": "cancel_on_shutdown_failed", "error": type(exc).__name__})
        self._flush_store_events_safe()
        final = status in FINAL_STATUSES
        report_path = trace_path = None
        report: dict[str, Any] = {}
        try:
            report_path, trace_path, report = self.write_report(status, final=final)
        except Exception as exc:  # noqa: BLE001 - the run result must still be returned
            self._safe_audit("alert", {"alert": "report_failed", "level": "error", "error": type(exc).__name__,
                                       "detail": self._scrub.text(str(exc))[:300]})
        return RunResult(status=status, exit_code=STATUS_EXIT.get(status, EXIT_ERROR), cycles=self.cycle,
                         reason=self.reason, report_path=report_path, trace_path=trace_path, report=report)

    def _flush_store_events_safe(self) -> None:
        try:
            self._flush_store_events()
        except Exception:  # noqa: BLE001
            pass

    def _end_books(self) -> tuple[dict[str, OrderBook], dict[str, Any] | None, list[str]]:
        books: dict[str, OrderBook] = {}
        fees = None
        stale: list[str] = []
        for s in (self.cfg.btc_symbol, self.cfg.usdt_symbol):
            try:
                books[s] = self.rt.exchange.get_order_book(s)
            except Exception:  # noqa: BLE001
                if self._last_view is not None and s in self._last_view.books:
                    books[s] = self._last_view.books[s]
                    stale.append(f"book:{s}")
        try:
            fees = dict(self.rt.exchange.get_fees())
        except Exception:  # noqa: BLE001
            fees = None if self._last_view is None else self._last_view.fees
            stale.append("fees")
        return books, fees, stale

    def _final_exchange_check(self, stale: list[str]) -> dict[str, Any]:
        """Live only: is the final ledger confirmed by the exchange balances right now? (F3: the
        summary must not present a ledger valuation as fact when the exchange cannot confirm it.)"""
        from .execution import drift_text
        out: dict[str, Any] = {"at": float(self.clock.now()), "stale_inputs": stale}
        try:
            rr = self.rt.portfolio.reconcile(self.rt.exchange.get_balances(), self.engine.tolerance)
        except Exception as exc:  # noqa: BLE001
            out.update(verified=None, detail=f"exchange balances unavailable ({type(exc).__name__})")
            return out
        out.update(verified=bool(rr.ok and not stale),
                   detail=(drift_text(rr) if not rr.ok else "") + (
                       f"; valued with last observed {', '.join(stale)} (exchange unavailable)" if stale else ""))
        return out

    def write_report(self, status: str, *, final: bool) -> tuple[Path, Path, dict[str, Any]]:
        from .clock import SimClock
        now = float(self.clock.now())
        books, fees, stale = self._end_books()
        started = self.start_ts if self.start_ts is not None else self.store.kv_get(K_START)
        if started is None:
            started = next((ev.ts for ev in self.store.iter_events()), now)
        caveats = list(self.rt.caveats)
        if status == "interrupted":
            caveats.append("PARTIAL report: the run was interrupted and is resumable.")
        live = getattr(self.rt.mode, "value", self.rt.mode) == "live"
        rep = build_final_report(config=self.cfg, mode=self.rt.mode, run_id=self.rt.run_id,
                                 started_ts=float(started), ended_ts=now, portfolio=self.rt.portfolio,
                                 engine_status=self.engine.status(), store=self.store,
                                 memory_snapshot=self.brain.memory_snapshot(), books_at_end=books, fees=fees,
                                 caveats=caveats, stop_reason=f"{status}: {self.reason}",
                                 synthetic_data=self.rt.synthetic,
                                 clock_kind="simulated" if isinstance(self.clock, SimClock) else "real",
                                 exchange_check=self._final_exchange_check(stale) if live else None)
        rep["run"] = {"status": status, "final": final, "cycles_this_process": self.cycle,
                      "competition_start": None if self.start_ts is None else rep.get("started_at"),
                      "deadline_ts": self.deadline_ts, "run_dir": str(self.rt.run_dir),
                      "labels": list(self.rt.labels)}
        name = "final_report.json" if final else "partial_report.json"
        path = write_final_report(self.rt.run_dir / name, rep, secrets=self.rt.secrets)
        self.audit("outcome", {"stage": "report", "status": status, "final": final, "path": str(path)})
        trace = render_trace(self.rt.audit.path)
        tpath = self.rt.run_dir / "trace.txt"
        tpath.write_text(self._scrub.text(trace), encoding="utf-8")
        return path, tpath, rep
