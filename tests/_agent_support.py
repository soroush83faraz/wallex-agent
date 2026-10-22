"""Builders for the agent tests: observations, histories and a decisions-only paper loop over the
seeded SyntheticMarket (instant fills by walking the synthetic book; no SimExchange, no network)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from decimal import Decimal as D
from typing import Any

from tests._sim_support import FEES, RULES
from wallex_agent.agent import AgentBrain, AgentMemory, Observation, PlanContext, Planner, PriceHistory
from wallex_agent.clock import SimClock
from wallex_agent.config import Config
from wallex_agent.market_data import BTC_TEMPLATE, USDTTMN_TEMPLATE, SyntheticMarket, book_from_template
from wallex_agent.portfolio import Position
from wallex_agent.store import InMemoryStore
from wallex_agent.types import Action, Balance, BotState, Side

BTC, USD = "BTCUSDT", "USDTTMN"
T0 = 1_700_000_000.0
ZERO_TIMER = lambda: 0.0  # noqa: E731  deterministic latency for equality tests


def btc_book(mid: float, ts: float, half_spread_bps: float = 3.0):
    return book_from_template(BTC, mid, BTC_TEMPLATE, ts, half_spread_bps=half_spread_bps)


def fx_book(mid: float, ts: float, half_spread_bps: float = 0.8):
    return book_from_template(USD, mid, USDTTMN_TEMPLATE, ts, half_spread_bps=half_spread_bps,
                              price_decimals=0, qty_decimals=2)


def prices(n: int, start: float = 81000.0, bps_per_min: float = 0.0, noise_bps: float = 2.0,
           seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        p *= math.exp((bps_per_min + rng.gauss(0.0, noise_bps)) / 10000.0)
        out.append(p)
    return out


def history(ps: list[float], now: float) -> PriceHistory:
    h = PriceHistory()
    for i, p in enumerate(ps):
        h.add(now - 60.0 * (len(ps) - 1 - i), p)
    return h


def obs(now: float, mid: float, *, state: BotState = BotState.READY, usdt: str = "18", tmn: str = "0",
        btc: str = "0", position: Any = None, fx_mid: float = 272500.0, fx_half_spread: float = 0.8,
        btc_half_spread: float = 3.0, fees: Any = FEES, rules: Any = RULES, book_ts: float | None = None,
        **kw: Any) -> Observation:
    ts = now if book_ts is None else book_ts
    books = {BTC: btc_book(mid, ts, btc_half_spread), USD: fx_book(fx_mid, ts, fx_half_spread)}
    bal = {"USDT": Balance("USDT", D(usdt)), "TMN": Balance("TMN", D(tmn)), "BTC": Balance("BTC", D(btc))}
    base = dict(now=now, books=books, balances=bal, state=state, fees=fees, rules=rules, position=position,
                equity_tmn=D("5000000"), peak_equity_tmn=D("5000000"), round_trips_done=0,
                time_left_s=43200.0, total_s=86400.0)
    base.update(kw)
    return Observation(**base)


def plan(o: Observation, ps: list[float], *, memory: AgentMemory | None = None, cfg: Config | None = None,
         cycle: int = 1, **ctx_kw: Any):
    cfg = cfg or Config()
    memory = memory or AgentMemory(InMemoryStore())
    ctx = PlanContext(cycle=cycle, history=history(ps, o.now), fx_history=PriceHistory(), timer=ZERO_TIMER, **ctx_kw)
    return Planner(memory, cfg).decide(o, ctx)


@dataclass
class PaperLoop:
    """Decisions-only plumbing: instant fills at walked prices, taker fee in the received asset."""

    market: SyntheticMarket
    cfg: Config
    clock: SimClock = field(default_factory=SimClock)
    bal: dict[str, D] = field(default_factory=lambda: {"TMN": D(5000000), "USDT": D(0), "BTC": D(0)})
    state: BotState = BotState.UNFUNDED
    pos: Position | None = None
    trips: int = 0
    entry_cost: D = D(0)
    decisions: list[Any] = field(default_factory=list)
    reflections: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.src = self.market.build_source(self.clock)
        self.end = self.clock.now() + self.market.n_steps * self.market.step_s
        self.done = False

    def brain(self, store: Any) -> AgentBrain:
        return AgentBrain(self.cfg, store, self.clock, book_source=self.src, timer=ZERO_TIMER)

    def step(self, brain: AgentBrain) -> Any:
        now = self.clock.now()
        books = {s: self.src.get_book(s) for s in (BTC, USD)}
        o = Observation(now=now, books=books, balances={k: Balance(k, v) for k, v in self.bal.items()},
                        state=self.state, fees=FEES, rules=RULES, position=self.pos,
                        equity_tmn=D(5000000), peak_equity_tmn=D(5000000), round_trips_done=self.trips,
                        time_left_s=self.end - now, total_s=self.end - self.market.start_ts)
        d = brain.decide(o)
        brain.record_decision(d)
        self.decisions.append(d)
        fee = FEES[BTC].taker
        if d.action is Action.CONVERT_TMN_USDT:
            w = books[USD].walk_quote(Side.BUY, self.bal["TMN"])
            self.bal["USDT"] += w.filled_qty * (1 - FEES[USD].taker)
            self.bal["TMN"] -= w.quote_sum
            self.state = BotState.READY
            brain.on_order_result(d.decision_id, {"status": "filled", "filled_qty": str(w.filled_qty)})
        elif d.action is Action.BUY_BTC:
            q = RULES[BTC].round_qty(d.intent.quantity)
            w = books[BTC].walk_qty(Side.BUY, q)
            got = q * (1 - fee)
            self.bal["USDT"] -= w.quote_sum
            self.bal["BTC"] += got
            self.entry_cost = w.quote_sum
            self.pos = Position(got, w.quote_sum / got, now, D(0), self.trips, 0, 0, cost_usdt=w.quote_sum)
            self.state = BotState.BTC_HELD
            brain.on_order_result(d.decision_id, {"status": "filled", "filled_qty": str(q),
                                                  "avg_price": str(w.avg_price)})
        elif d.action is Action.SELL_BTC:
            q = self.bal["BTC"]
            w = books[BTC].walk_qty(Side.SELL, q)
            proceeds = w.quote_sum * (1 - fee)
            self.bal["USDT"] += proceeds
            self.bal["BTC"] = D(0)
            self.trips += 1
            self.state = BotState.READY
            self.pos = None
            brain.on_order_result(d.decision_id, {"status": "filled", "filled_qty": str(q)})
            self.reflections.append(brain.on_round_trip_closed(
                pnl_usdt=proceeds - self.entry_cost, cost_usdt=self.entry_cost, exit_ts=now,
                exit_decision_id=d.decision_id, reason=d.hypothesis["strategy"]))
        self.reflections.extend(brain.reflect_pending(now, books))
        if not self.src.advance():
            self.done = True
        return d


def run_loop(seed: int, cfg: Config | None = None, *, n_steps: int = 1440, schedule=None, store: Any = None):
    m = SyntheticMarket(seed=seed, n_steps=n_steps, regime_schedule=schedule)
    loop = PaperLoop(m, cfg or Config())
    store = store if store is not None else InMemoryStore(clock=loop.clock)
    brain = loop.brain(store)
    while not loop.done:
        loop.step(brain)
    return loop, brain


__all__ = ["BTC", "USD", "T0", "ZERO_TIMER", "btc_book", "fx_book", "prices", "history", "obs", "plan",
           "PaperLoop", "run_loop", "FEES", "RULES"]
