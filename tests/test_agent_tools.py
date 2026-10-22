"""agent.tools: each tool's contract, failure capture, cost formula parity with risk.py."""
from __future__ import annotations

from decimal import Decimal as D

from tests._agent_support import BTC, FEES, RULES, T0, USD, obs, prices
from wallex_agent.agent.features import PriceHistory
from wallex_agent.agent.tools import TOOLS, ToolCallLog, ToolContext
from wallex_agent.clock import SimClock
from wallex_agent.config import Config
from wallex_agent.market_data import SyntheticMarket
from wallex_agent.risk import RiskContext, RiskGate
from wallex_agent.types import Balance, BotState, Side, TradeIntent


def ctx_for(o, **kw):
    return ToolContext(o, Config(), PriceHistory(), PriceHistory(), **kw)


def run(name, ctx, **kw):
    return TOOLS[name].run(ctx, lambda: 0.0, **kw)


def test_read_book_fresh_and_stale():
    o = obs(T0, 81000.0)
    r = run("read_book", ctx_for(o))
    assert r.ok and r.data[BTC]["bid"] < r.data[BTC]["ask"] and not r.data["stale"]
    stale = obs(T0, 81000.0, book_ts=T0 - 60)
    r2 = run("read_book", ctx_for(stale))
    assert not r2.ok and "age" in r2.error


def test_round_trip_cost_matches_risk_gate_formula():
    o = obs(T0, 81000.0)
    r = run("estimate_round_trip_cost", ctx_for(o), notional_usdt=D("4.5"))
    assert r.ok and r.data["complete"]
    d = r.data
    book = o.books[BTC]
    fee = FEES[BTC].taker
    # independent recomputation exactly like risk.RiskGate (walk_qty both sides + both taker fees)
    buy, sell = book.walk_qty(Side.BUY, d["qty"]), book.walk_qty(Side.SELL, d["qty"])
    expect = (buy.avg_price - sell.avg_price) / book.mid * 10000 + 2 * fee * 10000
    assert abs(d["cost_bps"] - expect) < D("1e-9")
    assert d["cost_bps"] >= 2 * fee * 10000 + book.spread_bps - D("1e-9")
    # risk.py reports the same cost for the same size
    intent = TradeIntent("entry", Side.BUY, BTC, RULES[BTC].round_qty(d["qty"]), book.best_ask, D("20"))
    ctx = RiskContext(state=BotState.READY, balances=o.balances, fees=FEES, rules=RULES, books=o.books, now=T0,
                      equity_tmn=D(1), peak_equity_tmn=D(1), expected_move_bps=D("500"), confidence=0.9)
    v = RiskGate(Config()).evaluate(intent, ctx)
    assert abs(D(v.metrics["round_trip_cost_bps"]) - d["cost_bps"]) < D("0.05")


def test_round_trip_cost_grows_with_size_and_flags_incomplete():
    o = obs(T0, 81000.0)
    small = run("estimate_round_trip_cost", ctx_for(o), notional_usdt=D("5")).data
    big = run("estimate_round_trip_cost", ctx_for(o), notional_usdt=D("20000")).data
    huge = run("estimate_round_trip_cost", ctx_for(o), notional_usdt=D("1e9")).data
    assert big["cost_bps"] > small["cost_bps"]
    assert not huge["complete"]
    ex = run("estimate_round_trip_cost", ctx_for(o), qty=D("0.0001"), leg="exit").data
    assert ex["leg"] == "exit" and ex["cost_bps"] > FEES[BTC].taker * 10000


def test_tool_failures_are_captured_not_raised():
    o = obs(T0, 81000.0, fees={})
    r = run("estimate_round_trip_cost", ctx_for(o), notional_usdt=D(5))
    assert not r.ok and "fee unknown" in r.error
    r2 = run("read_candles", ctx_for(o))
    assert not r2.ok and "no candle source" in r2.error


def test_fx_check_flags_abnormal_spread_vs_history():
    normal = obs(T0, 81000.0, fx_half_spread=0.8)
    r = run("fx_check", ctx_for(normal, fx_spreads=[1.6] * 50), tmn_amount=D(5000000))
    assert r.ok and not r.data["abnormal"] and r.data["complete"]
    assert r.data["cost_bps"] >= r.data["fee_bps"]
    wide = obs(T0, 81000.0, fx_half_spread=12.0)
    r2 = run("fx_check", ctx_for(wide, fx_spreads=[1.6] * 50), tmn_amount=D(5000000))
    assert r2.ok and r2.data["abnormal"]


def test_read_candles_backfills_without_lookahead():
    m = SyntheticMarket(seed=2, n_steps=30, history_steps=120)
    clock = SimClock()
    src = m.build_source(clock)
    o = obs(clock.now(), 81000.0)
    c = ctx_for(o, book_source=src)
    r = run("read_candles", c, minutes=90)
    assert r.ok and r.data["points_added"] >= 85
    assert all(ts <= clock.now() for ts, _ in c.history.points())


def test_signal_tools_and_headroom():
    o = obs(T0, 81000.0, usdt="18", btc="0.00005")
    c = ctx_for(o)
    from wallex_agent.agent.features import compute_features
    from tests._agent_support import history
    c.features = compute_features(history(prices(90, bps_per_min=4.0, noise_bps=2.0), T0), o.books, None,
                                  {"now": T0, "time_left_s": 43200.0, "total_s": 86400.0})
    t = run("trend_signal", c).data["hypothesis"]
    assert t.name == "trend_follow" and t.direction == "up" and t.expected_move_bps > 0
    assert "price_below" in t.invalidation
    h = run("risk_headroom", c).data
    assert h["round_trips_left"] == Config().max_round_trips
    assert h["equity_usdt"] > D(18) and h["exposure_room_usdt"] < h["equity_usdt"]


def test_call_log_records_why_latency_and_skips():
    o = obs(T0, 81000.0)
    ticks = iter([0.0, 0.002])
    log = ToolCallLog(ctx_for(o), timer=lambda: next(ticks))
    log.call("read_book", "observe")
    log.skip("read_candles", "history sufficient")
    assert log.calls[0]["tool"] == "read_book" and log.calls[0]["why"] == "observe"
    assert log.calls[0]["latency_ms"] == 2.0 and log.calls[0]["ok"]
    assert log.calls[1] == {"tool": "read_candles", "called": False, "why": "history sufficient", "cost": 3}
    assert log.effort == 1
    assert isinstance(Balance("X", D(1)).spendable, D) and USD in o.books
