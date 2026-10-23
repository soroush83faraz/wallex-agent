"""Audit log (hash-chained JSONL), final report and human-readable trace.

* ``AuditLog`` implements the engine's ``AuditSink``: one fsync'd JSON line per record with
  ``seq, ts, run_id, strategy_version, kind, payload, prev_hash, hash`` where
  ``hash = sha256(prev_hash + canonical_json(record without hash))``. Every payload passes through a
  secret scrubber first; the chain resumes across restarts and refuses to extend a broken chain.
* ``build_final_report`` / ``write_final_report`` produce ``final_report.json`` with honest labels and
  caveats (it never claims more than the data supports).
* ``render_trace`` turns audit records into an observe -> plan -> tool -> action -> verify -> reflect
  narrative, tolerant of record kinds it does not know.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.parse
from collections import Counter
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import STRATEGY_VERSION
from .clock import Clock, RealClock
from .errors import WallexAgentError
from .exchange.probe import UNVERIFIED
from .live_gate import atomic_write_json
from .types import Mode, OrderBook, plain

GENESIS_HASH = "0" * 64
REDACTED = "***redacted***"
REPORT_VERSION = 1
_SECRET_NAME_PARTS = ("secret", "token", "authoriz", "password", "passwd", "passphrase", "apikey",
                      "api_key", "private_key", "access_key", "credential", "cookie")
_KEY_TEXT_RE = re.compile(r"(?i)((?:x[-_ ]?)?api[-_ ]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+")
_AUTH_TEXT_RE = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+|basic\s+)?)"
                           r"[^\s\"',;&}]+")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,15}://)(?!\*{3}@)[^\s/]*@")   # proxy credentials


class AuditChainError(WallexAgentError):
    """The existing audit log does not verify; appending to it would hide tampering."""


# --------------------------------------------------------------------------- scrubbing
def is_secret_name(name: Any) -> bool:
    n = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    return n == "key" or any(part in n for part in _SECRET_NAME_PARTS)


class Scrubber:
    """Redacts literal secrets (raw, JSON-escaped, URL-encoded), ``X-API-Key: ...`` /
    ``Authorization: ...`` text, credentials inside URLs (``http://user:pass@host``), and every value
    stored under a secret-looking key name."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        variants: set[str] = set()
        for s in secrets:
            if not s:
                continue
            js = json.dumps(s)[1:-1]
            variants |= {s, js, js.replace("/", "\\/"), urllib.parse.quote(s, safe=""),
                         urllib.parse.quote_plus(s)}
        self._variants = tuple(sorted((v for v in variants if v), key=len, reverse=True))

    def text(self, value: Any) -> str:
        s = value if isinstance(value, str) else str(value)
        for v in self._variants:
            if v in s:
                s = s.replace(v, REDACTED)
        s = _KEY_TEXT_RE.sub(lambda m: m.group(1) + REDACTED, s)
        s = _URL_USERINFO_RE.sub(lambda m: m.group(1) + REDACTED + "@", s)
        return _AUTH_TEXT_RE.sub(lambda m: m.group(1) + REDACTED, s)

    def contains_secret(self, text: str) -> bool:
        return any(v in text for v in self._variants)

    def tree(self, o: Any) -> Any:
        """JSON-able, scrubbed copy: Decimal -> plain string, Enum -> value, dataclass -> dict."""
        if o is None or isinstance(o, bool) or isinstance(o, int):
            return o
        if isinstance(o, float):
            return o if math.isfinite(o) else str(o)
        if isinstance(o, Decimal):
            return plain(o) if o.is_finite() else str(o)
        if isinstance(o, Enum):
            return self.tree(o.value)
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, (bytes, bytearray)):
            return self.text(bytes(o).decode("utf-8", "replace"))
        if isinstance(o, Mapping):
            out: dict[str, Any] = {}
            for k, v in o.items():
                key = self.text(k)
                secret_slot = is_secret_name(k) and not (v is None or isinstance(v, bool) or v == "")
                out[key] = REDACTED if secret_slot else self.tree(v)
            return out
        if isinstance(o, (list, tuple, set, frozenset)):
            items = sorted(o, key=str) if isinstance(o, (set, frozenset)) else o
            return [self.tree(v) for v in items]
        if is_dataclass(o) and not isinstance(o, type):
            return self.tree({f.name: getattr(o, f.name) for f in fields(o)})
        if isinstance(o, Path):
            return self.text(str(o))
        return self.text(str(o))


# --------------------------------------------------------------------------- hash chain
def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def record_hash(prev_hash: str, record_without_hash: Mapping[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(record_without_hash)).encode("utf-8")).hexdigest()


def _check_record(raw: bytes, prev: str, seq: int) -> dict[str, Any] | None:
    """The parsed record if ``raw`` is the valid successor of (``prev``, ``seq - 1``)."""
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("seq") != seq or rec.get("prev_hash") != prev:
        return None
    body = {k: v for k, v in rec.items() if k != "hash"}
    try:
        ok = record_hash(prev, body) == rec.get("hash")
    except ValueError:
        return None
    return rec if ok else None


def verify_chain(path: str | Path) -> tuple[bool, int | None]:
    """``(True, None)`` if every line chains correctly; else ``(False, first_bad_line)`` (1-based;
    0 = the file does not exist). A torn (unterminated) last line counts as bad."""
    p = Path(path)
    if not p.is_file():
        return False, 0
    prev, seq = GENESIS_HASH, 1
    with open(p, "rb") as fh:
        for lineno, raw in enumerate(fh, 1):
            if not raw.endswith(b"\n"):
                return False, lineno
            rec = _check_record(raw[:-1], prev, seq)
            if rec is None:
                return False, lineno
            prev, seq = rec["hash"], seq + 1
    return True, None


def read_audit(path: str | Path) -> list[dict[str, Any]]:
    """All parseable records (unparseable lines become ``{"kind": "_unreadable", "line": n}``)."""
    out: list[dict[str, Any]] = []
    p = Path(path)
    if not p.is_file():
        return out
    with open(p, "rb") as fh:
        for lineno, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
            except (UnicodeDecodeError, ValueError):
                rec = {"kind": "_unreadable", "line": lineno, "payload": {}}
            out.append(rec)
    return out


def _iso(ts: float | None, ms: bool = False) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(
        timespec="milliseconds" if ms else "seconds").replace("+00:00", "Z")


class AuditLog:
    """Append-only, hash-chained, fsync-per-line audit sink (``execution.AuditSink``)."""

    def __init__(self, path: str | Path, *, run_id: str, clock: Clock | None = None,
                 strategy_version: str = STRATEGY_VERSION, secrets: Sequence[str] = (),
                 durable: bool = True) -> None:
        # durable=False (backtest only: disposable simulated runs) skips the per-line fsync
        self.durable = bool(durable)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._scrub = Scrubber(secrets)
        self.run_id = self._scrub.text(run_id)
        self.strategy_version = self._scrub.text(strategy_version)
        self.clock = clock or RealClock()
        self._lock = threading.Lock()
        dropped = self._repair_torn_tail()
        ok, bad = verify_chain(self.path) if self.path.exists() else (True, None)
        if not ok:
            raise AuditChainError(f"audit log {self.path} fails verification at line {bad}; refusing "
                                  "to append (move it aside and investigate)")
        self.seq, self.last_hash = 0, GENESIS_HASH
        if self.path.exists():
            last = self._last_record()
            if last is not None:
                self.seq, self.last_hash = int(last["seq"]), str(last["hash"])
        self._fh = open(self.path, "ab", buffering=0)      # raw: no Python-side buffer to tear
        self._good_size = os.fstat(self._fh.fileno()).st_size
        self._pending: tuple[int, str, bytes] | None = None
        if dropped is not None:
            self.write("audit_torn_tail_dropped", dropped)

    # A crash mid-write can leave an unterminated last line. If it is nevertheless a complete, valid
    # successor it is kept (newline added); otherwise it was never durable and is dropped (and the
    # drop itself is recorded once the log is open).
    def _repair_torn_tail(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return None
        cut = data.rfind(b"\n") + 1
        tail = data[cut:]
        prev_hash, prev_seq = GENESIS_HASH, 0
        if cut:
            prev_line = data[:cut - 1].rsplit(b"\n", 1)[-1]
            try:
                prev = json.loads(prev_line.decode("utf-8"))
                prev_hash, prev_seq = str(prev["hash"]), int(prev["seq"])
            except (UnicodeDecodeError, ValueError, KeyError, TypeError):
                prev_hash, prev_seq = "", -1    # chain verification will report it
        with open(self.path, "r+b") as fh:
            if _check_record(tail, prev_hash, prev_seq + 1) is not None:
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
                info = None
            else:
                fh.truncate(cut)
                info = {"bytes": len(tail), "sha256": hashlib.sha256(tail).hexdigest()}
            fh.flush()
            os.fsync(fh.fileno())
        return info

    def _last_record(self) -> dict[str, Any] | None:
        last = None
        with open(self.path, "rb") as fh:
            for raw in fh:
                if raw.strip():
                    last = raw
        return json.loads(last.decode("utf-8")) if last else None

    def write(self, kind: str, payload: Mapping[str, Any] | None) -> None:
        """Append one record. ``seq``/``last_hash`` advance only AFTER the whole line is in the file
        (and fsync'd when durable): an exception or interrupt mid-write leaves the in-memory chain at
        the last good record, and the next write first adopts the line (if it fully landed) or cuts
        the torn bytes off, so the chain can never fork or carry a half line."""
        with self._lock:
            self._settle_pending()
            rec: dict[str, Any] = {
                "seq": self.seq + 1,
                "ts": _iso(self.clock.now(), ms=True),
                "run_id": self.run_id,
                "strategy_version": self.strategy_version,
                "kind": self._scrub.text(kind),
                "payload": self._scrub.tree(dict(payload or {})),
                "prev_hash": self.last_hash,
            }
            rec["hash"] = record_hash(self.last_hash, rec)
            line = (canonical_json(rec) + "\n").encode("utf-8")
            self._pending = (int(rec["seq"]), str(rec["hash"]), line)
            self._raw_write(line)
            self._commit_pending()

    def _raw_write(self, line: bytes) -> None:
        fd = self._fh.fileno()
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view):]
        if self.durable:
            os.fsync(fd)

    def _commit_pending(self) -> None:
        assert self._pending is not None
        seq, digest, line = self._pending
        self.seq, self.last_hash = seq, digest
        self._good_size += len(line)
        self._pending = None

    def _settle_pending(self) -> None:
        if self._pending is None:
            return
        _, _, line = self._pending
        fd = self._fh.fileno()
        size = os.fstat(fd).st_size
        landed = False
        if size == self._good_size + len(line):
            with open(self.path, "rb") as fh:
                fh.seek(self._good_size)
                landed = fh.read(len(line)) == line
        if landed:
            if self.durable:
                os.fsync(fd)
            self._commit_pending()          # the interrupted write had completed: keep it
            return
        os.ftruncate(fd, self._good_size)   # torn or missing: drop it; the chain continues intact
        if self.durable:
            os.fsync(fd)
        self._pending = None

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                try:
                    self._settle_pending()
                finally:
                    self._fh.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"AuditLog({str(self.path)!r}, seq={self.seq})"


# --------------------------------------------------------------------------- final report
def _q(x: Decimal | None, places: str) -> str | None:
    return None if x is None else plain(x.quantize(Decimal(places)))


def _book_summary(book: OrderBook) -> dict[str, Any]:
    return {"best_bid": plain(book.best_bid), "best_ask": plain(book.best_ask), "mid": plain(book.mid),
            "spread_bps": _q(book.spread_bps, "0.01"), "ts": _iso(book.ts)}


def _decision_id(payload: Mapping[str, Any]) -> str | None:
    d = payload.get("decision_id")
    if d is None and isinstance(payload.get("decision"), Mapping):
        d = payload["decision"].get("decision_id")
    return None if d is None else str(d)


def _equity_summary(curve: list[tuple[float, Decimal]], initial: Decimal, final_net: Decimal | None,
                    end_ts: float, peak_recorded: Decimal) -> dict[str, Any]:
    pts = list(curve)
    if final_net is not None and (not pts or pts[-1][1] != final_net or pts[-1][0] != end_ts):
        pts.append((end_ts, final_net))
    peak, max_dd = initial, Decimal(0)
    for _, v in pts:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    step = max(1, math.ceil(len(pts) / 100))
    sample = pts[::step]
    if pts and sample[-1] is not pts[-1]:
        sample.append(pts[-1])
    values = [v for _, v in pts]
    return {
        "points": len(pts),
        "includes_final_valuation": final_net is not None,
        "first_at": _iso(pts[0][0]) if pts else None,
        "last_at": _iso(pts[-1][0]) if pts else None,
        "min_tmn": _q(min(values), "0.01") if values else None,
        "max_tmn": _q(max(values), "0.01") if values else None,
        "peak_tmn": _q(max(peak, peak_recorded), "0.01"),
        "max_drawdown_pct": _q(max_dd * 100, "0.0001"),
        "sample": [[_iso(t), _q(v, "0.01")] for t, v in sample],
    }


def _base_decision_id(did: Any) -> str | None:
    """Orders of a forced exit carry ``<decision_id>:forced``: they belong to that decision."""
    return None if did is None else str(did).split(":", 1)[0]


def _compact_tools(calls: Any) -> list[dict[str, Any]]:
    out = []
    for tc in calls or []:
        if isinstance(tc, Mapping):
            out.append({"tool": tc.get("tool") or tc.get("name"), "called": tc.get("called"),
                        "ok": tc.get("ok"), "why": str(tc.get("why") or "")[:160]})
    return out


def _decision_section(store: Any, order_rows: Mapping[str, dict[str, Any]],
                      fills_by_order: Mapping[str, list[dict[str, Any]]],
                      outcomes: Mapping[str, list[dict[str, Any]]],
                      reflections: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Every NON-HOLD decision (and any decision that led to orders) in full, with the spec fields;
    HOLDs as counts plus a compact sample (first, last, every state change)."""
    linked: dict[str, list[str]] = {}
    for cid, row in order_rows.items():
        base = _base_decision_id(row.get("decision_id"))
        if base:
            linked.setdefault(base, []).append(cid)
    for ev in store.iter_events("decision_orders"):
        base = _base_decision_id(ev.payload.get("decision_id"))
        for cid in ev.payload.get("order_ids") or []:
            if base and cid not in linked.setdefault(base, []):
                linked[base].append(cid)
    actions: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    entries: list[dict[str, Any]] = []
    hold_strategies: Counter[str] = Counter()
    hold_sample: list[dict[str, Any]] = []
    holds = scored = 0
    last_hold: dict[str, Any] | None = None
    prev_state: Any = None
    seen: set[str] = set()
    for ev in store.iter_events("decision"):
        raw = ev.payload
        p = raw["decision"] if isinstance(raw.get("decision"), Mapping) else raw   # AgentMemory wraps it
        ctx = raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
        action = str(p.get("action", "UNKNOWN"))
        actions[action] += 1
        for tc in p.get("tool_calls") or []:
            if isinstance(tc, Mapping):
                tool_counts[str(tc.get("name") or tc.get("tool") or "?")] += 1
        did = _decision_id(p)
        if did:
            seen.add(did)
        hyp = p.get("hypothesis") if isinstance(p.get("hypothesis"), Mapping) else {}
        strategy = hyp.get("strategy") or hyp.get("name")
        state = (p.get("state_features") or {}).get("state") if isinstance(p.get("state_features"), Mapping) \
            else None
        state = state or ctx.get("state")
        ts = _iso(float(p["ts"])) if isinstance(p.get("ts"), (int, float)) else _iso(ev.ts)
        ids = sorted(linked.get(did or "", []), key=lambda c: (order_rows[c].get("created_at") or "", c)
                     if c in order_rows else ("", c))
        if action == "HOLD" and not ids:
            holds += 1
            scored += 1 if did in reflections else 0
            hold_strategies[str(strategy or "?")] += 1
            compact = {"decision_id": did, "timestamp": ts, "state": state, "strategy": strategy,
                       "confidence": p.get("confidence"), "chosen_tool": p.get("chosen_tool"),
                       "rationale": str(p.get("rationale", ""))[:200]}
            if not hold_sample or state != prev_state:
                hold_sample.append(compact)
            last_hold = compact
            prev_state = state
            continue
        prev_state = state
        fills = [f for c in ids for f in fills_by_order.get(c, [])]
        outs = outcomes.get(did or "", [])
        refl = reflections.get(did or "", [])
        entries.append({
            "decision_id": did, "timestamp": ts, "at": ts, "cycle": p.get("cycle"),
            "strategy_version": p.get("strategy_version"), "action": action, "strategy": strategy,
            "rationale": str(p.get("rationale", ""))[:500], "confidence": p.get("confidence"),
            "estimated_net_edge": p.get("estimated_net_edge"), "hypothesis": hyp,
            "alternatives": list(p.get("alternatives") or []),
            "state_features": dict(p.get("state_features") or {}),
            "chosen_tool": p.get("chosen_tool"), "tool_calls": _compact_tools(p.get("tool_calls")),
            "observed_prices": dict(p.get("observed_prices") or {}), "intent": p.get("intent"),
            "risk_verdict": p.get("risk_verdict"),
            "order_ids": ids, "orders": [order_rows[c] for c in ids if c in order_rows], "fills": fills,
            "fee_assets": sorted({str(f["fee_asset"]) for f in fills}),
            "balances": {"ledger": ctx.get("ledger"), "exchange": ctx.get("exchange_balances"),
                         "note": "at decision time"} if ctx else None,
            "equity": {"equity_tmn_net": ctx.get("equity_tmn_net"), "peak_tmn": ctx.get("peak_equity_tmn"),
                       "note": "at decision time, net of exit costs"} if ctx else None,
            "drawdown": {"pct": ctx.get("drawdown_pct"), "note": "at decision time"} if ctx else None,
            "outcome": outs[-1] if outs else None, "reflection": refl[-1] if refl else None,
            "result": {"order": next((o.get("result") for o in reversed(outs) if o.get("kind") == "order"), None),
                       "round_trip": next((o.get("result") for o in reversed(outs) if o.get("kind") == "round_trip"),
                                          None),
                       "reflection": (refl[-1].get("result") or refl[-1].get("lesson")) if refl else None},
        })
    if last_hold is not None and (not hold_sample or hold_sample[-1] is not last_hold):
        hold_sample.append(last_hold)
    orphans = [{"client_id": c, "decision_id": r.get("decision_id"), "purpose": r.get("purpose")}
               for c, r in order_rows.items() if _base_decision_id(r.get("decision_id")) not in seen]
    return {"entries": entries, "actions": actions, "tool_counts": tool_counts, "holds": holds,
            "scored": scored, "hold_strategies": hold_strategies, "hold_sample": hold_sample[:200],
            "orphans": orphans}


def build_final_report(*, config: Any, mode: Mode | str, run_id: str, started_ts: float,
                       ended_ts: float, portfolio: Any, engine_status: Mapping[str, Any] | None,
                       store: Any, memory_snapshot: Any, books_at_end: Mapping[str, OrderBook] | None,
                       fees: Mapping[str, Any] | None, caveats: Sequence[str] | None,
                       stop_reason: str | None, synthetic_data: bool = False,
                       clock_kind: str | None = None,
                       exchange_check: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble ``final_report.json`` content from persisted facts (store, ledger, books).
    Values are net of exit costs; nothing is rounded in the agent's favour. ``clock_kind``:
    'simulated' (SimClock: backtest/demo) or 'real' (default: by mode). ``exchange_check``: whether
    the final ledger was verified against exchange balances (live)."""
    scrub = Scrubber([config.api_key] if getattr(config, "api_key", "") else [])
    mode_v = Mode(mode).value
    clock_kind = clock_kind or ("simulated" if mode_v == Mode.BACKTEST.value else "real")
    books = dict(books_at_end or {})
    initial = portfolio.initial_tmn

    # ---- valuation (net of exit costs: BTC -> USDT -> TMN through the bids at taker fee)
    flags: list[str] = []
    try:
        eq = portfolio.liquidation_value(books, fees)
        final_net, final_mid, bd = eq.equity_tmn_net, eq.equity_tmn_mid, dict(eq.breakdown)
        flags += list(eq.flags)
    except Exception as exc:  # noqa: BLE001 - the report must still be written
        final_net = final_mid = None
        bd = {}
        flags.append(f"valuation_error:{type(exc).__name__}")
    ubook = books.get(portfolio.usdt_symbol)
    usdt_mid = ubook.mid if ubook is not None else None
    final_usdt = None
    if final_net is not None and usdt_mid:
        final_usdt = bd.get("usdt_after_btc_liquidation", Decimal(0)) + bd.get("tmn_qty", Decimal(0)) / usdt_mid
    basis = portfolio.usdt_cost_basis_tmn()
    initial_usdt = initial / basis if basis else None

    pnl_abs = None if final_net is None else final_net - initial
    multiple = None if final_net is None else final_net / initial
    target = Decimal(config.target_multiple)
    if multiple is None:
        reached = None
        statement = ("UNKNOWN whether the target was reached: the final value could not be computed "
                     f"net of exit costs ({', '.join(flags) or 'valuation unavailable'})")
    else:
        reached = multiple >= target
        statement = (f"Target {plain(target)}x {'REACHED' if reached else 'NOT reached'}: final/initial = "
                     f"{plain(multiple.quantize(Decimal('0.0001')))}x net of exit costs")

    # ---- orders & fills (from the store, i.e. what actually happened)
    orders = store.list_orders()
    fills = store.list_fills()
    order_rows = [{
        "client_id": o.client_id, "exchange_client_order_id": o.exchange_client_order_id,
        "symbol": o.symbol, "side": o.side, "purpose": o.purpose, "decision_id": o.decision_id,
        "lifecycle": o.lifecycle, "price": o.price, "quantity": o.quantity,
        "executed_qty": o.executed_qty, "executed_sum": o.executed_sum, "fee_total": o.fee_total,
        "fee_asset": o.fee_asset, "fee_source": o.fee_source, "created_at": _iso(o.created_ts),
        "updated_at": _iso(o.updated_ts), "note": o.note} for o in orders]
    fill_rows = [{"fill_key": f.fill_key, "client_id": f.client_id, "symbol": f.symbol, "side": f.side,
                  "price": f.price, "quantity": f.quantity, "quote_sum": f.quote_sum, "fee": f.fee,
                  "fee_asset": f.fee_asset, "fee_source": f.fee_source, "at": _iso(f.ts)} for f in fills]
    fee_by_source: dict[str, dict[str, Decimal]] = {}
    for f in fills:
        slot = fee_by_source.setdefault(f.fee_source, {})
        slot[f.fee_asset] = slot.get(f.fee_asset, Decimal(0)) + f.fee
    source_counts = Counter(f.fee_source for f in fills)

    # ---- decisions: hypothesis vs result, with the orders/fills each one led to
    outcomes: dict[str, list[dict[str, Any]]] = {}
    reflections: dict[str, list[dict[str, Any]]] = {}
    for kind, sink in (("outcome", outcomes), ("reflection", reflections)):
        for ev in store.iter_events(kind):
            did = _decision_id(ev.payload)
            if did is not None:
                sink.setdefault(did, []).append(dict(ev.payload))
    fills_by_order: dict[str, list[dict[str, Any]]] = {}
    for row in fill_rows:
        fills_by_order.setdefault(str(row["client_id"]), []).append(row)
    sec = _decision_section(store, {str(r["client_id"]): r for r in order_rows}, fills_by_order,
                            outcomes, reflections)
    actions, trade_decisions, scored_holds = sec["actions"], sec["entries"], sec["scored"]
    tool_events = Counter(str(ev.payload.get("name") or ev.payload.get("tool") or "?")
                          for ev in store.iter_events("tool_call"))
    tool_usage = ({"source": "tool_call events", "counts": dict(tool_events)} if tool_events else
                  {"source": "decision.tool_calls", "counts": dict(sec["tool_counts"])})
    max_listed = 500
    pos = portfolio.btc_position()
    holdings = portfolio.expected_balances()

    # ---- labels & caveats
    labels: list[str] = []
    if mode_v in (Mode.PAPER.value, Mode.BACKTEST.value):
        labels.append("paper_no_real_orders")
        labels.append("assumed_fee")
    if mode_v == Mode.BACKTEST.value:
        labels.append("backtest_simulated")
    if synthetic_data:
        labels.append("synthetic_data")
    if source_counts.get("estimated"):
        labels.append("estimated_fee_fills")
    if final_net is None:
        labels.append("valuation_incomplete")
    if clock_kind == "simulated":
        labels.append("simulated_clock")
    verified = (exchange_check or {}).get("verified")
    if mode_v == Mode.LIVE.value and verified is not True:
        labels.append("valuation_unverified")

    def fee_text(src: str) -> str:
        assets = fee_by_source.get(src, {})
        return ", ".join(f"{plain(v)} {a}" for a, v in sorted(assets.items())) or "none"

    honest = [
        "Past results (backtest, paper or this run) are not predictive of future results.",
        f"Reaching {plain(target)}x is NOT guaranteed and was never assumed: the agent trades only when "
        "its estimated edge after all costs clears the hurdle, and holding is the default.",
        "Paper and backtest fills are SIMULATED (SimExchange walking order books): real queue position, "
        "latency, partial fills and market impact can differ."
        + (" No real orders were placed in this run." if mode_v != Mode.LIVE.value else
           " This run was LIVE: its fills are real exchange fills."),
        "UNVERIFIED against the live Wallex API (GET-only probing cannot settle these): "
        + "; ".join(f"{k}: {v}" for k, v in UNVERIFIED),
        f"Fee sources: {source_counts.get('exchange', 0)} fill(s) with exchange-reported fees "
        f"({fee_text('exchange')}); {source_counts.get('estimated', 0)} fill(s) with ESTIMATED fees "
        f"({fee_text('estimated')}, executed sum x fee rate)."
        + (f" In {mode_v} mode 'exchange' fees come from the simulator at the ASSUMED rate "
           f"{plain(Decimal(config.paper_fee_rate))}, not from Wallex." if mode_v != Mode.LIVE.value else ""),
        "Final value is net of exit costs (BTC sold into BTCUSDT bids, then USDT into USDTTMN bids, taker "
        "fee each); the mid-price value is for reference only.",
    ]
    if synthetic_data:
        honest.append("Market data was SYNTHETIC (generated), not real Wallex history.")
    if clock_kind == "simulated":
        honest.append("All timestamps in this report are SIMULATED market time (the replay/synthetic clock), "
                      "not wall-clock time.")
    if mode_v == Mode.LIVE.value and verified is not True:
        honest.append("The final value is the LEDGER (recorded fills) valued at the final books; it was NOT "
                      "verified against the exchange balances at the end"
                      + (f" ({(exchange_check or {}).get('detail')})" if (exchange_check or {}).get("detail") else "")
                      + ". Check the account in the Wallex app.")
    if flags:
        honest.append(f"Valuation flags: {', '.join(flags)}.")
    honest += [str(c) for c in (caveats or [])]

    policy = getattr(config, "final_policy", "report_only")
    btc_left = holdings.get(portfolio.base, Decimal(0))
    policy_note = {
        "report_only": "report_only: holdings were left as they were; nothing was sold at the end.",
        "flatten_to_usdt": "flatten_to_usdt: BTC was to be sold to USDT at the end.",
        "flatten_to_tmn": "flatten_to_tmn: BTC and USDT were to be sold to TMN at the end.",
    }.get(policy, policy)
    if policy != "report_only" and btc_left > 0:
        policy_note += f" BTC still held: {plain(btc_left)} (dust/below minimum or exit not completed)."

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "mode": mode_v,
        "strategy_version": STRATEGY_VERSION,
        "labels": labels,
        "started_at": _iso(started_ts),
        "ended_at": _iso(ended_ts),
        "clock": clock_kind,
        "clock_note": ("timestamps are SIMULATED market time (SimClock), not wall-clock time"
                       if clock_kind == "simulated" else "timestamps are wall-clock UTC"),
        "duration_h": round((float(ended_ts) - float(started_ts)) / 3600, 4),
        "stop_reason": stop_reason,
        "config": config.redacted(),
        "value": {
            "basis": "ledger (recorded fills only) valued at the final books net of exit costs",
            "verified_against_exchange": verified if mode_v == Mode.LIVE.value else None,
            "exchange_check": dict(exchange_check) if exchange_check else None,
            "initial_tmn": initial,
            "final_tmn_net_of_exit_costs": _q(final_net, "0.01"),
            "final_tmn_at_mid": _q(final_mid, "0.01"),
            "initial_usdt_equivalent": _q(initial_usdt, "0.000001"),
            "final_usdt_equivalent": _q(final_usdt, "0.000001"),
            "usdt_cost_basis_tmn": _q(basis, "0.01"),
            "valuation_flags": flags,
            "breakdown": bd,
        },
        "pnl": {
            "abs_tmn": _q(pnl_abs, "0.01"),
            "pct": _q(None if pnl_abs is None else pnl_abs / initial * 100, "0.0001"),
            "abs_usdt": _q(final_usdt - initial_usdt, "0.000001")
            if final_usdt is not None and initial_usdt is not None else None,
            "realized_btc_pnl_usdt": pos.realized_pnl_usdt,
            "multiple": _q(multiple, "0.000001"),
            "target_multiple": target,
            "target_reached": reached,
            "target_statement": statement,
        },
        "fees_paid": portfolio.fees_paid(),
        "fees_by_source": fee_by_source,
        "fee_source_counts": dict(source_counts),
        "orders": {"count": len(orders), "by_lifecycle": dict(Counter(o.lifecycle.value for o in orders)),
                   "order_ids": [o.client_id for o in orders],
                   "exchange_order_ids": [o.exchange_client_order_id for o in orders
                                          if o.exchange_client_order_id],
                   "list": order_rows},
        "fills": {"count": len(fills), "list": fill_rows},
        "balances_expected": holdings,
        "position": pos,
        "equity_curve": _equity_summary(portfolio.equity_curve(), initial, final_net, float(ended_ts),
                                        portfolio.peak_equity()),
        "round_trips": {"count": pos.round_trips, "wins": pos.wins, "losses": pos.losses,
                        "decisions": trade_decisions[:max_listed],
                        "decisions_truncated": max(0, len(trade_decisions) - max_listed)},
        "decisions_by_action": dict(actions),
        "hold_decisions_scored": scored_holds,
        "hold_decisions": {"count": sec["holds"], "scored": scored_holds,
                           "by_strategy": dict(sec["hold_strategies"]),
                           "sample": sec["hold_sample"],
                           "sample_rule": "first and last HOLD, plus every HOLD where the bot state changed"},
        "orders_without_decision": sec["orphans"],
        "tool_usage": tool_usage,
        "memory": memory_snapshot,
        "end": {"holdings": holdings, "final_policy": policy, "final_policy_applied": policy_note,
                "books": {s: _book_summary(b) for s, b in sorted(books.items())}},
        "engine_status": dict(engine_status or {}),
        "honest_caveats": honest,
    }
    return scrub.tree(report)


def write_final_report(path: str | Path, report: Mapping[str, Any], *,
                       secrets: Sequence[str] = ()) -> Path:
    """Atomic write (temp + fsync + rename) of an already scrubbed report, scrubbed once more."""
    return atomic_write_json(path, Scrubber(secrets).tree(dict(report)))


# --------------------------------------------------------------------------- trace
_STAGE = {
    "observe": "OBSERVE",
    "decision": "PLAN", "plan": "PLAN", "risk_verdict": "PLAN", "forced_exit": "PLAN",
    "tool_call": "TOOL", "tool_result": "TOOL",
    "submit_result": "ACTION", "submit_blocked": "ACTION", "submit_declined": "ACTION",
    "send_refused": "ACTION", "conversion": "ACTION", "kill": "ACTION",
    "fill": "VERIFY", "state_transition": "VERIFY", "reconcile": "VERIFY", "verify": "VERIFY",
    "order_update": "VERIFY", "order_cancelled": "VERIFY", "order_not_found": "VERIFY",
    "filled_during_cancel": "VERIFY", "quarantine_lifted": "VERIFY", "read_failed": "VERIFY",
    "fill_deferred": "VERIFY", "order_status_unrecognised": "VERIFY", "cancel_unverified": "VERIFY",
    "outcome": "REFLECT", "reflection": "REFLECT",
    "alert": "ALERT",
}
_KEYS_BY_STAGE = {
    "OBSERVE": ("cycle", "symbol", "bid", "ask", "mid", "prices", "observed_prices", "regime", "state"),
    "PLAN": ("decision_id", "action", "confidence", "estimated_net_edge", "approved", "reasons",
             "strategy", "chosen_tool"),
    "TOOL": ("name", "tool", "ok", "latency_ms", "why", "reason", "error"),
    "ACTION": ("decision_id", "client_id", "purpose", "symbol", "side", "price", "quantity", "qty",
               "status", "filled_qty", "rounds", "tmn_spent", "usdt_received", "reason", "order_ids"),
    "VERIFY": ("client_id", "from", "to", "lifecycle", "symbol", "side", "quantity", "price", "fee",
               "fee_asset", "fee_source", "executed_qty", "ok", "reason"),
    "REFLECT": ("decision_id", "result", "pnl_usdt", "pnl_bps", "error_bps", "correct", "lesson"),
    "ALERT": ("alert", "level", "reason", "client_id", "detail"),
    "OTHER": (),
}


def _stage(kind: str) -> str:
    k = kind.lower()
    if k in _STAGE:
        return _STAGE[k]
    if "alert" in k or k == "needs_manual_reconcile":
        return "ALERT"
    if k.startswith(("order_", "submit_", "cancel")):
        return "ACTION"
    if k.startswith(("reconcile", "unknown_", "verify")):
        return "VERIFY"
    if k.startswith(("tool", )):
        return "TOOL"
    return "OTHER"


def _short(v: Any, n: int = 70) -> str:
    if isinstance(v, Mapping):
        s = ",".join(f"{k}={_short(x, 24)}" for k, x in list(v.items())[:4])
    elif isinstance(v, (list, tuple)):
        s = "[" + ",".join(_short(x, 24) for x in list(v)[:4]) + (",..." if len(v) > 4 else "") + "]"
    else:
        s = str(v)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _summary(stage: str, kind: str, payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    p = {k: v for k, v in payload.items() if not (k in ("intent", "result") and isinstance(v, Mapping))}
    for nested in ("intent", "result"):          # engine records wrap the interesting fields
        if isinstance(payload.get(nested), Mapping):
            for k, v in payload[nested].items():
                p.setdefault(k, v)
    if kind == "state_transition" and "from" in p and "to" in p:
        return f"{p.get('from')} -> {p.get('to')}: {_short(p.get('reason', ''), 90)}"
    if kind == "fill" and {"side", "quantity", "price"} <= set(p):
        return (f"{p.get('side')} {p.get('quantity')} {p.get('symbol', '')} @ {p.get('price')} "
                f"fee {p.get('fee', '?')} {p.get('fee_asset', '')} ({p.get('fee_source', '?')})").strip()
    for key in _KEYS_BY_STAGE.get(stage, ()):
        if key in p and p[key] not in (None, "", [], {}):
            parts.append(f"{key}={_short(p[key])}")
    hyp = p.get("hypothesis")
    if isinstance(hyp, Mapping) and hyp.get("name"):
        parts.append(f"hypothesis={hyp.get('name')}"
                     + (f"({hyp.get('direction')}, {hyp.get('expected_move_bps')}bps)"
                        if hyp.get("direction") else ""))
    if not parts:   # unknown kind: first few scalar fields
        parts = [f"{k}={_short(v)}" for k, v in list(p.items())[:5]]
    text = " ".join(parts)
    for long_key in ("rationale", "lesson"):
        if long_key in p and long_key not in text:
            text += f" | {_short(p[long_key], 110)}"
    return text


def _mentions(obj: Any, needle: str) -> bool:
    if isinstance(obj, Mapping):
        return any(_mentions(v, needle) for v in obj.values())
    if isinstance(obj, list):
        return any(_mentions(v, needle) for v in obj)
    return obj == needle


def render_trace(audit_path: str | Path, *, last: int | None = None, decision_id: str | None = None) -> str:
    """Human-readable observe -> plan -> tool -> action -> verify -> reflect trace. Records are grouped
    into cycles (each ``observe`` starts one); ``last`` keeps the last N cycles, ``decision_id`` keeps
    the cycles mentioning that decision."""
    path = Path(audit_path)
    recs = read_audit(path)
    ok, bad = verify_chain(path)
    head = (f"Audit trace {path.name}: {len(recs)} record(s), chain "
            + ("OK" if ok else ("MISSING" if bad == 0 else f"BROKEN at line {bad}")))
    blocks: list[list[dict[str, Any]]] = [[]]
    for r in recs:
        if str(r.get("kind", "")).lower() == "observe" and blocks[-1]:
            blocks.append([])
        blocks[-1].append(r)
    blocks = [b for b in blocks if b]
    if decision_id is not None:
        blocks = [b for b in blocks if any(_mentions(r.get("payload"), str(decision_id)) for r in b)]
        if not blocks:
            return head + f"\n(no records mention decision_id {decision_id})\n"
    if last is not None:
        blocks = blocks[-last:] if last > 0 else []
    out = [head]
    for b in blocks:
        first = b[0]
        p0 = first.get("payload") if isinstance(first.get("payload"), Mapping) else {}
        if str(first.get("kind", "")).lower() == "observe":
            dids = sorted({d for r in b if isinstance(r.get("payload"), Mapping)
                           for d in [_decision_id(r["payload"])] if d})
            title = f"cycle {p0.get('cycle', '?')} @ {first.get('ts', '?')}"
            if dids:
                title += f" (decision {', '.join(dids)})"
        else:
            title = "records before the first observe"
        out.append(f"=== {title} ===")
        for r in b:
            kind = str(r.get("kind", "?"))
            if kind == "_unreadable":
                out.append(f"  [line {r.get('line')}] UNREADABLE record")
                continue
            payload = r.get("payload") if isinstance(r.get("payload"), Mapping) else {}
            st = _stage(kind)
            out.append(f"  {'#' + str(r.get('seq', '?')):>6} {str(r.get('ts', ''))[11:23]:<12} {st:<7} "
                       f"{kind:<18} {_summary(st, kind, payload)}".rstrip())
    return "\n".join(out) + "\n"
