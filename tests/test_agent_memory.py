"""agent.memory: event-sourced rebuild, calibration formula, posteriors, abstention cadence."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from wallex_agent.agent.memory import (CAL_PRIOR_OBS, CAL_PRIOR_SCALE_BPS, CF_SPACING_S, AgentMemory, BetaPosterior,
                                       Calibration)
from wallex_agent.agent.reflection import Reflection
from wallex_agent.clock import SimClock
from wallex_agent.store import InMemoryStore, SqliteStore
from wallex_agent.types import Action, Decision


def dec_(i: int, ts: float, action: Action = Action.HOLD, **hyp) -> Decision:
    h = {"strategy": "stand_aside", "expected_move_bps": D(0), "horizon_s": 1800.0, "invalidation": {}}
    h.update(hyp)
    return Decision(decision_id=f"d{i}", ts=ts, cycle=i, strategy_version="t", action=action, rationale="r",
                    confidence=0.5, estimated_net_edge=D(0), hypothesis=h,
                    observed_prices={"BTCUSDT.mid": "81000"})


def test_beta_posterior():
    p = BetaPosterior()
    assert p.mean == 0.5 and p.n == 0
    p.alpha += 3
    assert p.mean == pytest.approx(0.8) and p.n == 3


def test_calibration_formula():
    c = Calibration()
    assert c.slope() == 1.0                       # prior: trust the raw model until evidence arrives
    for _ in range(50):
        c.spr += 100 * 50
        c.spp += 100 * 100
        c.n += 1
    w0 = CAL_PRIOR_OBS * CAL_PRIOR_SCALE_BPS ** 2
    assert c.slope() == pytest.approx((50 * 5000 + w0) / (50 * 10000 + w0))
    assert 0.5 < c.slope() < 0.52                 # realised = half of predicted -> k -> 0.5
    bad = Calibration(spr=-1e6, spp=1e6, n=10)
    assert bad.slope() == 0.0                     # wrong sign is clamped, never inverted
    great = Calibration(spr=4e6, spp=1e6, n=10)
    assert great.slope() == 1.0                   # never amplified


def test_rebuild_from_sqlite_is_identical(tmp_path):
    clock = SimClock(1000.0)
    path = tmp_path / "db" / "s.sqlite"
    s = SqliteStore(path, clock=clock)
    m = AgentMemory(s)
    m.record_decision(dec_(1, 1000.0, Action.BUY_BTC, strategy="trend_follow"))
    m.record_outcome("d1", {"status": "filled", "filled_qty": "0.0001"})
    m.record_decision(dec_(2, 1100.0, counterfactual={"best": "trend_follow", "horizon_s": 600}))
    m.record_reflection(Reflection("d9", "round_trip", 1200.0, {}, {}, 1.0, "lesson one",
                                   [{"kind": "bandit", "regime": "chop", "strategy": "trend_follow",
                                     "success": 1, "failure": 0},
                                    {"kind": "calibration", "strategy": "trend_follow", "regime": "chop",
                                     "predicted_bps": 80, "realized_bps": 20, "weight": 1}]))
    snap = m.snapshot()
    assert m.open_entry()["decision_id"] == "d1"
    assert [d["decision_id"] for d in m.pending_abstentions()] == ["d2"]
    s.close()
    s2 = SqliteStore(path, clock=clock)
    m2 = AgentMemory(s2)
    assert m2.snapshot() == snap
    assert m2.posterior("chop", "trend_follow").alpha == 2.0
    assert m2.calibration_factor("trend_follow") == m.calibration_factor("trend_follow") < 1.0
    assert m2.lessons(1) == ["lesson one"]
    m2.record_outcome("d1", {"exit_reason": "x"}, kind="round_trip")
    assert m2.open_entry() is None
    s2.close()


def test_abstention_cadence_and_convert_streak():
    m = AgentMemory(InMemoryStore())
    cf = {"best": "x", "horizon_s": 600}
    m.record_decision(dec_(1, 0.0, counterfactual=cf))
    m.record_decision(dec_(2, CF_SPACING_S - 1, counterfactual=cf))   # too soon: not scored
    m.record_decision(dec_(3, CF_SPACING_S, counterfactual=cf))
    assert [d["decision_id"] for d in m.pending_abstentions()] == ["d1", "d3"]
    m.record_decision(dec_(4, 2000.0, strategy="convert_wait"))
    m.record_decision(dec_(5, 2030.0, strategy="convert_wait"))
    assert m.convert_wait_streak == 2
    m.record_decision(dec_(6, 2060.0, strategy="convert"))
    assert m.convert_wait_streak == 0


def test_abstention_reflection_clears_pending_and_updates_posteriors():
    m = AgentMemory(InMemoryStore())
    m.record_decision(dec_(1, 0.0, counterfactual={"best": "x", "horizon_s": 600}))
    m.record_reflection({"decision_id": "d1", "kind": "abstention", "ts": 700.0, "lesson": "abstained ok",
                         "updates": [{"kind": "bandit", "regime": "chop", "strategy": "stand_aside",
                                      "success": 1.0, "failure": 0.0}]})
    assert m.pending_abstentions() == []
    assert m.posteriors()["chop"]["stand_aside"]["alpha"] == 2.0
    assert m.snapshot()["actions"] == {"HOLD": 1}
