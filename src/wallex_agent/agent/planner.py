"""Planner: observe -> (adaptive) tools -> hypotheses -> priced alternatives -> one auditable Decision.

Policy, in order (each step can end the cycle with a HOLD that says why):
  1. ``read_book`` (always, cheapest). Stale/missing book, unknown fee/rules, STOPPED, order in flight,
     deadline -> HOLD/STOP (fail closed).
  2. Features; if history is short and a candle source exists -> ``read_candles`` to backfill.
  3. Branch on what is held:
     * TMN (UNFUNDED/CONVERTING, or READY without tradable USDT) -> conversion stage via ``fx_check``.
       Bounded patience: while the USDTTMN spread is abnormally wide (> max(3x recent median, 5 bps))
       but within hard limits, WAIT up to ``CONVERT_MAX_WAIT_CYCLES`` cycles, then convert anyway.
       Beyond hard limits (spread > max_spread_bps, cost > conversion cap, depth short) -> wait, no cap.
     * BTC held -> exit evaluation: re-test the entry thesis with its signal tool, price the exit by
       walking bids, then exit on (a) thesis invalidation (price level or signal flip), (b) time-stop
       at ``TIME_STOP_MULT`` x horizon unless the re-tested thesis would pass the entry gate afresh
       (selling and re-buying would only pay fees twice), (c) take-profit when net-of-exit P&L > 0, the remaining
       calibrated move < exit cost and P&L >= ``TP_FRACTION`` of the predicted net edge. Hard stop-loss,
       drawdown and deadline exits are NOT here (risk.py).
     * USDT -> entry evaluation: warm-up gate, ``risk_headroom``, regime-preferred signal tools first,
       the other signal tools only when uncertainty is high (signals disagree, thin experience in this
       regime, borderline confidence); ``estimate_round_trip_cost`` (book walk for the real size) only
       when some long candidate's gross move exceeds the fee+spread floor — otherwise the floor already
       proves the edge is below hurdle. Gates: long-only, net edge >= ``min_net_edge_bps``, confidence
       >= ``min_confidence``, book absorbs the size, enough time left for the horizon.
  4. Contextual bandit (regime x strategy Beta posteriors) with Thompson sampling ONLY among gate-passing
     candidates plus ``stand_aside``; the best sampled ``theta * net_edge`` must also beat the sampled
     ``stand_aside`` theta, else HOLD. Randomness: ``random.Random(f"{seed}:{cycle}")`` — reproducible
     and identical after a restart (cycle is rebuilt from the event store).

Exit confidence = 1 - Phi(remaining_move / sigma_h) (belief that holding on does not pay);
hold confidence = Phi(remaining_move / sigma_h). Conversion confidence = share of the TMN amount the
visible book fills within the conversion slippage cap.
"""
from __future__ import annotations

import hashlib
import math
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping

from .. import STRATEGY_VERSION
from ..config import Config
from ..types import (BPS, ONE, ZERO, Action, Balance, BotState, Decision, FeeRates, MarketRules, OrderBook,
                     Side, TradeIntent, plain)
from . import strategies
from .features import PriceHistory, compute_features
from .strategies import Hypothesis, norm_cdf
from .tools import SIGNAL_TOOLS, ToolCallLog, ToolContext

TIME_STOP_MULT = 1.5
TP_FRACTION = 0.5
MIN_ENTRY_TIME_S = 900.0
CONVERT_MAX_WAIT_CYCLES = 5
UNCERTAIN_CONF_BAND = 0.10
THIN_EXPERIENCE_N = 3.0
_Q6 = Decimal("0.000001")
_Q2 = Decimal("0.01")

REGIME_TOOLS = {"trend_up": ["trend_signal", "book_pressure_signal"],
                "trend_down": ["trend_signal", "book_pressure_signal"],
                "chop": ["reversion_signal", "book_pressure_signal"],
                "quiet": ["reversion_signal", "book_pressure_signal"]}
ALL_SIGNAL_TOOLS = ["trend_signal", "reversion_signal", "book_pressure_signal"]
TOOL_STRATEGY = {v: k for k, v in SIGNAL_TOOLS.items()}


@dataclass(frozen=True)
class Observation:
    """What the agent sees in one cycle (built by the runner from exchange/ledger reads)."""

    now: float
    books: Mapping[str, OrderBook]
    balances: Mapping[str, Balance]
    state: BotState
    fees: Mapping[str, FeeRates] | None = None
    rules: Mapping[str, MarketRules] | None = None
    position: Any = None                       # portfolio.Position | None
    ledger: Mapping[str, Decimal] | None = None
    equity_tmn: Decimal | None = None
    peak_equity_tmn: Decimal | None = None
    round_trips_done: int = 0
    time_left_s: float | None = None
    total_s: float | None = None
    unresolved_orders: bool = False


@dataclass
class PlanContext:
    cycle: int
    history: PriceHistory
    fx_history: PriceHistory
    fx_spreads: list[float] = field(default_factory=list)
    book_source: Any = None
    timer: Callable[[], float] = time.perf_counter
    convert_wait_streak: int = 0
    open_entry: dict[str, Any] | None = None


def _d(x: float | Decimal, q: Decimal = _Q2) -> Decimal:
    return (x if isinstance(x, Decimal) else Decimal(repr(float(x)))).quantize(q)


def _clean(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 6) if math.isfinite(v) else None
    return v


def _held(obs: Observation, asset: str) -> Decimal:
    b = obs.balances.get(asset)
    v = b.spendable if b is not None else ZERO
    if obs.ledger is not None:
        v = min(v, obs.ledger.get(asset, ZERO))
    return max(ZERO, v)


class Planner:
    def __init__(self, memory: Any, config: Config, rng_seed: int | None = None) -> None:
        self.memory = memory
        self.config = config
        self.seed = config.seed if rng_seed is None else rng_seed

    def rng_for(self, cycle: int) -> random.Random:
        return random.Random(f"{self.seed}:{cycle}")

    def decide(self, obs: Observation, ctx: PlanContext) -> Decision:
        return _Cycle(self, obs, ctx).run()


class _Cycle:
    """One planning cycle (keeps the tool log, alternatives and features together)."""

    def __init__(self, planner: Planner, obs: Observation, ctx: PlanContext) -> None:
        self.p, self.obs, self.ctx = planner, obs, ctx
        self.cfg = planner.config
        self.mem = planner.memory
        self.tctx = ToolContext(obs, self.cfg, ctx.history, ctx.fx_history, self.mem, ctx.book_source,
                                {}, ctx.fx_spreads)
        self.log = ToolCallLog(self.tctx, ctx.timer)
        self.features: dict[str, Any] = {}
        self.alternatives: list[dict[str, Any]] = []
        h = hashlib.sha256(f"{planner.seed}:{ctx.cycle}:{obs.now}".encode()).hexdigest()[:8]
        self.decision_id = f"d{ctx.cycle:06d}-{h}"

    # ------------------------------------------------------------------ output
    def finish(self, action: Action, rationale: str, confidence: float, edge: Decimal | float,
               hypothesis: Mapping[str, Any], intent: TradeIntent | None = None,
               chosen_tool: str | None = None) -> Decision:
        hyp = dict(hypothesis)
        for k in ("expected_move_bps", "horizon_s", "strategy", "invalidation"):
            hyp.setdefault(k, {"expected_move_bps": ZERO, "horizon_s": 0.0, "strategy": "stand_aside",
                               "invalidation": {"note": "n/a"}}[k])
        hyp["expected_move_bps"] = _d(hyp["expected_move_bps"])
        sf = {k: _clean(v) for k, v in self.features.items()}
        sf["state"] = self.obs.state.value
        prices: dict[str, str] = {}
        for sym, b in self.obs.books.items():
            prices.update({f"{sym}.bid": plain(b.best_bid), f"{sym}.ask": plain(b.best_ask),
                           f"{sym}.mid": plain(b.mid)})
        return Decision(
            decision_id=self.decision_id, ts=self.obs.now, cycle=self.ctx.cycle,
            strategy_version=STRATEGY_VERSION, action=action, rationale=rationale,
            confidence=round(max(0.0, min(1.0, float(confidence))), 4),
            estimated_net_edge=_d(edge, _Q6),
            alternatives=self.alternatives, state_features=sf, tool_calls=self.log.calls,
            hypothesis=hyp, observed_prices=prices, intent=intent, chosen_tool=chosen_tool)

    def hold(self, rationale: str, *, strategy: str = "stand_aside", chosen_tool: str | None = None,
             extra: Mapping[str, Any] | None = None, confidence: float | None = None) -> Decision:
        sa = strategies.stand_aside(self.features, self.mem, rationale)
        hyp = sa.to_dict()
        hyp["strategy"] = strategy
        hyp.update(extra or {})
        return self.finish(Action.HOLD, rationale, sa.confidence if confidence is None else confidence,
                           ZERO, hyp, None, chosen_tool)

    # ------------------------------------------------------------------ main flow
    def run(self) -> Decision:
        obs, cfg = self.obs, self.cfg
        rb = self.log.call("read_book", "observe: every cycle starts from fresh books (cheapest tool)")
        self._features()
        if obs.state is BotState.STOPPED:
            return self.finish(Action.STOP, "bot is STOPPED: no trading decisions until an operator resumes",
                               1.0, ZERO, {"strategy": "stopped", "invalidation": {"note": "operator resume"}})
        if not rb.ok:
            return self.hold(f"no fresh market data ({rb.error}): fail closed", chosen_tool="read_book")
        if obs.unresolved_orders or obs.state in (BotState.BUY_PENDING, BotState.SELL_PENDING):
            return self.hold(f"order in flight (state {obs.state.value}): the execution engine resolves it first",
                             strategy="wait_order")
        if obs.time_left_s is not None and obs.time_left_s <= 0:
            return self.hold("competition deadline reached: no new decisions; any flatten is risk.py's "
                             "final-policy job", strategy="deadline")
        if not obs.rules or cfg.btc_symbol not in obs.rules or cfg.usdt_symbol not in obs.rules:
            return self.hold("market rules unknown: fail closed")
        if self._needs_conversion():
            return self._conversion()
        if not obs.fees or cfg.btc_symbol not in obs.fees:
            return self.hold(f"{cfg.btc_symbol} fee unknown: cannot price any trade, fail closed")
        if obs.state is BotState.BTC_HELD or self._btc_qty() > 0 and obs.state is not BotState.READY:
            return self._exit()
        if obs.state is BotState.READY:
            return self._entry()
        return self.hold(f"nothing to do in state {obs.state.value}")

    def _features(self) -> None:
        obs, cfg = self.obs, self.cfg
        info = {"now": obs.now, "time_left_s": obs.time_left_s, "total_s": obs.total_s or cfg.competition_hours * 3600,
                "max_slippage_bps": float(cfg.max_slippage_bps), "fx_history": self.ctx.fx_history.points(),
                "btc_symbol": cfg.btc_symbol, "usdt_symbol": cfg.usdt_symbol}
        self.features = compute_features(self.ctx.history, obs.books, obs.position, info)
        if self.features["data_sufficient"] < 1 and obs.state is not BotState.STOPPED:
            if self.ctx.book_source is not None and obs.state in (BotState.READY, BotState.BTC_HELD):
                n = int(self.features["n_points"])
                r = self.log.call("read_candles", f"warm-up: only {n} 1-min points (< 30); backfill from "
                                                  "public candles instead of waiting", minutes=120)
                if r.ok:
                    info["fx_history"] = self.ctx.fx_history.points()
                    self.features = compute_features(self.ctx.history, obs.books, obs.position, info)
            elif obs.state in (BotState.READY, BotState.BTC_HELD):
                self.log.skip("read_candles", "history short but no candle source is attached")
        self.tctx.features = self.features

    def _btc_qty(self) -> Decimal:
        pos_q = getattr(self.obs.position, "qty", None)
        held = _held(self.obs, "BTC")
        return min(held, pos_q) if pos_q is not None else held

    # ------------------------------------------------------------------ conversion
    def _needs_conversion(self) -> bool:
        obs, cfg = self.obs, self.cfg
        if obs.state in (BotState.UNFUNDED, BotState.CONVERTING):
            return True
        if obs.state is BotState.READY:
            ur = obs.rules[cfg.usdt_symbol]
            br = obs.rules[cfg.btc_symbol]
            return _held(obs, ur.base) < br.min_notional and _held(obs, ur.quote) >= ur.min_notional
        return False

    def _conversion(self) -> Decision:
        obs, cfg = self.obs, self.cfg
        usd = cfg.usdt_symbol
        ur = obs.rules[usd]
        tmn = _held(obs, ur.quote)
        if not obs.fees or usd not in obs.fees:
            return self.hold(f"{usd} fee unknown: cannot price the conversion, fail closed", strategy="convert_wait")
        book = obs.books.get(usd)
        if tmn < ur.min_notional or book is None:
            return self.hold(f"nothing to convert ({plain(tmn)} {ur.quote} < minimum {plain(ur.min_notional)})",
                             strategy="convert_idle")
        fx = self.log.call("fx_check", "TMN held: check USDTTMN spread/depth before converting", tmn_amount=tmn)
        if not fx.ok:
            return self.hold(f"USDTTMN book unusable ({fx.error}): wait", strategy="convert_wait", chosen_tool="fx_check")
        d = fx.data
        spread = d["spread_bps"]
        cost = d.get("cost_bps", spread)
        limit = cfg.conversion_slippage_bps + d["fee_bps"]
        streak = self.ctx.convert_wait_streak
        median = d["median_spread_bps"]
        exp_gain = max(0.0, (float(spread) - (median or 0.0)) / 2)
        wait_hyp = {"direction": "flat", "expected_move_bps": exp_gain, "horizon_s": cfg.loop_interval_s,
                    "invalidation": {"patience_cycles": CONVERT_MAX_WAIT_CYCLES, "waited": streak},
                    "rationale": "spread expected to normalise"}
        depth_conf = float(min(ONE, d["depth_within_cap_tmn"] / tmn)) if tmn > 0 else 0.0
        self.alternatives = [{"strategy": "convert_now", "net_edge_bps": -round(float(cost), 2),
                              "confidence": round(depth_conf, 4), "rejected_because": None},
                             {"strategy": "convert_wait", "net_edge_bps": round(exp_gain, 2),
                              "confidence": None, "rejected_because": None}]

        def wait(reason: str, capped: bool) -> Decision:
            self.alternatives[0]["rejected_because"] = reason
            return self.hold(f"conversion: WAIT — {reason}" + (f" (patience {streak + 1}/{CONVERT_MAX_WAIT_CYCLES})"
                                                                if capped else " (hard limit; no patience cap)"),
                             strategy="convert_wait", chosen_tool="fx_check", extra=wait_hyp, confidence=depth_conf)

        if not d.get("complete", False):
            return wait("USDTTMN depth cannot fill the amount", False)
        if spread > cfg.max_spread_bps:
            return wait(f"spread {spread:.1f} bps > hard max {cfg.max_spread_bps} bps", False)
        if cost > limit:
            return wait(f"walked cost {cost:.1f} bps > cap {limit:.1f} bps (slippage cap + fee)", False)
        if d["abnormal"] and streak < CONVERT_MAX_WAIT_CYCLES:
            return wait(f"spread {spread:.1f} bps abnormally wide vs recent median "
                        f"{median if median is None else round(median, 2)} bps", True)
        self.alternatives[1]["rejected_because"] = ("patience exhausted: costs within limits, convert anyway"
                                                    if d["abnormal"] else "spread normal: nothing to wait for")
        intent = TradeIntent(purpose="convert", side=Side.BUY, symbol=usd, quantity=tmn / book.best_ask,
                             reference_price=book.best_ask, max_slippage_bps=cfg.conversion_slippage_bps,
                             note=f"{self.decision_id} TMN->USDT conversion")
        hyp = {"strategy": "convert", "direction": "flat", "expected_move_bps": ZERO, "horizon_s": 0.0,
               "invalidation": {"note": "required funding stage; not a market view"},
               "rationale": f"walked conversion cost {cost:.1f} bps within cap {limit:.1f} bps",
               "cost_bps": _d(cost)}
        why = ("patience exhausted after abnormal spread; " if d["abnormal"] else "") + \
            f"convert {plain(tmn)} {ur.quote} -> USDT at ~{plain(book.best_ask)} (cost {cost:.1f} bps)"
        return self.finish(Action.CONVERT_TMN_USDT, why, depth_conf, -(cost / BPS), hyp, intent, "fx_check")

    # ------------------------------------------------------------------ entry
    def _entry(self) -> Decision:
        obs, cfg, f = self.obs, self.cfg, self.features
        if f["data_sufficient"] < 1:
            return self.hold(f"warm-up: {int(f['n_points'])} of 30 required 1-min points; no trading before "
                             "enough history", strategy="warmup")
        hr = self.log.call("risk_headroom", "budget check before spending effort on signals")
        if not hr.ok:
            return self.hold(f"cannot assess budget ({hr.error}): fail closed", chosen_tool="risk_headroom")
        h = hr.data
        if h["round_trips_left"] <= 0:
            return self.hold(f"round-trip budget used ({obs.round_trips_done}/{cfg.max_round_trips})",
                             chosen_tool="risk_headroom")
        if obs.time_left_s is not None and obs.time_left_s < MIN_ENTRY_TIME_S:
            return self.hold(f"only {obs.time_left_s:.0f}s left (< {MIN_ENTRY_TIME_S:.0f}s): no new entries",
                             chosen_tool="risk_headroom")
        book = obs.books[cfg.btc_symbol]
        fee = obs.fees[cfg.btc_symbol].taker  # type: ignore[index]
        rules = obs.rules[cfg.btc_symbol]  # type: ignore[index]
        notional = min(h["equity_usdt"] * cfg.max_exposure_fraction, h["exposure_room_usdt"],
                       h["usdt_spendable"] / (ONE + fee + cfg.fee_safety_margin))
        if notional < rules.min_notional:
            return self.hold(f"entry size {plain(notional.quantize(_Q2))} USDT below exchange minimum "
                             f"{plain(rules.min_notional)}", chosen_tool="risk_headroom")
        regime = str(f["regime"])
        hyps: dict[str, Hypothesis] = {}
        first = REGIME_TOOLS.get(regime, ALL_SIGNAL_TOOLS)
        for t in first:
            why = (f"regime {regime}: preferred signal" if regime in REGIME_TOOLS
                   else f"regime {regime}: no preferred signal, gather all evidence")
            r = self.log.call(t, why)
            if r.ok:
                hyps[TOOL_STRATEGY[t]] = r.data["hypothesis"]
        rest = [t for t in ALL_SIGNAL_TOOLS if t not in first]
        if rest:
            reason = self._uncertainty(hyps, regime)
            for t in rest:
                if reason:
                    r = self.log.call(t, f"uncertainty high: {reason}")
                    if r.ok:
                        hyps[TOOL_STRATEGY[t]] = r.data["hypothesis"]
                else:
                    self.log.skip(t, "cheap evidence is consistent and decisive; extra signal not needed")

        spread = book.spread_bps
        floor = 2 * fee * BPS + spread
        ups = [x for x in hyps.values() if x.direction == "up"]
        best_gross = max((x.expected_move_bps for x in ups), default=None)
        cost_bps, complete, walk, priced = floor, True, None, False
        if best_gross is not None and best_gross > float(floor):
            r = self.log.call("estimate_round_trip_cost", f"best long gross move {best_gross:+.1f} bps exceeds the "
                                                          f"fee+spread floor {floor:.1f} bps: walk the book for "
                                                          f"{plain(notional.quantize(_Q2))} USDT",
                              notional_usdt=notional)
            if not r.ok:
                return self.hold(f"round-trip cost unknown ({r.error}): fail closed",
                                 chosen_tool="estimate_round_trip_cost")
            walk = r.data
            cost_bps, complete, priced = walk["cost_bps"], walk["complete"], True
        else:
            self.log.skip("estimate_round_trip_cost",
                          f"no long candidate beats the fee+spread floor {floor:.1f} bps: a book walk cannot "
                          "make the edge positive")
        hurdle = float(cfg.min_net_edge_bps)
        cost_f = float(cost_bps)
        evald: list[tuple[Hypothesis, float | None, str | None]] = []
        for name in sorted(hyps):
            x = hyps[name]
            post = self.mem.posterior(regime, name)
            net: float | None = None
            why: str | None = None
            if x.direction == "flat":
                why = f"no signal: {x.rationale}"
            elif x.direction == "down":
                x = x.with_cost(cost_f, post.mean, post.n)
                why = f"bearish view {x.expected_move_bps:+.1f} bps: spot account cannot short"
            else:
                x = x.with_cost(cost_f, post.mean, post.n)
                net = x.expected_move_bps - cost_f
                if net < hurdle:
                    why = (f"net edge {net:+.1f} bps < hurdle {hurdle:.0f} bps (move {x.expected_move_bps:+.1f} - "
                           f"cost {cost_f:.1f}{'' if priced else ' floor'})")
                elif x.confidence < cfg.min_confidence:
                    why = f"confidence {x.confidence:.2f} < {cfg.min_confidence}"
                elif not complete:
                    why = "visible book cannot absorb the round trip"
                elif obs.time_left_s is not None and obs.time_left_s < x.horizon_s:
                    why = f"horizon {x.horizon_s:.0f}s exceeds time left {obs.time_left_s:.0f}s"
            evald.append((x, net, why))
            self.alternatives.append({"strategy": x.name, "direction": x.direction,
                                      "expected_move_bps": round(x.expected_move_bps, 2),
                                      "net_edge_bps": None if net is None else round(net, 2),
                                      "confidence": round(x.confidence, 4), "rejected_because": why})
        sa = strategies.stand_aside(f, self.mem)
        self.alternatives.append({"strategy": "stand_aside", "direction": "flat", "expected_move_bps": 0.0,
                                  "net_edge_bps": 0.0, "confidence": round(sa.confidence, 4),
                                  "rejected_because": None})
        cf = self._counterfactual(evald, regime, book, fee, notional, walk)
        eligible = [(x, n) for x, n, why in evald if why is None and n is not None]
        decisive = "estimate_round_trip_cost" if priced else (first[0] if first else None)
        if not eligible:
            best = max((e for e in evald if e[1] is not None), key=lambda e: e[1], default=None)
            msg = (f"stand aside: best long candidate {best[0].name} nets {best[1]:+.1f} bps after costs "
                   f"(hurdle {hurdle:.0f}) — {best[2]}" if best else
                   "stand aside: no long hypothesis (" + "; ".join(f"{x.name}: {w}" for x, _, w in evald) + ")")
            return self.hold(msg, chosen_tool=decisive, extra={"counterfactual": cf} if cf else None)
        # ---- Thompson sampling among gate-passing candidates + stand_aside
        rng = self.p.rng_for(self.ctx.cycle)
        draws = {name: self.mem.posterior(regime, name).sample(rng)
                 for name in sorted(x.name for x, _ in eligible) + ["stand_aside"]}
        best_x, best_net = max(eligible, key=lambda e: (draws[e[0].name] * e[1], e[0].name))
        theta, theta_sa = draws[best_x.name], draws["stand_aside"]
        if theta <= theta_sa:
            for a in self.alternatives:
                if a["strategy"] == best_x.name:
                    a["rejected_because"] = f"Thompson draw {theta:.3f} <= stand_aside draw {theta_sa:.3f} in {regime}"
            return self.hold(f"bandit: stand_aside sampled {theta_sa:.3f} >= {best_x.name} {theta:.3f} in {regime} "
                             f"(candidate nets {best_net:+.1f} bps, conf {best_x.confidence:.2f}); abstain",
                             chosen_tool=SIGNAL_TOOLS[best_x.name], extra={"counterfactual": cf} if cf else None)
        for a in self.alternatives:
            if a["strategy"] == "stand_aside":
                a["rejected_because"] = f"Thompson draw {theta_sa:.3f} < {best_x.name} {theta:.3f}"
            elif a["strategy"] != best_x.name and a["rejected_because"] is None:
                a["rejected_because"] = f"lower sampled value than {best_x.name} (draw {draws[a['strategy']]:.3f})"
        qty = walk["qty"] if walk else notional / book.best_ask
        intent = TradeIntent(purpose="entry", side=Side.BUY, symbol=cfg.btc_symbol, quantity=qty,
                             reference_price=book.best_ask, max_slippage_bps=cfg.max_slippage_bps,
                             note=f"{self.decision_id} {best_x.name}")
        hyp = best_x.to_dict()
        hyp.update(entry_mid=plain(book.mid), cost_bps=_d(cost_f), net_edge_bps=_d(best_net),
                   thompson={k: round(v, 4) for k, v in draws.items()})
        return self.finish(Action.BUY_BTC,
                           f"{best_x.name} in {regime}: calibrated move {best_x.expected_move_bps:+.1f} bps over "
                           f"{best_x.horizon_s:.0f}s vs round-trip cost {cost_f:.1f} bps -> net {best_net:+.1f} bps "
                           f">= hurdle {hurdle:.0f}; confidence {best_x.confidence:.2f}; Thompson {theta:.3f} > "
                           f"stand_aside {theta_sa:.3f}. {best_x.rationale}",
                           best_x.confidence, best_net / 10000.0, hyp, intent, SIGNAL_TOOLS[best_x.name])

    def _uncertainty(self, hyps: Mapping[str, Hypothesis], regime: str) -> str | None:
        dirs = {x.direction for x in hyps.values() if x.direction != "flat"}
        if len(dirs) > 1:
            return "signals disagree on direction"
        if regime not in REGIME_TOOLS:
            return f"regime {regime}"
        ups = [x for x in hyps.values() if x.direction == "up"]
        if ups:
            exp = sum(self.mem.posterior(regime, x.name).n for x in ups)
            if exp < THIN_EXPERIENCE_N:
                return f"little scored experience in {regime} ({exp:.1f} obs)"
            if any(abs(x.confidence - self.cfg.min_confidence) < UNCERTAIN_CONF_BAND for x in ups):
                return "confidence near the gate"
        return None

    def _counterfactual(self, evald: list[tuple[Hypothesis, float | None, str | None]], regime: str,
                        book: OrderBook, fee: Decimal, notional: Decimal,
                        walk: Mapping[str, Any] | None) -> dict[str, Any] | None:
        cands = [x for x, _, _ in evald if x.direction != "flat"]
        if not cands:
            return None
        ups = [(x, n) for x, n, _ in evald if x.direction == "up" and n is not None]
        best = max(ups, key=lambda e: e[1])[0] if ups else max(cands, key=lambda x: abs(x.expected_move_bps))
        entry = walk["buy_avg"] if walk else book.best_ask
        qty = walk["qty"] if walk else notional / book.best_ask
        return {"regime": regime, "best": best.name, "horizon_s": best.horizon_s,
                "entry_price": plain(entry), "qty": plain(qty), "mid": plain(book.mid),
                "fee_buy": plain(fee), "fee_sell": plain(fee),
                "candidates": [{"strategy": x.name, "direction": x.direction,
                                "raw_move_bps": round(x.raw_move_bps, 2),
                                "expected_move_bps": round(x.expected_move_bps, 2), "horizon_s": x.horizon_s}
                               for x in cands]}

    # ------------------------------------------------------------------ exit
    def _exit(self) -> Decision:
        obs, cfg = self.obs, self.cfg
        qty = self._btc_qty()
        book = obs.books[cfg.btc_symbol]
        fee = obs.fees[cfg.btc_symbol].taker  # type: ignore[index]
        rules = obs.rules[cfg.btc_symbol]  # type: ignore[index]
        if qty <= 0 or rules.violations(rules.round_price(book.best_bid, Side.SELL), rules.round_qty(qty)):
            return self.hold(f"BTC {plain(qty)} is dust (below exchange minimums): nothing sellable",
                             strategy="hold_position")
        entry = self.ctx.open_entry or {}
        eh = dict(entry.get("hypothesis") or {})
        strat = eh.get("strategy") if eh.get("strategy") in SIGNAL_TOOLS else "trend_follow"
        tool = SIGNAL_TOOLS[strat]
        r = self.log.call(tool, f"re-test the entry thesis ({strat})" if eh else
                          "no recorded entry thesis: use momentum as the default re-test")
        now_h: Hypothesis | None = r.data["hypothesis"] if r.ok else None
        c = self.log.call("estimate_round_trip_cost", "price the exit for the held size (walk bids + fee)",
                          qty=qty, leg="exit")
        if not c.ok:
            return self.hold(f"exit cost unknown ({c.error}); keeping position — risk.py hard stops still apply",
                             strategy="hold_position", chosen_tool="estimate_round_trip_cost")
        exit_cost = float(c.data["cost_bps"])
        pos = obs.position
        basis = getattr(pos, "cost_usdt", ZERO) or (getattr(pos, "avg_cost_usdt", ZERO) or ZERO) * qty
        pos_q = getattr(pos, "qty", None) or qty
        basis = basis * qty / pos_q if pos_q > 0 else basis
        proceeds = c.data["sell_avg"] * qty * (ONE - fee)
        unreal = float((proceeds / basis - ONE) * BPS) if basis > 0 else None
        if now_h is None:
            remaining = 0.0
        else:
            remaining = now_h.expected_move_bps if now_h.direction == "up" else (
                -abs(now_h.expected_move_bps) if now_h.direction == "down" else 0.0)
        sigma = now_h.sigma_h_bps if now_h is not None and now_h.sigma_h_bps > 0 else \
            max(float(self.features.get("vol_1m_bps", 1.0)), 1.0) * math.sqrt(30)
        p_hold = norm_cdf(remaining / sigma)
        horizon = float(eh.get("horizon_s") or strategies.DEFAULT_HORIZON_S)
        entry_ts = getattr(pos, "entry_ts", None) or entry.get("ts")
        age = obs.now - float(entry_ts) if entry_ts else 0.0
        target = float(eh.get("net_edge_bps") or self.cfg.min_net_edge_bps)
        inv = eh.get("invalidation") or {}
        mid = float(book.mid)
        # Thesis renewed: the re-tested hypothesis would pass the entry gate as a FRESH trade (round trip
        # approximated as 2 x exit cost). Exiting on the clock and buying back would only pay fees twice.
        renewed = (now_h is not None and now_h.direction == "up"
                   and now_h.expected_move_bps - 2 * exit_cost >= float(cfg.min_net_edge_bps)
                   and now_h.confidence >= cfg.min_confidence)
        reason: str | None = None
        if "price_below" in inv and mid < float(inv["price_below"]):
            reason = f"thesis invalidated: mid {mid:.2f} < invalidation level {float(inv['price_below']):.2f}"
            kind = "invalidation"
        elif now_h is not None and now_h.direction == "down" and now_h.confidence >= cfg.min_confidence:
            reason = f"thesis invalidated: {now_h.name} now bearish ({now_h.expected_move_bps:+.1f} bps, conf {now_h.confidence:.2f})"
            kind = "invalidation"
        elif age >= horizon * TIME_STOP_MULT and not renewed:
            reason = f"time-stop: held {age:.0f}s >= {TIME_STOP_MULT} x horizon {horizon:.0f}s"
            kind = "time_stop"
        elif unreal is not None and unreal > 0 and remaining < exit_cost and unreal >= TP_FRACTION * target:
            reason = (f"take-profit: net-of-exit P&L {unreal:+.1f} bps >= {TP_FRACTION} x predicted net "
                      f"{target:.1f} bps and remaining calibrated move {remaining:+.1f} < exit cost {exit_cost:.1f} bps")
            kind = "take_profit"
        unreal_s = "unknown" if unreal is None else f"{unreal:+.1f} bps"
        self.alternatives = [
            {"strategy": "exit_now", "net_edge_bps": None if unreal is None else round(unreal, 2),
             "confidence": round(1 - p_hold, 4), "rejected_because": None if reason else
             f"no exit trigger (P&L {unreal_s}, remaining {remaining:+.1f} bps, age {age:.0f}/{horizon:.0f}s)"},
            {"strategy": "hold_position", "net_edge_bps": round(remaining, 2), "confidence": round(p_hold, 4),
             "rejected_because": reason}]
        if reason is None:
            return self.finish(Action.HOLD, f"keep position: net-of-exit P&L {unreal_s}, remaining calibrated move "
                                            f"{remaining:+.1f} bps, held {age:.0f}s of {horizon:.0f}s horizon; "
                                            + ("time-stop waived: re-tested thesis would pass the entry gate "
                                               "afresh" if age >= horizon * TIME_STOP_MULT else
                                               "no invalidation/time-stop/take-profit trigger"),
                               p_hold, remaining / 10000.0,
                               {"strategy": "hold_position", "direction": "up" if remaining > 0 else "flat",
                                "expected_move_bps": remaining, "horizon_s": max(0.0, horizon - age),
                                "invalidation": inv or {"note": "risk.py hard stop-loss"},
                                "entry_decision_id": entry.get("decision_id"), "regime": self.features.get("regime")},
                               None, tool)
        intent = TradeIntent(purpose="exit", side=Side.SELL, symbol=cfg.btc_symbol, quantity=qty,
                             reference_price=book.best_bid, max_slippage_bps=cfg.max_slippage_bps,
                             note=f"{self.decision_id} {kind}")
        return self.finish(Action.SELL_BTC, reason + f" (exit cost {exit_cost:.1f} bps)", 1 - p_hold,
                           (unreal or 0.0) / 10000.0,
                           {"strategy": f"exit_{kind}", "direction": "flat", "expected_move_bps": remaining,
                            "horizon_s": 0.0, "invalidation": {"note": "closing the position"},
                            "entry_decision_id": entry.get("decision_id"), "exit_cost_bps": _d(exit_cost),
                            "regime": self.features.get("regime")},
                           intent, "estimate_round_trip_cost" if kind == "take_profit" else tool)
