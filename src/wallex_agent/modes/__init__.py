"""Runtime wiring per mode: ``build_runtime(config, mode, **opts) -> Runtime``.

* **backtest** (``modes/backtest.py``): ``SimExchange`` over a replay of recorded candles or a seeded
  ``SyntheticMarket``, ``SimClock``, zero network, no key, ASSUMED fee (``config.paper_fee_rate``).
* **paper** (``modes/paper.py``): ``SimExchange`` over live public books from a
  ``WallexClient(None, public_only=True)``: that client cannot send a POST/DELETE or any private GET
  (``ForbiddenEndpointError`` before any I/O), and no keyed client exists in paper at all.
* **live** (``modes/live.py``): the ONLY place a keyed client with write permission is built, and only
  after ``live_gate.check_live_allowed`` passed.

Each mode has its own directory ``<data_dir>/<mode>/`` (ledgers are never mixed). Backtest and paper
runs get a fresh store per run under ``<data_dir>/<mode>/runs/<run_id>/``; live keeps ONE store
(``<data_dir>/live/state.db``) across restarts so a resume continues the same ledger, audit chain and
competition clock. Operator evidence (selftest, attestation, canary, KILL, LIVE_ARMED) stays in
``<data_dir>``.
"""
from __future__ import annotations

import json
import random
import secrets
import sqlite3
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from ..agent import AgentBrain
from ..clock import Clock
from ..config import Config
from ..execution import ExecutionEngine
from ..portfolio import Portfolio
from ..reporting import AuditLog
from ..risk import RiskGate
from ..state import StateMachine
from ..store import _SqlStore
from ..types import Mode, OrderBook

LAST_RUN_FILE = "LAST_RUN"
DB_NAME = "state.db"
AUDIT_NAME = "audit.jsonl"


# --------------------------------------------------------------------------- paths
def mode_dir(config: Config, mode: Mode | str) -> Path:
    return config.state_path / Mode(mode).value


def runs_dir(config: Config, mode: Mode | str) -> Path:
    return mode_dir(config, mode) / "runs"


def live_store_path(config: Config) -> Path:
    return mode_dir(config, Mode.LIVE) / DB_NAME


def new_run_id(mode: Mode | str) -> str:
    """Directory-safe, unique id (wall time + random); never derived from a simulated clock."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{Mode(mode).value}-{stamp}-{secrets.token_hex(3)}"


def write_last_run(config: Config, mode: Mode | str, run_id: str) -> None:
    d = mode_dir(config, mode)
    d.mkdir(parents=True, exist_ok=True)
    (d / LAST_RUN_FILE).write_text(run_id + "\n", encoding="utf-8")


def last_run(config: Config, mode: Mode | str) -> str | None:
    try:
        text = (mode_dir(config, mode) / LAST_RUN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def run_paths(config: Config, mode: Mode | str, run_id: str | None = None) -> tuple[str | None, Path | None, Path | None]:
    """``(run_id, run_dir, db_path)`` of a run (default: the mode's last run)."""
    rid = run_id or last_run(config, mode)
    if rid is None:
        return None, None, None
    rdir = runs_dir(config, mode) / rid
    db = live_store_path(config) if Mode(mode) is Mode.LIVE else rdir / DB_NAME
    return rid, rdir, db


def open_snapshot(db_path: str | Path, clock: Clock | None = None) -> _SqlStore:
    """Read-only, lock-free COPY of a store (sqlite backup into memory). Used by ``status``,
    ``report`` and ``trace`` so they never take (or wait for) a running agent's instance lock."""
    p = Path(db_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no store at {p}")
    src = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    try:
        src.backup(mem)
    finally:
        src.close()
    return _SqlStore(mem, clock)


# --------------------------------------------------------------------------- transport recorder
class RecordingTransport:
    """``Transport`` wrapper that records request METADATA only: method, path, query parameter
    NAMES, whether an ``X-API-Key`` header was attached, HTTP status, body size and JSON key names.
    It never stores header values, bodies or amounts. With ``responder`` (tests) nothing touches the
    network: ``responder(method, url, headers, body) -> (status, headers, body_bytes)``."""

    def __init__(self, inner: Any = None, *, responder: Callable[..., tuple[int, Mapping[str, str], bytes]] | None = None,
                 maxlen: int = 1000) -> None:
        if inner is None and responder is None:
            raise ValueError("RecordingTransport needs an inner transport or a responder")
        self.inner = inner
        self.responder = responder
        self.records: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                timeout: float) -> tuple[int, Mapping[str, str], bytes]:
        parts = urllib.parse.urlsplit(url)
        rec: dict[str, Any] = {
            "method": str(method).upper(), "host": parts.hostname, "path": parts.path,
            "query_keys": sorted(urllib.parse.parse_qs(parts.query).keys()),
            "with_key": any(str(k).lower() == "x-api-key" for k in headers),
            "has_body": body is not None, "status": None, "bytes": None, "json_keys": None,
            "result_keys": None, "error": None}
        try:
            if self.responder is not None:
                status, hdrs, raw = self.responder(method, url, headers, body)
            else:
                status, hdrs, raw = self.inner.request(method, url, headers, body, timeout)
        except Exception as exc:
            rec["error"] = type(exc).__name__
            self.records.append(rec)
            raise
        rec["status"], rec["bytes"] = status, len(raw)
        try:
            doc = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (UnicodeDecodeError, ValueError):
            doc = None
        if isinstance(doc, dict):
            rec["json_keys"] = sorted(str(k)[:40] for k in doc)[:40]
            if isinstance(doc.get("result"), dict):
                rec["result_keys"] = sorted(str(k)[:40] for k in doc["result"])[:60]
        self.records.append(rec)
        return status, hdrs, raw

    def last(self, method: str, path_prefix: str = "") -> dict[str, Any] | None:
        for r in reversed(self.records):
            if r["method"] == method.upper() and str(r["path"]).startswith(path_prefix):
                return dict(r)
        return None


# --------------------------------------------------------------------------- books
class CachedBookSource:
    """Short-TTL cache in front of a (live, public) ``BookSource`` so the simulator's matching and the
    agent's reads in one cycle do not hammer the public API. The cached book keeps its receipt time."""

    def __init__(self, inner: Any, clock: Clock, ttl_s: float = 2.0) -> None:
        self.inner = inner
        self.clock = clock
        self.ttl_s = float(ttl_s)
        self._cache: dict[str, OrderBook] = {}

    def get_book(self, symbol: str) -> OrderBook:
        b = self._cache.get(symbol)
        if b is not None and 0 <= self.clock.now() - b.ts <= self.ttl_s:
            return b
        b = self.inner.get_book(symbol)
        self._cache[symbol] = b
        return b

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Any]:
        return list(self.inner.get_candles(symbol, resolution, start, end))


# --------------------------------------------------------------------------- runtime
class EngineAudit:
    """Engine audit sink -> AuditLog (normalises the engine's ``ALERT`` kind to ``alert``)."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.audit.write("alert" if kind.upper() == "ALERT" else kind, payload)


@dataclass
class Runtime:
    mode: Mode
    config: Config
    run_id: str
    run_dir: Path
    clock: Clock
    exchange: Any
    book_source: Any
    store: Any
    portfolio: Portfolio
    sm: StateMachine
    engine: ExecutionEngine
    brain: AgentBrain
    risk: RiskGate
    audit: AuditLog
    labels: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    synthetic: bool = False
    advance: Callable[[], bool] | None = None     # backtest: move the replay to the next step
    recorder: RecordingTransport | None = None
    gate: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def secrets(self) -> list[str]:
        return [self.config.api_key] if self.config.api_key else []

    def close(self) -> None:
        for fn in (self.audit.close, self.store.close):
            try:
                fn()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


def assemble(config: Config, mode: Mode, *, run_id: str, run_dir: Path, store: Any, clock: Clock,
             exchange: Any, book_source: Any, labels: list[str] | None = None,
             caveats: list[str] | None = None, synthetic: bool = False,
             advance: Callable[[], bool] | None = None, recorder: RecordingTransport | None = None,
             brain_timer: Callable[[], float] | None = None, rng_seed: int | None = None,
             audit_run_id: str | None = None, durable_audit: bool = True,
             state_root: Path | None = None) -> Runtime:
    """Common wiring: ledger, state machine, engine, brain, risk gate and the hash-chained audit."""
    run_dir.mkdir(parents=True, exist_ok=True)
    portfolio = Portfolio(store, config.initial_tmn, btc_symbol=config.btc_symbol,
                          usdt_symbol=config.usdt_symbol)
    sm = StateMachine(store, clock)
    seed = config.seed if rng_seed is None else rng_seed
    engine = ExecutionEngine(exchange, store, portfolio, sm, config, clock, None,
                             rng=random.Random(f"engine:{seed}"))
    audit = AuditLog(run_dir / AUDIT_NAME, run_id=audit_run_id or run_id, clock=clock,
                     secrets=[config.api_key] if config.api_key else [], durable=durable_audit)
    engine.audit = EngineAudit(audit)
    brain = AgentBrain(config, store, clock, book_source=book_source, timer=brain_timer)
    rt = Runtime(mode=mode, config=config, run_id=run_id, run_dir=run_dir, clock=clock, exchange=exchange,
                 book_source=book_source, store=store, portfolio=portfolio, sm=sm, engine=engine, brain=brain,
                 risk=RiskGate(config, clock), audit=audit, labels=list(labels or []),
                 caveats=list(caveats or []), synthetic=synthetic, advance=advance, recorder=recorder)
    root = config if state_root is None else replace(config, data_dir=str(state_root))
    write_last_run(root, mode, run_id)
    return rt


def build_runtime(config: Config, mode: Mode | str, **opts: Any) -> Runtime:
    """Dispatch to the mode builder. ``live`` requires the gate inputs (``flag_ack``,
    ``typed_phrase``/``input_fn``, ``resume``) and raises ``LiveNotAllowedError`` otherwise."""
    m = Mode(mode)
    if m is Mode.BACKTEST:
        from .backtest import build_backtest
        return build_backtest(config, **opts)
    if m is Mode.PAPER:
        from .paper import build_paper
        return build_paper(config, **opts)
    from .live import build_live
    return build_live(config, **opts)


__all__ = ["Runtime", "RecordingTransport", "CachedBookSource", "EngineAudit", "assemble", "build_runtime",
           "mode_dir", "runs_dir", "live_store_path", "new_run_id", "last_run", "write_last_run", "run_paths",
           "open_snapshot", "LAST_RUN_FILE", "DB_NAME", "AUDIT_NAME"]
