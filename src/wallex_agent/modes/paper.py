"""Paper wiring: ``SimExchange`` (orders, balances, fills: all in-process) over LIVE public books.

The only network client here is ``WallexClient(None, public_only=True)``: it has no key, and its
allow-list refuses every POST/DELETE and every private GET before any I/O. So paper mode cannot place,
cancel or even look at a real order, structurally (``tests/test_modes.py`` records every request).
Fees are ASSUMED (``config.paper_fee_rate``) because the account fee endpoint is private.
"""
from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..clock import Clock, RealClock
from ..config import Config
from ..errors import ExchangeError
from ..exchange.sim import SimExchange
from ..exchange.wallex import UrllibTransport, WallexClient
from ..market_data import LiveBookSource
from ..store import SqliteStore
from ..types import Mode
from . import DB_NAME, CachedBookSource, RecordingTransport, Runtime, assemble, new_run_id, runs_dir
from .backtest import assumed_fees


def public_client(config: Config, *, transport: Any = None, clock: Clock | None = None) -> WallexClient:
    """Key-less, public-GET-only client (paper, diagnose, fetch-history)."""
    return WallexClient(None, public_only=True, base_url=config.api_base,
                        transport=transport if transport is not None
                        else UrllibTransport(use_env_proxy=config.use_env_proxy),
                        clock=clock, timeout=config.http_timeout_s, max_rps=config.max_requests_per_s)


def build_paper(config: Config, *, transport: Any = None, clock: Clock | None = None,
                run_id: str | None = None, run_dir: Path | None = None, book_ttl_s: float = 2.0,
                record: bool = True) -> Runtime:
    clk = clock or RealClock()
    cfg = replace(config, mode=Mode.PAPER)
    rid = run_id or new_run_id(Mode.PAPER)
    rdir = Path(run_dir) if run_dir is not None else runs_dir(cfg, Mode.PAPER) / rid
    inner = transport if transport is not None else UrllibTransport(use_env_proxy=cfg.use_env_proxy)
    recorder = inner if isinstance(inner, RecordingTransport) else (
        RecordingTransport(inner) if record else None)
    client = public_client(cfg, transport=recorder or inner, clock=clk)
    rules = client.get_markets()                    # public GET
    missing = [s for s in (cfg.btc_symbol, cfg.usdt_symbol) if s not in rules]
    if missing:
        raise ExchangeError(f"public /v1/markets does not list {missing}: cannot paper trade them")
    source = CachedBookSource(LiveBookSource(client, clk), clk, ttl_s=book_ttl_s)
    sim = SimExchange(source, clk, rules, assumed_fees(cfg),
                      {"TMN": cfg.initial_tmn, "USDT": Decimal(0), "BTC": Decimal(0)},
                      rng=random.Random(f"sim:{cfg.seed}"))
    store = SqliteStore(rdir / DB_NAME, clock=clk)
    caveats = [f"PAPER: real public Wallex books, SIMULATED orders/fills (no order was ever sent). Fees "
               f"ASSUMED at {cfg.paper_fee_rate}: the account fee endpoint is private.",
               "Paper fills assume we can take visible liquidity instantly at the walked prices; resting "
               "orders fill when later public books cross them (no queue position modelled)."]
    rt = assemble(cfg, Mode.PAPER, run_id=rid, run_dir=rdir, store=store, clock=clk, exchange=sim,
                  book_source=source, labels=["paper_no_real_orders", "assumed_fee", "live_public_books"],
                  caveats=caveats, recorder=recorder)
    rt.extras["public_client"] = client
    return rt
