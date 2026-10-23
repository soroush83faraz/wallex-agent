"""Builders for runner / modes / CLI / e2e / canary tests: SimClock + SimExchange worlds wired through
the real ``modes`` builders, a scripted brain for deterministic trading flows, and a fixture-backed
fake Wallex transport (public endpoints only)."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any, Callable

from tests._sim_support import BTC, BTC_ASKS, BTC_BIDS, FEES, INITIAL, RULES, USD, USD_ASKS, USD_BIDS
from wallex_agent.agent import AgentBrain
from wallex_agent.clock import SimClock
from wallex_agent.config import Config
from wallex_agent.exchange.sim import SimExchange, StaticBookSource
from wallex_agent.modes import RecordingTransport, assemble
from wallex_agent.modes.backtest import build_backtest
from wallex_agent.store import SqliteStore
from wallex_agent.types import Action, BotState, Decision, Mode, Side, TradeIntent, plain

T0 = 1_700_000_000.0
FIXTURES = Path(__file__).parent / "fixtures"
SENTINEL = "SENTINELkey-0123456789abcdefXYZ"
OPEN_RUNTIMES: list[Any] = []      # closed by the autouse fixture in tests/conftest.py


def track(rt: Any) -> Any:
    OPEN_RUNTIMES.append(rt)
    return rt


# --------------------------------------------------------------------------- scripted brain
Script = Callable[["ScriptedBrain", Any], tuple[Action, TradeIntent | None]]


class ScriptedBrain(AgentBrain):
    """Real AgentBrain (memory, reflection, record_decision) with a scripted ``decide``."""

    def __init__(self, config: Config, store: Any, clock: Any, script: Script) -> None:
        super().__init__(config, store, clock, timer=lambda: 0.0)
        self.script = script
        self.decided: list[Decision] = []

    def decide(self, obs: Any) -> Decision:
        self._cycle += 1
        action, intent = self.script(self, obs)
        prices: dict[str, str] = {}
        for s, b in obs.books.items():
            prices.update({f"{s}.bid": plain(b.best_bid), f"{s}.ask": plain(b.best_ask), f"{s}.mid": plain(b.mid)})
        d = Decision(decision_id=f"s{self._cycle:05d}", ts=obs.now, cycle=self._cycle, strategy_version="scripted",
                     action=action, rationale=f"scripted {action.value}", confidence=0.9,
                     estimated_net_edge=D("0.02"),
                     hypothesis={"strategy": "scripted", "direction": "up", "expected_move_bps": D("300"),
                                 "horizon_s": 600.0, "regime": "chop", "invalidation": {"note": "test"}},
                     observed_prices=prices, intent=intent,
                     tool_calls=[{"tool": "read_book", "called": True, "why": "test", "ok": True,
                                  "latency_ms": 0.0, "cost": 1}])
        self.decided.append(d)
        return d


def held(obs: Any, asset: str) -> D:
    b = obs.balances.get(asset)
    v = b.spendable if b else D(0)
    return max(D(0), min(v, (obs.ledger or {}).get(asset, D(0))))


def convert_intent(obs: Any, cfg: Config) -> TradeIntent:
    ask = obs.books[USD].best_ask
    return TradeIntent("convert", Side.BUY, USD, held(obs, "TMN") / ask, ask, cfg.conversion_slippage_bps)


def entry_intent(obs: Any, qty: str = "0.00005", style: str = "aggressive_limit") -> TradeIntent:
    return TradeIntent("entry", Side.BUY, BTC, D(qty), obs.books[BTC].best_ask, D("20"), style=style)


def exit_intent(obs: Any) -> TradeIntent:
    return TradeIntent("exit", Side.SELL, BTC, held(obs, "BTC"), obs.books[BTC].best_bid, D("20"))


def trader_script(*, buy_every: int = 4, hold_cycles: int = 2, qty: str = "0.00005",
                  entry_style: str = "aggressive_limit") -> Script:
    """convert when UNFUNDED; buy when READY every ``buy_every`` cycles; sell after ``hold_cycles``."""
    state = {"bought_at": None}

    def script(brain: ScriptedBrain, obs: Any) -> tuple[Action, TradeIntent | None]:
        n = brain._cycle
        if BTC not in obs.books or USD not in obs.books:
            return Action.HOLD, None
        if obs.state in (BotState.UNFUNDED, BotState.CONVERTING):
            return Action.CONVERT_TMN_USDT, convert_intent(obs, brain.config)
        if obs.state is BotState.READY and n % buy_every == 0:
            state["bought_at"] = n
            return Action.BUY_BTC, entry_intent(obs, qty, entry_style)
        if obs.state is BotState.BTC_HELD and n - (state["bought_at"] or n) >= hold_cycles:
            return Action.SELL_BTC, exit_intent(obs)
        return Action.HOLD, None
    return script


def hold_script(brain: ScriptedBrain, obs: Any) -> tuple[Action, TradeIntent | None]:
    return Action.HOLD, None


# --------------------------------------------------------------------------- worlds
@dataclass
class SimWorld:
    tmp: Path
    cfg: Config
    clock: SimClock
    source: StaticBookSource
    sim: SimExchange
    step_s: float
    run_id: str = "run-test"

    @property
    def run_dir(self) -> Path:
        return self.tmp / "backtest" / "runs" / self.run_id

    def advance(self) -> bool:
        self.clock.advance(self.step_s)
        return True

    def runtime(self, script: Script | None = None) -> Any:
        """(Re)build a runtime over the SAME simulated exchange and the same store file (a restart)."""
        rt = build_backtest(self.cfg, exchange=self.sim, clock=self.clock, book_source=self.source,
                            advance=self.advance, run_id=self.run_id, run_dir=self.run_dir)
        if script is not None:
            rt.brain = ScriptedBrain(rt.config, rt.store, self.clock, script)
        return track(rt)


def sim_world(tmp_path: Path, *, step_s: float = 60.0, balances: dict[str, D] | None = None,
              sim_kw: dict | None = None, **cfg_kw: Any) -> SimWorld:
    import random
    clock = SimClock(T0)
    cfg = replace(Config(data_dir=str(tmp_path)), **cfg_kw)
    source = StaticBookSource(clock)
    source.set_book(BTC, BTC_BIDS, BTC_ASKS)
    source.set_book(USD, USD_BIDS, USD_ASKS)
    sim = SimExchange(source, clock, RULES, FEES, balances or {"TMN": INITIAL, "USDT": D(0), "BTC": D(0)},
                      rng=random.Random(5), **(sim_kw or {}))
    return SimWorld(tmp_path, cfg, clock, source, sim, step_s)


def live_like_runtime(tmp_path: Path, *, sim_kw: dict | None = None, balances: dict[str, D] | None = None,
                      preflight: bool = True, **cfg_kw: Any) -> tuple[Any, SimExchange, SimClock]:
    """A LIVE-mode Runtime wired to a SimExchange (canary tests): the canary file goes to tmp_path. With
    ``preflight`` (default) it does what ``cli canary`` does before ``run_canary``: ``reconcile_startup``
    then the engine's connectivity preflight (both must pass)."""
    import random
    clock = SimClock(T0)
    cfg = replace(Config(data_dir=str(tmp_path), mode=Mode.LIVE), **cfg_kw)
    source = StaticBookSource(clock)
    source.set_book(BTC, BTC_BIDS, BTC_ASKS)
    source.set_book(USD, USD_BIDS, USD_ASKS)
    sim = SimExchange(source, clock, RULES, FEES, balances or {"TMN": INITIAL, "USDT": D(0), "BTC": D(0)},
                      rng=random.Random(9), **(sim_kw or {}))
    store = SqliteStore(tmp_path / "live" / "state.db", clock=clock)
    rt = assemble(cfg, Mode.LIVE, run_id="live-test", run_dir=tmp_path / "live" / "runs" / "live-test", store=store,
                  clock=clock, exchange=sim, book_source=source, labels=["test"])
    track(rt)
    if preflight:
        assert rt.engine.reconcile_startup()["ok"]
        assert rt.engine.preflight()["ok"]
    return rt, sim, clock


def ledger_matches(rt: Any, sim: SimExchange, assets=("BTC", "USDT", "TMN")) -> bool:
    exp = rt.portfolio.expected_balances()
    return all(exp.get(a, D(0)) == sim.total(a) for a in assets)


# --------------------------------------------------------------------------- fake public Wallex
def _envelope(path: str) -> bytes:
    doc = json.loads((FIXTURES / path).read_text(encoding="utf-8"))
    if "success" not in doc:
        doc = {"success": True, "message": "fixture", "result": doc["result"]}
    return json.dumps(doc).encode("utf-8")


def public_responder() -> Callable[..., tuple[int, dict[str, str], bytes]]:
    """Serves the real public captures; anything else is a 404 (a test would notice)."""
    markets = _envelope("markets_real_2026-10-22.json")
    depth = {"BTCUSDT": _envelope("depth_btcusdt_real.json"), "USDTTMN": _envelope("depth_usdttmn_real.json")}
    udf = (FIXTURES / "udf_btcusdt_1m_real.json").read_bytes()

    def respond(method: str, url: str, headers: Any, body: Any) -> tuple[int, dict[str, str], bytes]:
        from urllib.parse import parse_qs, urlsplit
        parts = urlsplit(url)
        q = parse_qs(parts.query)
        if method == "GET" and parts.path == "/v1/markets":
            return 200, {}, markets
        if method == "GET" and parts.path == "/v1/depth":
            return 200, {}, depth[q["symbol"][0]]
        if method == "GET" and parts.path == "/v1/udf/history":
            return 200, {}, udf
        return 404, {}, b'{"success": false, "message": "not in fixture"}'
    return respond


def recording_public_transport() -> RecordingTransport:
    return RecordingTransport(responder=public_responder())
