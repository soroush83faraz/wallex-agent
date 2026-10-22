"""State features for the planner (floats: analytics only, never money).

Prices are bucketed to 1-minute closes (``PriceHistory``) so features do not depend on the loop
interval. No look-ahead: only points with ``ts <= clock_info['now']`` are used.

Regime rule (fixed priors, documented, not fitted):
  * fewer than ``WARMUP_MIN`` 1-min points                -> ``unknown``
  * 1-min realised vol > ``VOLATILE_BPS``                   -> ``volatile``
  * 1-min realised vol < ``QUIET_BPS``                      -> ``quiet``
  * efficiency ratio(30m) >= ``TREND_ER`` and |ret30| > 1.5 * vol * sqrt(30) -> ``trend_up/down``
  * otherwise                                               -> ``chop``
"""
from __future__ import annotations

import bisect
import math
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from ..types import OrderBook, Side

WARMUP_MIN = 30          # 1-min points needed before any entry is considered
VOLATILE_BPS = 12.0
QUIET_BPS = 1.5
TREND_ER = 0.30
NEAR_BOOK_BPS = 25.0     # imbalance looks at liquidity within this distance of the touch
MAX_POINTS = 600


class PriceHistory:
    """1-minute bucketed closes: last observation inside a minute wins; backfill fills gaps only."""

    def __init__(self, bucket_s: int = 60, max_points: int = MAX_POINTS) -> None:
        self.bucket_s = bucket_s
        self.max_points = max_points
        self._keys: list[int] = []
        self._vals: list[float] = []

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, ts: float, price: float, *, overwrite: bool = True) -> None:
        if not (price > 0 and math.isfinite(price)):
            return
        k = int(ts // self.bucket_s)
        i = bisect.bisect_left(self._keys, k)
        if i < len(self._keys) and self._keys[i] == k:
            if overwrite:
                self._vals[i] = price
            return
        self._keys.insert(i, k)
        self._vals.insert(i, price)
        if len(self._keys) > self.max_points:
            del self._keys[0], self._vals[0]

    def points(self, until: float | None = None) -> list[tuple[float, float]]:
        hi = len(self._keys) if until is None else bisect.bisect_right(self._keys, int(until // self.bucket_s))
        return [(k * self.bucket_s, v) for k, v in zip(self._keys[:hi], self._vals[:hi])]

    def last(self) -> tuple[float, float] | None:
        return (self._keys[-1] * self.bucket_s, self._vals[-1]) if self._keys else None


def _ret_bps(prices: Sequence[float], n: int) -> float | None:
    if len(prices) <= n:
        return None
    return (prices[-1] / prices[-1 - n] - 1.0) * 10000.0


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def book_imbalance(book: OrderBook, within_bps: float = NEAR_BOOK_BPS) -> float:
    """Depth-weighted (bid - ask)/(bid + ask) of quote notional within ``within_bps`` of mid; each
    level weighted by 1/(1 + distance_bps/5) so the touch matters most. In [-1, 1]."""
    mid = float(book.mid)

    def side(levels: Iterable[Any]) -> float:
        tot = 0.0
        for lv in levels:
            d = abs(float(lv.price) - mid) / mid * 10000.0
            if d > within_bps:
                break
            tot += float(lv.price * lv.quantity) / (1.0 + d / 5.0)
        return tot

    b, a = side(book.bids), side(book.asks)
    return 0.0 if b + a <= 0 else (b - a) / (b + a)


def compute_features(history: Sequence[tuple[float, float]] | PriceHistory,
                     books: Mapping[str, OrderBook], position: Any,
                     clock_info: Mapping[str, Any]) -> dict[str, float | str]:
    """Features at ``clock_info['now']``. ``history``: 1-min (ts, BTC mid) points (or a
    ``PriceHistory``); ``clock_info`` keys: ``now``, ``time_left_s``, ``total_s``,
    ``max_slippage_bps``, optional ``fx_history`` (points), ``btc_symbol``, ``usdt_symbol``."""
    now = float(clock_info["now"])
    pts = history.points(until=now) if isinstance(history, PriceHistory) else \
        [p for p in history if p[0] <= now]
    prices = [p for _, p in pts]
    n = len(prices)
    btc_sym = clock_info.get("btc_symbol", "BTCUSDT")
    usd_sym = clock_info.get("usdt_symbol", "USDTTMN")
    f: dict[str, float | str] = {"n_points": float(n), "data_sufficient": 1.0 if n >= WARMUP_MIN else 0.0}

    rets = [(prices[i] / prices[i - 1] - 1.0) * 10000.0 for i in range(max(1, n - 60), n)]
    vol = _std(rets) if len(rets) >= 5 else 0.0
    f["vol_1m_bps"] = vol
    for w in (5, 15, 30, 60):
        r = _ret_bps(prices, w)
        f[f"ret_{w}m_bps"] = r if r is not None else 0.0
    w30 = prices[-31:] if n >= 31 else prices
    path = sum(abs(w30[i] / w30[i - 1] - 1.0) for i in range(1, len(w30))) * 10000.0
    net30 = (w30[-1] / w30[0] - 1.0) * 10000.0 if len(w30) >= 2 else 0.0
    f["efficiency_30m"] = abs(net30) / path if path > 0 else 0.0
    w60 = prices[-60:]
    if len(w60) >= 10:
        sma = sum(w60) / len(w60)
        sd = _std(w60)
        f["zscore_60m"] = (prices[-1] - sma) / sd if sd > 0 else 0.0
        f["sma_60m_dev_bps"] = (prices[-1] / sma - 1.0) * 10000.0
        lo, hi = min(w60), max(w60)
        f["range_pos_60m"] = (prices[-1] - lo) / (hi - lo) if hi > lo else 0.5
    else:
        f["zscore_60m"], f["sma_60m_dev_bps"], f["range_pos_60m"] = 0.0, 0.0, 0.5

    if n < WARMUP_MIN:
        regime = "unknown"
    elif vol > VOLATILE_BPS:
        regime = "volatile"
    elif vol < QUIET_BPS:
        regime = "quiet"
    elif f["efficiency_30m"] >= TREND_ER and abs(net30) > 1.5 * vol * math.sqrt(30):
        regime = "trend_up" if net30 > 0 else "trend_down"
    else:
        regime = "chop"
    f["regime"] = regime

    book = books.get(btc_sym)
    slip = float(clock_info.get("max_slippage_bps", 25))
    if book is not None:
        f["spread_bps"] = float(book.spread_bps)
        f["mid"] = float(book.mid)
        f["imbalance"] = book_imbalance(book)
        f["depth_bid_quote"] = float(book.depth_quote(Side.SELL, Decimal(str(slip))))
        f["depth_ask_quote"] = float(book.depth_quote(Side.BUY, Decimal(str(slip))))
    fx = books.get(usd_sym)
    if fx is not None:
        f["fx_spread_bps"] = float(fx.spread_bps)
        f["fx_mid"] = float(fx.mid)
    fx_hist = [p for p in clock_info.get("fx_history") or () if p[0] <= now]
    f["fx_drift_60m_bps"] = (fx_hist[-1][1] / fx_hist[max(0, len(fx_hist) - 61)][1] - 1.0) * 10000.0 \
        if len(fx_hist) >= 2 else 0.0

    total = float(clock_info.get("total_s") or 0.0)
    left = clock_info.get("time_left_s")
    f["time_left_s"] = float(left) if left is not None else -1.0
    f["time_left_frac"] = max(0.0, min(1.0, float(left) / total)) if left is not None and total > 0 else 1.0

    qty = float(getattr(position, "qty", 0) or 0)
    f["has_position"] = 1.0 if qty > 0 else 0.0
    entry_ts = getattr(position, "entry_ts", None)
    f["position_age_s"] = now - entry_ts if qty > 0 and entry_ts else 0.0
    avg = float(getattr(position, "avg_cost_usdt", 0) or 0)
    f["unrealized_gross_bps"] = (float(book.best_bid) / avg - 1.0) * 10000.0 if qty > 0 and avg > 0 and book else 0.0
    return f
