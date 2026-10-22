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
