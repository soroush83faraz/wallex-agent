"""Live wiring. THE ONLY MODULE THAT BUILDS A KEYED CLIENT WITH WRITE PERMISSION.

``_keyed_write_client`` is called from exactly three places, each after its own gate:

* ``build_live``            - after ``live_gate.check_live_allowed`` (all live conditions incl. canary);
* ``build_canary_runtime``  - after the same gate with the canary phrase (``RUN CANARY <tmn>``) and
  without the canary condition (the canary is what produces that evidence);
* ``build_cancel_only_runtime`` - for ``kill --cancel-open``: the client is wrapped in
  ``CancelOnlyExchange``, whose ``create_order`` refuses locally, so it can only read and cancel.

Everything else that needs the key (``check-live``, ``unstop`` via ``build_unstop_runtime``) gets
``read_only=True`` (no POST/DELETE possible). ``tests/test_modes.py`` greps the source tree to keep it
that way.
"""
from __future__ import annotations

import secrets
from dataclasses import replace
from typing import Any, Callable

from .. import live_gate
from ..clock import Clock, RealClock
from ..config import Config
from ..errors import ForbiddenEndpointError, LiveNotAllowedError
from ..exchange.probe import unverified_after_canary
from ..exchange.wallex import UrllibTransport, WallexClient
from ..execution import K_RUN
from ..market_data import LiveBookSource
from ..store import SqliteStore
from ..types import Mode
from . import RecordingTransport, Runtime, assemble, live_store_path, open_snapshot, runs_dir
from .paper import public_client

K_COMPETITION_START = "runner.competition_start"   # (runner.py owns the key; read here to detect a resume)


# --------------------------------------------------------------------------- clients
def _transport(config: Config, transport: Any) -> RecordingTransport:
    inner = transport if transport is not None else UrllibTransport(use_env_proxy=config.use_env_proxy)
    return inner if isinstance(inner, RecordingTransport) else RecordingTransport(inner)


def _keyed_write_client(config: Config, transport: Any, clock: Clock) -> WallexClient:
    """Private on purpose: only the gated builders below may call it."""
    return WallexClient(config.api_key, base_url=config.api_base, transport=transport, clock=clock,
                        timeout=config.http_timeout_s, max_rps=config.max_requests_per_s,
                        order_rps=config.order_requests_per_s)


def read_only_client(config: Config, *, transport: Any = None, clock: Clock | None = None) -> WallexClient:
    """Keyed but GET-only (``check-live``): every POST/DELETE raises before any I/O."""
    return WallexClient(config.api_key, read_only=True, base_url=config.api_base,
                        transport=transport if transport is not None
                        else UrllibTransport(use_env_proxy=config.use_env_proxy),
                        clock=clock, timeout=config.http_timeout_s, max_rps=config.max_requests_per_s)


class CancelOnlyExchange:
    """Reads + ``cancel_order`` of a keyed client; ``create_order`` is refused locally (never sent)."""

    def __init__(self, client: WallexClient) -> None:
        self._c = client

    def get_markets(self) -> Any:
        return self._c.get_markets()

    def get_order_book(self, symbol: str) -> Any:
        return self._c.get_order_book(symbol)

    get_book = get_order_book

    def get_balances(self) -> Any:
        return self._c.get_balances()

    def get_fees(self) -> Any:
        return self._c.get_fees()

    def get_order(self, client_order_id: str) -> Any:
        return self._c.get_order(client_order_id)

    def list_open_orders(self, symbol: str | None = None) -> Any:
        return self._c.list_open_orders(symbol)

    def get_trades(self, symbol: str | None = None, side: Any = None) -> Any:
        return self._c.get_trades(symbol, side)

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> Any:
        return self._c.get_candles(symbol, resolution, start, end)

    def cancel_order(self, client_order_id: str) -> None:
        return self._c.cancel_order(client_order_id)

    def create_order(self, req: Any) -> Any:
        raise ForbiddenEndpointError("cancel-only session: placing orders is impossible here")

    def __repr__(self) -> str:
        return f"CancelOnlyExchange({self._c!r})"


# --------------------------------------------------------------------------- helpers
def competition_started(config: Config) -> bool:
    """True when the live store exists and already holds a competition start (=> a resume)."""
    path = live_store_path(config)
    if not path.is_file():
        return False
    try:
        snap = open_snapshot(path)
    except Exception:  # noqa: BLE001 - unreadable => not a (provable) resume
        return False
    try:
        return snap.kv_get(K_COMPETITION_START) is not None
    finally:
        snap.close()


def _open_live_store(config: Config, clock: Clock) -> tuple[SqliteStore, str]:
    store = SqliteStore(live_store_path(config), clock=clock)
    run = store.kv_get(K_RUN)
    if run is None:     # same format the engine would create; set here so the audit dir is known
        run = f"{int(clock.now()) % 0xFFFFFFFF:08x}{secrets.randbits(16):04x}"
        store.kv_set(K_RUN, run)
    return store, f"live-{run}"


def _live_runtime(cfg: Config, *, clock: Clock, transport: RecordingTransport, client: Any,
                  gate: dict[str, Any] | None, label: str) -> Runtime:
    store, rid = _open_live_store(cfg, clock)
    try:
        books = LiveBookSource(public_client(cfg, transport=transport, clock=clock), clock)
        now = clock.now()
        verified = live_gate.canary_verified_items(cfg, cfg.state_path, now)
        still = [k for k, _ in unverified_after_canary(verified)]
        caveats = ["LIVE: orders were REAL; fills and fees are the exchange's.",
                   f"Canary-verified API behaviours: {', '.join(verified) or 'none'}; still unverified: "
                   f"{', '.join(still) or 'none'}."]
        rt = assemble(cfg, Mode.LIVE, run_id=rid, run_dir=runs_dir(cfg, Mode.LIVE) / rid, store=store,
                      clock=clock, exchange=client, book_source=books, labels=[label], caveats=caveats,
                      recorder=transport)
    except BaseException:
        store.close()
        raise
    rt.gate = gate
    if gate is not None:
        rt.audit.write("live_gate", {k: v for k, v in gate.items() if k != "conditions"}
                       | {"conditions": [{"name": c["name"], "met": c["met"]} for c in gate["conditions"]]})
    return rt


# --------------------------------------------------------------------------- builders
def build_live(config: Config, *, flag_ack: bool, typed_phrase: str | None = None, resume: bool = False,
               input_fn: Callable[[str], str] | None = None, transport: Any = None,
               clock: Clock | None = None, now: float | None = None) -> Runtime:
    """Gate first, then (and only then) the keyed client.

    ``resume`` (systemd, no TTY) only CONTINUES a competition already started in this live store
    (``runner.competition_start`` present): the first live start is always an operator at a terminal
    typing the phrase. It takes the phrase from the structured ``<state>/LIVE_ARMED`` record, which
    must match the current phrase, source tree, base URL and data dir and be unexpired, and it accepts
    selftest/attestation evidence up to 48 h old; a fresh start always needs <= 24 h."""
    clk = clock or RealClock()
    cfg = replace(config, mode=Mode.LIVE)
    t = float(now if now is not None else clk.now())
    phrase = typed_phrase
    resuming = False
    if resume:
        if not competition_started(cfg):
            raise LiveNotAllowedError(
                f"--resume only CONTINUES a competition already started in {live_store_path(cfg)}; none is "
                "started there. The first live start must be interactive: run `wallex-agent live "
                "--i-understand-real-money` at a terminal and type the confirmation phrase.")
        resuming = True
        if phrase is None:
            doc, why = live_gate.read_arming(cfg.state_path)
            path = cfg.state_path / live_gate.ARMING_FILE
            if doc is None:
                raise LiveNotAllowedError(f"--resume needs a valid arming record {path} ({why}); run "
                                          "`wallex-agent arm-live --i-understand-real-money` at a terminal")
            problems = live_gate.arming_problems(doc, cfg, t)
            if problems:
                raise LiveNotAllowedError(f"--resume refused: {path}: " + "; ".join(problems))
            phrase = str(doc["phrase"])
    gate = live_gate.check_live_allowed(
        cfg, flag_ack=flag_ack, typed_phrase=phrase, now=t, input_fn=input_fn,
        max_evidence_age_s=live_gate.RESUME_MAX_EVIDENCE_AGE_S if resuming else live_gate.MAX_EVIDENCE_AGE_S)
    gate["resume"] = resuming
    tr = _transport(cfg, transport)
    client = _keyed_write_client(cfg, tr, clk)
    return _live_runtime(cfg, clock=clk, transport=tr, client=client, gate=gate, label="live_real_orders")


def build_canary_runtime(config: Config, *, flag_ack: bool, typed_phrase: str | None = None,
                         input_fn: Callable[[str], str] | None = None, transport: Any = None,
                         clock: Clock | None = None, now: float | None = None) -> Runtime:
    """Same gate as live (flag, enable_live, key, selftest, attestation, no KILL) with the canary
    phrase ``RUN CANARY <initial_tmn>``; the canary condition itself is not required."""
    clk = clock or RealClock()
    cfg = replace(config, mode=Mode.LIVE)
    gate = live_gate.check_live_allowed(
        cfg, flag_ack=flag_ack, typed_phrase=typed_phrase, now=now if now is not None else clk.now(),
        input_fn=input_fn, require_canary=False, expected_phrase=live_gate.required_canary_phrase(cfg))
    gate["purpose"] = "canary"
    tr = _transport(cfg, transport)
    client = _keyed_write_client(cfg, tr, clk)
    return _live_runtime(cfg, clock=clk, transport=tr, client=client, gate=gate, label="live_canary")


def build_unstop_runtime(config: Config, *, flag_ack: bool, input_fn: Callable[[str], str] | None = None,
                         transport: Any = None, clock: Clock | None = None, now: float | None = None) -> Runtime:
    """``unstop`` (live): the SAME gate evidence as ``live --resume`` (flag, ENABLE_LIVE, key, explicit
    DATA_DIR, selftest, check-live, canary, no KILL, and the phrase: from a valid LIVE_ARMED record
    when the competition has started, otherwise typed at a terminal) - but the exchange client is
    READ-ONLY, so ``unstop`` can never place or cancel an order; it does not start trading."""
    clk = clock or RealClock()
    cfg = replace(config, mode=Mode.LIVE)
    t = float(now if now is not None else clk.now())
    started = competition_started(cfg)
    phrase = None
    if started:
        doc, _ = live_gate.read_arming(cfg.state_path)
        if doc is not None and not live_gate.arming_problems(doc, cfg, t):
            phrase = str(doc["phrase"])
    gate = live_gate.check_live_allowed(
        cfg, flag_ack=flag_ack, typed_phrase=phrase, now=t, input_fn=input_fn,
        max_evidence_age_s=live_gate.RESUME_MAX_EVIDENCE_AGE_S if started else live_gate.MAX_EVIDENCE_AGE_S)
    gate["purpose"] = "unstop"
    tr = _transport(cfg, transport)
    client = read_only_client(cfg, transport=tr, clock=clk)
    return _live_runtime(cfg, clock=clk, transport=tr, client=client, gate=gate, label="live_unstop")


def build_cancel_only_runtime(config: Config, *, transport: Any = None,
                              clock: Clock | None = None) -> Runtime:
    """For ``kill --cancel-open`` (an emergency path, so no live gate: KILL is present by then). The
    exchange is ``CancelOnlyExchange``: it can read and cancel, never place an order."""
    if not config.api_key:
        raise LiveNotAllowedError("cancelling real orders needs WALLEX_API_KEY")
    clk = clock or RealClock()
    cfg = replace(config, mode=Mode.LIVE)
    tr = _transport(cfg, transport)
    client = CancelOnlyExchange(_keyed_write_client(cfg, tr, clk))
    return _live_runtime(cfg, clock=clk, transport=tr, client=client, gate=None, label="live_cancel_only")
