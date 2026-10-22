"""agent.planner: gates, adaptive tool use, bandit/learning effects, conversion patience, exits."""
from __future__ import annotations

import json
from decimal import Decimal as D

from tests._agent_support import BTC, FEES, RULES, T0, USD, obs, plan, prices
from wallex_agent import STRATEGY_VERSION
from wallex_agent.agent import AgentBrain, AgentMemory
from wallex_agent.agent.planner import CONVERT_MAX_WAIT_CYCLES
from wallex_agent.config import Config
from wallex_agent.portfolio import Position
from wallex_agent.risk import RiskContext, RiskGate
from wallex_agent.store import InMemoryStore
from wallex_agent.types import Action, BotState, FeeRates, Side

REQUIRED_HYP = ("expected_move_bps", "horizon_s", "strategy", "invalidation")
UP = prices(90, start=79000.0, bps_per_min=5.0, noise_bps=2.0, seed=3)
FLAT = prices(90, start=81000.0, bps_per_min=0.0, noise_bps=2.0, seed=4)


def mem_with(*updates):
    m = AgentMemory(InMemoryStore())
    m.apply_updates(updates)
    return m


def bandit(regime, strategy, s, f):
    return {"kind": "bandit", "regime": regime, "strategy": strategy, "success": s, "failure": f}


def tools_called(d):
    return [c["tool"] for c in d.tool_calls if c["called"]]


def check_shape(d):
    assert d.strategy_version == STRATEGY_VERSION
    assert all(k in d.hypothesis for k in REQUIRED_HYP)
    assert isinstance(d.estimated_net_edge, D) and 0.0 <= d.confidence <= 1.0
    assert d.rationale and d.tool_calls and d.observed_prices and d.state_features
    json.dumps(d.to_dict())


def test_warmup_holds_and_says_so():
    d = plan(obs(T0, FLAT[-1]), FLAT[:10])
    check_shape(d)
    assert d.action is Action.HOLD and "warm-up" in d.rationale and d.intent is None


def test_flat_market_holds_and_skips_cost_walk():
    d = plan(obs(T0, FLAT[-1]), FLAT)
    check_shape(d)
    assert d.action is Action.HOLD and d.estimated_net_edge == 0
    assert tools_called(d)[:2] == ["read_book", "risk_headroom"]
    skipped = [c for c in d.tool_calls if not c["called"]]
    assert any(c["tool"] == "estimate_round_trip_cost" and "floor" in c["why"] for c in skipped)
    names = [a["strategy"] for a in d.alternatives]
    assert "stand_aside" in names
    assert all(a["rejected_because"] for a in d.alternatives if a["strategy"] != "stand_aside")


def test_strong_trend_buys_with_priced_edge_and_risk_agrees():
    m = mem_with(bandit("trend_up", "stand_aside", 0, 20))     # abstaining has been wrong in trends
    o = obs(T0, UP[-1])
    d = plan(o, UP, memory=m)
    check_shape(d)
    assert d.state_features["regime"] == "trend_up"
    assert d.action is Action.BUY_BTC, d.rationale
    assert d.chosen_tool == "trend_signal" and d.hypothesis["strategy"] == "trend_follow"
    called = tools_called(d)
    assert called[0] == "read_book" and "estimate_round_trip_cost" in called
    assert d.estimated_net_edge * 10000 >= Config().min_net_edge_bps
    assert d.confidence >= Config().min_confidence
    assert "price_below" in d.hypothesis["invalidation"]
    i = d.intent
    assert (i.purpose, i.side, i.symbol) == ("entry", Side.BUY, BTC)
    assert i.quantity * i.reference_price <= D("18") * Config().max_exposure_fraction + D("0.01")
    ri = AgentBrain.risk_inputs(d)
    ctx = RiskContext(state=BotState.READY, balances=o.balances, fees=FEES, rules=RULES, books=o.books, now=T0,
                      equity_tmn=o.equity_tmn, peak_equity_tmn=o.peak_equity_tmn, **ri)
    v = RiskGate(Config()).evaluate(i, ctx)
    assert v.approved, v.reasons


def test_learned_abstention_overrides_same_signal():
    m = mem_with(bandit("trend_up", "stand_aside", 40, 0), bandit("trend_up", "trend_follow", 0, 40))
    d = plan(obs(T0, UP[-1]), UP, memory=m)
    assert d.action is Action.HOLD
    tf = next(a for a in d.alternatives if a["strategy"] == "trend_follow")
    assert "confidence" in tf["rejected_because"] or "Thompson" in tf["rejected_because"]


def test_calibration_shrinks_move_below_hurdle():
    upd = [{"kind": "calibration", "strategy": "trend_follow", "regime": "trend_up", "predicted_bps": 150.0,
            "realized_bps": 0.0, "weight": 1.0}] * 10
    m = mem_with(bandit("trend_up", "stand_aside", 0, 20), *upd)
    d = plan(obs(T0, UP[-1]), UP, memory=m)
    assert d.action is Action.HOLD
    tf = next(a for a in d.alternatives if a["strategy"] == "trend_follow")
    assert m.calibration_factor("trend_follow") < 0.1
    assert tf["rejected_because"]


def test_costs_above_edge_hold():
    fees = {s: FeeRates(s, D("0.02"), D("0.02")) for s in (BTC, USD)}
    d = plan(obs(T0, UP[-1], fees=fees), UP, memory=mem_with(bandit("trend_up", "stand_aside", 0, 20)))
    assert d.action is Action.HOLD and "hurdle" in d.rationale


def test_fail_closed_paths():
    assert "fee unknown" in plan(obs(T0, UP[-1], fees=None), UP).rationale
    assert "rules unknown" in plan(obs(T0, UP[-1], rules=None), UP).rationale
    stale = plan(obs(T0, UP[-1], book_ts=T0 - 100), UP)
    assert stale.action is Action.HOLD and "fail closed" in stale.rationale
    assert plan(obs(T0, UP[-1], state=BotState.STOPPED), UP).action is Action.STOP
    assert "in flight" in plan(obs(T0, UP[-1], state=BotState.BUY_PENDING), UP).rationale
    assert "deadline" in plan(obs(T0, UP[-1], time_left_s=0.0), UP).rationale
    assert "budget" in plan(obs(T0, UP[-1], round_trips_done=Config().max_round_trips), UP).rationale
    assert "no new entries" in plan(obs(T0, UP[-1], time_left_s=600.0), UP).rationale
    for d in (plan(obs(T0, UP[-1], fees=None), UP), plan(obs(T0, UP[-1], state=BotState.STOPPED), UP)):
        check_shape(d)


def test_adaptive_tools_by_regime():
    flat = plan(obs(T0, FLAT[-1]), FLAT)
    assert flat.state_features["regime"] in ("chop", "quiet")
    assert any(c["tool"] == "trend_signal" and not c["called"] for c in flat.tool_calls)
    wild = prices(90, noise_bps=40.0, seed=9)
    v = plan(obs(T0, wild[-1]), wild)
    assert v.state_features["regime"] == "volatile"
    assert {"trend_signal", "reversion_signal", "book_pressure_signal"} <= set(tools_called(v))


def test_conversion_normal_spread_converts():
    o = obs(T0, 81000.0, state=BotState.UNFUNDED, usdt="0", tmn="5000000")
    d = plan(o, [], fx_spreads=[1.6] * 50)
    check_shape(d)
    assert d.action is Action.CONVERT_TMN_USDT and d.chosen_tool == "fx_check"
    i = d.intent
    assert (i.purpose, i.side, i.symbol) == ("convert", Side.BUY, USD)
    assert i.max_slippage_bps == Config().conversion_slippage_bps
    assert d.estimated_net_edge < 0          # a conversion is a cost, honestly reported


def test_conversion_waits_on_abnormal_spread_with_bounded_patience():
    o = obs(T0, 81000.0, state=BotState.UNFUNDED, usdt="0", tmn="5000000", fx_half_spread=12.0)
    w = plan(o, [], fx_spreads=[1.6] * 50, convert_wait_streak=0)
    assert w.action is Action.HOLD and w.hypothesis["strategy"] == "convert_wait"
    assert f"patience 1/{CONVERT_MAX_WAIT_CYCLES}" in w.rationale
    c = plan(o, [], fx_spreads=[1.6] * 50, convert_wait_streak=CONVERT_MAX_WAIT_CYCLES)
    assert c.action is Action.CONVERT_TMN_USDT and "patience exhausted" in c.rationale
    hard = obs(T0, 81000.0, state=BotState.UNFUNDED, usdt="0", tmn="5000000", fx_half_spread=25.0)
    h = plan(hard, [], fx_spreads=[1.6] * 50, convert_wait_streak=99)
    assert h.action is Action.HOLD and "hard" in h.rationale


def test_conversion_streak_counts_from_memory():
    m = AgentMemory(InMemoryStore())
    brain_cfg = Config()
    brain = AgentBrain(brain_cfg, m, timer=lambda: 0.0)
    brain.fx_spreads = [1.6] * 50
    actions = []
    for k in range(CONVERT_MAX_WAIT_CYCLES + 1):
        o = obs(T0 + 30 * k, 81000.0, state=BotState.UNFUNDED, usdt="0", tmn="5000000", fx_half_spread=12.0)
        d = brain.decide(o)
        brain.record_decision(d)
        actions.append(d.action)
    assert actions[:CONVERT_MAX_WAIT_CYCLES] == [Action.HOLD] * CONVERT_MAX_WAIT_CYCLES
    assert actions[-1] is Action.CONVERT_TMN_USDT


def _held(entry_ts, cost, *, price_below=70000.0, net_edge="40"):
    qty = D("0.00005")
    pos = Position(qty, cost / qty, entry_ts, D(0), 0, 0, 0, cost_usdt=cost)
    entry = {"decision_id": "d000001-x", "ts": entry_ts,
             "hypothesis": {"strategy": "trend_follow", "horizon_s": 3600.0, "net_edge_bps": net_edge,
                            "invalidation": {"price_below": price_below}}}
    return pos, entry


def test_exit_on_invalidation():
    pos, entry = _held(T0 - 600, D("4.05"), price_below=FLAT[-1] * 1.01)
    d = plan(obs(T0, FLAT[-1], state=BotState.BTC_HELD, usdt="14", btc="0.00005", position=pos), FLAT,
             open_entry=entry)
    check_shape(d)
    assert d.action is Action.SELL_BTC and d.hypothesis["strategy"] == "exit_invalidation"
    assert d.intent.purpose == "exit" and d.intent.side is Side.SELL and d.intent.quantity == D("0.00005")


def test_exit_on_time_stop():
    pos, entry = _held(T0 - 6000, D("4.05"))
    d = plan(obs(T0, FLAT[-1], state=BotState.BTC_HELD, usdt="14", btc="0.00005", position=pos), FLAT,
             open_entry=entry)
    assert d.action is Action.SELL_BTC and d.hypothesis["strategy"] == "exit_time_stop"


def test_take_profit_when_remaining_edge_below_exit_cost():
    cost = D("0.00005") * D(str(FLAT[-1])) * D("0.985")      # bought ~150 bps cheaper
    pos, entry = _held(T0 - 600, cost)
    d = plan(obs(T0, FLAT[-1], state=BotState.BTC_HELD, usdt="14", btc="0.00005", position=pos), FLAT,
             open_entry=entry)
    assert d.action is Action.SELL_BTC and d.hypothesis["strategy"] == "exit_take_profit", d.rationale
    assert d.estimated_net_edge > 0


def test_keep_position_without_trigger():
    cost = D("0.00005") * D(str(FLAT[-1]))
    pos, entry = _held(T0 - 600, cost)
    d = plan(obs(T0, FLAT[-1], state=BotState.BTC_HELD, usdt="14", btc="0.00005", position=pos), FLAT,
             open_entry=entry)
    check_shape(d)
    assert d.action is Action.HOLD and d.hypothesis["strategy"] == "hold_position"
    assert {a["strategy"] for a in d.alternatives} == {"exit_now", "hold_position"}


def test_same_inputs_same_decision():
    m1 = mem_with(bandit("trend_up", "stand_aside", 0, 3))
    m2 = mem_with(bandit("trend_up", "stand_aside", 0, 3))
    a = plan(obs(T0, UP[-1]), UP, memory=m1, cycle=7).to_dict()
    b = plan(obs(T0, UP[-1]), UP, memory=m2, cycle=7).to_dict()
    assert a == b
