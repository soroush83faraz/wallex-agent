"""M4: an ABANDONED order (unknown POST outcome, not found anywhere) that the exchange accepts LATE.

The order stays on a permanent watch list checked on every poll, before every new order and at
startup; its quarantine is never lifted on absence alone; an in-run balance shortfall stops the bot.
SimExchange + SimClock only: nothing touches the network.
"""
from decimal import Decimal as D

from wallex_agent.errors import TransportError
from wallex_agent.execution import QUARANTINE_MIN_CHECKS, SHORTFALL_CONFIRM_S
from wallex_agent.types import BotState as S, OrderLifecycle as L, OrderRequest, Side, plain

from ._sim_support import BTC, funded_world


def alerts(w, name):
    return [p for p in w.audit.of("ALERT") if p.get("alert") == name]


def queued_gateway_timeout(w):
    """First BTC POST: the gateway times out while the exchange QUEUES the order (not visible yet)."""
    real_create = w.sim.create_order
    stash = []

    def create(req):
        if req.symbol == BTC and not stash:
            stash.append(req)
            raise TransportError("simulated gateway timeout (order queued at the exchange)")
        return real_create(req)

    w.sim.create_order = create
    return stash, real_create


def lift(w):
    for _ in range(QUARANTINE_MIN_CHECKS):
        w.clock.advance(w.cfg.unknown_resolve_window_s + 1)
        w.engine.poll_and_manage()


def btc_buys_executed(w):
    return [o for o in w.sim.all_orders() if o.symbol == BTC and o.side is Side.BUY and o.executed_qty > 0]


# --------------------------------------------------------------------------- R6
def test_r6_late_accept_after_abandonment_never_allows_a_second_entry(tmp_path):
    """Reviewer's R6: entry #1 times out at the gateway but is queued; it is resolved ABANDONED, its
    quarantine is (eventually) lifted, THEN the exchange executes it. Entry #2 must not be sent, and
    the bot must end STOPPED with the ledger/exchange difference explained."""
    w = funded_world(tmp_path)
    stash, real_create = queued_gateway_timeout(w)
    r1 = w.engine.submit(w.entry("0.0002"))
    assert r1.status == "abandoned" and w.store.get_order(r1.order_ids[0]).lifecycle is L.ABANDONED
    lift(w)                                                  # quarantine lifted after spaced re-checks
    assert w.engine.quarantined() == frozenset() and w.sm.state is S.READY
    real_create(stash[0])                                    # the exchange finally executes the queued order
    posts_before = sum(w.sim.post_counts.values())
    r2 = w.engine.submit(w.entry("0.0002"))
    assert r2.status == "blocked" and r2.order_ids == []     # entry #2 never sent
    assert sum(w.sim.post_counts.values()) == posts_before
    assert len(btc_buys_executed(w)) == 1                    # only the late order: no duplicate exposure
    assert w.sm.state is S.STOPPED
    reason = w.engine.status()["stop"]["reason"]
    assert reason.startswith(f"late_accept: ABANDONED order {r1.order_ids[0]}")
    assert "executed 0.0002 BTC" in reason                   # the late order's execution ...
    led = w.portfolio.expected_balances()
    assert f"BTC ledger {plain(led['BTC'])} exchange {plain(w.sim.total('BTC'))}" in reason   # ... vs the
    assert f"USDT ledger {plain(led['USDT'])} exchange {plain(w.sim.total('USDT'))}" in reason  # exchange
    assert led["BTC"] == 0 and w.sim.total("BTC") > 0         # the whole difference is the late order
    assert alerts(w, "late_accept")


def test_late_accept_detected_before_the_quarantine_is_lifted_cancels_the_resting_order(tmp_path):
    w = funded_world(tmp_path)
    stash, real_create = queued_gateway_timeout(w)
    r1 = w.engine.submit(w.entry("0.0002", style="passive_limit"))
    assert r1.status == "abandoned"
    real_create(stash[0])                                    # accepted late, RESTS on the book
    w.clock.advance(5)
    w.engine.poll_and_manage()                               # every poll re-checks the watch list
    assert w.sm.state is S.STOPPED and alerts(w, "late_accept")
    late = next(o for o in w.sim.all_orders() if o.symbol == BTC)
    assert late.active is False and late.status.value == "CANCELED"     # cancelled (cancel only)
    assert "cancel requested; now inactive" in w.engine.status()["stop"]["reason"]
    assert not [o for o in w.sim.all_orders() if o.side is Side.SELL]   # nothing was sold


def test_late_accept_found_at_startup_after_a_restart(tmp_path):
    w = funded_world(tmp_path, file_db=True)
    try:
        stash, real_create = queued_gateway_timeout(w)
        assert w.engine.submit(w.entry("0.0002")).status == "abandoned"
        lift(w)
        real_create(stash[0])                                # accepted while the bot was down
        eng = w.restart()
        rep = eng.reconcile_startup()
        assert not rep["ok"] and w.sm.state is S.STOPPED and alerts(w, "late_accept")
    finally:
        w.close()


# --------------------------------------------------------------------------- quarantine
def test_quarantine_is_never_lifted_on_absence_alone(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("post_timeout_before_accept")
    assert w.engine.submit(w.entry()).status == "abandoned"
    key = (BTC, Side.BUY)
    win = w.cfg.unknown_resolve_window_s
    for _ in range(10):                                      # many polls INSIDE one window: no count
        w.clock.advance(win / 20)
        w.engine.poll_and_manage()
    assert key in w.engine.quarantined()
    w.clock.advance(win)
    w.engine.poll_and_manage()                               # 1st counted absent re-check
    w.clock.advance(win + 1)
    w.sim.inject("get_order_error", times=1)                 # a failed read breaks the streak
    w.engine.poll_and_manage()
    assert w.store.kv_get("exec.quarantine")[f"{BTC}:BUY"]["absent_checks"] == 0
    for i in range(QUARANTINE_MIN_CHECKS):
        assert key in w.engine.quarantined(), i
        w.clock.advance(win + 1)
        w.engine.poll_and_manage()
    assert key not in w.engine.quarantined()
    lifted = w.audit.of("quarantine_lifted")[-1]
    assert lifted["absent_checks"] == QUARANTINE_MIN_CHECKS and lifted["span_s"] >= QUARANTINE_MIN_CHECKS * win
    assert lifted["still_watched"] is True


def test_unverifiable_watch_blocks_new_orders(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("post_timeout_before_accept")
    assert w.engine.submit(w.entry()).status == "abandoned"
    lift(w)
    w.sim.inject("trades_error", times=5)
    r = w.engine.submit(w.entry())
    assert r.status == "blocked" and r.reason.startswith("abandoned_watch_unverified")
    w.sim.clear_faults()
    assert w.engine.submit(w.entry()).status == "filled"


def test_unknown_open_order_while_watching_stops_without_touching_it(tmp_path):
    w = funded_world(tmp_path, sim_kw={"echo_client_id": False, "lookup_by_client_id": False})
    w.sim.inject("post_timeout_before_accept")
    assert w.engine.submit(w.entry()).status == "abandoned"
    w.sim.create_order(OrderRequest("manual-1", BTC, Side.BUY, D("70000"), D("0.0001")))   # rests
    w.clock.advance(5)
    w.engine.poll_and_manage()
    assert w.sm.state is S.STOPPED
    reason = alerts(w, "needs_manual_reconcile")[-1]["reason"]
    assert "unknown to the store while ABANDONED order" in reason and "possible late accept" in reason
    assert any(o.active for o in w.sim.all_orders())         # not ours for sure: never cancelled


# --------------------------------------------------------------------------- in-run balance shortfall
def test_in_run_shortfall_blocks_at_once_and_stops_when_confirmed(tmp_path):
    w = funded_world(tmp_path)
    w.sim._total["USDT"] -= D("1")                           # funds vanish during the run
    w.engine.poll_and_manage()
    assert w.sm.state is S.READY and alerts(w, "balance_shortfall_suspected")
    r = w.engine.submit(w.entry())
    assert r.status == "blocked" and r.reason.startswith("balance_shortfall_unconfirmed")
    w.clock.advance(SHORTFALL_CONFIRM_S + 1)
    w.engine.poll_and_manage()
    assert w.sm.state is S.STOPPED
    reason = w.engine.status()["stop"]["reason"]
    usdt = w.portfolio.expected_balances()["USDT"]
    assert reason == (f"needs_manual_reconcile: ledger/exchange shortfall: USDT ledger {usdt.normalize():f} "
                      f"exchange {(usdt - 1).normalize():f} short 1")


def test_a_transient_shortfall_that_settles_is_not_fatal(tmp_path):
    w = funded_world(tmp_path)
    w.sim._total["USDT"] -= D("1")
    w.engine.poll_and_manage()
    w.sim._total["USDT"] += D("1")                           # exchange balance settles
    w.clock.advance(SHORTFALL_CONFIRM_S + 1)
    w.engine.poll_and_manage()
    assert w.sm.state is S.READY and "balance_shortfall_cleared" in w.audit.kinds()
    assert w.engine.submit(w.entry()).status == "filled"


def test_no_shortfall_verdict_while_an_order_of_ours_is_in_flight(tmp_path):
    w = funded_world(tmp_path, order_timeout_s=10_000)
    r = w.engine.submit(w.entry(style="passive_limit"))
    assert r.status == "open"
    assert w.engine.check_balances(stage="test")["skipped"] == "orders in flight"


def test_runner_stops_on_a_confirmed_in_run_shortfall_with_the_numbers(tmp_path):
    from tests._runner_support import hold_script, sim_world
    from wallex_agent.runner import AgentRunner
    w = sim_world(tmp_path)
    rt = w.runtime(hold_script)
    r = AgentRunner(rt)
    assert r.start() is None
    w.sim._total["TMN"] -= D("1000")
    r.run_once()
    w.clock.advance(SHORTFALL_CONFIRM_S + 1)
    status = r.run_once()
    assert status == "stopped" and rt.sm.state is S.STOPPED
    assert "TMN ledger 5000000 exchange 4999000 short 1000" in rt.engine.status()["stop"]["reason"]
