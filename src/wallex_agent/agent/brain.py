"""``AgentBrain`` facade: decide -> record -> verify (order results) -> reflect, all event-sourced.

The brain's working state (price history, recent USDTTMN spreads, decision counter, memory) is
rebuilt from the store on construction, so a restarted brain makes the same decisions as one that
kept running (same seed, same subsequent observations). For that, the runner must
``record_decision`` every decision it obtained from ``decide`` (the decision event carries the price
points the brain added to its history that cycle).
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable, Mapping

from ..clock import Clock, RealClock
from ..config import Config
from ..types import BPS, ONE, Decision, OrderBook, Side
from .features import PriceHistory
from .memory import AgentMemory
from .planner import Observation, PlanContext, Planner
from .reflection import Reflection, reflect, render_trace, trace_record


class _JournaledHistory(PriceHistory):
    def __init__(self, tag: str, journal: list[list[Any]]) -> None:
        super().__init__()
        self.tag = tag
        self.journal = journal

    def add(self, ts: float, price: float, *, overwrite: bool = True) -> None:
        self.journal.append([self.tag, float(ts), float(price), bool(overwrite)])
        super().add(ts, price, overwrite=overwrite)


def _dict(x: Any) -> dict[str, Any]:
    if x is None:
        return {}
    if hasattr(x, "to_dict"):
        return x.to_dict()
    if hasattr(x, "__dataclass_fields__"):
        return {k: getattr(x, k) for k in x.__dataclass_fields__}
    return dict(x)


class AgentBrain:
    def __init__(self, config: Config, store_or_memory: Any, clock: Clock | None = None, *,
                 book_source: Any = None, timer: Callable[[], float] | None = None) -> None:
        self.config = config
        self.clock = clock or RealClock()
        self.memory = store_or_memory if isinstance(store_or_memory, AgentMemory) else AgentMemory(store_or_memory)
        self.book_source = book_source
        self.timer = timer or time.perf_counter
        self.planner = Planner(self.memory, config)
        self._journal: list[list[Any]] = []
        self.history = _JournaledHistory("btc", self._journal)
        self.fx_history = _JournaledHistory("fx", self._journal)
        self.fx_spreads: list[float] = []
        self._pending_points: dict[str, list[list[Any]]] = {}
        self._cycle = 0
        self._rebuild()

    # ------------------------------------------------------------------ restart
    def _rebuild(self) -> None:
        for p in self.memory.iter_decision_payloads():
            for tag, ts, price, ow in p.get("points") or []:
                (self.history if tag == "btc" else self.fx_history).add(ts, price, overwrite=ow)
            sp = (p["decision"].get("state_features") or {}).get("fx_spread_bps")
            if isinstance(sp, (int, float)):
                self.fx_spreads.append(float(sp))
            self._cycle = max(self._cycle, int(p["decision"].get("cycle") or 0))
        self._journal.clear()
        self.fx_spreads = self.fx_spreads[-500:]

    # ------------------------------------------------------------------ decide
    def decide(self, obs: Observation) -> Decision:
        cfg = self.config
        self._cycle += 1
        self._journal.clear()
        btc = obs.books.get(cfg.btc_symbol)
        fx = obs.books.get(cfg.usdt_symbol)
        if btc is not None:
            self.history.add(obs.now, float(btc.mid))
        if fx is not None:
            self.fx_history.add(obs.now, float(fx.mid))
        ctx = PlanContext(cycle=self._cycle, history=self.history, fx_history=self.fx_history,
                          fx_spreads=list(self.fx_spreads), book_source=self.book_source, timer=self.timer,
                          convert_wait_streak=self.memory.convert_wait_streak,
                          open_entry=self.memory.open_entry())
        d = self.planner.decide(obs, ctx)
        self._pending_points[d.decision_id] = list(self._journal)
        self._journal.clear()
        if fx is not None:
            self.fx_spreads.append(round(float(fx.spread_bps), 6))
            del self.fx_spreads[:-500]
        return d

    step_decide = decide

    def record_decision(self, d: Decision) -> int:
        return self.memory.record_decision(d, extra={"points": self._pending_points.pop(d.decision_id, [])})

    # ------------------------------------------------------------------ verify
    def on_order_result(self, decision_id: str, result: Any) -> dict[str, Any]:
        """Record what execution reported for a decision and verify it against the intent
        (fill ratio, realised vs reference price when an average price is given)."""
        res = _dict(result)
        d = self.memory.decision(decision_id) or {}
        intent = d.get("intent") or {}
        verify: dict[str, Any] = {"status": res.get("status")}
        try:
            want = Decimal(str(intent.get("quantity"))) if intent else None
            got = Decimal(str(res.get("filled_qty", "0")))
            if want and want > 0:
                verify["fill_ratio"] = float((got / want).quantize(Decimal("0.0001")))
            avg = res.get("avg_price")
            ref = intent.get("reference_price")
            if avg is not None and ref:
                sign = ONE if intent.get("side") == Side.BUY.value else -ONE
                verify["slippage_bps"] = float(((Decimal(str(avg)) / Decimal(str(ref)) - ONE) * BPS * sign)
                                               .quantize(Decimal("0.01")))
        except (ArithmeticError, ValueError):
            verify["note"] = "could not compare result with intent"
        self.memory.record_outcome(decision_id, res, kind="order", ts=self.clock.now(), verify=verify)
        return verify

    on_outcome = on_order_result

    # ------------------------------------------------------------------ reflect
    def on_round_trip_closed(self, entry_decision_id: str | None = None, *, pnl_usdt: Decimal,
                             cost_usdt: Decimal, exit_ts: float, exit_price: Decimal | None = None,
                             exit_decision_id: str | None = None, reason: str = "") -> Reflection:
        """Close the open entry's round trip and reflect on it. ``pnl_usdt`` is net of all fees,
        ``cost_usdt`` the fee-inclusive entry cost; the realised market move uses ``exit_price`` (or the
        exit decision's mid) against the entry decision's mid."""
        eid = entry_decision_id or (self.memory.open_entry() or {}).get("decision_id")
        d = self.memory.decision(eid) if eid else None
        if d is None:
            raise ValueError(f"unknown entry decision {eid!r}")
        sym = self.config.btc_symbol
        entry_mid = Decimal(d["observed_prices"][f"{sym}.mid"])
        ref = exit_price
        if ref is None and exit_decision_id:
            xd = self.memory.decision(exit_decision_id)
            if xd is not None:
                ref = Decimal(xd["observed_prices"][f"{sym}.mid"])
        net_bps = float(Decimal(pnl_usdt) / Decimal(cost_usdt) * BPS) if cost_usdt else 0.0
        realized = float((Decimal(ref) / entry_mid - ONE) * BPS) if ref is not None else net_bps
        outcome = {"kind": "round_trip", "ts": float(exit_ts), "realized_move_bps": round(realized, 2),
                   "net_return_bps": round(net_bps, 2), "pnl_usdt": str(pnl_usdt), "exit_reason": reason,
                   "exit_decision_id": exit_decision_id}
        self.memory.record_outcome(eid, outcome, kind="round_trip", ts=float(exit_ts))   # type: ignore[arg-type]
        refl = reflect(d, outcome)
        self.memory.record_reflection(refl)
        return refl

    def reflect_pending(self, now: float, books: Mapping[str, OrderBook]) -> list[Reflection]:
        """Score abstentions whose horizon has elapsed: would buying the best candidate (recorded
        entry price and size) and selling into today's bids have paid after both taker fees?"""
        book = books.get(self.config.btc_symbol)
        out: list[Reflection] = []
        if book is None:
            return out
        for d in self.memory.pending_abstentions():
            cf = d["hypothesis"]["counterfactual"]
            if now < float(d["ts"]) + float(cf["horizon_s"]):
                continue
            qty, entry = Decimal(cf["qty"]), Decimal(cf["entry_price"])
            fb, fs = Decimal(cf["fee_buy"]), Decimal(cf["fee_sell"])
            w = book.walk_qty(Side.SELL, qty)
            if w.filled_qty <= 0 or entry <= 0:
                continue
            # unfilled remainder (if any) valued at zero: conservative for "would it have paid"
            proceeds = w.quote_sum * (ONE - fs)
            paid_in = entry * qty * (ONE + fb)
            cf_net = float((proceeds / paid_in - ONE) * BPS)
            realized = float((book.mid / Decimal(cf["mid"]) - ONE) * BPS)
            outcome = {"kind": "abstention", "ts": float(now), "elapsed_s": round(now - float(d["ts"]), 3),
                       "realized_move_bps": round(realized, 2), "best_cf_net_bps": round(cf_net, 2),
                       "candidates": [{"strategy": c["strategy"], "cf_net_bps": round(cf_net, 2)}
                                      for c in cf.get("candidates") or [] if c.get("direction") == "up"]}
            refl = reflect(d, outcome)
            self.memory.record_reflection(refl)
            out.append(refl)
        return out

    # ------------------------------------------------------------------ reporting
    @staticmethod
    def risk_inputs(d: Decision | Mapping[str, Any]) -> dict[str, Any]:
        """``expected_move_bps`` (Decimal) and ``confidence`` for ``risk.RiskContext``."""
        dd = d.to_dict() if isinstance(d, Decision) else d
        move = (dd.get("hypothesis") or {}).get("expected_move_bps")
        return {"expected_move_bps": Decimal(str(move)) if move is not None else None,
                "confidence": dd.get("confidence")}

    def memory_snapshot(self) -> dict[str, Any]:
        snap = self.memory.snapshot()
        snap.update(cycle=self._cycle, history_points=len(self.history), fx_points=len(self.fx_history))
        return snap

    def trace(self, d: Decision | Mapping[str, Any]) -> dict[str, Any]:
        dd = d.to_dict() if isinstance(d, Decision) else dict(d)
        did = dd["decision_id"]
        return trace_record(dd, self.memory.outcomes.get(did, []), self.memory.reflections.get(did, []))

    def render_trace(self, d: Decision | Mapping[str, Any]) -> str:
        return render_trace(self.trace(d))


__all__ = ["AgentBrain", "Observation", "PlanContext"]
