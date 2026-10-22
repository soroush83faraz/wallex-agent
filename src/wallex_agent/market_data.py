"""Market-data sources: live (public depth via the client), replay (recorded/derived books) and a
seeded SYNTHETIC market for offline demos/backtests. Also candle CSV/UDF-JSON IO and freshness checks.

Everything synthetic is labelled as such (``SYNTHETIC_LABEL``): it is a plausibility model for
exercising the agent offline, not market data and not evidence of any strategy's performance.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import random
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, RealClock, SimClock
from .errors import TransportError, WallexAgentError
from .types import BookLevel, Candle, OrderBook, dec

SYNTHETIC_LABEL = "SYNTHETIC (seeded random model - not market data)"
REGIMES = ("trend_up", "trend_down", "chop", "spike", "low_vol")


class StaleQuote(WallexAgentError):
    """Book missing, too old, or stamped in the future relative to ``now``."""


def ensure_fresh(book: OrderBook | None, now: float, max_age: float) -> None:
    """Raise ``StaleQuote`` unless ``book`` exists and was received within ``max_age`` seconds."""
    if book is None:
        raise StaleQuote("no book")
    age = book.age(now)
    if age > max_age:
        raise StaleQuote(f"{book.symbol} book age {age:.1f}s > {max_age}s")
    if age < -1.0:
        raise StaleQuote(f"{book.symbol} book stamped {-age:.1f}s in the future")


# --------------------------------------------------------------------------- live
class LiveBookSource:
    """``BookSource`` over a (public-only) Wallex client. Depth has no exchange timestamp, so every
    book is stamped with OUR receipt time ``clock.now()``."""

    def __init__(self, client: Any, clock: Clock | None = None) -> None:
        self._client = client
        self._clock = clock or RealClock()

    def get_book(self, symbol: str) -> OrderBook:
        getter = getattr(self._client, "get_order_book", None) or self._client.get_book
        return replace(getter(symbol), ts=self._clock.now())

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        return list(self._client.get_candles(symbol, resolution, start, end))


# --------------------------------------------------------------------------- replay
class ReplayBookSource:
    """Deterministic ``BookSource`` over pre-built book series, driven by a ``SimClock``.

    ``get_book`` returns the latest snapshot with ``ts <= clock.now()`` (never a future one). With
    ``stamp_now`` (default) the snapshot is presented as just received, like a live poll would; with
    ``stamp_now=False`` the original timestamp is kept so staleness checks see the true age.
    ``get_candles`` only returns candles that are CLOSED at ``now`` (no look-ahead).
    """

    def __init__(self, series: Mapping[str, Sequence[OrderBook]], clock: Clock, *,
                 candles: Mapping[str, Sequence[Candle]] | None = None, candle_step_s: int = 60,
                 stamp_now: bool = True) -> None:
        self._series = dict(series)
        self._times: dict[str, list[float]] = {}
        for sym, seq in self._series.items():
            times = getattr(seq, "times", None)
            self._times[sym] = list(times) if times is not None else [b.ts for b in seq]
            if any(b < a for a, b in zip(self._times[sym], self._times[sym][1:])):
                raise ValueError(f"{sym}: replay series must be sorted by ts")
        self._candles = {k: sorted(v, key=lambda c: c.ts) for k, v in (candles or {}).items()}
        self._candle_ts = {k: [c.ts for c in v] for k, v in self._candles.items()}
        self._clock = clock
        self.candle_step_s = candle_step_s
        self.stamp_now = stamp_now

    @property
    def clock(self) -> Clock:
        return self._clock

    def symbols(self) -> list[str]:
        return list(self._series)

    def times(self, symbol: str) -> list[float]:
        return list(self._times[symbol])

    def get_book(self, symbol: str) -> OrderBook:
        times = self._times.get(symbol)
        if times is None:
            raise TransportError(f"replay: no series for {symbol}")
        now = self._clock.now()
        i = bisect.bisect_right(times, now) - 1
        if i < 0:
            raise TransportError(f"replay: no {symbol} book at or before {now}")
        book = self._series[symbol][i]
        return replace(book, ts=now) if self.stamp_now else book

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        cs = self._candles.get(symbol, [])
        ts = self._candle_ts.get(symbol, [])
        closed_before = self._clock.now() - self.candle_step_s
        hi = min(float(end), closed_before)
        lo_i = bisect.bisect_left(ts, start)
        out = []
        for c in cs[lo_i:]:
            if c.ts > hi:
                break
            out.append(c)
        return out

    def next_time(self) -> float | None:
        """Earliest snapshot time (any symbol) strictly after ``now``."""
        now = self._clock.now()
        nxt = [t[i] for t in self._times.values() if (i := bisect.bisect_right(t, now)) < len(t)]
        return min(nxt) if nxt else None

    def advance(self) -> bool:
        """Move the ``SimClock`` to the next snapshot. Returns False when the replay is exhausted."""
        nxt = self.next_time()
        if nxt is None:
            return False
        set_ = getattr(self._clock, "set", None)
        if set_ is None:
            raise TypeError("advance() needs a SimClock-like clock with set()")
        set_(nxt)
        return True

    @property
    def exhausted(self) -> bool:
        return self.next_time() is None


# --------------------------------------------------------------------------- depth templates
@dataclass(frozen=True)
class DepthTemplate:
    """Book SHAPE: per level the distance from mid (bps) and the quote notional, per side."""

    bid: tuple[tuple[float, float], ...]
    ask: tuple[tuple[float, float], ...]

    @staticmethod
    def from_book(book: OrderBook) -> "DepthTemplate":
        mid = book.mid

        def side(levels: Sequence[BookLevel]) -> tuple[tuple[float, float], ...]:
            return tuple((float(abs(lv.price - mid) / mid * 10000), float(lv.price * lv.quantity))
                         for lv in levels)

        return DepthTemplate(side(book.bids), side(book.asks))


# Shapes condensed from the real captures in tests/fixtures/depth_*_real.json (2026-10-22, every
# other level): (bps from mid, quote notional). BTCUSDT notional in USDT, USDTTMN in TMN.
BTC_TEMPLATE = DepthTemplate(
    bid=((2.5, 162), (3.5, 200), (10.1, 200), (13.3, 211), (18.6, 274), (19.2, 143), (26.6, 256),
         (26.9, 574), (28.0, 735), (30.4, 139), (40.4, 308), (50.3, 265), (52.7, 200), (57.9, 55),
         (70.6, 311), (89.7, 494), (102.0, 521), (107.5, 262), (138.9, 1513), (144.2, 248)),
    ask=((2.5, 28342), (11.2, 150), (12.9, 195), (13.7, 254), (14.9, 140), (30.7, 826),
         (186.6, 13), (197.6, 636), (216.7, 28954), (232.9, 302), (279.7, 4061), (310.4, 4041),
         (341.2, 1216), (384.8, 2396), (396.6, 4000), (409.1, 603), (438.8, 481), (445.9, 269),
         (452.6, 233), (463.5, 112)))
USDTTMN_TEMPLATE = DepthTemplate(
    bid=((0.4, 831061), (4.3, 1094577338), (4.6, 503875), (8.2, 148507490), (8.3, 39587040),
         (11.0, 98976450), (11.5, 10609420), (13.0, 111575), (14.3, 519711), (14.5, 2375372),
         (17.9, 61091200), (24.9, 8138021), (28.3, 4996912), (29.1, 16494664), (35.3, 10861120),
         (37.9, 29591310), (42.1, 11249881), (48.0, 1998604), (48.5, 111563939), (51.1, 9998057)),
    ask=((0.4, 8174970), (2.1, 293550043), (3.7, 207168), (7.2, 146849053), (7.3, 207244),
         (7.7, 258568686), (10.5, 97932040), (11.0, 207320), (14.0, 7894129), (15.1, 2729000),
         (18.3, 207472), (18.5, 2566144), (18.8, 1370536440), (22.4, 206051219), (25.7, 207624),
         (32.6, 546756), (36.7, 207852), (37.1, 108571295), (37.7, 162195581), (40.4, 207928)))


def _q(x: float, decimals: int) -> Decimal:
    return Decimal(f"{x:.{decimals}f}")


def book_from_template(symbol: str, mid: float, template: DepthTemplate, ts: float, *,
                       half_spread_bps: float | None = None, price_decimals: int = 2,
                       qty_decimals: int = 8, rng: random.Random | None = None,
                       size_noise: float = 0.0) -> OrderBook:
    """Build a book around ``mid`` with the template's shape. ``half_spread_bps`` re-anchors the touch
    (inner levels keep their relative spacing); ``size_noise`` = lognormal sigma on level notional."""

    def side(levels: Sequence[tuple[float, float]], sign: int) -> list[tuple[Decimal, Decimal]]:
        if not levels:
            return []
        shift = 0.0 if half_spread_bps is None else half_spread_bps - levels[0][0]
        out: list[tuple[Decimal, Decimal]] = []
        for off, notional in levels:
            off = max(0.05, off + shift)
            n = notional * (math.exp(rng.gauss(0.0, size_noise)) if rng is not None and size_noise else 1.0)
            price = _q(mid * (1 + sign * off / 10000.0), price_decimals)
            if price <= 0:
                continue
            qty = _q(n / float(price), qty_decimals)
            if qty > 0:
                out.append((price, qty))
        return out

    return OrderBook.build(symbol, side(template.bid, -1), side(template.ask, +1), ts)


def candles_to_books(candles: Sequence[Candle], depth_template: OrderBook | DepthTemplate, *,
                     symbol: str | None = None, half_spread_bps: float | None = None,
                     price_decimals: int = 2, qty_decimals: int = 8,
                     close_offset_s: int = 60) -> list[OrderBook]:
    """One book per candle, centred on the close and stamped at the candle CLOSE time
    (``ts + close_offset_s``) so a replay never shows a price before it existed."""
    tpl = depth_template if isinstance(depth_template, DepthTemplate) else DepthTemplate.from_book(depth_template)
    sym = symbol or (depth_template.symbol if isinstance(depth_template, OrderBook) else "BTCUSDT")
    return [book_from_template(sym, float(c.close), tpl, float(c.ts + close_offset_s),
                               half_spread_bps=half_spread_bps, price_decimals=price_decimals,
                               qty_decimals=qty_decimals) for c in candles]


def parse_depth(payload: Mapping[str, Any], symbol: str, ts: float) -> OrderBook:
    """Wallex ``/v1/depth`` payload (with or without the envelope) -> OrderBook."""
    res = payload.get("result", payload)
    return OrderBook.build(symbol, [(dec(x["price"]), dec(x["quantity"])) for x in res["bid"]],
                           [(dec(x["price"]), dec(x["quantity"])) for x in res["ask"]], ts)


def load_depth(path: str | Path, symbol: str, ts: float = 0.0) -> OrderBook:
    data = json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)
    return parse_depth(data, symbol, ts)


# --------------------------------------------------------------------------- candles IO
def _candles_from_udf(data: Mapping[str, Any]) -> list[Candle]:
    if data.get("s") == "no_data":
        return []
    if data.get("s") != "ok":
        raise ValueError(f"UDF status {data.get('s')!r}")
    keys = ("t", "o", "h", "l", "c", "v")
    n = len(data["t"])
    if any(len(data[k]) != n for k in keys):
        raise ValueError("UDF arrays have different lengths")
    return [Candle(int(dec(data["t"][i])), dec(data["o"][i]), dec(data["h"][i]), dec(data["l"][i]),
                   dec(data["c"][i]), dec(data["v"][i])) for i in range(n)]


def load_candles(path: str | Path) -> list[Candle]:
    """CSV (``time,open,high,low,close,volume``) or Wallex UDF JSON (``{"s":"ok","t":[..],...}``).
    Returned sorted by time, duplicates (same ts) keep the last row."""
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    if text.lstrip().startswith("{"):
        out = _candles_from_udf(json.loads(text, parse_float=Decimal))
    else:
        rows = list(csv.reader(text.splitlines()))
        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        start = 1 if header and header[0] in ("time", "t", "ts", "timestamp") else 0
        idx = {k: header.index(k) for k in ("time", "open", "high", "low", "close", "volume")} if start \
            else {k: i for i, k in enumerate(("time", "open", "high", "low", "close", "volume"))}
        out = []
        for r in rows[start:]:
            if not r or not "".join(r).strip():
                continue
            out.append(Candle(int(dec(r[idx["time"]])), dec(r[idx["open"]]), dec(r[idx["high"]]),
                              dec(r[idx["low"]]), dec(r[idx["close"]]), dec(r[idx["volume"]])))
    uniq = {c.ts: c for c in out}
    return [uniq[t] for t in sorted(uniq)]


def save_candles(path: str | Path, candles: Iterable[Candle]) -> Path:
    """Write CSV, or UDF JSON (string numbers, like Wallex) when the path ends with ``.json``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cs = sorted(candles, key=lambda c: c.ts)
    if p.suffix.lower() == ".json":
        doc = {"s": "ok" if cs else "no_data", "t": [c.ts for c in cs],
               "o": [str(c.open) for c in cs], "h": [str(c.high) for c in cs],
               "l": [str(c.low) for c in cs], "c": [str(c.close) for c in cs],
               "v": [str(c.volume) for c in cs]}
        p.write_text(json.dumps(doc), encoding="utf-8")
    else:
        with p.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close", "volume"])
            for c in cs:
                w.writerow([c.ts, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume)])
    return p


# --------------------------------------------------------------------------- synthetic market
# Per-regime 1-minute model: (drift bps/min range, vol bps/min range). Plausibility priors chosen
# by hand around the ~4 bps/min realised vol of the real 1m capture; not fitted to anything.
_REGIME_PARAMS: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "trend_up": ((1.0, 4.0), (4.0, 8.0)),
    "trend_down": ((-4.0, -1.0), (4.0, 8.0)),
    "chop": ((0.0, 0.0), (3.0, 6.0)),
    "spike": ((0.0, 0.0), (10.0, 20.0)),
    "low_vol": ((0.0, 0.0), (0.8, 2.0)),
}


class _LazyBooks(Sequence[OrderBook]):
    """Books built on first access (keeps a 24 h synthetic run cheap)."""

    def __init__(self, times: list[float], build: Any) -> None:
        self.times = times
        self._build = build
        self._cache: dict[int, OrderBook] = {}

    def __len__(self) -> int:
        return len(self.times)

    def __getitem__(self, i: Any) -> Any:
        if isinstance(i, slice):
            return [self[j] for j in range(*i.indices(len(self)))]
        if i < 0:
            i += len(self)
        b = self._cache.get(i)
        if b is None:
            b = self._cache[i] = self._build(i)
        return b


class SyntheticMarket:
    """Seeded, labelled-synthetic BTCUSDT + USDTTMN market on a fixed step grid.

    * BTCUSDT mid follows a regime-switching random walk (``REGIMES``) with optional Student-t fat
      tails; spike segments add a jump. Spread 5-10 bps; depth shaped like the real capture.
    * USDTTMN follows a small drift with tiny noise; spread normally ~1-3 bps with rare episodes of
      abnormal widening (to exercise the conversion-patience logic).
    * ``history_steps`` of candles exist BEFORE ``start_ts`` (books only from ``start_ts``) so an
      agent can backfill history with a candles tool.
    ``regime_schedule``: ``[(regime, n_steps), ...]``; ``None`` = seeded random segments.
    """

    label = SYNTHETIC_LABEL
    synthetic = True

    def __init__(self, seed: int = 0, n_steps: int = 1440, step_s: int = 60,
                 regime_schedule: Sequence[tuple[str, int]] | None = None, *,
                 start_ts: float = 1_700_000_000.0, btc_start: float = 81200.0,
                 usdttmn_start: float = 272500.0, fat_tails: bool = True, history_steps: int = 120,
                 fx_wide_prob: float = 0.004) -> None:
        if n_steps < 1 or step_s < 1:
            raise ValueError("n_steps and step_s must be >= 1")
        self.seed, self.n_steps, self.step_s = seed, n_steps, int(step_s)
        self.start_ts = float(start_ts)
        self.history_steps = max(0, int(history_steps))
        self._rng = random.Random(f"synthetic:{seed}")
        total = self.history_steps + n_steps + 1
        self.schedule = self._make_schedule(regime_schedule, total)
        self._regimes: list[str] = []
        for name, k in self.schedule:
            self._regimes.extend([name] * k)
        self._regimes = self._regimes[:total]
        self._simulate(total, btc_start, usdttmn_start, fat_tails, fx_wide_prob)

    # ---------------------------------------------------------------- generation
    def _make_schedule(self, schedule: Sequence[tuple[str, int]] | None, total: int) -> list[tuple[str, int]]:
        if schedule:
            # An explicit schedule starts at ``start_ts``; the pre-start history uses its first regime.
            out = [(schedule[0][0], self.history_steps)] if self.history_steps else []
            for name, k in schedule:
                if name not in REGIMES:
                    raise ValueError(f"unknown regime {name!r}; one of {REGIMES}")
                out.append((name, int(k)))
            covered = sum(k for _, k in out)
            if covered < total:   # extend the last regime to cover the run
                out[-1] = (out[-1][0], out[-1][1] + total - covered)
            return out
        out, covered = [], 0
        weights = {"trend_up": 2, "trend_down": 2, "chop": 4, "spike": 1, "low_vol": 2}
        names = list(weights)
        while covered < total:
            name = self._rng.choices(names, [weights[n] for n in names])[0]
            k = self._rng.randint(10, 30) if name == "spike" else self._rng.randint(60, 180)
            out.append((name, k))
            covered += k
        return out

    def _simulate(self, total: int, btc0: float, fx0: float, fat_tails: bool, fx_wide_prob: float) -> None:
        rng = self._rng
        sub = 4                                       # sub-steps per bar for high/low
        price, fx = btc0, fx0
        seg_params: dict[int, tuple[float, float]] = {}
        seg_id, seg_left = -1, 0
        sched = iter(self.schedule)
        self.btc_mid: list[float] = []
        self.btc_half_spread: list[float] = []
        self.fx_mid: list[float] = []
        self.fx_half_spread: list[float] = []
        self._bars: list[tuple[float, float, float, float, float]] = []   # o,h,l,c,v for bar ending at i
        anchor = price
        wide_left = 0
        scale = math.sqrt(self.step_s / 60.0)
        for i in range(total):
            if seg_left <= 0:
                name, seg_left = next(sched)
                seg_id += 1
                (d_lo, d_hi), (v_lo, v_hi) = _REGIME_PARAMS[name]
                seg_params[seg_id] = (rng.uniform(d_lo, d_hi), rng.uniform(v_lo, v_hi))
                anchor = price
                if name == "spike":
                    price *= math.exp(rng.choice((-1, 1)) * rng.uniform(80, 250) / 10000.0)
            seg_left -= 1
            regime = self._regimes[i]
            drift, vol = seg_params[seg_id]
            o, hi, lo = price, price, price
            if i > 0:
                for _ in range(sub):
                    z = rng.gauss(0.0, 1.0)
                    if fat_tails:   # Student-t(4) scaled to unit variance
                        z = z / math.sqrt(max(1e-9, sum(rng.gauss(0, 1) ** 2 for _ in range(4)) / 4)) / math.sqrt(2.0)
                    step = (drift / sub + vol / math.sqrt(sub) * z) * scale
                    if regime == "chop":        # mean reversion toward the segment anchor
                        step -= 0.08 / sub * math.log(price / anchor) * 10000.0
                    price *= math.exp(step / 10000.0)
                    hi, lo = max(hi, price), min(lo, price)
                fx *= math.exp((0.02 + 0.3 * rng.gauss(0.0, 1.0)) / 10000.0 * scale)
            self._bars.append((o, hi, lo, price, abs(rng.gauss(0.5, 0.3))))
            self.btc_mid.append(price)
            self.btc_half_spread.append(rng.uniform(2.5, 5.0))
            if wide_left <= 0 and rng.random() < fx_wide_prob:
                wide_left = rng.randint(3, 8)
            if wide_left > 0:
                wide_left -= 1
                self.fx_half_spread.append(rng.uniform(8.0, 30.0))
            else:
                self.fx_half_spread.append(rng.uniform(0.4, 1.5))
            self.fx_mid.append(fx)

    # ---------------------------------------------------------------- accessors
    def time_at(self, step: int) -> float:
        """Wall time of book ``step`` (0 = ``start_ts``)."""
        return self.start_ts + step * self.step_s

    def _idx(self, step: int) -> int:
        return self.history_steps + step

    def regime_at(self, ts: float) -> str:
        i = self._idx(int(round((ts - self.start_ts) / self.step_s)))
        return self._regimes[max(0, min(i, len(self._regimes) - 1))]

    def candles(self, symbol: str = "BTCUSDT") -> list[Candle]:
        """All bars (incl. pre-start history). Bar for index i spans [t(i-1), t(i)) -> ts=t(i-1)."""
        out = []
        dp = 2 if symbol == "BTCUSDT" else 0
        for i in range(1, len(self._bars)):
            ts = int(self.time_at(i - 1 - self.history_steps))
            if symbol == "BTCUSDT":
                o, h, l, c, v = self._bars[i]
            else:
                o, c = self.fx_mid[i - 1], self.fx_mid[i]
                h, l, v = max(o, c), min(o, c), 1000.0
            out.append(Candle(ts, _q(o, dp), _q(h, dp), _q(l, dp), _q(c, dp), _q(v, 8)))
        return out

    def _book(self, symbol: str, step: int) -> OrderBook:
        i = self._idx(step)
        r = random.Random(f"book:{self.seed}:{symbol}:{step}")
        if symbol == "BTCUSDT":
            # The real capture is one snapshot with a large ask wall; mirroring it at random keeps the
            # shape without making the synthetic book permanently one-sided.
            tpl = BTC_TEMPLATE if r.random() < 0.5 else DepthTemplate(BTC_TEMPLATE.ask, BTC_TEMPLATE.bid)
            return book_from_template(symbol, self.btc_mid[i], tpl, self.time_at(step),
                                      half_spread_bps=self.btc_half_spread[i], price_decimals=2,
                                      qty_decimals=8, rng=r, size_noise=0.4)
        return book_from_template(symbol, self.fx_mid[i], USDTTMN_TEMPLATE, self.time_at(step),
                                  half_spread_bps=self.fx_half_spread[i], price_decimals=0,
                                  qty_decimals=2, rng=r, size_noise=0.4)

    def books(self, symbol: str) -> _LazyBooks:
        times = [self.time_at(s) for s in range(self.n_steps + 1)]
        return _LazyBooks(times, lambda s: self._book(symbol, s))

    def build_source(self, clock: Clock) -> "SyntheticBookSource":
        """Replay source over this market. A ``SimClock`` is moved to ``start_ts``; then the runner
        calls ``source.advance()`` once per step."""
        if isinstance(clock, SimClock) or hasattr(clock, "set"):
            clock.set(self.start_ts)  # type: ignore[attr-defined]
        return SyntheticBookSource(self, clock)


class SyntheticBookSource(ReplayBookSource):
    """``ReplayBookSource`` over a ``SyntheticMarket`` (keeps the synthetic label/regime visible)."""

    synthetic = True
    label = SYNTHETIC_LABEL

    def __init__(self, market: SyntheticMarket, clock: Clock) -> None:
        super().__init__({"BTCUSDT": market.books("BTCUSDT"), "USDTTMN": market.books("USDTTMN")}, clock,
                         candles={"BTCUSDT": market.candles("BTCUSDT"), "USDTTMN": market.candles("USDTTMN")},
                         candle_step_s=market.step_s)
        self.market = market

    def regime_now(self) -> str:
        return self.market.regime_at(self.clock.now())
