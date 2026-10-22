import random
import socket
from decimal import Decimal as D

import pytest

from wallex_agent.clock import SimClock
from wallex_agent.errors import (OrderNotFound, RateLimitError, RejectedError, SchemaError, ServerError,
                                 TransportError)
from wallex_agent.exchange.sim import FAULT_KINDS, SimExchange, StaticBookSource
from wallex_agent.interfaces import BookSource, Exchange
from wallex_agent.types import Balance, OrderRequest, OrderStatus, Side

from ._sim_support import BTC, BTC_ASKS, BTC_BIDS, FEES, RULES, USD, USD_ASKS, USD_BIDS


def sim(balances=None, *, seed=1, **kw):
    clock = SimClock()
    src = StaticBookSource(clock)
    src.set_book(BTC, BTC_BIDS, BTC_ASKS)
    src.set_book(USD, USD_BIDS, USD_ASKS)
    s = SimExchange(src, clock, RULES, FEES, balances or {"USDT": D("10000"), "BTC": D("0.1"), "TMN": D(0)},
                    rng=random.Random(seed), **kw)
    return s, src, clock


_n = iter(range(10_000_000))


def req(side=Side.BUY, price="81010", qty="0.001", symbol=BTC, cid=None):
    return OrderRequest(cid or f"c-{next(_n)}", symbol, side, D(price), D(qty))


def test_satisfies_exchange_protocol_and_book_source():
    s, src, _ = sim()
    assert isinstance(s, Exchange)
    assert isinstance(src, BookSource)
    assert s.get_markets() == RULES and set(s.get_fees()) == {BTC, USD}
    assert s.get_order_book(BTC).best_ask == D("81010") and s.get_candles(BTC, "1", 0, 1) == []


def test_never_touches_the_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    s, _, _ = sim()
    s.create_order(req())
    s.get_balances(), s.get_trades(), s.list_open_orders(), s.get_fees(), s.get_markets()


@pytest.mark.parametrize("r,code", [
    (req(price="81010.123"), 1001),
    (req(qty="0.000000001"), 1002),
    (req(qty="0.00000001"), 1004),            # notional 0.0008 USDT < 1
    (req(qty="1"), 1006),                     # 81010 USDT > 1000 held
    (req(Side.SELL, price="81000", qty="0.2"), 1006),   # 0.2 BTC > 0.1 held
    (req(symbol=USD, price="272000.5", qty="1"), 1001),
    (req(symbol=USD, price="272000", qty="0.1"), 1004),  # 27,200 TMN < 50,000 minNotional
])
def test_wallex_style_rejections(r, code):
    s, _, _ = sim({"USDT": D("1000"), "BTC": D("0.1"), "TMN": D("1000000")})
    with pytest.raises(RejectedError) as ei:
        s.create_order(r)
    assert ei.value.status == 422 and ei.value.error_code == code
    assert s.order_count() == 0 and s.post_counts[r.client_id] == 1


def test_client_id_unique_and_validated():
    s, _, _ = sim()
    s.create_order(req(cid="dup"))
    with pytest.raises(RejectedError):
        s.create_order(req(cid="dup"))
    with pytest.raises(RejectedError):
        s.create_order(req(cid="bad id!"))
    with pytest.raises(RejectedError):
        s.create_order(req(symbol="ETHUSDT"))
    assert s.post_counts["dup"] == 2 and s.max_posts_per_client_id() == 2


def test_taker_buy_walks_book_fee_in_base_and_trades_have_no_order_id():
    s, _, _ = sim()
    info = s.create_order(req(price="81020", qty="0.02"))   # 0.01 @81010 + 0.01 @81020
    assert info.status is OrderStatus.FILLED and not info.active
    assert info.executed_qty == D("0.02") and info.executed_sum == D("810.1") + D("810.2")
    trades = s.get_trades(BTC)
    assert len(trades) == 2 and all(t.fee_asset == "BTC" and t.fee == t.quantity * D("0.0035") for t in trades)
    assert not any(hasattr(t, "client_order_id") or hasattr(t, "order_id") for t in trades)
    assert s.total("USDT") == D("10000") - D("1620.3")
    assert s.total("BTC") == D("0.1") + D("0.02") * (1 - D("0.0035"))
    assert s.get_balances()["USDT"].locked == 0


def test_taker_sell_fee_in_quote():
    s, _, _ = sim()
    info = s.create_order(req(Side.SELL, price="80990", qty="0.02"))
    assert info.status is OrderStatus.FILLED
    gross = D("0.01") * 81000 + D("0.01") * 80990
    assert s.total("USDT") == D("10000") + gross - gross * D("0.0035")
    assert s.get_trades(side=Side.SELL)[0].fee_asset == "USDT"


def test_remainder_rests_locked_and_matches_on_a_later_book_with_maker_fee():
    s, src, _ = sim(resting_fee="maker")
    info = s.create_order(req(price="81010", qty="0.03"))     # only 0.01 available at <= 81010
    assert info.status is OrderStatus.PARTIALLY_FILLED and info.active and info.executed_qty == D("0.01")
    bal = s.get_balances()["USDT"]
    assert bal.locked == D("81010") * D("0.02") and bal.spendable == bal.value - bal.locked
    assert len(s.list_open_orders(BTC)) == 1
    # same book again: the consumed liquidity is not re-used
    assert s.get_order(info.client_order_id).executed_qty == D("0.01")
    src.set_book(BTC, [(D("80000"), D("1"))], [(D("81005"), D("1"))])
    after = s.get_order(info.client_order_id)
    assert after.status is OrderStatus.FILLED and s.get_balances()["USDT"].locked == 0
    last = s.get_trades(BTC)[0]
    assert last.price == D("81005") and last.fee == D("0.02") * D("0.002")   # maker
    assert s.list_open_orders() == []


def test_cancel_releases_lock_and_inactive_cancel_is_rejected():
    s, _, _ = sim()
    info = s.create_order(req(price="80000", qty="0.01"))    # passive, rests
    assert info.status is OrderStatus.NEW and s.get_balances()["USDT"].locked == D("800")
    assert s.cancel_order(info.client_order_id) is None
    got = s.get_order(info.client_order_id)
    assert got.status is OrderStatus.CANCELED and not got.active and s.get_balances()["USDT"].locked == 0
    with pytest.raises(RejectedError):
        s.cancel_order(info.client_order_id)
    with pytest.raises(OrderNotFound):
        s.cancel_order("nope")
    with pytest.raises(OrderNotFound):
        s.get_order("nope")


def test_fault_post_timeout_after_accept_creates_the_order():
    s, _, _ = sim()
    s.inject("post_timeout_after_accept")
    r = req()
    with pytest.raises(TransportError):
        s.create_order(r)
    assert s.get_order(r.client_id).status is OrderStatus.FILLED


def test_fault_post_timeout_before_accept_creates_nothing():
    s, _, _ = sim()
    s.inject("post_timeout_before_accept")
    r = req()
    with pytest.raises(TransportError):
        s.create_order(r)
    with pytest.raises(OrderNotFound):
        s.get_order(r.client_id)
    assert s.total("USDT") == D("10000")


@pytest.mark.parametrize("kind,exc", [("post_5xx", ServerError), ("undecodable", SchemaError)])
@pytest.mark.parametrize("accepted", [True, False])
def test_fault_5xx_and_undecodable(kind, exc, accepted):
    s, _, _ = sim()
    s.inject(kind, accepted=accepted)
    r = req()
    with pytest.raises(exc):
        s.create_order(r)
    assert (s.order_count() == 1) is accepted


def test_fault_rate_limit_and_reject():
    s, _, _ = sim()
    s.inject("rate_limit")
    with pytest.raises(RateLimitError):
        s.create_order(req())
    s.inject("reject", code=1002)
    with pytest.raises(RejectedError) as ei:
        s.create_order(req())
    assert ei.value.error_code == 1002 and s.order_count() == 0


def test_fault_partial_fill_sticks_until_book_changes():
    s, src, _ = sim()
    s.inject("partial_fill", frac=D("0.25"))
    info = s.create_order(req(price="81060", qty="0.004"))
    assert info.executed_qty == D("0.001") and info.active
    assert s.get_order(info.client_order_id).executed_qty == D("0.001")
    other = s.create_order(req(price="81060", qty="0.001"))   # a new order still gets the liquidity
    assert other.status is OrderStatus.FILLED
    src.shift(BTC, D("-1"), 2)
    assert s.get_order(info.client_order_id).status is OrderStatus.FILLED


def test_fault_get_order_404_and_error():
    s, _, _ = sim()
    i = s.create_order(req())
    s.inject("get_order_404")
    with pytest.raises(OrderNotFound):
        s.get_order(i.client_order_id)
    s.inject("get_order_error")
    with pytest.raises(TransportError):
        s.get_order(i.client_order_id)
    assert s.get_order(i.client_order_id).status is OrderStatus.FILLED


def test_fault_cancel_race_filled_and_cancel_fail():
    s, _, _ = sim()
    i = s.create_order(req(price="80000", qty="0.01"))
    s.inject("cancel_fail")
    with pytest.raises(TransportError):
        s.cancel_order(i.client_order_id)
    assert s.get_order(i.client_order_id).active
    s.inject("cancel_race_filled")
    assert s.cancel_order(i.client_order_id) is None          # "cancel requested" ... but it filled
    got = s.get_order(i.client_order_id)
    assert got.status is OrderStatus.FILLED and got.executed_sum == D("800")
    i2 = s.create_order(req(price="80000", qty="0.01"))
    s.inject("cancel_race_filled", raise_error=True)
    with pytest.raises(RejectedError):
        s.cancel_order(i2.client_order_id)
    assert s.get_order(i2.client_order_id).status is OrderStatus.FILLED


def test_fault_stale_book_network_down_balances_trades_errors():
    s, _, clock = sim()
    s.inject("stale_book", age_s=100)
    assert s.get_order_book(BTC).age(clock.now()) == pytest.approx(100)
    assert s.get_order_book(BTC).age(clock.now()) == pytest.approx(0)
    s.inject("network_down", times=2)
    for _ in range(2):
        with pytest.raises(TransportError):
            s.get_balances()
    s.get_balances()
    s.inject("balances_error")
    with pytest.raises(ServerError):
        s.get_balances()
    s.inject("trades_error")
    with pytest.raises(TransportError):
        s.get_trades()
    with pytest.raises(ValueError):
        s.inject("teleport")
    assert {"post_timeout_after_accept", "post_timeout_before_accept", "post_5xx", "rate_limit",
            "partial_fill", "reject", "get_order_404", "cancel_race_filled", "stale_book",
            "network_down"} <= FAULT_KINDS


def test_alias_ids_and_lookup_modes():
    s, _, _ = sim(echo_client_id=False)
    r = req()
    info = s.create_order(r)
    assert info.client_order_id.startswith("LIMIT-") and info.client_order_id != r.client_id
    assert s.get_order(info.client_order_id) == s.get_order(r.client_id)
    s2, _, _ = sim(echo_client_id=False, lookup_by_client_id=False)
    r2 = req()
    i2 = s2.create_order(r2)
    s2.get_order(i2.client_order_id)
    with pytest.raises(OrderNotFound):
        s2.get_order(r2.client_id)


def test_deterministic_with_seed():
    def run(seed):
        s, src, clock = sim(echo_client_id=False, seed=seed)
        out = []
        for k in range(5):
            out.append(s.create_order(req(price="81060", qty="0.003", cid=f"x{k}")).client_order_id)
            clock.advance(1)
        return out, [(t.price, t.quantity, t.fee) for t in s.get_trades()], s.totals()
    assert run(5) == run(5)
    assert run(5)[0] != run(6)[0]


def test_hidden_fee_and_account_value_and_initial_balance_types():
    s, _, _ = sim({"USDT": Balance("USDT", D("10")), "TMN": D("100000"), "BTC": D(0)})
    s.hidden_fees.add(BTC)
    assert BTC not in s.get_fees()
    v = s.account_value()
    mid_usd = (D("271900") + D("272000")) / 2
    assert D(v["value_mid"]) == D("100000") + D("10") * mid_usd and v["value_asset"] == "TMN"
    with pytest.raises(ValueError):
        sim({"USDT": D("-1")})
    with pytest.raises(ValueError):
        sim(resting_fee="zero")


def test_resting_fills_default_to_taker_rate():
    s, src, _ = sim()
    info = s.create_order(req(price="80000", qty="0.01"))
    src.set_book(BTC, [(D("79000"), D("1"))], [(D("79990"), D("1"))])
    s.get_order(info.client_order_id)
    assert s.get_trades(BTC)[0].fee == D("0.01") * D("0.0035")
