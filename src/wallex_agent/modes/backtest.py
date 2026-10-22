"""Backtest wiring: ``SimExchange`` over a replay (recorded candles or a seeded ``SyntheticMarket``),
``SimClock``, zero network, no key. Fees are ASSUMED (``config.paper_fee_rate``) and labelled.

Deterministic for a given seed: the market, the simulator, the engine's client ids and the agent's
choices are all seeded, and the agent's tool timer is a constant (latencies are meaningless here).
"""
from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from ..clock import SimClock
from ..config import Config
from ..exchange.sim import SimExchange
from ..market_data import (BTC_TEMPLATE, SYNTHETIC_LABEL, USDTTMN_TEMPLATE, ReplayBookSource, SyntheticMarket,
                           book_from_template, candles_to_books, load_candles)
from ..store import SqliteStore
from ..types import FeeRates, MarketRules, Mode
from . import DB_NAME, Runtime, assemble, new_run_id, runs_dir

# Trading rules as published by GET /v1/markets on 2026-10-22 (stepSize/tickSize = decimal places).
REFERENCE_RULES: dict[str, MarketRules] = {
    "BTCUSDT": MarketRules("BTCUSDT", "BTC", "USDT", 8, 2, Decimal("1e-8"), Decimal("1")),
    "USDTTMN": MarketRules("USDTTMN", "USDT", "TMN", 2, 0, Decimal("0.01"), Decimal("50000")),
}
REFERENCE_USDTTMN_MID = 272500.0     # used when replaying BTCUSDT-only candle files


def assumed_fees(config: Config) -> dict[str, FeeRates]:
    r = Decimal(config.paper_fee_rate)
    return {s: FeeRates(s, r, r) for s in (config.btc_symbol, config.usdt_symbol)}


def _zero_timer() -> float:
    return 0.0


def build_backtest(config: Config, *, data: str | Path | None = None, synthetic: bool | None = None,
                   seed: int | None = None, hours: float | None = None, step_s: int = 60,
                   regime_schedule: Any = None, run_id: str | None = None, run_dir: Path | None = None,
                   fast_io: bool = True, clock: SimClock | None = None, exchange: Any = None,
                   book_source: Any = None, advance: Callable[[], bool] | None = None,
                   store: Any = None) -> Runtime:
    """``data`` (CSV / UDF JSON candles) or ``synthetic`` (default when no data). Test hooks:
    ``clock``/``exchange``/``book_source``/``advance``/``store`` replace the generated world, and
    ``run_id``/``run_dir`` reopen an existing run (crash/restart tests)."""
    seed = config.seed if seed is None else int(seed)
    rid = run_id or new_run_id(Mode.BACKTEST)
    rdir = Path(run_dir) if run_dir is not None else runs_dir(config, Mode.BACKTEST) / rid
    labels = ["backtest_simulated", "assumed_fee", "paper_no_real_orders"]
    caveats = [f"Fees ASSUMED at {config.paper_fee_rate} (taker) for every fill; the account's real fee is "
               "unknown in backtest."]
    synth = False
    cfg_over: dict[str, Any] = {"mode": Mode.BACKTEST, "seed": seed, "data_dir": str(rdir)}
    if hours is not None:
        cfg_over["competition_hours"] = float(hours)
    cfg = replace(config, **cfg_over)
    clk = clock or SimClock()

    if exchange is None:
        if data is not None and synthetic:
            raise ValueError("choose --data FILE or --synthetic, not both")
        if data is not None:
            candles = load_candles(data)
            if len(candles) < 2:
                raise ValueError(f"{data}: need at least 2 candles")
            btc_books = candles_to_books(candles, BTC_TEMPLATE, symbol=cfg.btc_symbol)
            fx_books = [book_from_template(cfg.usdt_symbol, REFERENCE_USDTTMN_MID, USDTTMN_TEMPLATE, b.ts,
                                           half_spread_bps=1.0, price_decimals=0, qty_decimals=2)
                        for b in btc_books]
            book_source = ReplayBookSource({cfg.btc_symbol: btc_books, cfg.usdt_symbol: fx_books}, clk,
                                           candles={cfg.btc_symbol: candles},
                                           candle_step_s=max(1, int(candles[1].ts - candles[0].ts)))
            clk.set(btc_books[0].ts)
            labels.append("recorded_btc_candles_constant_fx")
            caveats.append(f"Replay of {len(candles)} recorded {cfg.btc_symbol} candles from {Path(data).name}; "
                           "order books are SHAPED from a real depth capture around each close, and USDTTMN is "
                           f"held CONSTANT at {REFERENCE_USDTTMN_MID:.0f} TMN (synthetic book).")
        else:
            h = float(hours if hours is not None else cfg.competition_hours)
            n_steps = max(1, int(round(h * 3600 / step_s)))
            market = SyntheticMarket(seed=seed, n_steps=n_steps, step_s=step_s,
                                     regime_schedule=regime_schedule)
            book_source = market.build_source(clk)
            synth = True
            labels.append("synthetic_data")
            caveats.append(f"Market data is {SYNTHETIC_LABEL}; seed {seed}, {n_steps} steps of {step_s}s.")
        advance = book_source.advance
        exchange = SimExchange(book_source, clk, REFERENCE_RULES, assumed_fees(cfg),
                               {"TMN": cfg.initial_tmn, "USDT": Decimal(0), "BTC": Decimal(0)},
                               rng=random.Random(f"sim:{seed}"))
    else:
        synth = bool(getattr(book_source, "synthetic", False))
        if synth:
            labels.append("synthetic_data")

    if store is None:
        store = SqliteStore(rdir / DB_NAME, clock=clk)
        if fast_io:
            # Backtest data is disposable: skip the per-commit fsync (live keeps synchronous=FULL).
            store._conn.execute("PRAGMA synchronous=OFF")
    return assemble(cfg, Mode.BACKTEST, run_id=rid, run_dir=rdir, store=store, clock=clk,
                    exchange=exchange, book_source=book_source, labels=labels, caveats=caveats,
                    synthetic=synth, advance=advance, brain_timer=_zero_timer, rng_seed=seed,
                    durable_audit=not fast_io, state_root=config.state_path)
