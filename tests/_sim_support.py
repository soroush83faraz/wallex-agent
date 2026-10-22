"""Shared builders for the risk / simulator / execution tests (no network, SimClock only)."""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from wallex_agent.clock import SimClock
from wallex_agent.config import Config
from wallex_agent.exchange.sim import SimExchange, StaticBookSource
from wallex_agent.execution import ExecutionEngine, ListAudit
from wallex_agent.portfolio import Portfolio
from wallex_agent.state import StateMachine
from wallex_agent.store import InMemoryStore, SqliteStore
from wallex_agent.types import FeeRates, MarketRules, Side, TradeIntent

BTC = "BTCUSDT"
USD = "USDTTMN"
INITIAL = D("5000000")
RULES = {BTC: MarketRules(BTC, "BTC", "USDT", 8, 2, D("1e-8"), D("1")),
         USD: MarketRules(USD, "USDT", "TMN", 2, 0, D("0.01"), D("50000"))}
FEES = {BTC: FeeRates(BTC, D("0.002"), D("0.0035")), USD: FeeRates(USD, D("0.002"), D("0.0035"))}
BTC_BIDS = [(D("81000"), D("0.01")), (D("80990"), D("0.02")), (D("80950"), D("0.5"))]
BTC_ASKS = [(D("81010"), D("0.01")), (D("81020"), D("0.02")), (D("81060"), D("0.5"))]
USD_BIDS = [(D("271900"), D("100")), (D("271800"), D("1000"))]
USD_ASKS = [(D("272000"), D("100")), (D("272100"), D("1000"))]


def make_config(tmp_path: Path | str, **kw: Any) -> Config:
    return replace(Config(data_dir=str(tmp_path)), **kw)


@dataclass
class World:
    tmp: Path
    cfg: Config
    clock: SimClock
    source: StaticBookSource
    sim: SimExchange
    store: Any
    portfolio: Portfolio
    sm: StateMachine
    audit: ListAudit
    engine: ExecutionEngine
    db_path: Path | None = None

    # --- wiring helpers
    def build_engine(self) -> ExecutionEngine:
        self.portfolio = Portfolio(self.store, INITIAL, rules=RULES)
        self.sm = StateMachine(self.store, self.clock)
        self.engine = ExecutionEngine(self.sim, self.store, self.portfolio, self.sm, self.cfg, self.clock,
                                      self.audit, rng=random.Random(7))
        return self.engine

    def restart(self) -> ExecutionEngine:
        """Simulate a process restart: drop the engine; reopen the DB (file stores)."""
        if self.db_path is not None:
            try:
                self.store.close()
            except Exception:
                pass
            self.store = SqliteStore(self.db_path, clock=self.clock)
        return self.build_engine()

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    # --- intents
    def entry(self, qty: str = "0.0002", **kw: Any) -> TradeIntent:
        args = dict(purpose="entry", side=Side.BUY, symbol=BTC, quantity=D(qty),
                    reference_price=self.source.get_book(BTC).best_ask, max_slippage_bps=D("20"))
        args.update(kw)
        return TradeIntent(**args)

    def exit(self, qty: str | D | None = None, **kw: Any) -> TradeIntent:
        held = self.portfolio.expected_balances().get("BTC", D(0))
        args = dict(purpose="exit", side=Side.SELL, symbol=BTC, quantity=D(qty) if qty is not None else held,
                    reference_price=self.source.get_book(BTC).best_bid, max_slippage_bps=D("20"))
        args.update(kw)
        return TradeIntent(**args)

    def ledger_matches_sim(self, assets=("BTC", "USDT", "TMN")) -> bool:
        exp = self.portfolio.expected_balances()
        return all(exp.get(a, D(0)) == self.sim.total(a) for a in assets)


def make_world(tmp_path: Path, *, file_db: bool = False, balances: dict[str, D] | None = None,
               btc_book=(BTC_BIDS, BTC_ASKS), usd_book=(USD_BIDS, USD_ASKS), sim_kw: dict | None = None,
               **cfg_kw: Any) -> World:
    clock = SimClock(1_700_000_000.0)
    cfg = make_config(tmp_path, **cfg_kw)
    source = StaticBookSource(clock)
    source.set_book(BTC, *btc_book)
    source.set_book(USD, *usd_book)
    sim = SimExchange(source, clock, RULES, FEES, balances or {"TMN": INITIAL, "USDT": D(0), "BTC": D(0)},
                      rng=random.Random(11), **(sim_kw or {}))
    db_path = tmp_path / "db" / "state.sqlite" if file_db else None
    store = SqliteStore(db_path, clock=clock) if file_db else InMemoryStore(clock=clock)
    w = World(tmp_path, cfg, clock, source, sim, store, None, None, ListAudit(), None, db_path)  # type: ignore[arg-type]
    w.build_engine()
    return w


def funded_world(tmp_path: Path, **kw: Any) -> World:
    """A world that has converted its TMN into USDT through the engine (state READY)."""
    w = make_world(tmp_path, **kw)
    res = w.engine.convert_tmn_to_usdt()
    assert res.status == "done", res
    return w


def held_world(tmp_path: Path, qty: str = "0.0002", **kw: Any) -> World:
    w = funded_world(tmp_path, **kw)
    r = w.engine.submit(w.entry(qty))
    assert r.status == "filled", r
    return w
