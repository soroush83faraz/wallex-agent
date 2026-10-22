"""End-to-end scenarios through the real runner, engine, risk gate and store (SimExchange + SimClock,
no network): demo, crash/restart, unknown outcomes, kill switch, deadline policies, faults, determinism."""
from __future__ import annotations

import json
import random
from decimal import Decimal as D

import pytest

from tests._runner_support import (BTC, USD, ledger_matches, sim_world, track, trader_script)
from wallex_agent.config import Config
from wallex_agent.execution import SimulatedCrash
from wallex_agent.modes import build_runtime
from wallex_agent.reporting import read_audit, verify_chain
from wallex_agent.runner import EXIT_KILLED, AgentRunner
from wallex_agent.types import Action, BotState, OrderLifecycle, Side, TradeIntent

REQUIRED_REPORT_FIELDS = ("report_version", "run_id", "mode", "labels", "started_at", "ended_at", "duration_h",
                          "stop_reason", "config", "value", "pnl", "fees_paid", "orders", "fills", "position",
                          "equity_curve", "round_trips", "decisions_by_action", "tool_usage", "memory", "end",
                          "honest_caveats", "run")


def invariants(rt, sim):
    assert sim.max_posts_per_client_id() <= 1, "a client_id was POSTed twice"
    assert ledger_matches(rt, sim), (rt.portfolio.expected_balances(), sim.totals())
    ours = {o.client_id for o in rt.store.list_orders()}
    assert {o.client_order_id for o in sim.all_orders()} <= ours, "an exchange order unknown to the store"


# --------------------------------------------------------------------------- demo
def test_demo_end_to_end_audit_and_final_report(tmp_path):
    rt = track(build_runtime(Config(data_dir=str(tmp_path)), "backtest", synthetic=True, seed=11, hours=4))
    res = AgentRunner(rt).run()
    assert res.status == "deadline" and res.exit_code == 0
    assert verify_chain(rt.audit.path) == (True, None)
    doc = json.loads(res.report_path.read_text(encoding="utf-8"))
    for k in REQUIRED_REPORT_FIELDS:
        assert k in doc, k
    assert {"synthetic_data", "assumed_fee", "backtest_simulated"} <= set(doc["labels"])
    assert doc["value"]["initial_tmn"] == "5000000" and doc["value"]["final_tmn_net_of_exit_costs"] is not None
    assert doc["pnl"]["target_reached"] in (True, False) and "2x" in doc["pnl"]["target_statement"]
    assert doc["duration_h"] == 4.0 and doc["end"]["final_policy"] == "report_only"
    assert doc["decisions_by_action"].get("HOLD", 0) > 0 and "UNKNOWN" not in doc["decisions_by_action"]
    assert doc["orders"]["count"] >= 1 and doc["fills"]["count"] >= 1          # at least the conversion
    assert (rt.run_dir / "trace.txt").read_text(encoding="utf-8").startswith("Audit trace")
    assert rt.sm.state is BotState.STOPPED
    invariants(rt, rt.exchange)
    # F1 with the REAL planner: the conversion decision carries its alternatives, features, tool
    # choice, risk verdict, linked order(s), fills and the context it was taken in
    conv = [d for d in doc["round_trips"]["decisions"] if d["action"] == "CONVERT_TMN_USDT"]
    assert conv and conv[0]["order_ids"] and conv[0]["fills"]
    c = conv[0]
    assert c["strategy"] == "convert" and c["chosen_tool"] == "fx_check" and len(c["alternatives"]) == 2
    assert c["state_features"]["state"] in ("UNFUNDED", "CONVERTING") and c["risk_verdict"]["approved"]
    assert c["balances"]["ledger"]["TMN"] == "5000000" and c["equity"] and c["drawdown"]["pct"] is not None
    assert c["fee_assets"] == ["USDT"] and c["strategy_version"] and c["timestamp"]
    assert {o["decision_id"] for o in doc["orders"]["list"]} <= {d["decision_id"] for d in
                                                               doc["round_trips"]["decisions"]}
    assert doc["clock"] == "simulated" and "SIMULATED market time" in " ".join(doc["honest_caveats"])
    assert doc["hold_decisions"]["sample"][0]["decision_id"] and doc["hold_decisions"]["by_strategy"]


def test_same_seed_same_decisions(tmp_path):
    def decisions(sub):
        rt = track(build_runtime(Config(data_dir=str(tmp_path / sub)), "backtest", synthetic=True, seed=5,
                                 hours=2))
        AgentRunner(rt).run()
        return [r["payload"] for r in read_audit(rt.audit.path) if r["kind"] in ("decision", "risk_verdict")]
    a, b = decisions("a"), decisions("b")
    assert a and a == b
    c = [r["payload"] for r in read_audit(track(build_runtime(Config(data_dir=str(tmp_path / "c")), "backtest",
                                                              synthetic=True, seed=6, hours=2)).audit.path)]
    assert c == [] or c != a


# --------------------------------------------------------------------------- crash / restart
def test_conversion_resumes_after_a_crash_mid_converting_without_rebuying(tmp_path):
    w = sim_world(tmp_path)
    rt = w.runtime(trader_script())
    r = AgentRunner(rt)
    assert r.start() is None
    rt.engine.crash_at = {"after_post"}          # the POST was accepted; the process died before recording it
    with pytest.raises(SimulatedCrash):
        r.run_once()
    assert rt.sm.state is BotState.CONVERTING
    assert [o.lifecycle for o in rt.store.list_orders()] == [OrderLifecycle.SENDING]
    rt.close()                                   # process death
    w.advance()
    rt2 = w.runtime(trader_script(buy_every=10**6))      # convert only
    res = AgentRunner(rt2).run(max_cycles=3)
    assert res.status == "max_cycles"
    assert len([o for o in w.sim.all_orders() if o.symbol == USD]) == 1      # no re-buy
    assert rt2.sm.state is BotState.READY
    assert rt2.store.list_orders()[0].lifecycle is OrderLifecycle.FILLED
    invariants(rt2, w.sim)


def _crash_sweep(tmp_path, seed: int) -> int:
    """Run 18 cycles; a crash point (engine stage) stays armed until an order hits it, then the
    'process' dies and a fresh runtime is rebuilt on the same store + exchange. Returns #crashes."""
    rng = random.Random(seed)
    w = sim_world(tmp_path, min_trade_interval_s=30.0, order_timeout_s=90.0)
    script = trader_script(buy_every=3, hold_cycles=2, entry_style=rng.choice(["aggressive_limit", "passive_limit"]))
    plan = sorted((rng.randint(1, 10), rng.choice(["after_intent", "after_sending", "after_post", "after_ack"]))
                  for _ in range(3))
    crashed = 0
    rt = w.runtime(script)
    r = AgentRunner(rt)
    assert r.start() is None
    for cycle in range(1, 19):
        if plan and cycle >= plan[0][0] and not rt.engine.crash_at:
            rt.engine.crash_at = {plan.pop(0)[1]}
        try:
            status = r.run_once()
        except SimulatedCrash:
            crashed += 1
            rt.close()
            w.advance()
            rt = w.runtime(script)
            r = AgentRunner(rt)
            status = r.start()
        if status is not None:
            break
        w.advance()
        if cycle % 4 == 0:                       # some market movement
            w.source.shift(BTC, D(rng.choice(["-3", "4", "-6", "2"])), 2)
    rt.engine.crash_at = set()
    rt.engine.poll_and_manage()
    invariants(rt, w.sim)
    return crashed


@pytest.mark.parametrize("seed", range(8))
def test_crash_restart_sweep_keeps_one_post_per_client_id_and_ledger_equals_exchange(tmp_path, seed):
    _crash_sweep(tmp_path, seed)


def test_crash_sweep_really_crashes(tmp_path):
    assert sum(_crash_sweep(tmp_path / f"s{s}", 100 + s) for s in range(4)) >= 4


def test_unknown_post_outcomes_are_resolved_never_duplicated(tmp_path):
    w = sim_world(tmp_path, min_trade_interval_s=30.0)
    w.sim.inject("post_timeout_after_accept")          # the conversion
    w.sim.inject("post_5xx", times=1)                  # the next order (accepted, answer lost)
    rt = w.runtime(trader_script(buy_every=3, hold_cycles=2))
    res = AgentRunner(rt).run(max_cycles=10)
    assert res.status == "max_cycles"
    invariants(rt, w.sim)
    assert w.sim.order_count() >= 2
    alerts = [x["payload"] for x in read_audit(rt.audit.path) if x["kind"] == "alert"]
    assert any(a.get("alert") == "unknown_outcome" for a in alerts)
    assert all(o.lifecycle is not OrderLifecycle.UNKNOWN_OUTCOME for o in rt.store.list_orders())


# --------------------------------------------------------------------------- kill switch
def test_kill_switch_mid_run_cancels_and_stops_never_sells(tmp_path):
    w = sim_world(tmp_path, min_trade_interval_s=30.0, order_timeout_s=10_000)
    state = {"exit_sent": False}

    def script(brain, obs):
        if obs.state in (BotState.UNFUNDED, BotState.CONVERTING):
            return Action.CONVERT_TMN_USDT, TradeIntent("convert", Side.BUY, USD, D("18"), obs.books[USD].best_ask,
                                                        D("30"))
        if obs.state is BotState.READY and brain._cycle == 2:
            return Action.BUY_BTC, TradeIntent("entry", Side.BUY, BTC, D("0.00005"), obs.books[BTC].best_ask, D("20"))
        if obs.state is BotState.BTC_HELD and not state["exit_sent"]:
            state["exit_sent"] = True             # a resting (passive) sell
            return Action.SELL_BTC, TradeIntent("exit", Side.SELL, BTC, obs.ledger["BTC"], obs.books[BTC].best_ask,
                                                D("20"), style="passive_limit")
        return Action.HOLD, None

    rt = w.runtime(script)
    r = AgentRunner(rt)
    assert r.start() is None
    for _ in range(4):
        assert r.run_once() is None
        w.advance()
    assert rt.sm.state is BotState.SELL_PENDING
    btc_before = rt.portfolio.expected_balances()["BTC"]
    orders_before = w.sim.order_count()
    (rt.config.kill_file).write_text("operator", encoding="utf-8")
    res = r.run()
    assert res.status == "killed" and res.exit_code == EXIT_KILLED
    assert rt.sm.state is BotState.STOPPED
    assert w.sim.order_count() == orders_before                     # nothing new sent
    assert all(not o.active for o in w.sim.all_orders())            # resting sell cancelled
    assert rt.portfolio.expected_balances()["BTC"] == btc_before    # nothing sold
    invariants(rt, w.sim)


# --------------------------------------------------------------------------- deadline
def _held_until_deadline(tmp_path, policy):
    w = sim_world(tmp_path, step_s=3600.0, min_trade_interval_s=30.0, final_policy=policy,
                  max_exposure_fraction=D("0.9"))

    def script(brain, obs):
        if obs.state in (BotState.UNFUNDED, BotState.CONVERTING):
            return Action.CONVERT_TMN_USDT, TradeIntent("convert", Side.BUY, USD, D("18"), obs.books[USD].best_ask,
                                                        D("30"))
        if obs.state is BotState.READY and brain._cycle == 2:
            return Action.BUY_BTC, TradeIntent("entry", Side.BUY, BTC, D("0.0001"), obs.books[BTC].best_ask, D("20"))
        return Action.HOLD, None
    rt = w.runtime(script)
    res = AgentRunner(rt).run()
    return w, rt, res


def test_deadline_report_only_leaves_holdings_untouched(tmp_path):
    w, rt, res = _held_until_deadline(tmp_path, "report_only")
    assert res.status == "deadline" and rt.sm.state is BotState.STOPPED
    doc = json.loads(res.report_path.read_text(encoding="utf-8"))
    assert doc["duration_h"] == 24.0 and doc["end"]["final_policy"] == "report_only"
    assert not [o for o in rt.store.list_orders() if o.side is Side.SELL]
    assert rt.portfolio.expected_balances()["BTC"] > 0
    invariants(rt, w.sim)


def test_deadline_flatten_to_usdt_sells_btc_only(tmp_path):
    w, rt, res = _held_until_deadline(tmp_path, "flatten_to_usdt")
    assert res.status == "deadline"
    sells = [o for o in rt.store.list_orders() if o.side is Side.SELL]
    assert sells and {o.symbol for o in sells} == {BTC} and {o.purpose for o in sells} == {"flatten"}
    held = rt.portfolio.expected_balances()
    assert held["BTC"] * D("81000") < 1 and held["USDT"] > 1
    invariants(rt, w.sim)


def test_deadline_flatten_to_tmn_sells_btc_then_usdt(tmp_path):
    w, rt, res = _held_until_deadline(tmp_path, "flatten_to_tmn")
    assert res.status == "deadline"
    sells = [(o.symbol, o.purpose) for o in rt.store.list_orders() if o.side is Side.SELL]
    assert (BTC, "flatten") in sells and (USD, "flatten") in sells
    assert rt.portfolio.expected_balances()["USDT"] < D("0.2")
    invariants(rt, w.sim)


# --------------------------------------------------------------------------- faults
def test_partial_fill_stale_quote_and_network_down_keep_invariants(tmp_path):
    w = sim_world(tmp_path, min_trade_interval_s=30.0, order_timeout_s=60.0)
    rt = w.runtime(trader_script(buy_every=3, hold_cycles=2))
    r = AgentRunner(rt)
    assert r.start() is None
    for cycle in range(1, 21):
        if cycle == 3:
            w.sim.inject("partial_fill", frac="0.4")
        if cycle == 6:
            w.sim.inject("stale_book", times=4, age_s=900)
        if cycle == 9:
            w.sim.inject("network_down", times=6)
        if cycle == 12:
            w.sim.inject("cancel_race_filled")
        assert r.run_once() in (None,)
        w.advance()
        if cycle % 5 == 0:
            w.source.shift(BTC, D("3"), 2)
    rt.engine.poll_and_manage()
    invariants(rt, w.sim)
    assert rt.sm.state is not BotState.STOPPED
    assert any(o.lifecycle is OrderLifecycle.CANCELED and o.executed_qty > 0 for o in rt.store.list_orders()) \
        or any(o.executed_qty < o.quantity for o in rt.store.list_orders())
