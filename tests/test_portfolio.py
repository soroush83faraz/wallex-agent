import itertools
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from wallex_agent.clock import SimClock
from wallex_agent.errors import InvariantError
from wallex_agent.portfolio import Portfolio
from wallex_agent.store import FillRecord, InMemoryStore, OrderRecord, SqliteStore
from wallex_agent.types import (Balance, FeeRates, MarketRules, OrderBook, OrderLifecycle as L, Side,
                                quantize_down)

FIX = Path(__file__).parent / "fixtures"
INITIAL = D("5000000")
FEES = {"BTCUSDT": FeeRates("BTCUSDT", D("0.002"), D("0.0035")),
        "USDTTMN": FeeRates("USDTTMN", D("0.002"), D("0.0035"))}


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"), parse_float=D)


def real_rules():
    syms = _load("markets_real_2026-10-22.json")["result"]["symbols"]
    return {s: MarketRules(s, m["baseAsset"], m["quoteAsset"], int(m["stepSize"]), int(m["tickSize"]),
                           D(str(m["minQty"])), D(str(m["minNotional"])))
            for s, m in syms.items() if s in ("BTCUSDT", "USDTTMN")}


def real_book(symbol, fname):
    r = _load(fname)["result"]
    lv = lambda side: [(D(str(x["price"])), D(str(x["quantity"]))) for x in r[side]]
    return OrderBook.build(symbol, lv("bid"), lv("ask"), 0.0)


RULES = real_rules()
BOOKS = {"BTCUSDT": real_book("BTCUSDT", "depth_btcusdt_real.json"),
         "USDTTMN": real_book("USDTTMN", "depth_usdttmn_real.json")}
_seq = itertools.count()


def close(a, b, eps=D("1e-30")):
    return abs(a - b) <= eps


def trade(pf, symbol, side, price, qty, fee, fee_asset, ts=1.0, *, via_store=False):
    """Persist an order like execution does, then record its fill."""
    n = next(_seq)
    cid = f"wa-{n}"
    st = pf._store
    with st.transaction():
        st.insert_order_intent(OrderRecord(client_id=cid, symbol=symbol, side=side, price=D(price),
                                           quantity=D(qty), purpose="test"))
        st.update_order(cid, lifecycle=L.SENDING)
    f = FillRecord(f"fill-{n}", cid, symbol, side, D(price), D(qty), D(price) * D(qty), D(fee), fee_asset,
                   "exchange", ts)
    if via_store:
        assert st.add_fill(f)
    else:
        assert pf.apply_fill(f) is True
    st.update_order(cid, lifecycle=L.FILLED, executed_qty=D(qty), executed_sum=f.quote_sum)
    return f


def convert(pf, ts=1.0):   # 18.30 USDT @ 272,400 TMN, fee 0.35% in USDT (base)
    return trade(pf, "USDTTMN", Side.BUY, "272400", "18.30", "0.06405", "USDT", ts)


def new_pf(**kw):
    return Portfolio(InMemoryStore(clock=SimClock()), INITIAL, **kw)


# --------------------------------------------------------------------------- ledger
def test_conversion_buy_with_base_fee_then_sell_with_quote_fee():
    pf = new_pf()
    convert(pf)
    assert pf.expected_balances() == {"TMN": D("15080"), "USDT": D("18.23595"), "BTC": D(0)}
    trade(pf, "BTCUSDT", Side.BUY, "81000", "0.00022", "0.00000077", "BTC", ts=100.0)
    assert pf.expected_balances() == {"TMN": D("15080"), "USDT": D("0.41595"), "BTC": D("0.00021923")}
    pos = pf.btc_position()
    assert pos.qty == D("0.00021923") and pos.cost_usdt == D("17.82") and pos.entry_ts == 100.0
    assert pos.avg_cost_usdt == D("17.82") / D("0.00021923")      # fee-inclusive (fewer coins received)
    assert pos.round_trips == 0 and pos.realized_pnl_usdt == 0

    trade(pf, "BTCUSDT", Side.SELL, "82000", "0.00021923", "0.06291901", "USDT", ts=200.0)
    bal = pf.expected_balances()
    assert bal == {"TMN": D("15080"), "USDT": D("18.32989099"), "BTC": D(0)}
    pos = pf.btc_position()
    assert pos.qty == 0 and pos.avg_cost_usdt == 0 and pos.entry_ts is None
    assert pos.realized_pnl_usdt == D("17.97686") - D("0.06291901") - D("17.82") == D("0.09394099")
    assert (pos.round_trips, pos.wins, pos.losses) == (1, 1, 0)
    basis = D("4984920") / D("18.23595")
    assert pf.usdt_cost_basis_tmn() == basis and pos.realized_pnl_tmn == pos.realized_pnl_usdt * basis
    assert pf.fees_paid() == {"USDT": D("0.06405") + D("0.06291901"), "BTC": D("0.00000077")}
    assert pf.anomalies() == []


def test_partial_exits_and_losing_round_trip():
    pf = new_pf()
    convert(pf)
    trade(pf, "BTCUSDT", Side.BUY, "80000", "0.0002", "0.0000007", "BTC", ts=10.0)       # qty 0.0001993, cost 16
    trade(pf, "BTCUSDT", Side.SELL, "81000", "0.0001", "0.02835", "USDT", ts=20.0)
    pos = pf.btc_position()
    cost_out = D(16) * D("0.0001") / D("0.0001993")
    assert pos.qty == D("0.0000993") and close(pos.cost_usdt, D(16) - cost_out)
    assert close(pos.realized_pnl_usdt, D("8.07165") - cost_out) and pos.realized_pnl_usdt > 0
    assert pos.round_trips == 0 and pos.entry_ts == 10.0 and pos.open_trip_pnl_usdt == pos.realized_pnl_usdt
    assert close(pos.avg_cost_usdt, D(16) / D("0.0001993"))                             # unchanged by exits

    trade(pf, "BTCUSDT", Side.SELL, "78000", "0.0000993", "0.0271089", "USDT", ts=30.0)
    pos = pf.btc_position()
    assert pos.qty == 0 and close(pos.cost_usdt, D(0))
    assert close(pos.realized_pnl_usdt, D("8.07165") + D("7.7182911") - D(16))           # -0.2100589
    assert (pos.round_trips, pos.wins, pos.losses) == (1, 0, 1)
    assert pos.open_trip_pnl_usdt == 0 and pos.entry_ts is None


def test_dust_remainder_closes_round_trip_and_carries_cost():
    pf = new_pf(rules=RULES)
    convert(pf)
    trade(pf, "BTCUSDT", Side.BUY, "81000", "0.00022", "0.00000077", "BTC", ts=1.0)
    trade(pf, "BTCUSDT", Side.SELL, "81500", "0.0002192", "0.0625", "USDT", ts=2.0)
    pos = pf.btc_position()
    assert pos.qty == D("0.00000003") and pos.round_trips == 1 and pos.entry_ts is None
    assert close(pos.cost_usdt, D("17.82") * D("0.00000003") / D("0.00021923"))
    trade(pf, "BTCUSDT", Side.BUY, "80000", "0.0001", "0.00000035", "BTC", ts=3.0)
    pos = pf.btc_position()
    assert pos.entry_ts == 3.0 and pos.qty == D("0.00009968") and pos.round_trips == 1


def test_sell_fee_in_base_is_consumed_from_position():
    pf = new_pf()
    convert(pf)
    trade(pf, "BTCUSDT", Side.BUY, "80000", "0.0002", "0.056", "USDT")                 # fee in quote
    pos = pf.btc_position()
    assert pos.qty == D("0.0002") and pos.cost_usdt == D("16.056")
    trade(pf, "BTCUSDT", Side.SELL, "81000", "0.0001", "0.00000035", "BTC")
    pos = pf.btc_position()
    assert pf.expected_balances()["BTC"] == pos.qty == D("0.00009965")
    assert close(pos.realized_pnl_usdt, D("8.1") - D("16.056") * D("0.00010035") / D("0.0002"))


def test_fee_in_other_asset_is_deducted_from_that_asset():
    pf = new_pf()
    convert(pf)
    trade(pf, "BTCUSDT", Side.BUY, "81000", "0.0002", "1000", "TMN")
    bal = pf.expected_balances()
    assert bal["TMN"] == D("14080") and bal["BTC"] == D("0.0002") and bal["USDT"] == D("18.23595") - D("16.2")
    pos = pf.btc_position()
    assert pos.cost_usdt == D("16.2") and pos.untracked_fee_assets == ("TMN",)
    trade(pf, "BTCUSDT", Side.SELL, "81000", "0.0001", "5", "WXT")
    assert pf.expected_balances()["WXT"] == D("-5")
    assert pf.fees_paid()["WXT"] == D(5)


def test_apply_fill_is_idempotent_and_store_recorded_fills_are_picked_up():
    pf = new_pf()
    f = convert(pf)
    snapshot = pf.expected_balances()
    assert pf.apply_fill(f) is False and pf.expected_balances() == snapshot
    trade(pf, "BTCUSDT", Side.BUY, "81000", "0.0001", "0.00000035", "BTC", via_store=True)   # execution path
    assert pf.expected_balances()["BTC"] == D("0.00009965")
    assert [x.fill_key for x in pf.fills()][-1] == f"fill-{next(_seq) - 1}"


def test_sell_exceeding_position_and_negative_ledger_are_flagged():
    pf = new_pf()
    trade(pf, "BTCUSDT", Side.BUY, "81000", "0.0001", "0.00000035", "BTC")   # no USDT in the ledger!
    assert pf.expected_balances()["USDT"] < 0
    assert any("negative USDT" in a for a in pf.anomalies())
    rep = pf.reconcile({"TMN": Balance("TMN", INITIAL), "BTC": Balance("BTC", D("0.00009965"))}, {})
    assert not rep.ok and any("negative" in r for r in rep.reasons)
    assert pf.liquidation_value(BOOKS, FEES).equity_tmn_net is None
    pf2 = new_pf()
    convert(pf2)
    trade(pf2, "BTCUSDT", Side.BUY, "81000", "0.0001", "0.00000035", "BTC")
    trade(pf2, "BTCUSDT", Side.SELL, "81000", "0.0001", "0.01", "USDT")      # more than the 0.00009965 held
    assert any("exceeds position" in a for a in pf2.anomalies())
    assert pf2.btc_position().qty == 0


def test_unknown_symbol_fill_is_refused():
    st = InMemoryStore(clock=SimClock())
    pf = Portfolio(st, INITIAL)
    st.insert_order_intent(OrderRecord(client_id="e", symbol="ETHUSDT", side=Side.BUY, price=D(1),
                                       quantity=D(1), purpose="x"))
    st.update_order("e", lifecycle=L.SENDING)
    with pytest.raises(InvariantError):
        pf.apply_fill(FillRecord("k", "e", "ETHUSDT", Side.BUY, D(1), D(1), D(1), D(0), "ETH", "exchange", 1.0))


# --------------------------------------------------------------------------- persistence
def test_everything_rebuilds_from_store_after_restart(tmp_path):
    p = tmp_path / "db.sqlite"
    with SqliteStore(p) as st:
        pf = Portfolio(st, INITIAL, rules=RULES)
        convert(pf)
        trade(pf, "BTCUSDT", Side.BUY, "80000", "0.0002", "0.0000007", "BTC", ts=10.0)
        trade(pf, "BTCUSDT", Side.SELL, "81000", "0.0001", "0.02835", "USDT", ts=20.0)
        pf.record_equity(30.0, D("4990000.5"))
        pf.record_equity(40.0, D("5010000"))
        pf.record_equity(50.0, D("4950000"))
        pf.record_conversion(D("4984920"), D("18.23595"), ts=1.0, note="initial")
        before = (pf.expected_balances(), pf.btc_position(), pf.equity_curve(), pf.peak_equity(),
                  pf.drawdown(), pf.fees_paid(), pf.usdt_cost_basis_tmn())
    with SqliteStore(p) as st:
        pf = Portfolio(st, INITIAL, rules=RULES)
        after = (pf.expected_balances(), pf.btc_position(), pf.equity_curve(), pf.peak_equity(),
                 pf.drawdown(), pf.fees_paid(), pf.usdt_cost_basis_tmn())
        assert after == before
        assert pf.peak_equity() == D("5010000") and pf.drawdown() == D("60000") / D("5010000")
        with pytest.raises(InvariantError):
            Portfolio(st, D("6000000"))                   # silently changing the budget is refused


def test_rolled_back_fill_and_equity_are_not_kept_in_memory():
    pf = new_pf()
    convert(pf)
    before = pf.expected_balances()
    st = pf._store
    with pytest.raises(RuntimeError):
        with st.transaction():
            trade(pf, "BTCUSDT", Side.BUY, "81000", "0.0001", "0.00000035", "BTC")
            pf.record_equity(5.0, D("1"))
            assert pf.expected_balances()["BTC"] == D("0.00009965")   # provisional view
            raise RuntimeError("crash before commit")
    assert pf.expected_balances() == before and pf.btc_position().qty == 0
    assert pf.equity_curve() == [] and pf.drawdown() == 0
    trade(pf, "BTCUSDT", Side.BUY, "81000", "0.0002", "0.0000007", "BTC")      # a different fill reuses seq
    assert pf.expected_balances()["BTC"] == D("0.0001993")


def test_equity_peak_and_drawdown():
    pf = new_pf()
    assert pf.peak_equity() == INITIAL and pf.drawdown() == 0 and pf.equity_curve() == []
    pf.record_equity(1.0, D("4900000"))
    assert pf.peak_equity() == INITIAL and pf.drawdown() == D("0.02")    # initial capital is the baseline
    pf.record_equity(2.0, D("5100000"))
    pf.record_equity(3.0, D("4998000"))
    assert pf.peak_equity() == D("5100000") and pf.drawdown() == D("102000") / D("5100000")
    assert pf.equity_curve() == [(1.0, D("4900000")), (2.0, D("5100000")), (3.0, D("4998000"))]
    with pytest.raises(TypeError):
        pf.record_equity(4.0, None)
    with pytest.raises(TypeError):
        pf.record_equity(4.0, 4998000.0)


def test_conversion_metadata_overrides_fill_derived_basis():
    pf = new_pf()
    assert pf.usdt_cost_basis_tmn() is None and pf.btc_position().realized_pnl_tmn is None
    convert(pf)
    assert pf.usdt_cost_basis_tmn() == D("4984920") / D("18.23595")
    pf.record_conversion(D("5000000"), D("18"))
    assert pf.usdt_cost_basis_tmn() == D("5000000") / D("18")
    with pytest.raises(ValueError):
        pf.record_conversion(D("0"), D("1"))


# --------------------------------------------------------------------------- reconcile
def held_pf():
    pf = new_pf()
    convert(pf)
    trade(pf, "BTCUSDT", Side.BUY, "81000", "0.00022", "0.00000077", "BTC")
    return pf   # TMN 15080, USDT 0.41595, BTC 0.00021923


def actual(tmn="15080", usdt="0.41595", btc="0.00021923", usdt_locked="0", **extra):
    out = {"TMN": Balance("TMN", D(tmn)), "USDT": Balance("USDT", D(usdt), D(usdt_locked)),
           "BTC": Balance("BTC", D(btc))}
    out.update({k: Balance(k, D(v)) for k, v in extra.items()})
    return out


def test_reconcile_exact_match():
    rep = held_pf().reconcile(actual(), {})
    assert rep.ok and rep.reasons == [] and all(d.direction == "match" for d in rep.drifts.values())


def test_reconcile_shortfall_is_dangerous():
    pf = held_pf()
    rep = pf.reconcile(actual(usdt="0.40"), {"USDT": D("0.01")})
    assert not rep.ok and rep.drifts["USDT"].direction == "short" and rep.drifts["USDT"].drift == D("-0.01595")
    assert "USDT" in rep.reasons[0]
    assert pf.reconcile(actual(usdt="0.40"), {"USDT": D("0.02")}).ok
    rep = pf.reconcile({"TMN": Balance("TMN", D("15080")), "USDT": Balance("USDT", D("0.41595"))}, {})
    assert not rep.ok and rep.drifts["BTC"].direction == "short"           # BTC missing entirely


def test_reconcile_excess_is_reported_not_fatal_unless_strict():
    pf = held_pf()
    rep = pf.reconcile(actual(tmn="20015080", ETH="1"), {})
    assert rep.ok and rep.drifts["TMN"].direction == "excess" and len(rep.excess) == 2
    assert not pf.reconcile(actual(tmn="20015080"), {}, strict_excess=True).ok


def test_reconcile_locked_semantics():
    pf = held_pf()
    a = actual(usdt="0.1", usdt_locked="0.31595")
    rep = pf.reconcile(a, {})
    assert rep.ok and rep.drifts["USDT"].interpretation == "value+locked"
    assert not pf.reconcile(a, {}, locked_semantics="value_includes_locked").ok
    assert pf.reconcile(a, {}, locked_semantics="value_excludes_locked").ok
    with pytest.raises(ValueError):
        pf.reconcile(a, {}, locked_semantics="guess")


# --------------------------------------------------------------------------- liquidation value
def expected_net(btc, usdt, tmn, fee=D("0.0035")):
    bq = RULES["BTCUSDT"].round_qty(btc)
    btc_net = BOOKS["BTCUSDT"].walk_qty(Side.SELL, bq).quote_sum * (1 - fee) if bq > 0 else D(0)
    uq = quantize_down(usdt + btc_net, 2)
    return tmn + BOOKS["USDTTMN"].walk_qty(Side.SELL, uq).quote_sum * (1 - fee)


def test_liquidation_value_walks_both_books_net_of_fees():
    pf = held_pf()
    rep = pf.liquidation_value(BOOKS, FEES, RULES)
    assert rep.ok and rep.flags == ()
    assert rep.equity_tmn_net == expected_net(D("0.00021923"), D("0.41595"), D("15080"))
    mid = (D("0.00021923") * BOOKS["BTCUSDT"].mid * BOOKS["USDTTMN"].mid
           + D("0.41595") * BOOKS["USDTTMN"].mid + D("15080"))
    assert rep.equity_tmn_mid == mid
    assert rep.equity_tmn_net < rep.equity_tmn_mid
    assert D("4850000") < rep.equity_tmn_net < INITIAL                   # exit costs really deducted
    assert rep.breakdown["btc_fee_rate"] == D("0.0035") and rep.breakdown["usdt_sell_qty"] == quantize_down(
        rep.breakdown["usdt_after_btc_liquidation"], 2)
    as_decimal = pf.liquidation_value(BOOKS, {k: v.taker for k, v in FEES.items()}, RULES)
    assert as_decimal.equity_tmn_net == rep.equity_tmn_net


def test_liquidation_value_unknown_fee_is_flagged_not_assumed():
    pf = held_pf()
    rep = pf.liquidation_value(BOOKS, {"USDTTMN": FEES["USDTTMN"]}, RULES)
    assert rep.equity_tmn_net is None and not rep.ok and "unknown_fee:BTCUSDT" in rep.flags
    assert rep.equity_tmn_mid is not None
    rep = pf.liquidation_value(BOOKS, None, RULES)
    assert rep.equity_tmn_net is None and {"unknown_fee:BTCUSDT", "unknown_fee:USDTTMN"} <= set(rep.flags)
    flat = new_pf()
    convert(flat)                                    # no BTC: the BTCUSDT fee is not needed
    rep = flat.liquidation_value(BOOKS, {"USDTTMN": FEES["USDTTMN"]}, RULES)
    assert rep.ok and rep.equity_tmn_net == expected_net(D(0), D("18.23595"), D("15080"))


def test_liquidation_value_missing_book_and_thin_depth():
    pf = held_pf()
    rep = pf.liquidation_value({"BTCUSDT": BOOKS["BTCUSDT"]}, FEES, RULES)
    assert rep.equity_tmn_net is None and rep.equity_tmn_mid is None and "missing_book:USDTTMN" in rep.flags
    thin = OrderBook.build("BTCUSDT", [(D("81000"), D("0.0001"))], [(D("81100"), D("1"))], 0.0)
    rep = pf.liquidation_value({"BTCUSDT": thin, "USDTTMN": BOOKS["USDTTMN"]}, FEES, RULES)
    assert not rep.ok and "insufficient_depth:BTCUSDT" in rep.flags
    assert rep.breakdown["btc_gross"] == D("8.1")                        # lower bound: unfilled part = 0


def test_liquidation_value_dust_is_worthless_with_rules():
    pf = new_pf()
    trade(pf, "USDTTMN", Side.BUY, "272400", "0.12", "0.00042", "USDT")   # 0.11958 USDT < 50,000 TMN notional
    rep = pf.liquidation_value(BOOKS, FEES, RULES)
    assert "dust:USDT" in rep.flags and rep.equity_tmn_net == INITIAL - D("272400") * D("0.12")
    no_rules = pf.liquidation_value(BOOKS, FEES)
    assert "dust:USDT" not in no_rules.flags and no_rules.equity_tmn_net > rep.equity_tmn_net
