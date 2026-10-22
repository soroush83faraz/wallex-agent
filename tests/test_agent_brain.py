"""AgentBrain end to end on a seeded SYNTHETIC 24 h market (decisions-only paper plumbing, no
SimExchange, no network): few trades, complete decisions, HOLD below hurdle, determinism, restart."""
from __future__ import annotations

import json
import time

import pytest

from tests._agent_support import PaperLoop, run_loop
from wallex_agent import STRATEGY_VERSION
from wallex_agent.agent import AgentBrain
from wallex_agent.config import Config
from wallex_agent.market_data import SyntheticMarket
from wallex_agent.store import SqliteStore
from wallex_agent.types import Action, BotState

REQUIRED_HYP = ("expected_move_bps", "horizon_s", "strategy", "invalidation")


@pytest.fixture(scope="module")
def day():
    t0 = time.perf_counter()
    loop, brain = run_loop(seed=7)
    return loop, brain, time.perf_counter() - t0


def test_24h_synthetic_run_is_cautious_and_complete(day):
    loop, brain, elapsed = day
    cfg = Config()
    ds = loop.decisions
    assert len(ds) == 1441
    assert elapsed < 20.0, elapsed
    buys = [d for d in ds if d.action is Action.BUY_BTC]
    sells = [d for d in ds if d.action is Action.SELL_BTC]
    assert len(sells) <= cfg.max_round_trips and len(buys) <= cfg.max_round_trips
    assert loop.trips <= cfg.max_round_trips
    assert ds[0].action is Action.CONVERT_TMN_USDT
    assert sum(d.action is Action.HOLD for d in ds) > 0.9 * len(ds)       # standing aside is the default
    for d in ds:
        assert d.strategy_version == STRATEGY_VERSION and d.rationale
        assert all(k in d.hypothesis for k in REQUIRED_HYP), d.hypothesis
        assert d.tool_calls and d.tool_calls[0]["tool"] == "read_book"
        assert d.observed_prices and "state" in d.state_features
        assert 0.0 <= d.confidence <= 1.0
        json.dumps(d.to_dict())
        if d.action is Action.HOLD:
            assert d.intent is None
    hurdle = float(cfg.min_net_edge_bps)
    for d in buys:
        assert float(d.estimated_net_edge) * 10000 >= hurdle and d.confidence >= cfg.min_confidence
        assert d.intent is not None and d.intent.purpose == "entry"
    # HOLD whenever no candidate clears the hurdle (entry decisions in READY)
    checked = 0
    for d in ds:
        if d.state_features["state"] != "READY" or not d.alternatives:
            continue
        nets = [a["net_edge_bps"] for a in d.alternatives
                if a["strategy"] != "stand_aside" and a.get("net_edge_bps") is not None]
        if all(n < hurdle for n in nets):
            assert d.action is Action.HOLD, d.rationale
            checked += 1
    assert checked > 100
    # candles are fetched adaptively (warm-up backfill), not every cycle
    n_candles = sum(1 for d in ds for c in d.tool_calls if c["tool"] == "read_candles" and c["called"])
    assert 1 <= n_candles <= 3
    snap = brain.memory_snapshot()
    json.dumps(snap)
    assert snap["decisions"] == 1441 and snap["reflections"] > 0
    assert "stand_aside" in json.dumps(snap["posteriors"])


def test_trace_renders_every_stage(day):
    loop, brain, _ = day
    d = next((x for x in loop.decisions if x.action is Action.BUY_BTC), loop.decisions[-1])
    text = brain.render_trace(d)
    for stage in ("OBSERVE", "PLAN", "TOOL", "ACTION", "VERIFY", "REFLECT"):
        assert stage in text


def test_determinism_by_seed(day):
    loop, _, _ = day
    again, _ = run_loop(seed=7)
    assert [d.to_dict() for d in again.decisions] == [d.to_dict() for d in loop.decisions]
    other, _ = run_loop(seed=8, n_steps=60)
    assert [d.observed_prices for d in other.decisions[:5]] != [d.observed_prices for d in loop.decisions[:5]]


SCHED = [("chop", 60), ("trend_up", 240), ("chop", 300)]


def test_rebuilt_brain_decides_identically(tmp_path):
    cfg = Config()
    ref, _ = run_loop(seed=2, n_steps=600, schedule=SCHED)
    m = SyntheticMarket(seed=2, n_steps=600, regime_schedule=SCHED)
    loop = PaperLoop(m, cfg)
    path = tmp_path / "db" / "brain.sqlite"
    store = SqliteStore(path, clock=loop.clock)
    brain = loop.brain(store)
    split = 150
    for _ in range(split):
        loop.step(brain)
    assert loop.state is BotState.BTC_HELD            # restart with an open position (entry thesis in memory)
    store.close()                                     # "crash": drop the brain, reopen the store
    store2 = SqliteStore(path, clock=loop.clock)
    brain2 = loop.brain(store2)
    assert brain2.memory.n_decisions == split
    while not loop.done:
        loop.step(brain2)
    store2.close()
    got = [d.to_dict() for d in loop.decisions]
    want = [d.to_dict() for d in ref.decisions]
    assert got == want
    acts = {d.action for d in ref.decisions[split:]}
    assert len(ref.decisions) == 601 and {Action.SELL_BTC, Action.BUY_BTC} <= acts


def test_order_result_verification_and_aliases(day):
    loop, brain, _ = day
    conv = loop.decisions[0]
    v = brain.on_outcome(conv.decision_id, {"status": "partial", "filled_qty": "0", "avg_price": "1"})
    assert v["status"] == "partial" and "fill_ratio" in v
    assert brain.step_decide == brain.decide
    ri = AgentBrain.risk_inputs(loop.decisions[-1])
    assert set(ri) == {"expected_move_bps", "confidence"}
    assert BotState.READY.value in {d.state_features["state"] for d in loop.decisions}
