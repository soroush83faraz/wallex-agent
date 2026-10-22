"""AgentRunner: cycle stages, audit coverage, failure counting, competition clock, signals, stages."""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal as D

import pytest

from tests._runner_support import (BTC, USD, ScriptedBrain, entry_intent, hold_script, ledger_matches,
                                   sim_world, trader_script)
from wallex_agent.agent import AgentMemory
from wallex_agent.reporting import read_audit, verify_chain
from wallex_agent.runner import (EXIT_INTERRUPTED, EXIT_STOPPED, K_START, MAX_CONSECUTIVE_FAILURES,
                                 AgentRunner)
from wallex_agent.types import Action, BotState, OrderLifecycle, Side


def kinds(rt):
    return Counter(r["kind"] for r in read_audit(rt.audit.path))


def once(r):
    status = r.run_once()
    r.rt.advance()                 # run() advances the simulated market between cycles
    return status


def run(rt, **kw):
    r = AgentRunner(rt, **{k: v for k, v in kw.items() if k in ("stage", "interval_s", "max_failures")})
    res = r.run(**{k: v for k, v in kw.items() if k in ("max_cycles", "seconds")})
    return r, res


# --------------------------------------------------------------------------- stages & audit
def test_cycle_covers_every_stage_and_audit_chain_verifies(tmp_path):
    w = sim_world(tmp_path)
    rt = w.runtime(trader_script(buy_every=3, hold_cycles=2))
    _, res = run(rt, max_cycles=8)
    assert res.status == "max_cycles" and res.exit_code == 0
    k = kinds(rt)
    for kind in ("observe", "decision", "risk_verdict", "tool_call", "reconcile", "outcome", "state_transition",
                 "fill", "verify", "reflection", "order_sending", "order_update"):
        assert k[kind] > 0, kind
    assert verify_chain(rt.audit.path) == (True, None)
    assert k["observe"] == 8 and k["decision"] == 8
    # one cycle_end outcome per cycle, written in a finally block
    ends = [r for r in read_audit(rt.audit.path) if r["kind"] == "outcome" and r["payload"].get("stage") == "cycle_end"]
    assert [e["payload"]["cycle"] for e in ends] == list(range(1, 9))
    assert ledger_matches(rt, w.sim)
    assert res.report_path.name == "partial_report.json"


def test_every_decision_is_recorded_in_memory(tmp_path):
    w = sim_world(tmp_path)
    rt = w.runtime(trader_script())
    _, res = run(rt, max_cycles=6)
    mem = AgentMemory(rt.store)
    assert mem.n_decisions == 6
    recorded = [mem.decision(i) for i in mem.order]
    assert all(d["risk_verdict"] is None or "approved" in d["risk_verdict"] for d in recorded)
    convert = next(d for d in recorded if d["action"] == "CONVERT_TMN_USDT")
    assert convert["risk_verdict"]["approved"] is True and convert["risk_verdict"]["intent_source"] == "agent"


def test_round_trip_is_reported_to_the_brain(tmp_path):
    w = sim_world(tmp_path, min_trade_interval_s=60.0)
    rt = w.runtime(trader_script(buy_every=3, hold_cycles=2))
    _, res = run(rt, max_cycles=9)
    mem = AgentMemory(rt.store)
    rts = [o for outs in mem.outcomes.values() for o in outs if o.get("kind") == "round_trip"]
    assert rts, "a closed round trip must reach the brain"
    assert rt.portfolio.btc_position().round_trips >= 1
    assert any(r["kind"] == "reflection" for r in read_audit(rt.audit.path))


def test_start_reconciles_first_and_competition_start_survives_restart(tmp_path):
    w = sim_world(tmp_path)
    rt = w.runtime(hold_script)
    r = AgentRunner(rt)
    assert r.start() is None
    first = rt.store.kv_get(K_START)
    recs = read_audit(rt.audit.path)
    assert recs[0]["kind"] in ("reconcile_startup", "reconcile")
    rt.close()                                     # process exit
    w.clock.advance(5 * 3600)
    rt2 = w.runtime(hold_script)
    r2 = AgentRunner(rt2)
    assert r2.start() is None
    assert rt2.store.kv_get(K_START) == first and r2.deadline_ts == first + 24 * 3600


# --------------------------------------------------------------------------- failures
def test_cycle_exceptions_are_audited_counted_and_stop_after_n(tmp_path, monkeypatch):
    w = sim_world(tmp_path, min_trade_interval_s=30.0, order_timeout_s=10_000)
    rt = w.runtime(trader_script(buy_every=2, entry_style="passive_limit"))
    r0 = AgentRunner(rt)
    assert r0.start() is None
    once(r0)
    once(r0)                                       # converted + a resting passive buy
    assert rt.sm.state is BotState.BUY_PENDING
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("simulated poll failure")

    r = AgentRunner(rt)
    assert r.start() is None
    monkeypatch.setattr(rt.engine, "poll_and_manage", boom)
    statuses = [once(r) for _ in range(MAX_CONSECUTIVE_FAILURES)]
    assert statuses[:-1] == [None] * (MAX_CONSECUTIVE_FAILURES - 1) and statuses[-1] == "stopped"
    assert rt.sm.state is BotState.STOPPED
    recs = read_audit(rt.audit.path)
    alerts = [x for x in recs if x["kind"] == "alert" and x["payload"].get("alert") == "cycle_exception"]
    assert len(alerts) == MAX_CONSECUTIVE_FAILURES and "simulated poll failure" in alerts[-1]["payload"]["detail"]
    assert any(x["kind"] == "alert" and x["payload"].get("alert") == "safety_stop" for x in recs)
    # cancel only: no open order left, nothing sold
    assert not [o for o in rt.store.list_orders() if o.lifecycle in (OrderLifecycle.OPEN, OrderLifecycle.PARTIAL)]
    assert not [o for o in rt.store.list_orders() if o.side is Side.SELL]
    res = r.finish("stopped")
    assert res.exit_code == EXIT_STOPPED and res.report_path.name == "final_report.json"


def test_failure_counter_resets_after_a_good_cycle(tmp_path, monkeypatch):
    w = sim_world(tmp_path)
    rt = w.runtime(hold_script)
    r = AgentRunner(rt)
    assert r.start() is None
    real = rt.engine.poll_and_manage
    flaky = iter([True, True, False, True, True, False, True])

    def maybe():
        if next(flaky):
            raise RuntimeError("flaky")
        return real()

    monkeypatch.setattr(rt.engine, "poll_and_manage", maybe)
    assert [once(r) for _ in range(7)] == [None] * 7
    assert rt.sm.state is not BotState.STOPPED


# --------------------------------------------------------------------------- unknown facts
def test_unknown_fee_means_no_trade_but_the_loop_continues(tmp_path):
    w = sim_world(tmp_path)
    w.sim.hidden_fees = {USD}
    rt = w.runtime(trader_script())
    _, res = run(rt, max_cycles=4)
    assert res.status == "max_cycles"
    assert w.sim.order_count() == 0
    verdicts = [x["payload"] for x in read_audit(rt.audit.path) if x["kind"] == "risk_verdict"]
    assert verdicts and all(not v["approved"] or v["blocked"] for v in verdicts)
    assert any("fee_unknown" in " ".join(v["reasons"]) for v in verdicts)
    assert any(x["kind"] == "alert" and x["payload"].get("alert") == "market_facts_unknown"
               for x in read_audit(rt.audit.path))


def test_stale_book_blocks_orders(tmp_path):
    w = sim_world(tmp_path)
    w.sim.inject("stale_book", times=50, age_s=600)
    rt = w.runtime(trader_script())
    _, res = run(rt, max_cycles=2)
    assert w.sim.order_count() == 0
    obs = [x["payload"] for x in read_audit(rt.audit.path) if x["kind"] == "observe"]
    assert any(any(u.startswith("book_stale") for u in o["unknown"]) for o in obs)


# --------------------------------------------------------------------------- signals / stages
def test_stop_request_cancels_resting_orders_and_leaves_a_resumable_state(tmp_path):
    w = sim_world(tmp_path, min_trade_interval_s=30.0)
    rt = w.runtime(trader_script(buy_every=2, entry_style="passive_limit"))
    r = AgentRunner(rt)
    assert r.start() is None
    once(r)                                   # convert
    once(r)                                   # passive buy rests
    assert rt.sm.state is BotState.BUY_PENDING
    r.request_stop("SIGTERM")
    res = r.run()
    assert res.status == "interrupted" and res.exit_code == EXIT_INTERRUPTED
    assert rt.sm.state is BotState.READY           # resumable: not STOPPED
    assert all(not o.active for o in w.sim.all_orders())
    assert res.report_path.name == "partial_report.json"
    doc = json.loads(res.report_path.read_text(encoding="utf-8"))
    assert any("PARTIAL" in c for c in doc["honest_caveats"])


def test_conversion_orders_carry_the_decision_id_and_the_stop_check(tmp_path):
    """F1: the conversion order is linked to the CONVERT decision; L1: the runner hands the stage a
    stop check covering signals and the deadline."""
    w = sim_world(tmp_path)
    rt = w.runtime(trader_script())
    r = AgentRunner(rt)
    assert r.start() is None
    once(r)
    conv = [o for o in rt.store.list_orders() if o.symbol == USD]
    decided = [d.decision_id for d in rt.brain.decided if d.action is Action.CONVERT_TMN_USDT]
    assert conv and decided and {o.decision_id for o in conv} == {decided[0]}
    assert r._conversion_stop_reason() is None
    r.request_stop("SIGTERM")
    assert r._conversion_stop_reason() == "shutdown requested (SIGTERM)"
    r2 = AgentRunner(rt)
    r2.deadline_ts = w.clock.now() - 1
    assert r2._conversion_stop_reason() == "competition deadline reached"


def test_stage_convert_runs_only_the_conversion_and_stops_at_ready(tmp_path):
    w = sim_world(tmp_path)
    rt = w.runtime(trader_script(buy_every=1))     # would buy immediately if allowed
    _, res = run(rt, stage="convert", max_cycles=10)
    assert res.status == "stage_done" and res.exit_code == 0
    assert rt.sm.state is BotState.READY
    assert {o.symbol for o in w.sim.all_orders()} == {USD}


def test_stage_convert_blocks_non_conversion_intents(tmp_path):
    w = sim_world(tmp_path)
    w.sim.hidden_fees = set()

    def script(brain, obs):
        return Action.BUY_BTC, entry_intent(obs)
    rt = w.runtime(script)
    _, res = run(rt, stage="convert", max_cycles=2)
    v = [x["payload"] for x in read_audit(rt.audit.path) if x["kind"] == "risk_verdict"]
    assert all(p["blocked"] for p in v)
    assert w.sim.order_count() == 0


def _crash_after_post(w, rt, stage="after_post"):
    from wallex_agent.execution import SimulatedCrash
    from wallex_agent.types import TradeIntent
    rt.engine.convert_tmn_to_usdt()
    rt.engine.crash_at = {stage}
    book = w.source.get_book(BTC)
    with pytest.raises(SimulatedCrash):
        rt.engine.submit(TradeIntent("entry", Side.BUY, BTC, D("0.00005"), book.best_ask, D("20"),
                                     style="passive_limit"))
    rt.engine.crash_at = set()
    rec = rt.store.list_orders()[-1]
    assert rec.lifecycle is OrderLifecycle.SENDING          # POSTed, outcome never recorded
    return rec.client_id


def test_shutdown_resolves_an_unknown_order_and_cancels_it(tmp_path):
    """I7: finish() adopts SENDING/UNKNOWN_OUTCOME orders with one lookup, then cancels (cancel only)."""
    w = sim_world(tmp_path, order_timeout_s=10_000)
    rt = w.runtime(hold_script)
    r = AgentRunner(rt)
    assert r.start() is None
    cid = _crash_after_post(w, rt)
    res = r.finish("interrupted")
    assert rt.store.get_order(cid).lifecycle is OrderLifecycle.CANCELED
    assert not any(o.active for o in w.sim.all_orders())
    assert not [o for o in w.sim.all_orders() if o.side is Side.SELL]
    assert res.status == "interrupted" and rt.sm.state is BotState.READY


def test_safety_stop_also_resolves_unknown_orders(tmp_path):
    w = sim_world(tmp_path, order_timeout_s=10_000)
    rt = w.runtime(hold_script)
    r = AgentRunner(rt)
    assert r.start() is None
    cid = _crash_after_post(w, rt)
    r._safety_stop("test")
    assert rt.store.get_order(cid).lifecycle is OrderLifecycle.CANCELED and rt.sm.state is BotState.STOPPED
    assert not any(o.active for o in w.sim.all_orders())


def test_shutdown_leaves_an_unfindable_order_unknown_for_the_next_start(tmp_path):
    w = sim_world(tmp_path)
    rt = w.runtime(hold_script)
    r = AgentRunner(rt)
    assert r.start() is None
    cid = _crash_after_post(w, rt, stage="after_sending")    # persisted SENDING, never reached the exchange
    posts = sum(w.sim.post_counts.values())
    out = rt.engine.settle_for_shutdown("test")
    assert sum(w.sim.post_counts.values()) == posts            # one lookup, never a re-send
    assert out["still_unknown"] == [cid] and rt.store.get_order(cid).lifecycle is OrderLifecycle.UNKNOWN_OUTCOME
    assert any(x["kind"] == "alert" and x["payload"].get("alert") == "unknown_at_shutdown"
               for x in read_audit(rt.audit.path))


# --------------------------------------------------------------------------- forced exits / late fills
def test_hard_stop_loss_forces_an_exit_through_risk_and_engine(tmp_path):
    w = sim_world(tmp_path, min_trade_interval_s=60.0)
    rt = w.runtime(trader_script(buy_every=3, hold_cycles=1000))
    r = AgentRunner(rt)
    assert r.start() is None
    for _ in range(3):
        once(r)
    assert rt.sm.state is BotState.BTC_HELD
    w.source.shift(BTC, D("-500"), 2)               # -5 %: beyond the 1.5 % stop
    once(r)
    recs = read_audit(rt.audit.path)
    assert any(x["kind"] == "forced_exit" and "hard_stop_loss" in x["payload"]["reason"] for x in recs)
    sells = [o for o in rt.store.list_orders() if o.side is Side.SELL]
    assert sells and sells[0].decision_id.endswith(":forced")
    assert rt.sm.state is BotState.READY


def test_late_fill_of_a_resting_order_reaches_the_brain(tmp_path):
    w = sim_world(tmp_path, order_timeout_s=10_000, min_trade_interval_s=30.0)
    rt = w.runtime(trader_script(buy_every=2, hold_cycles=1000, entry_style="passive_limit"))
    r = AgentRunner(rt)
    assert r.start() is None
    once(r)
    once(r)
    assert rt.sm.state is BotState.BUY_PENDING
    w.source.shift(BTC, D("-10"), 2)                # asks fall through our resting bid
    once(r)
    late = [x["payload"] for x in read_audit(rt.audit.path)
            if x["kind"] == "outcome" and x["payload"].get("stage") == "late_fill"]
    assert late and late[0]["result"]["late_fill"] is True
    mem = AgentMemory(rt.store)
    assert mem.open_entry() is not None


def test_scripted_brain_is_a_real_brain(tmp_path):
    w = sim_world(tmp_path)
    rt = w.runtime(hold_script)
    assert isinstance(rt.brain, ScriptedBrain)


def test_slow_book_fetches_do_not_make_books_look_future_stamped(tmp_path):
    """Regression (found in a real paper run behind a slow proxy): each fetch takes seconds, books are
    stamped at receipt, and the cycle's `now` was read before the fetches => 'stamped in the future'."""
    w = sim_world(tmp_path)
    real_get = w.source.get_book

    def slow_get(symbol):
        book = real_get(symbol)
        w.clock.advance(3.0)            # network latency AFTER the receipt stamp
        return book

    w.source.get_book = slow_get
    rt = w.runtime(trader_script())
    run(rt, max_cycles=2)
    obs = [x["payload"] for x in read_audit(rt.audit.path) if x["kind"] == "observe"]
    assert obs and not any(u.startswith("book_stale") for o in obs for u in o["unknown"])
