"""agent.features: bucketing, no look-ahead, regime labels, imbalance, sufficiency flag."""
from __future__ import annotations

import math
import random
from decimal import Decimal as D
from pathlib import Path

from wallex_agent.agent.features import WARMUP_MIN, PriceHistory, book_imbalance, compute_features
from wallex_agent.market_data import load_candles, load_depth
from wallex_agent.types import OrderBook

FIX = Path(__file__).parent / "fixtures"
REGIMES = {"trend_up", "trend_down", "chop", "volatile", "quiet", "unknown"}


def _hist(prices, t0=1_000_020.0):
    h = PriceHistory()
    for i, p in enumerate(prices):
        h.add(t0 + 60 * i, p)
    return h


def _info(now, **kw):
    return {"now": now, "time_left_s": 3600.0, "total_s": 86400.0, "max_slippage_bps": 25.0, **kw}


def test_history_buckets_and_backfill_does_not_overwrite():
    h = PriceHistory()
    h.add(120.0, 10.0)
    h.add(150.0, 11.0)              # same minute: last observation wins
    h.add(60.0, 9.0, overwrite=False)
    h.add(130.0, 99.0, overwrite=False)   # backfill never replaces a live observation
    assert h.points() == [(60, 9.0), (120, 11.0)]
    assert h.points(until=100.0) == [(60, 9.0)]


def test_real_candles_features_and_no_lookahead():
    cs = load_candles(FIX / "udf_btcusdt_1m_real.json")
    depth = load_depth(FIX / "depth_btcusdt_real.json", "BTCUSDT", ts=0.0)
    pts = [(float(c.ts + 60), float(c.close)) for c in cs]
    now = pts[200][0]
    f_past = compute_features(pts[:201], {"BTCUSDT": depth}, None, _info(now))
    f_all = compute_features(pts, {"BTCUSDT": depth}, None, _info(now))   # future points present
    assert f_past == f_all
    assert f_past["data_sufficient"] == 1.0 and f_past["regime"] in REGIMES
    assert 0.5 < f_past["vol_1m_bps"] < 30
    assert abs(f_past["spread_bps"] - float(depth.spread_bps)) < 1e-9
    assert -1.0 <= f_past["imbalance"] <= 1.0
    assert 0.0 <= f_past["range_pos_60m"] <= 1.0


def test_short_history_is_unknown_and_insufficient():
    f = compute_features(_hist([100.0] * (WARMUP_MIN - 1)), {}, None, _info(2_000_000.0))
    assert f["regime"] == "unknown" and f["data_sufficient"] == 0.0


def test_regime_labels():
    rng = random.Random(1)
    up = [100 * math.exp(0.0004 * i + rng.gauss(0, 0.0002)) for i in range(90)]
    f = compute_features(_hist(up), {}, None, _info(1e7))
    assert f["regime"] == "trend_up" and f["ret_60m_bps"] > 0
    down = [100 * math.exp(-0.0004 * i + rng.gauss(0, 0.0002)) for i in range(90)]
    assert compute_features(_hist(down), {}, None, _info(1e7))["regime"] == "trend_down"
    chop = [100 * math.exp(rng.gauss(0, 0.0004)) for _ in range(90)]
    assert compute_features(_hist(chop), {}, None, _info(1e7))["regime"] == "chop"
    quiet = [100 * math.exp(rng.gauss(0, 0.00005)) for _ in range(90)]
    assert compute_features(_hist(quiet), {}, None, _info(1e7))["regime"] == "quiet"
    wild = [100 * math.exp(rng.gauss(0, 0.003)) for _ in range(90)]
    assert compute_features(_hist(wild), {}, None, _info(1e7))["regime"] == "volatile"


def test_imbalance_sign():
    bidheavy = OrderBook.build("BTCUSDT", [(D(99), D(10))], [(D(101), D(1))], 0.0)
    askheavy = OrderBook.build("BTCUSDT", [(D(99), D(1))], [(D(101), D(10))], 0.0)
    assert book_imbalance(bidheavy, within_bps=500) > 0.5
    assert book_imbalance(askheavy, within_bps=500) < -0.5


def test_time_left_and_position_features():
    class Pos:
        qty = D("0.001")
        entry_ts = 1e7 - 600
        avg_cost_usdt = D("99")

    book = OrderBook.build("BTCUSDT", [(D(100), D(1))], [(D(101), D(1))], 0.0)
    f = compute_features(_hist([100.0] * 40), {"BTCUSDT": book}, Pos(),
                         {"now": 1e7, "time_left_s": 43200.0, "total_s": 86400.0})
    assert f["time_left_frac"] == 0.5
    assert f["has_position"] == 1.0 and f["position_age_s"] == 600
    assert f["unrealized_gross_bps"] > 0
