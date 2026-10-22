"""market_data: freshness, candle IO, replay (no look-ahead), live stamping, seeded synthetic market."""
from __future__ import annotations

import json
import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from wallex_agent.clock import SimClock
from wallex_agent.errors import TransportError
from wallex_agent.interfaces import BookSource
from wallex_agent.market_data import (SYNTHETIC_LABEL, DepthTemplate, LiveBookSource, ReplayBookSource,
                                      StaleQuote, SyntheticMarket, candles_to_books, ensure_fresh, load_candles,
                                      load_depth, save_candles)
from wallex_agent.types import Candle, OrderBook, Side

FIX = Path(__file__).parent / "fixtures"


def _book(ts: float, mid: str = "100") -> OrderBook:
    m = D(mid)
    return OrderBook.build("BTCUSDT", [(m - 1, D(1))], [(m + 1, D(1))], ts)


def test_ensure_fresh():
    b = _book(100.0)
    ensure_fresh(b, 110.0, 15.0)
    with pytest.raises(StaleQuote):
        ensure_fresh(b, 116.0, 15.0)
    with pytest.raises(StaleQuote):
        ensure_fresh(None, 100.0, 15.0)
    with pytest.raises(StaleQuote):
        ensure_fresh(b, 90.0, 15.0)   # stamped in the future


def test_load_real_udf_and_roundtrip_csv_json(tmp_path):
    cs = load_candles(FIX / "udf_btcusdt_1m_real.json")
    assert len(cs) == 359
    assert all(isinstance(c.close, D) for c in cs)
    assert [c.ts for c in cs] == sorted(c.ts for c in cs)
    for name in ("c.csv", "c.json"):
        p = save_candles(tmp_path / name, cs)
        assert load_candles(p) == cs
    (tmp_path / "empty.json").write_text(json.dumps({"s": "no_data"}), encoding="utf-8")
    assert load_candles(tmp_path / "empty.json") == []


def test_load_csv_with_header_and_duplicates(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("time,open,high,low,close,volume\n120,1,2,0.5,1.5,3\n60,1,1,1,1,1\n120,1,2,0.5,1.6,3\n",
                 encoding="utf-8")
    cs = load_candles(p)
    assert [c.ts for c in cs] == [60, 120]
    assert cs[1].close == D("1.6")


def test_candles_to_books_uses_real_depth_shape():
    depth = load_depth(FIX / "depth_btcusdt_real.json", "BTCUSDT", ts=0.0)
    cs = load_candles(FIX / "udf_btcusdt_1m_real.json")[:20]
    books = candles_to_books(cs, depth)
    assert len(books) == 20
    for c, b in zip(cs, books):
        assert b.ts == c.ts + 60          # stamped at candle close: never earlier than the price
        assert abs(b.mid - c.close) / c.close < D("0.0005")
        assert b.best_bid < b.best_ask
        assert len(b.bids) == len(depth.bids) and len(b.asks) == len(depth.asks)
    assert abs(books[0].spread_bps - depth.spread_bps) < D("1")


def test_replay_no_lookahead_and_advance():
    clock = SimClock(1000.0)
    series = {"BTCUSDT": [_book(1000.0, "100"), _book(1060.0, "101"), _book(1120.0, "102")]}
    candles = {"BTCUSDT": [Candle(940, D(99), D(100), D(99), D(100), D(1)),
                           Candle(1000, D(100), D(101), D(100), D(101), D(1)),
                           Candle(1060, D(101), D(102), D(101), D(102), D(1))]}
    src = ReplayBookSource(series, clock, candles=candles)
    assert isinstance(src, BookSource)
    assert src.get_book("BTCUSDT").mid == D(100)
    assert [c.ts for c in src.get_candles("BTCUSDT", "1", 0, 10_000)] == [940]   # only closed candles
    clock.advance(59)
    assert src.get_book("BTCUSDT").mid == D(100)                                 # next book not yet
    assert src.advance() and clock.now() == 1060.0
    assert src.get_book("BTCUSDT").mid == D(101)
    assert src.advance() and not src.advance()
    assert src.exhausted
    src2 = ReplayBookSource(series, SimClock(999.0))
    with pytest.raises(TransportError):
        src2.get_book("BTCUSDT")
    src3 = ReplayBookSource(series, SimClock(1100.0), stamp_now=False)
    assert src3.get_book("BTCUSDT").ts == 1060.0


def test_live_source_stamps_receipt_time():
    class FakeClient:
        def get_order_book(self, symbol):
            return _book(0.0)

        def get_candles(self, symbol, resolution, start, end):
            return [Candle(start, D(1), D(1), D(1), D(1), D(1))]

    clock = SimClock(5000.0)
    src = LiveBookSource(FakeClient(), clock)
    assert src.get_book("BTCUSDT").ts == 5000.0
    assert src.get_candles("BTCUSDT", "1", 10, 20)[0].ts == 10


def test_synthetic_deterministic_and_seed_sensitive():
    a, b, c = SyntheticMarket(seed=3, n_steps=200), SyntheticMarket(seed=3, n_steps=200), SyntheticMarket(seed=4, n_steps=200)
    assert a.btc_mid == b.btc_mid and a.fx_mid == b.fx_mid
    assert a.btc_mid != c.btc_mid
    assert a.books("BTCUSDT")[17] == b.books("BTCUSDT")[17]
    assert a.label == SYNTHETIC_LABEL and "SYNTHETIC" in a.label


def test_synthetic_24h_is_fast_and_plausible():
    t0 = time.perf_counter()
    m = SyntheticMarket(seed=11, n_steps=1440, step_s=60)
    clock = SimClock()
    src = m.build_source(clock)
    assert clock.now() == m.start_ts
    spreads, fx_spreads = [], []
    steps = 0
    while True:
        bb, fb = src.get_book("BTCUSDT"), src.get_book("USDTTMN")
        spreads.append(float(bb.spread_bps))
        fx_spreads.append(float(fb.spread_bps))
        steps += 1
        if not src.advance():
            break
    elapsed = time.perf_counter() - t0
    assert steps == 1441
    assert elapsed < 5.0, elapsed
    assert 4.0 <= sorted(spreads)[len(spreads) // 2] <= 10.5          # ~5-10 bps like the real capture
    assert sorted(fx_spreads)[len(fx_spreads) // 2] < 4.0              # USDTTMN normally tight
    assert 1e3 < float(m.btc_mid[-1]) < 1e6 and 1e5 < m.fx_mid[-1] < 1e6
    assert abs(m.fx_mid[-1] / m.fx_mid[0] - 1) < 0.02                  # small drift only
    bb = src.get_book("BTCUSDT")
    assert len(bb.bids) == 20 and len(bb.asks) == 20
    assert bb.depth_quote(Side.BUY, D("1000")) > D("1000")             # USDT-scale depth like the fixture


def test_synthetic_schedule_history_and_candles_no_lookahead():
    m = SyntheticMarket(seed=1, n_steps=100, regime_schedule=[("trend_up", 50), ("low_vol", 10)], history_steps=60)
    assert m.regime_at(m.time_at(0)) == "trend_up"
    assert m.regime_at(m.time_at(99)) == "low_vol"
    clock = SimClock()
    src = m.build_source(clock)
    pre = src.get_candles("BTCUSDT", "1", int(m.start_ts) - 3600, int(m.start_ts))
    assert len(pre) == 60 and all(c.ts + 60 <= clock.now() for c in pre)   # history exists before start
    with pytest.raises(ValueError):
        SyntheticMarket(seed=1, n_steps=5, regime_schedule=[("moon", 5)])


def test_depth_template_from_real_book():
    depth = load_depth(FIX / "depth_usdttmn_real.json", "USDTTMN")
    t = DepthTemplate.from_book(depth)
    assert len(t.bid) == 50 and t.bid[0][0] < 1.0
