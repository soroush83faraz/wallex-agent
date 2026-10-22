"""Crash / unknown-outcome recovery. The SimExchange survives a 'crash'; the engine object is dropped
and a NEW engine is built on the same (file) database, exactly like a process restart."""
from decimal import Decimal as D

import pytest

from wallex_agent.execution import QUARANTINE_MIN_CHECKS, SimulatedCrash
from wallex_agent.types import BotState as S, OrderLifecycle as L, OrderRequest, Side

from ._sim_support import BTC, funded_world, held_world, make_world

STAGES = ["after_intent", "after_sending", "after_post", "after_ack"]


def alerts(w, name):
    return [p for p in w.audit.of("ALERT") if p.get("alert") == name]


def lift_quarantine(w, eng, key=(BTC, Side.BUY)):
    """M4: the quarantine needs QUARANTINE_MIN_CHECKS consecutive absent re-checks, each a full
    resolve window apart: it is still there after every earlier one."""
    for i in range(QUARANTINE_MIN_CHECKS):
        assert key in eng.quarantined(), f"lifted after only {i} re-check(s)"
        w.clock.advance(w.cfg.unknown_resolve_window_s + 1)
        eng.poll_and_manage()
    assert key not in eng.quarantined()


@pytest.fixture
def fw(tmp_path):
    w = funded_world(tmp_path, file_db=True)
    yield w
    w.close()


@pytest.mark.parametrize("stage", STAGES)
def test_crash_during_entry_never_duplicates(fw, stage):
    w = fw
    posts_before = sum(w.sim.post_counts.values())
    w.engine.crash_at = {stage}
    with pytest.raises(SimulatedCrash):
        w.engine.submit(w.entry())
    posted = sum(w.sim.post_counts.values()) - posts_before
    assert posted == (1 if stage in ("after_post", "after_ack") else 0)

    eng = w.restart()
    rep = eng.reconcile_startup()
    assert rep["ok"], rep
    assert w.sim.max_posts_per_client_id() == 1
    assert sum(w.sim.post_counts.values()) - posts_before == posted        # restart sent nothing
    assert w.ledger_matches_sim()
    assert w.store.unresolved_orders() == []
    if stage == "after_intent":
        assert len(w.store.list_orders()) == 1 and w.sm.state is S.READY    # only the conversion order
    elif stage == "after_sending":
        rec = w.store.list_orders()[-1]
        assert rec.lifecycle is L.ABANDONED and w.sm.state is S.READY
        assert (BTC, Side.BUY) in eng.quarantined()
    else:
        assert w.store.list_orders()[-1].lifecycle is L.FILLED and w.sm.state is S.BTC_HELD

    # the runner re-trying the same intent after the restart can never produce a duplicate
    again = eng.submit(w.entry())
    if stage == "after_intent":
        assert again.status == "filled"
    elif stage == "after_sending":
        assert again.status == "blocked" and "quarantined" in again.reason
        lift_quarantine(w, eng)
        assert eng.submit(w.entry()).status == "filled"
        assert (BTC, Side.BUY) not in eng.quarantined()
    else:
        assert again.status == "blocked" and again.reason.startswith("state_mismatch")
    assert w.sim.max_posts_per_client_id() == 1 and w.ledger_matches_sim()


@pytest.mark.parametrize("stage", STAGES)
def test_crash_during_exit_never_duplicates_nor_oversells(tmp_path, stage):
    w = held_world(tmp_path, file_db=True)
    try:
        held = w.portfolio.expected_balances()["BTC"]
        w.engine.crash_at = {stage}
        with pytest.raises(SimulatedCrash):
            w.engine.submit(w.exit())
        eng = w.restart()
        assert eng.reconcile_startup()["ok"]
        assert w.sim.max_posts_per_client_id() == 1 and w.ledger_matches_sim()
        sold = sum((o.executed_qty for o in w.sim.all_orders() if o.side is Side.SELL), D(0))
        assert sold <= held
        expected = S.READY if stage in ("after_post", "after_ack") else S.BTC_HELD
        assert w.sm.state is expected
    finally:
        w.close()


def test_crash_after_post_with_resting_order_is_adopted_then_managed(fw):
    w = fw
    w.engine.crash_at = {"after_post"}
    with pytest.raises(SimulatedCrash):
        w.engine.submit(w.entry(style="passive_limit"))
    eng = w.restart()
    assert eng.reconcile_startup()["ok"]
    rec = w.store.list_orders()[-1]
    assert rec.lifecycle is L.OPEN and w.sm.state is S.BUY_PENDING
    w.clock.advance(w.cfg.order_timeout_s + 1)
    eng.poll_and_manage()
    assert w.store.get_order(rec.client_id).lifecycle is L.CANCELED and w.sm.state is S.READY
    assert w.sim.max_posts_per_client_id() == 1


def test_crash_during_unknown_outcome_handling(fw):
    w = fw
    w.sim.inject("post_timeout_after_accept")
    w.engine.crash_at = {"after_post"}       # dies right after the failed POST, before persisting it
    with pytest.raises(SimulatedCrash):
        w.engine.submit(w.entry())
    assert w.store.list_orders()[-1].lifecycle is L.SENDING
    eng = w.restart()
    assert eng.reconcile_startup()["ok"]
    assert w.store.list_orders()[-1].lifecycle is L.FILLED and w.sm.state is S.BTC_HELD
    assert w.sim.max_posts_per_client_id() == 1 and w.ledger_matches_sim()


# --------------------------------------------------------------------------- unknown outcomes
@pytest.mark.parametrize("kind,kw,expect", [
    ("post_timeout_after_accept", {}, "filled"),
    ("post_5xx", {"accepted": True}, "filled"),
    ("undecodable", {"accepted": True}, "filled"),
    ("post_timeout_before_accept", {}, "abandoned"),
    ("post_5xx", {"accepted": False}, "abandoned"),
    ("undecodable", {"accepted": False}, "abandoned"),
    ("rate_limit", {}, "abandoned"),
])
def test_unknown_outcome_is_resolved_never_retried(tmp_path, kind, kw, expect):
    w = funded_world(tmp_path)
    posts = w.engine.posts
    w.sim.inject(kind, **kw)
    r = w.engine.submit(w.entry())
    assert r.status == expect and len(r.order_ids) == 1
    assert w.engine.posts - posts == 1 and w.sim.max_posts_per_client_id() == 1
    assert alerts(w, "unknown_outcome")
    resolved = w.audit.of("unknown_resolved")[-1]
    assert resolved["outcome"] == ("adopted" if expect == "filled" else "abandoned")
    assert w.ledger_matches_sim()
    if expect == "abandoned":
        assert w.sm.state is S.READY and (BTC, Side.BUY) in w.engine.quarantined()
        assert w.store.get_order(r.order_ids[0]).lifecycle is L.ABANDONED
        # quarantine: no new order on BTCUSDT BUY until repeated, spaced re-checks agree
        assert w.engine.submit(w.entry()).status == "blocked"
        lift_quarantine(w, w.engine)
        assert w.engine.quarantined() == frozenset() and "quarantine_lifted" in w.audit.kinds()
        assert w.engine.watched_abandoned()[0].client_id == r.order_ids[0]      # still watched forever
        assert w.engine.submit(w.entry()).status == "filled"
    else:
        assert w.sm.state is S.BTC_HELD


def test_unknown_outcome_with_resting_order_is_adopted_open(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("post_timeout_after_accept")
    r = w.engine.submit(w.entry(style="passive_limit"))
    assert r.status == "open" and w.sm.state is S.BUY_PENDING


def test_unknown_outcome_contradiction_stops_the_bot(tmp_path):
    # exchange returns its own id and cannot be queried by ours; the POST "timed out" after accept
    w = funded_world(tmp_path, sim_kw={"echo_client_id": False, "lookup_by_client_id": False})
    w.sim.inject("post_timeout_after_accept")
    r = w.engine.submit(w.entry())
    assert r.status == "stopped" and w.sm.state is S.STOPPED
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.UNKNOWN_OUTCOME
    a = alerts(w, "needs_manual_reconcile")
    assert a and "balances/trades moved" in a[-1]["reason"]
    assert w.engine.status()["stop"]["reason"].startswith("needs_manual_reconcile")
    assert w.engine.submit(w.entry()).status == "blocked"
    assert w.sim.max_posts_per_client_id() == 1


def test_unreachable_exchange_leaves_order_unknown_and_blocking(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("post_timeout_before_accept")
    real_create = w.sim.create_order

    def create_then_go_dark(req):
        try:
            return real_create(req)
        finally:   # every read needed as evidence fails from now on
            w.sim.inject("get_order_error", times=1000)
            w.sim.inject("balances_error", times=1000)

    w.sim.create_order = create_then_go_dark
    r = w.engine.submit(w.entry())
    assert r.status == "unknown" and alerts(w, "unknown_unresolved")
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.UNKNOWN_OUTCOME
    assert w.engine.submit(w.entry()).reason == "unresolved_orders"
    w.sim.clear_faults()
    w.engine.poll_and_manage()
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.ABANDONED and w.sm.state is S.READY
    assert w.sim.max_posts_per_client_id() == 1


def test_transient_404_during_resolution_is_tolerated(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("post_timeout_after_accept")
    w.sim.inject("get_order_404", times=1)
    r = w.engine.submit(w.entry(style="passive_limit"))
    assert r.status == "open"           # found on the open-orders list despite the 404


def test_abandoned_order_appearing_later_stops_the_bot(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("post_timeout_before_accept")
    r = w.engine.submit(w.entry())
    rec = w.store.get_order(r.order_ids[0])
    assert rec.lifecycle is L.ABANDONED
    # eventual consistency nightmare: the exchange shows the order after all
    w.sim.create_order(OrderRequest(rec.client_id, rec.symbol, rec.side, rec.price, rec.quantity))
    w.clock.advance(w.cfg.unknown_resolve_window_s)
    w.engine.poll_and_manage()
    assert w.sm.state is S.STOPPED and alerts(w, "late_accept")
    assert w.engine.status()["stop"]["reason"].startswith(f"late_accept: ABANDONED order {rec.client_id}")


def test_unexplained_trades_after_abandon_stop_the_bot(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("post_timeout_before_accept")
    w.engine.submit(w.entry())
    w.sim.create_order(OrderRequest("someone-else", BTC, Side.BUY, D("81010"), D("0.0001")))
    w.clock.advance(w.cfg.unknown_resolve_window_s)
    w.engine.poll_and_manage()
    assert w.sm.state is S.STOPPED
    reason = alerts(w, "needs_manual_reconcile")[-1]["reason"]
    assert "unexplained BUY trades on BTCUSDT" in reason and "0.0001 traded vs 0 recorded" in reason


def test_alias_ids_are_stored_and_used(tmp_path):
    w = funded_world(tmp_path, sim_kw={"echo_client_id": False, "lookup_by_client_id": False})
    r = w.engine.submit(w.entry(style="passive_limit"))
    rec = w.store.get_order(r.order_ids[0])
    assert rec.exchange_client_order_id.startswith("LIMIT-")
    assert w.store.find_order_by_exchange_id(rec.exchange_client_order_id).client_id == rec.client_id
    w.clock.advance(w.cfg.order_timeout_s + 1)
    w.engine.poll_and_manage()                 # refresh + cancel by alias (client id lookup would 404)
    assert w.store.get_order(rec.client_id).lifecycle is L.CANCELED and w.sm.state is S.READY


def test_crash_after_post_with_unqueryable_alias_fails_closed(tmp_path):
    w = funded_world(tmp_path, file_db=True,
                     sim_kw={"echo_client_id": False, "lookup_by_client_id": False})
    try:
        w.engine.crash_at = {"after_post"}
        with pytest.raises(SimulatedCrash):
            w.engine.submit(w.entry(style="passive_limit"))
        eng = w.restart()
        rep = eng.reconcile_startup()
        assert not rep["ok"] and w.sm.state is S.STOPPED
        assert w.sim.max_posts_per_client_id() == 1
    finally:
        w.close()


def test_acknowledged_order_missing_repeatedly_stops(tmp_path):
    w = funded_world(tmp_path)
    r = w.engine.submit(w.entry(style="passive_limit"))
    w.sim.inject("get_order_404", times=1)
    w.engine.poll_and_manage()
    assert w.sm.state is S.BUY_PENDING                     # resting order seen on openOrders
    oid = r.order_ids[0]
    w.sim.cancel_order(oid)                                # vanishes from openOrders too
    w.sim.inject("get_order_404", times=10)
    for _ in range(3):
        w.engine.poll_and_manage()
    assert w.sm.state is S.STOPPED and alerts(w, "needs_manual_reconcile")


# --------------------------------------------------------------------------- startup reconciliation
def test_reconcile_startup_clean_and_with_excess_funds(tmp_path):
    w = held_world(tmp_path)
    rep = w.restart().reconcile_startup()
    assert rep["ok"] and rep["derived"]["state"] == "BTC_HELD" and w.sm.state is S.BTC_HELD
    w.sim._total["USDT"] += D("500")                      # deposit outside the bot's budget
    rep = w.engine.reconcile_startup()
    assert rep["ok"] and rep["drift"]["excess"]


def test_reconcile_startup_detects_drift(tmp_path):
    w = held_world(tmp_path)
    w.sim._total["BTC"] -= D("0.0001")                    # funds vanished: never explainable
    rep = w.restart().reconcile_startup()
    assert not rep["ok"] and w.sm.state is S.STOPPED
    reason = alerts(w, "needs_manual_reconcile")[-1]["reason"]
    assert reason.startswith("ledger/exchange shortfall at startup:") and "short 0.0001" in reason
    assert "BTC ledger " in w.engine.status()["stop"]["reason"]          # numbers in the stop reason


def test_reconcile_startup_detects_unknown_open_order(tmp_path):
    w = funded_world(tmp_path)
    w.sim.create_order(OrderRequest("manual-1", BTC, Side.BUY, D("70000"), D("0.0001")))
    rep = w.restart().reconcile_startup()
    assert not rep["ok"] and w.sm.state is S.STOPPED


def test_crash_mid_conversion_is_resumed(tmp_path):
    w = make_world(tmp_path, file_db=True)
    try:
        w.engine.crash_at = {"after_post"}
        with pytest.raises(SimulatedCrash):
            w.engine.convert_tmn_to_usdt()
        assert w.sm.state is S.CONVERTING and w.store.list_orders()[-1].lifecycle is L.SENDING
        eng = w.restart()
        rep = eng.reconcile_startup()
        assert rep["ok"] and w.sm.state is S.CONVERTING
        assert any("convert_tmn_to_usdt" in a for a in rep["actions"])
        res = eng.convert_tmn_to_usdt()
        assert w.sm.state is S.READY and res.rounds == 0          # nothing re-sent
        assert w.sim.order_count() == 1 and w.sim.max_posts_per_client_id() == 1
        assert w.ledger_matches_sim()
    finally:
        w.close()


def test_when_stopped_unknown_orders_are_only_ever_adopted(tmp_path):
    w = funded_world(tmp_path)
    posts = sum(w.sim.post_counts.values())
    w.engine.crash_at = {"after_sending"}          # SENDING persisted, never POSTed
    with pytest.raises(SimulatedCrash):
        w.engine.submit(w.entry())
    eng = w.restart()
    w.sm.transition(S.STOPPED, "operator stop")
    eng.poll_and_manage()
    assert w.store.list_orders()[-1].lifecycle is L.UNKNOWN_OUTCOME   # no single-shot abandonment
    assert sum(w.sim.post_counts.values()) == posts
