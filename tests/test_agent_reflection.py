"""agent.reflection: round-trip and abstention scoring, lessons, updates, trace rendering."""
from __future__ import annotations

from tests._agent_support import T0, btc_book
from wallex_agent.agent import AgentBrain, AgentMemory
from wallex_agent.agent.reflection import OFF_POLICY_WEIGHT, reflect, render_trace, trace_record
from wallex_agent.config import Config
from wallex_agent.store import InMemoryStore
from wallex_agent.types import Action, Decision
from decimal import Decimal as D

ENTRY = {"decision_id": "d1", "ts": T0, "hypothesis": {"strategy": "trend_follow", "regime": "trend_up",
                                                       "expected_move_bps": "100", "raw_move_bps": 125.0,
                                                       "horizon_s": 3600.0, "invalidation": {}}}


def test_round_trip_win_and_loss():
    win = reflect(ENTRY, {"kind": "round_trip", "ts": T0 + 3600, "realized_move_bps": 150.0,
                          "net_return_bps": 70.0, "exit_reason": "exit_take_profit"})
    assert win.error_bps == 50.0 and "paid" in win.lesson and "take_profit" in win.lesson
    b, c = win.updates
    assert (b["strategy"], b["regime"], b["success"], b["failure"]) == ("trend_follow", "trend_up", 1.0, 0.0)
    assert c["predicted_bps"] == 125.0 and c["realized_bps"] == 150.0      # calibration uses the RAW move
    loss = reflect(ENTRY, {"kind": "round_trip", "ts": T0 + 3600, "realized_move_bps": -40.0,
                           "net_return_bps": -110.0})
    assert loss.updates[0]["failure"] == 1.0 and "did not pay" in loss.lesson and "shrinks" in loss.lesson


def _hold(cf_cands):
    return {"decision_id": "d2", "ts": T0, "hypothesis": {
        "strategy": "stand_aside", "expected_move_bps": "0", "horizon_s": 1800.0, "invalidation": {},
        "counterfactual": {"regime": "chop", "best": "trend_follow", "horizon_s": 1800.0, "candidates": cf_cands}}}


CANDS = [{"strategy": "trend_follow", "direction": "up", "raw_move_bps": 60.0, "expected_move_bps": 60.0},
         {"strategy": "mean_reversion", "direction": "down", "raw_move_bps": -20.0, "expected_move_bps": -20.0}]


def test_abstention_right_and_wrong():
    right = reflect(_hold(CANDS), {"kind": "abstention", "ts": T0 + 1800, "realized_move_bps": 10.0,
                                   "best_cf_net_bps": -65.0,
                                   "candidates": [{"strategy": "trend_follow", "cf_net_bps": -65.0}]})
    sa = right.updates[0]
    assert sa["strategy"] == "stand_aside" and sa["success"] == 1.0 and "right" in right.lesson
    tf_b = [u for u in right.updates if u["kind"] == "bandit" and u["strategy"] == "trend_follow"][0]
    assert tf_b["failure"] == OFF_POLICY_WEIGHT                            # off-policy evidence, half weight
    cal = [u for u in right.updates if u["kind"] == "calibration"]
    assert {u["strategy"] for u in cal} == {"trend_follow", "mean_reversion"}
    wrong = reflect(_hold(CANDS), {"kind": "abstention", "ts": T0 + 1800, "realized_move_bps": 200.0,
                                   "best_cf_net_bps": 120.0,
                                   "candidates": [{"strategy": "trend_follow", "cf_net_bps": 120.0}]})
    assert wrong.updates[0]["failure"] == 1.0 and "wrong" in wrong.lesson


def test_brain_scores_abstention_only_after_horizon():
    mem = AgentMemory(InMemoryStore())
    brain = AgentBrain(Config(), mem, timer=lambda: 0.0)
    d = Decision("d2", T0, 1, "t", Action.HOLD, "r", 0.9, D(0),
                 hypothesis={"strategy": "stand_aside", "expected_move_bps": D(0), "horizon_s": 1800.0,
                             "invalidation": {}, "regime": "chop",
                             "counterfactual": {"regime": "chop", "best": "trend_follow", "horizon_s": 1800.0,
                                                "entry_price": "81010", "qty": "0.00005", "mid": "81000",
                                                "fee_buy": "0.0035", "fee_sell": "0.0035", "candidates": CANDS}},
                 observed_prices={"BTCUSDT.mid": "81000"})
    brain.record_decision(d)
    assert brain.reflect_pending(T0 + 1000, {"BTCUSDT": btc_book(90000.0, T0 + 1000)}) == []
    out = brain.reflect_pending(T0 + 1800, {"BTCUSDT": btc_book(81100.0, T0 + 1800)})
    assert len(out) == 1 and out[0].kind == "abstention"
    assert out[0].result["best_cf_net_bps"] < 0 and "right" in out[0].lesson   # +12 bps never beats ~76 bps costs
    assert mem.posterior("chop", "stand_aside").alpha == 2.0
    assert brain.reflect_pending(T0 + 4000, {"BTCUSDT": btc_book(81100.0, T0 + 4000)}) == []   # scored once


def test_trace_has_all_stages():
    d = dict(ENTRY, cycle=1, action="BUY_BTC", confidence=0.7, estimated_net_edge="0.004", rationale="why",
             observed_prices={"BTCUSDT.mid": "81000"}, state_features={"regime": "trend_up"},
             tool_calls=[{"tool": "read_book", "called": True, "ok": True, "why": "observe", "latency_ms": 0.1,
                          "summary": "s"}, {"tool": "read_candles", "called": False, "why": "enough"}],
             alternatives=[{"strategy": "stand_aside", "net_edge_bps": 0, "confidence": 0.4,
                            "rejected_because": "lower"}],
             intent={"purpose": "entry", "side": "BUY", "quantity": "0.0001", "symbol": "BTCUSDT",
                     "reference_price": "81010", "max_slippage_bps": "25"})
    rec = trace_record(d, [{"kind": "order", "result": {"status": "filled"}}],
                       [{"kind": "round_trip", "error_bps": 3.0, "lesson": "ok"}])
    text = render_trace(rec)
    for stage in ("OBSERVE", "PLAN", "TOOL", "ACTION", "VERIFY", "REFLECT"):
        assert stage in text
    assert "skipped read_candles" in text and "intent entry BUY" in text
