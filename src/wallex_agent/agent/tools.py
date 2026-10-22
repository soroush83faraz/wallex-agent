"""Tools the agent can call. Each is ``Tool(name, description, cost, fn)``; ``run(ctx)`` never raises and
returns ``ToolResult(ok, data, error, latency_ms)``. The only market access is through the
``BookSource`` and the observation in ``ToolContext`` (no exchange I/O here).

``cost`` is a relative effort score (1 = trivial in-memory read, 3 = remote/historical fetch); the
planner calls cheap tools first and expensive ones only when the cheap evidence is insufficient.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping

from ..config import Config
from ..market_data import StaleQuote, ensure_fresh
from ..types import BPS, ZERO, Side, plain
from . import strategies
from .features import PriceHistory


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    latency_ms: float = 0.0


@dataclass
class ToolContext:
    obs: Any                                  # planner.Observation
    config: Config
    history: PriceHistory
    fx_history: PriceHistory
    memory: Any = None
    book_source: Any = None
    features: dict[str, Any] = field(default_factory=dict)
    fx_spreads: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    cost: int
    fn: Callable[..., dict[str, Any]]
    summarize: Callable[[dict[str, Any]], str] = lambda d: ""

    def run(self, ctx: ToolContext, timer: Callable[[], float] = time.perf_counter, **kw: Any) -> ToolResult:
        t0 = timer()
        try:
            data = self.fn(ctx, **kw)
            ok, err = True, ""
        except Exception as exc:   # a tool failure is evidence, not a crash
            data, ok, err = {}, False, f"{type(exc).__name__}: {exc}"
        return ToolResult(ok, data, err, round((timer() - t0) * 1000.0, 3))


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------- implementations
def _fee(ctx: ToolContext, symbol: str) -> Decimal:
    f = (ctx.obs.fees or {}).get(symbol)
    if f is None:
        raise ToolError(f"fee unknown for {symbol}")
    return f.taker


def read_book(ctx: ToolContext) -> dict[str, Any]:
    cfg, now = ctx.config, ctx.obs.now
    out: dict[str, Any] = {"stale": []}
    for sym in (cfg.btc_symbol, cfg.usdt_symbol):
        book = ctx.obs.books.get(sym)
        try:
            ensure_fresh(book, now, cfg.max_book_age_s)
        except StaleQuote as exc:
            out["stale"].append(f"{sym}: {exc}")
            continue
        assert book is not None
        out[sym] = {"bid": book.best_bid, "ask": book.best_ask, "mid": book.mid,
                    "spread_bps": book.spread_bps.quantize(Decimal("0.01")), "age_s": round(book.age(now), 3)}
    if cfg.btc_symbol not in out:
        raise ToolError("; ".join(out["stale"]) or "no BTC book")
    return out


def read_candles(ctx: ToolContext, minutes: int = 120) -> dict[str, Any]:
    src = ctx.book_source
    if src is None:
        raise ToolError("no candle source available")
    now = int(ctx.obs.now)
    start = now - minutes * 60
    added = 0
    btc = src.get_candles(ctx.config.btc_symbol, "1", start, now)
    for c in btc:
        if c.ts + 60 <= now:                   # closed candles only (no look-ahead)
            before = len(ctx.history)
            ctx.history.add(c.ts + 60, float(c.close), overwrite=False)
            added += len(ctx.history) - before
    fx = src.get_candles(ctx.config.usdt_symbol, "1", start, now)
    for c in fx:
        if c.ts + 60 <= now:
            ctx.fx_history.add(c.ts + 60, float(c.close), overwrite=False)
    return {"candles": len(btc), "fx_candles": len(fx), "points_added": added, "points_now": len(ctx.history)}


def estimate_round_trip_cost(ctx: ToolContext, notional_usdt: Decimal | None = None,
                             qty: Decimal | None = None, leg: str = "round_trip") -> dict[str, Any]:
    """Walk the visible BTC book for the intended size. Round trip cost (bps of mid) =
    (avg_buy - avg_sell)/mid + buy fee + sell fee — the same formula as the risk gate.
    ``leg='exit'``: only the sell side, cost = (mid - avg_sell)/mid + sell fee."""
    sym = ctx.config.btc_symbol
    book = ctx.obs.books[sym]
    fee = _fee(ctx, sym)
    mid = book.mid
    if leg == "exit":
        if qty is None or qty <= 0:
            raise ToolError("exit cost needs qty > 0")
        sell = book.walk_qty(Side.SELL, qty)
        if sell.filled_qty <= 0:
            raise ToolError("no bids")
        slip = (mid - sell.avg_price) / mid * BPS
        return {"leg": "exit", "qty": qty, "sell_avg": sell.avg_price, "complete": sell.complete,
                "slippage_bps": slip, "fee_bps": fee * BPS, "cost_bps": slip + fee * BPS, "mid": mid}
    if notional_usdt is None or notional_usdt <= 0:
        raise ToolError("round-trip cost needs notional > 0")
    buy = book.walk_quote(Side.BUY, notional_usdt)
    if buy.filled_qty <= 0:
        raise ToolError("no asks")
    sell = book.walk_qty(Side.SELL, buy.filled_qty)
    complete = buy.complete and sell.complete
    if sell.filled_qty <= 0:
        raise ToolError("no bids")
    spread_slip = (buy.avg_price - sell.avg_price) / mid * BPS
    fees_bps = 2 * fee * BPS
    return {"leg": "round_trip", "notional_usdt": notional_usdt, "qty": buy.filled_qty,
            "buy_avg": buy.avg_price, "sell_avg": sell.avg_price, "complete": complete,
            "entry_slippage_bps": (buy.avg_price - book.best_ask) / mid * BPS,
            "spread_slippage_bps": spread_slip, "fee_bps": fees_bps, "cost_bps": spread_slip + fees_bps,
            "mid": mid, "fee_rate": fee}


def _signal(gen: Callable[..., strategies.Hypothesis]) -> Callable[[ToolContext], dict[str, Any]]:
    def run(ctx: ToolContext) -> dict[str, Any]:
        return {"hypothesis": gen(ctx.features, ctx.memory)}
    return run


def fx_check(ctx: ToolContext, tmn_amount: Decimal | None = None) -> dict[str, Any]:
    """USDTTMN conversion quality: spread vs its recent median (abnormal = > max(3x median, 5 bps)),
    depth and walked cost for converting ``tmn_amount`` TMN."""
    cfg = ctx.config
    sym = cfg.usdt_symbol
    book = ctx.obs.books.get(sym)
    try:
        ensure_fresh(book, ctx.obs.now, cfg.max_book_age_s)
    except StaleQuote as exc:
        raise ToolError(str(exc)) from exc
    assert book is not None
    fee = _fee(ctx, sym)
    spread = book.spread_bps
    hist = sorted(ctx.fx_spreads[-120:])
    median = hist[len(hist) // 2] if hist else None
    normal_cap = max(3.0 * median, 5.0) if median is not None else 5.0
    out: dict[str, Any] = {"spread_bps": spread, "median_spread_bps": median, "normal_cap_bps": normal_cap,
                           "abnormal": float(spread) > normal_cap, "fee_bps": fee * BPS,
                           "depth_within_cap_tmn": book.depth_quote(Side.BUY, cfg.conversion_slippage_bps)}
    if tmn_amount is not None and tmn_amount > 0:
        w = book.walk_quote(Side.BUY, tmn_amount)
        slip = (w.avg_price - book.best_ask) / book.mid * BPS if w.filled_qty > 0 else None
        out.update(usdt_out=w.filled_qty, avg_price=w.avg_price, complete=w.complete,
                   slippage_bps=slip, cost_bps=(spread / 2 + (slip or ZERO) + fee * BPS))
    return out


def risk_headroom(ctx: ToolContext) -> dict[str, Any]:
    """Soft budget view for planning (the hard gates live in risk.py and are re-checked there)."""
    cfg, obs = ctx.config, ctx.obs
    btc_book = obs.books.get(cfg.btc_symbol)
    bal = obs.balances
    usdt = bal["USDT"].spendable if "USDT" in bal else ZERO
    btc = bal["BTC"].spendable if "BTC" in bal else ZERO
    if obs.ledger is not None:
        usdt = min(usdt, obs.ledger.get("USDT", ZERO))
        btc = min(btc, obs.ledger.get("BTC", ZERO))
    bid = btc_book.best_bid if btc_book is not None else ZERO
    equity_usdt = usdt + btc * bid
    dd = None
    if obs.equity_tmn is not None and obs.peak_equity_tmn and obs.peak_equity_tmn > 0:
        dd = max(ZERO, (obs.peak_equity_tmn - obs.equity_tmn) / obs.peak_equity_tmn)
    return {"round_trips_left": cfg.max_round_trips - obs.round_trips_done,
            "time_left_s": obs.time_left_s, "unresolved_orders": obs.unresolved_orders,
            "usdt_spendable": usdt, "btc_held": btc, "equity_usdt": equity_usdt,
            "exposure_room_usdt": max(ZERO, cfg.max_exposure_fraction * equity_usdt - btc * bid),
            "drawdown": dd, "drawdown_limit": cfg.max_drawdown_pct}


# --------------------------------------------------------------------------- registry
def _s(x: Any, nd: int = 2) -> str:
    if isinstance(x, Decimal):
        return plain(x.quantize(Decimal(1).scaleb(-nd)))
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _hyp_summary(d: dict[str, Any]) -> str:
    h = d["hypothesis"]
    return f"{h.name}: {h.direction} {h.expected_move_bps:+.1f} bps/{int(h.horizon_s)}s conf {h.confidence:.2f}"


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("read_book", "fresh BTCUSDT/USDTTMN top of book + staleness check", 1, read_book,
         lambda d: " ".join(f"{k} {_s(v['bid'])}/{_s(v['ask'])} ({_s(v['spread_bps'])} bps)"
                            for k, v in d.items() if isinstance(v, dict)) + (f" stale={d['stale']}" if d["stale"] else "")),
    Tool("read_candles", "backfill 1-min history from public candles", 3, read_candles,
         lambda d: f"{d['candles']} candles, +{d['points_added']} points -> {d['points_now']}"),
    Tool("estimate_round_trip_cost", "walk the book for the intended size: spread + slippage + both fees", 2,
         estimate_round_trip_cost,
         lambda d: f"{d['leg']} cost {_s(d['cost_bps'])} bps (complete={d['complete']})"),
    Tool("trend_signal", "momentum hypothesis (trend_follow)", 1, _signal(strategies.trend_follow), _hyp_summary),
    Tool("reversion_signal", "mean-reversion hypothesis", 1, _signal(strategies.mean_reversion), _hyp_summary),
    Tool("book_pressure_signal", "order-book imbalance hypothesis", 1, _signal(strategies.book_pressure), _hyp_summary),
    Tool("fx_check", "USDTTMN spread/depth/cost for the conversion stage", 1, fx_check,
         lambda d: f"spread {_s(d['spread_bps'])} bps (cap {d['normal_cap_bps']:.1f}) abnormal={d['abnormal']}"
                   + (f" cost {_s(d['cost_bps'])} bps complete={d['complete']}" if "cost_bps" in d else "")),
    Tool("risk_headroom", "round trips left, time left, exposure room, drawdown", 1, risk_headroom,
         lambda d: f"trips left {d['round_trips_left']}, equity {_s(d['equity_usdt'])} USDT, "
                   f"room {_s(d['exposure_room_usdt'])} USDT"),
)}
SIGNAL_TOOLS = {"trend_follow": "trend_signal", "mean_reversion": "reversion_signal",
                "book_pressure": "book_pressure_signal"}


class ToolCallLog:
    """Runs tools and records ``{tool, why, ok, summary, error, latency_ms, cost}`` per call (and
    deliberately skipped tools with ``called: False``) for ``Decision.tool_calls``."""

    def __init__(self, ctx: ToolContext, timer: Callable[[], float] = time.perf_counter,
                 registry: Mapping[str, Tool] | None = None) -> None:
        self.ctx = ctx
        self.timer = timer
        self.registry = dict(registry or TOOLS)
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, ToolResult] = {}

    def call(self, name: str, why: str, **kw: Any) -> ToolResult:
        tool = self.registry[name]
        res = tool.run(self.ctx, self.timer, **kw)
        try:
            summary = tool.summarize(res.data) if res.ok else ""
        except Exception:  # summaries are cosmetic
            summary = ""
        self.calls.append({"tool": name, "called": True, "why": why, "ok": res.ok, "summary": summary,
                           "error": res.error, "latency_ms": res.latency_ms, "cost": tool.cost})
        self.results[name] = res
        return res

    def skip(self, name: str, why: str) -> None:
        self.calls.append({"tool": name, "called": False, "why": why, "cost": self.registry[name].cost})

    @property
    def effort(self) -> int:
        return sum(c["cost"] for c in self.calls if c["called"])


__all__ = ["Tool", "ToolResult", "ToolContext", "ToolCallLog", "TOOLS", "SIGNAL_TOOLS", "ToolError",
           "read_book", "read_candles", "estimate_round_trip_cost", "fx_check", "risk_headroom"]
