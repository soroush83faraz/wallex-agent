import json
import re
from dataclasses import replace
from decimal import Decimal as D

import pytest

from wallex_agent.errors import KillSwitchActive
from wallex_agent.execution import ExecutionResult, ListAudit, NullAudit, trade_keys
from wallex_agent.types import BotState as S, OrderLifecycle as L, Side, TradeIntent

from ._sim_support import (BTC, BTC_ASKS, BTC_BIDS, FEES, INITIAL, RULES, USD, USD_ASKS, USD_BIDS,
                           funded_world, held_world, make_world)


def lifecycles(w):
    return [o.lifecycle for o in w.store.list_orders()]


def one_post_each(w):
    return w.sim.max_posts_per_client_id() <= 1


# --------------------------------------------------------------------------- conversion stage
def test_conversion_happy_path_unfunded_to_ready(tmp_path):
    w = make_world(tmp_path)
    assert w.sm.state is S.UNFUNDED
    res = w.engine.convert_tmn_to_usdt()
    assert res.status == "done" and res.rounds == 1 and len(res.order_ids) == 1
    assert w.sm.state is S.READY
    assert [h["to"] for h in w.sm.history()] == ["CONVERTING", "READY"]
    assert w.ledger_matches_sim()
    # sized with fee headroom: qty*price*(1+fee+margin) <= 5,000,000 TMN, residual below minNotional
    o = w.store.get_order(res.order_ids[0])
    assert o.quantity * o.price * (1 + FEES[USD].taker + w.cfg.fee_safety_margin) <= INITIAL
    assert w.sim.total("TMN") < RULES[USD].min_notional
    assert res.tmn_spent == o.executed_sum and res.usdt_received == o.executed_qty - o.fee_total
    assert w.portfolio.usdt_cost_basis_tmn() == res.tmn_spent / res.usdt_received
    assert one_post_each(w)


def test_conversion_partial_fill_cancels_remainder_and_continues(tmp_path):
    w = make_world(tmp_path)
    w.sim.inject("partial_fill", frac=D("0.4"))
    res = w.engine.convert_tmn_to_usdt()
    assert res.status == "done" and res.rounds == 2, res
    first, second = (w.store.get_order(c) for c in res.order_ids)
    assert first.lifecycle is L.CANCELED and 0 < first.executed_qty < first.quantity
    assert second.lifecycle is L.FILLED
    assert w.sm.state is S.READY and w.ledger_matches_sim()
    assert w.sim.total("TMN") < RULES[USD].min_notional and w.sim.get_balances()["TMN"].locked == 0
    assert "order_cancelled" in w.audit.kinds()


def test_conversion_inspects_actual_balances(tmp_path):
    # exchange holds MORE TMN than the ledger budget: only the bot's budget is converted
    w = make_world(tmp_path, balances={"TMN": D("8000000"), "USDT": D(0), "BTC": D(0)})
    res = w.engine.convert_tmn_to_usdt()
    assert res.status == "done" and res.tmn_spent <= INITIAL and w.sim.total("TMN") > D("2900000")
    # exchange holds LESS than the ledger says (M4: a shortfall): not a single order is sent, the
    # bot is blocked at once and STOPPED once the shortfall is confirmed - with the numbers
    w3 = make_world(tmp_path / "c", balances={"TMN": D("3000000"), "USDT": D(0), "BTC": D(0)})
    res3 = w3.engine.convert_tmn_to_usdt()
    assert w3.sim.order_count() == 0 and res3.status in ("declined", "nothing_to_convert", "blocked")
    assert any("balance_shortfall_unconfirmed" in n for n in res3.notes), res3.notes
    w3.clock.advance(60)
    w3.engine.poll_and_manage()
    assert w3.sm.state is S.STOPPED
    reason = w3.engine.status()["stop"]["reason"]
    assert reason.startswith("needs_manual_reconcile: ledger/exchange shortfall")
    assert "TMN ledger 5000000 exchange 3000000 short 2000000" in reason
    # nothing convertible at all: no order, state stays UNFUNDED
    w2 = make_world(tmp_path / "b", balances={"TMN": D("40000"), "USDT": D(0), "BTC": D(0)})
    res2 = w2.engine.convert_tmn_to_usdt()
    assert res2.status == "nothing_to_convert" and res2.order_ids == [] and w2.sim.order_count() == 0
    assert w2.sm.state is S.UNFUNDED


def test_conversion_round_after_a_spread_blowout_is_refused(tmp_path):
    """L1 / R5: rounds 2..8 pass the same risk gate as the first: a gap between rounds is never
    bought into."""
    w = make_world(tmp_path)
    w.sim.inject("partial_fill", frac=D("0.4"))
    real_create = w.sim.create_order
    posts = []

    def create(req):
        info = real_create(req)
        posts.append(req)
        if len(posts) == 1:                    # right after round 1 the USDTTMN asks gap up ~6.6 %
            w.source.set_book(USD, [(D("271900"), D("100"))], [(D("290000"), D("1000"))])
        return info

    w.sim.create_order = create
    res = w.engine.convert_tmn_to_usdt(decision_id="d-conv")
    assert len(posts) == 1 and res.rounds == 1 and res.status == "declined"
    assert any("round 2 refused by the risk gate" in n and "spread_too_wide" in n for n in res.notes)
    assert w.audit.of("conversion_round_refused")[-1]["decision_id"] == "d-conv"
    assert all(o.decision_id == "d-conv" for o in w.store.list_orders())       # F1: never null
    assert w.ledger_matches_sim()


def test_conversion_honours_the_stop_callback_before_every_round(tmp_path):
    """L1: SIGINT/SIGTERM (or the deadline) between rounds: no further round is sent."""
    w = make_world(tmp_path, order_timeout_s=5.0)
    w.sim.inject("partial_fill", frac=D("0.4"))
    asked = []

    def should_stop():
        asked.append(1)
        done = [o for o in w.store.list_orders() if o.is_terminal]
        return "shutdown requested (SIGTERM)" if done else None

    res = w.engine.convert_tmn_to_usdt(should_stop=should_stop)
    assert w.sim.order_count() == 1 and res.status == "interrupted" and asked
    assert any("round 2 not sent: shutdown requested (SIGTERM)" in n for n in res.notes)
    assert w.store.unresolved_orders() == []


def test_conversion_checks_the_kill_switch_before_every_round(tmp_path):
    w = make_world(tmp_path)
    w.sim.inject("partial_fill", frac=D("0.4"))
    real_create = w.sim.create_order

    def create(req):
        info = real_create(req)
        (tmp_path / "KILL").write_text("x", encoding="utf-8")   # operator kills mid-conversion
        return info

    w.sim.create_order = create
    with pytest.raises(KillSwitchActive):
        w.engine.convert_tmn_to_usdt()
    assert w.sim.order_count() == 1


def test_conversion_declines_on_unknown_fee_or_stale_book(tmp_path):
    w = make_world(tmp_path)
    w.sim.hidden_fees.add(USD)
    res = w.engine.convert_tmn_to_usdt()
    assert res.status == "declined" and "fee_unknown" in " ".join(res.notes) and w.sim.order_count() == 0
    w.sim.hidden_fees.clear()
    w.sim.inject("stale_book", age_s=60)
    res = w.engine.convert_tmn_to_usdt()
    assert res.status == "declined" and "book_stale" in " ".join(res.notes) and w.sim.order_count() == 0
    assert w.engine.convert_tmn_to_usdt().status == "done"


# --------------------------------------------------------------------------- entries / exits
def test_entry_fill_then_exit_round_trip(tmp_path):
    w = funded_world(tmp_path)
    r = w.engine.submit(w.entry(), decision_id="d-1")
    assert isinstance(r, ExecutionResult) and r.status == "filled" and r.filled_qty == D("0.0002")
    rec = w.store.get_order(r.order_ids[0])
    assert rec.decision_id == "d-1" and rec.fee_source == "exchange" and rec.fee_asset == "BTC"
    assert rec.exchange_client_order_id == rec.client_id
    assert w.sm.state is S.BTC_HELD and w.ledger_matches_sim()
    r2 = w.engine.submit(w.exit())
    assert r2.status == "filled" and w.sm.state is S.READY and w.ledger_matches_sim()
    fills = w.store.list_fills()
    assert all(f.fee_source == "exchange" for f in fills)
    assert w.portfolio.btc_position().round_trips == 1
    history = [h["to"] for h in w.sm.history()]
    assert history[-4:] == ["BUY_PENDING", "BTC_HELD", "SELL_PENDING", "READY"]
    assert one_post_each(w)


def test_client_ids_are_unique_and_wellformed(tmp_path):
    w = funded_world(tmp_path)
    ids = [o.client_id for o in w.store.list_orders()]
    w.engine.submit(w.entry())
    w.engine.submit(w.exit())
    ids = [o.client_id for o in w.store.list_orders()]
    assert len(set(ids)) == len(ids) == 3
    assert all(re.fullmatch(r"wa-[0-9a-f]+-\d+-[0-9a-f]{6}", i) and len(i) <= 64 for i in ids)


def test_buy_is_sized_with_fee_headroom_and_never_above_slippage_cap(tmp_path):
    w = funded_world(tmp_path)
    usdt = w.portfolio.expected_balances()["USDT"]
    r = w.engine.submit(w.entry(qty="1"))                    # way more than affordable
    rec = w.store.get_order(r.order_ids[0])
    assert rec.quantity * rec.price * (1 + FEES[BTC].taker + w.cfg.fee_safety_margin) <= usdt
    cap = D("81010") * (1 + D("20") / 10000)
    assert rec.price <= cap and rec.price == RULES[BTC].round_price(rec.price, Side.BUY)
    assert any("size reduced" in n for n in r.notes)
    assert w.ledger_matches_sim() and min(w.sim.totals().values()) >= 0


def test_sell_never_exceeds_ledger_or_spendable(tmp_path):
    w = held_world(tmp_path)
    held = w.portfolio.expected_balances()["BTC"]
    r = w.engine.submit(w.exit(qty="5"))
    rec = w.store.get_order(r.order_ids[0])
    assert rec.quantity <= held and rec.quantity == RULES[BTC].round_qty(held)
    assert any("clamped" in n for n in r.notes)


def test_sell_dust_is_reported_not_sent(tmp_path):
    w = held_world(tmp_path)
    n = w.sim.order_count()
    r = w.engine.submit(w.exit(qty="0.00000001"))
    assert r.status == "declined" and "below_minimum" in r.reason and w.sim.order_count() == n
    assert w.sm.state is S.BTC_HELD
    w.engine.submit(w.exit(qty="0.00019"))   # leaves 0.0000093 BTC (~0.75 USDT < minNotional 1)
    assert w.sm.state is S.READY and w.portfolio.expected_balances()["BTC"] > 0   # dust: not a position


def test_book_beyond_slippage_cap_declines_and_partial_cap_reduces_size(tmp_path):
    w = funded_world(tmp_path)
    w.source.set_book(BTC, [(D("82000"), D("1"))], [(D("82010"), D("1"))])
    r = w.engine.submit(w.entry(reference_price=D("81010")))
    assert r.status == "declined" and "no_liquidity_within_slippage_cap" in r.reason
    assert w.sim.order_count() == 1 and w.sm.state is S.READY
    # only 0.00005 BTC available inside the cap -> size reduced, never a blind fill above the cap
    w.source.set_book(BTC, [(D("81000"), D("1"))], [(D("81010"), D("0.00005")), (D("81500"), D("1"))])
    r = w.engine.submit(w.entry(qty="0.0002"))
    rec = w.store.get_order(r.order_ids[0])
    assert r.status == "filled" and rec.quantity == D("0.00005") and rec.price <= D("81010") * D("1.002")


@pytest.mark.parametrize("what", ["fee", "rules", "stale", "balances", "network"])
def test_never_trades_blind(tmp_path, what):
    w = funded_world(tmp_path)
    n = w.sim.order_count()
    if what == "fee":
        w.sim.hidden_fees.add(BTC)
    elif what == "rules":
        del w.sim.rules[BTC]
    elif what == "stale":
        w.sim.inject("stale_book", age_s=w.cfg.max_book_age_s + 1)
    elif what == "balances":
        w.sim.inject("balances_error")
    else:
        w.sim.inject("network_down", times=50)
    r = w.engine.submit(w.entry())
    assert r.status == "declined" and r.order_ids == [] and w.sim.order_count() == n
    assert w.sm.state is S.READY


def test_passive_order_rests_then_times_out_and_is_cancelled(tmp_path):
    w = funded_world(tmp_path)
    r = w.engine.submit(w.entry(style="passive_limit"))
    assert r.status == "open" and w.sm.state is S.BUY_PENDING
    rep = w.engine.poll_and_manage()
    assert rep["cancel_attempts"] == [] and w.sm.state is S.BUY_PENDING
    assert w.engine.submit(w.entry()).status == "blocked"          # one order at a time
    w.clock.advance(w.cfg.order_timeout_s + 1)
    rep = w.engine.poll_and_manage()
    assert rep["cancel_attempts"] == r.order_ids
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.CANCELED
    assert w.sm.state is S.READY and w.ledger_matches_sim()
    assert w.sim.get_balances()["USDT"].locked == 0


def test_partial_fill_then_remainder_cancel_keeps_states_honest(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("partial_fill", frac=D("0.5"))
    r = w.engine.submit(w.entry())
    assert r.status == "partial" and w.sm.state is S.BUY_PENDING
    w.clock.advance(w.cfg.order_timeout_s + 1)
    w.engine.poll_and_manage()
    rec = w.store.get_order(r.order_ids[0])
    assert rec.lifecycle is L.CANCELED and rec.executed_qty == D("0.0001")
    assert w.sm.state is S.BTC_HELD and w.ledger_matches_sim()


def test_partial_dust_returns_to_ready(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("partial_fill", frac=D("0.05"))     # 0.00001 BTC ~ 0.81 USDT < minNotional
    w.engine.submit(w.entry())
    w.clock.advance(w.cfg.order_timeout_s + 1)
    w.engine.poll_and_manage()
    assert w.sm.state is S.READY and w.portfolio.expected_balances()["BTC"] > 0


def test_resting_order_filled_by_later_book_records_exchange_fee(tmp_path):
    w = funded_world(tmp_path, sim_kw={"resting_fee": "maker"})
    r = w.engine.submit(w.entry(style="passive_limit"))
    w.source.set_book(BTC, [(D("80900"), D("1"))], [(D("80950"), D("1"))])   # market drops to us
    w.engine.poll_and_manage()
    rec = w.store.get_order(r.order_ids[0])
    assert rec.lifecycle is L.FILLED and rec.fee_source == "exchange"
    assert rec.fee_total == rec.quantity * FEES[BTC].maker   # maker != estimate: it came from trades
    assert w.sm.state is S.BTC_HELD and w.ledger_matches_sim()


def test_fee_estimated_when_trades_unavailable(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("trades_error", times=1)
    w.engine.submit(w.entry(style="passive_limit"))   # the pre-send snapshot needs trades: declines
    assert w.sim.order_count() == 1
    r = w.engine.submit(w.entry(style="passive_limit"))
    assert r.status == "open"
    w.source.set_book(BTC, [(D("80900"), D("1"))], [(D("80950"), D("1"))])
    w.sim.inject("trades_error", times=1)
    w.engine.poll_and_manage()
    rec = w.store.get_order(r.order_ids[0])
    f = w.store.list_fills(rec.client_id)[0]
    assert f.fee_source == "estimated" and f.fee_asset == "BTC" and f.fee == f.quantity * FEES[BTC].taker
    assert rec.fee_source == "estimated" and w.engine.status()["estimated_fee_fills"] == 1


def test_trade_rows_are_attributed_once(tmp_path):
    w = held_world(tmp_path)
    w.engine.submit(w.exit())
    keys = w.store.kv_get("exec.attributed_trades")
    assert len(keys) == len(set(keys)) == 2 + 1     # entry (1 level) + exit (1 level) + conversion
    assert {k for k, _ in trade_keys(w.sim.get_trades())} == set(keys)


def test_filled_while_cancelling_race(tmp_path):
    w = funded_world(tmp_path)
    r = w.engine.submit(w.entry(style="passive_limit"))
    w.sim.inject("cancel_race_filled")
    w.clock.advance(w.cfg.order_timeout_s + 1)
    w.engine.poll_and_manage()
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.FILLED
    assert "filled_during_cancel" in w.audit.kinds()
    assert w.sm.state is S.BTC_HELD and w.ledger_matches_sim()


def test_cancel_failed_race_is_retried_and_verified(tmp_path):
    w = funded_world(tmp_path)
    r = w.engine.submit(w.entry(style="passive_limit"))
    w.sim.inject("cancel_fail")
    w.clock.advance(w.cfg.order_timeout_s + 1)
    w.engine.poll_and_manage()
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.OPEN and w.sm.state is S.BUY_PENDING
    assert "cancel_failed" in w.audit.kinds()
    w.engine.poll_and_manage()
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.CANCELED and w.sm.state is S.READY


def test_cancel_unverified_keeps_order_open(tmp_path):
    w = funded_world(tmp_path)
    r = w.engine.submit(w.entry(style="passive_limit"))
    w.clock.advance(w.cfg.order_timeout_s + 1)
    w.sim.inject("get_order_error", times=2)          # refresh fails, then verification fails
    w.engine.poll_and_manage()
    rec = w.store.get_order(r.order_ids[0])
    # the cancel itself went through on the exchange, but we only believe what we verified
    assert rec.lifecycle is L.OPEN and w.sm.state is S.BUY_PENDING
    w.engine.poll_and_manage()
    assert w.store.get_order(r.order_ids[0]).lifecycle is L.CANCELED and w.sm.state is S.READY


# --------------------------------------------------------------------------- rejections
def test_rejection_1006_retries_with_smaller_size_and_new_client_id(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("reject", code=1006)
    r = w.engine.submit(w.entry(qty="1"))
    assert r.status == "filled" and len(r.order_ids) == 2
    a, b = (w.store.get_order(i) for i in r.order_ids)
    assert a.lifecycle is L.REJECTED and b.lifecycle is L.FILLED and b.quantity < a.quantity
    assert a.client_id != b.client_id and one_post_each(w)


def test_rejection_precision_codes_give_up_after_max_orders_per_intent(tmp_path):
    w = funded_world(tmp_path, max_orders_per_intent=3)
    w.source.set_book(BTC, [(D("81000.11"), D("1"))], [(D("81010.55"), D("1"))])
    w.sim.inject("reject", times=10, code=1001)
    r = w.engine.submit(w.entry())
    assert r.status == "rejected" and len(r.order_ids) == 3 and "gave up" in r.reason
    prices = [w.store.get_order(i).price for i in r.order_ids]
    assert prices[1] <= prices[0] and len(set(prices)) >= 2           # corrected each time
    assert all(w.store.get_order(i).lifecycle is L.REJECTED for i in r.order_ids)
    assert one_post_each(w) and w.sm.state is S.READY


def test_rejection_1004_without_possible_correction_gives_up(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("reject", times=5, code=1004)
    r = w.engine.submit(w.entry())
    assert r.status == "rejected" and len(r.order_ids) == 1 and "did not change" in r.reason


def test_non_correctable_rejection_is_final(tmp_path):
    w = funded_world(tmp_path)
    w.sim.inject("reject", times=5, code=1010)
    r = w.engine.submit(w.entry())
    assert r.status == "rejected" and len(r.order_ids) == 1 and w.sm.state is S.READY


# --------------------------------------------------------------------------- kill switch
def test_kill_file_blocks_every_send(tmp_path):
    w = funded_world(tmp_path)
    n = w.sim.order_count()
    (tmp_path / "KILL").write_text("stop", encoding="utf-8")
    with pytest.raises(KillSwitchActive):
        w.engine.submit(w.entry())
    with pytest.raises(KillSwitchActive):
        w.engine.convert_tmn_to_usdt()
    assert w.sim.order_count() == n and w.engine.status()["kill_requested"]


def test_kill_cancels_only_never_sells(tmp_path):
    w = held_world(tmp_path)
    btc = w.sim.total("BTC")
    w.engine.submit(w.exit(style="passive_limit"))       # resting sell
    assert w.sm.state is S.SELL_PENDING
    out = w.engine.kill(cancel_open=True)
    assert len(out["cancelled"]) == 1 and w.sm.state is S.STOPPED
    assert w.sim.total("BTC") == btc and w.sim.list_open_orders() == []
    assert not any(o.side is Side.SELL and o.executed_qty > 0 for o in w.sim.all_orders())
    with pytest.raises(KillSwitchActive):
        w.engine.submit(w.exit())
    assert "ALERT" in w.audit.kinds()


def test_kill_without_cancel(tmp_path):
    w = funded_world(tmp_path)
    w.engine.submit(w.entry(style="passive_limit"))
    w.engine.kill(cancel_open=False)
    assert w.sm.state is S.STOPPED and len(w.sim.list_open_orders()) == 1


# --------------------------------------------------------------------------- misc
def test_state_mismatch_and_stopped_block(tmp_path):
    w = funded_world(tmp_path)
    assert w.engine.submit(w.exit(qty="0.0001")).reason.startswith("state_mismatch")
    w.sm.transition(S.STOPPED, "test")
    r = w.engine.submit(w.entry())
    assert r.status == "blocked" and r.reason == "bot_stopped"


def test_status_is_json_serialisable(tmp_path):
    w = held_world(tmp_path)
    w.engine.submit(w.exit(style="passive_limit"))
    st = w.engine.status()
    text = json.dumps(st)
    assert st["state"] == "SELL_PENDING" and len(st["unresolved"]) == 1 and "BTC" in st["ledger"]
    assert "api_key" not in text


def test_audit_payloads_are_json_and_null_audit_works(tmp_path):
    w = held_world(tmp_path)
    for kind, payload in w.audit.records:
        json.dumps(payload)
    w.engine.audit = NullAudit()
    w.engine.submit(w.exit())
    assert isinstance(w.audit, ListAudit)


# --------------------------------------------------------------------------- order preflight (live engines)
def live_world(tmp_path, **kw):
    """A world whose engine is a LIVE one (config mode live): orders would be real."""
    from wallex_agent.types import Mode
    return make_world(tmp_path, mode=Mode.LIVE, **kw)


def posts(w):
    return sum(w.sim.post_counts.values())


def convert_intent():
    return TradeIntent("convert", Side.BUY, USD, D("10"), D("272000"), D("30"))


def assert_nothing_can_be_sent(w):
    res = w.engine.submit(convert_intent())
    assert res.status == "blocked" and res.reason.startswith("preflight_missing"), res
    conv = w.engine.convert_tmn_to_usdt()
    assert conv.status == "blocked" and conv.notes and conv.notes[0].startswith("preflight_missing")
    assert posts(w) == 0 and w.store.list_orders() == []


def test_a_live_engine_sends_nothing_until_its_preflight_passed_in_this_process(tmp_path):
    w = live_world(tmp_path)
    assert w.engine.require_preflight and not w.engine.preflight_ok
    assert_nothing_can_be_sent(w)
    pf = w.engine.preflight()                                   # (c) no startup reconcile yet
    assert not pf["ok"] and any(p.startswith("startup_reconcile_not_ok") for p in pf["problems"])
    assert_nothing_can_be_sent(w)
    assert w.engine.reconcile_startup()["ok"]
    pf = w.engine.preflight()
    assert pf["ok"] and set(pf["rules"]) == set(pf["books"]) == {BTC, USD} and w.engine.preflight_ok
    assert w.audit.of("preflight")[-1]["ok"] is True and json.dumps(w.audit.of("preflight"))
    assert w.engine.convert_tmn_to_usdt().status == "done" and posts(w) == 1
    w.restart()                                                 # a new process: the pass is not stored
    assert w.engine.require_preflight and not w.engine.preflight_ok
    assert w.engine.submit(w.entry()).reason.startswith("preflight_missing")


def _crossed_book(w, symbol):
    from wallex_agent.types import BookLevel, OrderBook
    return OrderBook(symbol, (BookLevel(D("272100"), D("5")),), (BookLevel(D("272000"), D("5")),), w.clock.now())


@pytest.mark.parametrize("fault,token", [
    ("markets_down", "rules_unavailable: TransportError"), ("rules_missing", "rules_unknown: BTCUSDT"),
    ("book_down", "book_unavailable: BTCUSDT TransportError"), ("book_stale", "book_stale: USDTTMN"),
    ("book_crossed", "book_crossed: USDTTMN"), ("stopped", "bot_stopped")])
def test_no_order_is_attempted_when_rules_books_or_reconcile_fail_the_preflight(tmp_path, monkeypatch, fault, token):
    from wallex_agent.errors import TransportError
    w = live_world(tmp_path)
    assert w.engine.reconcile_startup()["ok"]
    sim = w.sim
    if fault == "markets_down":
        sim.inject("network_down")                              # the preflight's markets read fails
    elif fault == "rules_missing":
        sim.rules.pop(BTC)
    elif fault == "book_down":
        real = sim.get_order_book
        monkeypatch.setattr(sim, "get_order_book", lambda s: (_ for _ in ()).throw(
            TransportError("x", category="read_timeout")) if s == BTC else real(s))
    elif fault == "book_stale":
        sim.inject("stale_book", age_s=3600)
    elif fault == "book_crossed":
        real = sim.get_order_book
        monkeypatch.setattr(sim, "get_order_book", lambda s: _crossed_book(w, s) if s == USD else real(s))
    elif fault == "stopped":
        w.sm.transition(S.STOPPED, "test")
    pf = w.engine.preflight()
    assert not pf["ok"] and any(p.startswith(token) for p in pf["problems"]), pf["problems"]
    assert_nothing_can_be_sent(w)
    if fault == "book_down":                                    # the transport category is kept
        assert any("TransportError[read_timeout]" in p for p in pf["problems"])


def test_the_send_path_rechecks_the_preflight_before_persisting_anything(tmp_path, monkeypatch):
    w = live_world(tmp_path)
    monkeypatch.setattr(w.engine, "_blocker", lambda intent: None)     # a hypothetical blocker bug
    res = w.engine.submit(convert_intent())
    assert res.status == "declined" and "preflight_missing" in res.reason
    assert posts(w) == 0 and w.store.list_orders() == []


def test_a_failed_preflight_or_a_new_reconcile_withdraws_the_permission(tmp_path):
    w = live_world(tmp_path)
    assert w.engine.reconcile_startup()["ok"] and w.engine.preflight()["ok"]
    w.sim.inject("network_down")
    assert not w.engine.preflight()["ok"] and not w.engine.preflight_ok
    assert w.engine.submit(convert_intent()).status == "blocked"
    assert w.engine.preflight()["ok"]
    assert w.engine.reconcile_startup()["ok"] and not w.engine.preflight_ok     # needs a new preflight
    assert w.engine.submit(convert_intent()).status == "blocked" and posts(w) == 0


def test_the_preflight_never_blocks_cancelling_or_killing(tmp_path):
    """Emergency paths stay open: a new process whose preflight is missing (or failing) still cancels
    the resting orders of the bot (kill / cancel-open / shutdown) - cancel only, nothing new is sent."""
    w = live_world(tmp_path)
    assert w.engine.reconcile_startup()["ok"] and w.engine.preflight()["ok"]
    assert w.engine.convert_tmn_to_usdt().status == "done"
    res = w.engine.submit(w.entry(style="passive_limit"))
    assert res.status == "open"
    oid = res.order_ids[0]
    w.restart()                                                    # new process: no preflight yet
    w.sim.inject("network_down")
    assert not w.engine.preflight()["ok"]
    before = posts(w)
    out = w.engine.kill(cancel_open=True, reason="test")
    assert oid in out["cancelled"] and posts(w) == before
    assert w.store.get_order(oid).lifecycle is L.CANCELED and w.sm.state is S.STOPPED


def test_simulated_engines_keep_working_without_a_preflight(tmp_path):
    w = make_world(tmp_path)                    # paper/backtest config: SimExchange, nothing is ever sent
    assert not w.engine.require_preflight and w.engine.convert_tmn_to_usdt().status == "done"


def test_unknown_outcome_alert_carries_the_classified_transport_failure_without_the_key(tmp_path):
    """Root-cause visibility: the POST fails in the REAL client (transport timeout after sending) ->
    the engine's unknown_outcome alert and the order note name the category; the key appears nowhere."""
    import socket
    from wallex_agent.clock import SimClock
    from wallex_agent.exchange.wallex import WallexClient
    key = "SENTINELKEY-Q7xZ9wPv3Lm8/Rk2+Tz=Hq4"

    class Timeout:
        posts = 0

        def request(self, method, *a, **k):
            Timeout.posts += method == "POST"
            raise socket.timeout(f"timed out {key}")
    client = WallexClient(key, transport=Timeout(), clock=SimClock(), max_read_retries=0)
    w = funded_world(tmp_path)
    w.sim.create_order = client.create_order          # the POST goes through the real client and fails
    w.engine.submit(w.entry())
    (alert,) = [p for k, p in w.audit.records if k == "ALERT" and p.get("alert") == "unknown_outcome"]
    assert alert["category"] == "read_timeout" and alert["error"] == "TransportError"
    assert alert["message"].startswith("POST /v1/account/orders: transport failure [read_timeout]")
    assert "did not arrive in time" in alert["hint"] and "never resent" in alert["hint"]
    rec = [o for o in w.store.list_orders() if o.purpose == "entry"][0]
    assert Timeout.posts == 1 and rec.lifecycle is L.ABANDONED      # resolved from facts, never resent
    assert key not in json.dumps(w.audit.records) and key not in json.dumps([o.note for o in w.store.list_orders()])
