"""``wallex-agent`` / ``python -m wallex_agent`` command line (argparse, stdlib only).

Every byte written to stdout/stderr passes through a scrubber that removes the API key (and any
``X-API-Key: ...`` text), so no command can print the key even by accident.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import textwrap
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from . import __version__, live_gate
from .agent import AgentMemory
from .config import Config, load_config, parse_dotenv
from .errors import ConfigError, ExchangeError, LiveNotAllowedError, WallexAgentError
from .exchange.probe import unverified_after_canary
from .market_data import save_candles
from .portfolio import INITIAL_TMN_KEY, Portfolio
from .reporting import Scrubber, build_final_report, render_trace, verify_chain, write_final_report
from .runner import (EXIT_ERROR, EXIT_GATE, EXIT_INTERRUPTED, EXIT_KILLED, EXIT_OK, EXIT_REFUSED,
                     EXIT_STOPPED, EXIT_UNAVAILABLE, EXIT_USAGE, K_BOOKS, K_FEES, K_LABELS, K_START, AgentRunner,
                     RunResult, books_from_kv, fees_from_kv)
from .store import InstanceLockError, probe_lock
from .types import BPS, ONE, Mode, Side, plain

EXIT_CODES_HELP = f"""exit codes:
  {EXIT_OK}    success (run finished: deadline, end of data, end of paper session, stage done)
  {EXIT_ERROR}    unexpected error
  {EXIT_USAGE}    usage error (bad arguments)
  {EXIT_GATE}    live gate refused (missing flag/evidence/phrase, KILL present, ...): nothing was sent
  {EXIT_KILLED}    kill switch engaged: open orders cancelled (never sold), bot STOPPED
  {EXIT_STOPPED}    safety stop: bot STOPPED (drift, contradiction, repeated failures, drawdown)
  {EXIT_REFUSED}    refused: a precondition is not met (e.g. canary balance, another instance running)
  {EXIT_UNAVAILABLE}    exchange/network unavailable for a required read
  {EXIT_INTERRUPTED}  interrupted by SIGINT/SIGTERM/SIGBREAK: resting orders cancelled, state resumable
"""

DESCRIPTION = """Agentic BTC/USDT spot trader for Wallex (backtest / paper / live).
Profit is NOT guaranteed: the agent trades only when its estimated edge after all costs clears the
hurdle, and holding is the default. Live trading is behind strict gates (see `check-live`, `canary`,
`arm-live`, `live --help`)."""


# --------------------------------------------------------------------------- output scrubbing
class ScrubbingStream:
    """Text stream wrapper: every write is scrubbed of the API key before it reaches the terminal."""

    def __init__(self, stream: TextIO, scrubber: Scrubber) -> None:
        self._stream = stream
        self._scrubber = scrubber

    def write(self, s: str) -> int:
        self._stream.write(self._scrubber.text(s))
        return len(s)

    def writelines(self, lines: Sequence[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except (AttributeError, ValueError):
            return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class Ctx:
    """Per-invocation context. ``transport``/``clock``/``input_fn`` are test hooks."""

    def __init__(self, args: argparse.Namespace, config: Config, default_mode: Mode, *,
                 transport: Any = None, clock: Any = None, input_fn: Callable[[str], str] | None = None) -> None:
        self.args = args
        self.config = config
        self.default_mode = default_mode
        self.transport = transport
        self.clock = clock
        self.input_fn = input_fn

    def mode(self) -> Mode:
        m = getattr(self.args, "mode", None)
        return Mode(m) if m else self.default_mode


_SIGNAL_NAMES = {int(getattr(signal, n)): n for n in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, n)}


def out(*parts: Any) -> None:
    print(*parts, flush=True)


def err(*parts: Any) -> None:
    print(*parts, file=sys.stderr, flush=True)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True,
                      default=lambda o: plain(o) if isinstance(o, Decimal) else str(o))


# --------------------------------------------------------------------------- signals
class StopFlag:
    """What a signal handler may touch: one attribute assignment. No I/O, no locks (the main loop
    may be inside a print or an audit write when the signal arrives)."""

    def __init__(self) -> None:
        self.reason: str | None = None

    def request_stop(self, reason: str = "signal") -> None:
        self.reason = reason

    @property
    def stop_requested(self) -> bool:
        return self.reason is not None


def _install_signals(target: Any) -> Callable[[], None]:
    """Route SIGINT/SIGTERM/SIGBREAK to ``target.request_stop(name)`` and nothing else; the main
    loop notices the flag and reports it (``AgentRunner.notify``)."""
    names = ["SIGINT", "SIGTERM", "SIGBREAK"]
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        target.request_stop(_SIGNAL_NAMES.get(signum, "signal"))

    for n in names:
        sig = getattr(signal, n, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):   # not the main thread / unsupported
            pass

    def restore() -> None:
        for sig, h in previous.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass
    return restore


# --------------------------------------------------------------------------- summaries
def _fmt_tmn(v: Any) -> str:
    try:
        return f"{Decimal(str(v)):,.0f}"
    except Exception:  # noqa: BLE001
        return str(v)


def print_summary(res: RunResult, rt: Any, title: str) -> None:
    rep = res.report or {}
    labels = rep.get("labels") or []
    live = rt.mode is Mode.LIVE
    out(f"== {title} ==")
    if "synthetic_data" in labels:
        out("   SYNTHETIC DATA (seeded random model, not market data); fills SIMULATED; fee ASSUMED; "
            "timestamps are SIMULATED time")
    elif not live:
        out(f"   SIMULATED orders/fills ({rt.mode.value}); fee ASSUMED at {rt.config.paper_fee_rate}")
    out(f"run:        {rt.run_id}")
    out(f"status:     {res.status} (exit {res.exit_code}) - {res.reason}")
    val = rep.get("value") or {}
    unverified = live and val.get("verified_against_exchange") is not True
    if rep:
        out(f"period:     {rep.get('started_at')} -> {rep.get('ended_at')} ({rep.get('duration_h')} h)")
        acts = rep.get("decisions_by_action") or {}
        out("decisions:  " + (", ".join(f"{k} {v}" for k, v in sorted(acts.items())) or "none"))
        orders = rep.get("orders") or {}
        rt_ = rep.get("round_trips") or {}
        out(f"orders:     {orders.get('count', 0)} {orders.get('by_lifecycle', {})}; fills "
            f"{(rep.get('fills') or {}).get('count', 0)}; round trips {rt_.get('count', 0)} "
            f"(wins {rt_.get('wins', 0)}, losses {rt_.get('losses', 0)})")
        pnl = rep.get("pnl") or {}
        out(f"value:      {_fmt_tmn(val.get('initial_tmn'))} TMN -> {_fmt_tmn(val.get('final_tmn_net_of_exit_costs'))} "
            f"TMN net of exit costs (P&L {pnl.get('abs_tmn')} TMN, {pnl.get('pct')} %)"
            + ("  [LEDGER-BASED, NOT verified against the exchange balances: "
               f"{(val.get('exchange_check') or {}).get('detail') or 'no confirmation'}]" if unverified else ""))
        out(f"target:     {pnl.get('target_statement')}" + (" (unverified)" if unverified else ""))
        fees = rep.get("fees_paid") or {}
        out("fees paid:  " + (", ".join(f"{v} {k}" for k, v in sorted(fees.items())) or "none"))
        holdings = ((rep.get("end") or {}).get("holdings") or {})
        out(("holdings:   " if not live else "ledger:     ")
            + (", ".join(f"{v} {k}" for k, v in sorted(holdings.items()) if v not in ("0", 0)) or "none"))
    if res.report_path:
        out(f"report:     {res.report_path}")
    if res.trace_path:
        out(f"trace:      {res.trace_path}")
    ok, bad = verify_chain(rt.audit.path)
    out(f"audit:      {rt.audit.path} (hash chain {'OK' if ok else f'BROKEN at line {bad}'})")
    if live:
        out("REAL-MONEY run: the numbers above are the bot's own fills ledger valued at the last books"
            + (", NOT confirmed by the exchange at the end: check the account in the Wallex app"
               if unverified else ", confirmed against the exchange balances at the end")
            + ". They say nothing about future results; 2x is not guaranteed.")
    else:
        out("Past (simulated) results do not predict future results; 2x is not guaranteed.")


def _run(rt: Any, runner: AgentRunner, title: str, **run_kw: Any) -> int:
    runner.notify = err
    restore = _install_signals(runner)
    try:
        res = runner.run(**run_kw)
    finally:
        restore()
        rt.close()
    print_summary(res, rt, title)
    return res.exit_code


# --------------------------------------------------------------------------- commands: simulation
def cmd_demo(ctx: Ctx) -> int:
    from .modes.backtest import build_backtest
    a = ctx.args
    rt = build_backtest(ctx.config, synthetic=True, seed=a.seed, hours=a.hours)
    return _run(rt, AgentRunner(rt),
                f"DEMO: offline synthetic {a.hours:g} h backtest (SYNTHETIC, no network, no key)")


def cmd_backtest(ctx: Ctx) -> int:
    from .modes.backtest import build_backtest
    a = ctx.args
    if a.data is None and not a.synthetic:
        err("backtest: give --data FILE or --synthetic")
        return EXIT_USAGE
    try:
        rt = build_backtest(ctx.config, data=a.data, synthetic=a.synthetic, seed=a.seed, hours=a.hours,
                            step_s=a.step)
    except (OSError, ValueError) as exc:
        err(f"backtest: cannot load data: {exc}")
        return EXIT_REFUSED
    title = "BACKTEST (SYNTHETIC)" if a.synthetic else f"BACKTEST over {Path(a.data).name}"
    return _run(rt, AgentRunner(rt), title)


def cmd_paper(ctx: Ctx) -> int:
    from .modes.paper import build_paper
    a = ctx.args
    try:
        rt = build_paper(ctx.config, transport=ctx.transport, clock=ctx.clock)
    except (ExchangeError, OSError) as exc:
        err(f"paper: public market data unavailable: {type(exc).__name__}: {exc}")
        return EXIT_UNAVAILABLE
    except InstanceLockError as exc:
        err(f"paper: {exc}")
        return EXIT_REFUSED
    runner = AgentRunner(rt, interval_s=a.interval)
    out(f"PAPER session {rt.run_id}: {a.seconds}s, a cycle every {a.interval}s, live PUBLIC books, simulated "
        "orders (nothing is ever sent to Wallex). Ctrl+C stops cleanly.")
    return _run(rt, runner, "PAPER (live public books, simulated orders)", seconds=a.seconds)


# --------------------------------------------------------------------------- commands: public data
def _public(ctx: Ctx) -> Any:
    from .modes.paper import public_client
    return public_client(ctx.config, transport=ctx.transport, clock=ctx.clock)


RES_SECONDS = {"1": 60, "60": 3600, "180": 10800, "360": 21600, "720": 43200, "1D": 86400}


def cmd_fetch_history(ctx: Ctx) -> int:
    a = ctx.args
    client = _public(ctx)
    step = RES_SECONDS[a.resolution]
    end = int(time.time() if ctx.clock is None else ctx.clock.now())
    start = end - int(a.hours * 3600)
    candles: dict[int, Any] = {}
    chunk = step * 500
    t = start
    try:
        while t < end:
            for c in client.get_candles(a.symbol, a.resolution, t, min(end, t + chunk)):
                candles[c.ts] = c
            t += chunk
    except ExchangeError as exc:
        err(f"fetch-history: {type(exc).__name__}: {exc}")
        return EXIT_UNAVAILABLE
    path = save_candles(a.out, candles.values())
    out(f"{len(candles)} {a.symbol} candles ({a.resolution}) from {_iso(start)} to {_iso(end)} -> {path}")
    return EXIT_OK


def cmd_diagnose(ctx: Ctx) -> int:
    cfg = ctx.config
    client = _public(ctx)
    btc, usd = cfg.btc_symbol, cfg.usdt_symbol
    report: dict[str, Any] = {"base_url": cfg.api_base, "public_only": True, "unknown": [], "problems": []}
    try:
        rules = client.get_markets()
        books = {s: client.get_order_book(s) for s in (btc, usd)}
    except ExchangeError as exc:
        err(f"diagnose: public API unavailable: {type(exc).__name__}: {exc}")
        if not cfg.use_env_proxy:
            err("(if this host reaches Wallex only through a proxy, set WALLEX_USE_ENV_PROXY=true)")
        return EXIT_UNAVAILABLE
    report["rules"] = {s: {"base": r.base, "quote": r.quote, "qty_decimals": r.qty_decimals,
                           "price_decimals": r.price_decimals, "min_qty": r.min_qty,
                           "min_notional": r.min_notional} for s, r in rules.items() if s in (btc, usd)}
    for s in (btc, usd):
        if s not in rules:
            report["problems"].append(f"rules for {s} missing")
    for s, b in books.items():
        side_depth = {"buy_within_slippage": b.depth_quote(Side.BUY, cfg.max_slippage_bps if s == btc
                                                           else cfg.conversion_slippage_bps),
                      "sell_within_slippage": b.depth_quote(Side.SELL, cfg.max_slippage_bps if s == btc
                                                            else cfg.conversion_slippage_bps)}
        report[f"book_{s}"] = {"best_bid": b.best_bid, "best_ask": b.best_ask, "mid": b.mid,
                               "spread_bps": b.spread_bps.quantize(Decimal("0.01")),
                               "levels": [len(b.bids), len(b.asks)], "depth_quote": side_depth}
        if b.spread_bps > cfg.max_spread_bps:
            report["problems"].append(f"{s} spread {b.spread_bps:.1f} bps > max {cfg.max_spread_bps} bps")
    tmn = Decimal(ctx.args.tmn) if ctx.args.tmn else cfg.initial_tmn
    fee = Decimal(cfg.paper_fee_rate)
    ub = books[usd]
    w = ub.walk_quote(Side.BUY, tmn)
    ur = rules.get(usd)
    conv = {"tmn": tmn, "usdt_gross": w.filled_qty.quantize(Decimal("0.000001")),
            "avg_price_tmn": None if w.avg_price is None else w.avg_price.quantize(Decimal("0.01")),
            "worst_price_tmn": w.worst_price, "book_can_fill": w.complete,
            "assumed_taker_fee": fee, "usdt_net_of_assumed_fee": (w.filled_qty * (ONE - fee)).quantize(Decimal("0.000001")),
            "cost_vs_mid_bps": None if w.avg_price is None else
            ((w.avg_price / ub.mid - ONE) * BPS + fee * BPS).quantize(Decimal("0.01"))}
    report["conversion"] = conv
    if ur is not None and tmn < ur.min_notional:
        report["problems"].append(f"{tmn} TMN < {usd} minNotional {ur.min_notional}")
    br = rules.get(btc)
    if br is not None and w.filled_qty * (ONE - fee) < br.min_notional:
        report["problems"].append(f"converted USDT below {btc} minNotional {br.min_notional}")
    if not w.complete:
        report["problems"].append(f"{usd} visible asks cannot fill {tmn} TMN")
    now = float(time.time() if ctx.clock is None else ctx.clock.now())
    verified = live_gate.canary_verified_items(cfg, cfg.state_path, now)
    report["unknown"] = (["account taker/maker fee (private endpoint: needs the key; conversion math above "
                          f"uses the ASSUMED {fee})"]
                         + [f"{k}: {v}" for k, v in unverified_after_canary(verified)])
    report["canary_verified"] = verified
    report["trading_feasible_now"] = not report["problems"]
    out(_json(report))
    out(f"\n{tmn} TMN buys ~{conv['usdt_net_of_assumed_fee']} USDT net of an ASSUMED {fee} fee "
        f"(walking {usd} asks; avg {conv['avg_price_tmn']} TMN/USDT).")
    out("Trading feasible now: " + ("YES (public facts only; the real fee is still unknown)" if
                                    report["trading_feasible_now"] else "NO: " + "; ".join(report["problems"])))
    return EXIT_OK


# --------------------------------------------------------------------------- commands: live gates
def _live_cfg(ctx: Ctx) -> Config:
    return replace(ctx.config, mode=Mode.LIVE)


def cmd_selftest(ctx: Ctx) -> int:
    out("running the project's test suite (this attests THIS source tree for live trading)...")
    res = live_gate.run_selftest(ctx.config.state_path)
    out(_json({k: res.get(k) for k in ("passed", "tests", "failures", "reason", "source_hash", "at",
                                         "duration_s")}))
    out(f"written: {ctx.config.state_path / live_gate.SELFTEST_FILE}")
    return EXIT_OK if res.get("passed") else EXIT_ERROR


def cmd_check_live(ctx: Ctx) -> int:
    from .exchange.probe import probe_private_schemas
    from .modes.live import read_only_client
    cfg = _live_cfg(ctx)
    if not cfg.api_key:
        err("check-live needs WALLEX_API_KEY (environment or .env). It only performs GET requests.")
        return EXIT_REFUSED
    try:
        client = read_only_client(cfg, transport=ctx.transport, clock=ctx.clock)
    except ConfigError as exc:
        err(f"check-live: {exc}")
        return EXIT_REFUSED
    out("GET-only probe of the private endpoints (no order is placed; read-only client)...")
    rep = probe_private_schemas(client)
    att = live_gate.write_schema_attestation(cfg.state_path, rep, cfg.api_base)
    out(_json({"checks": [{"name": c["name"], "ok": c["ok"]} for c in rep.checks],
               "attestation": {k: att[k] for k in ("ok", "at", "base_url", "failed_checks", "unverified",
                                                    "verified_by_canary", "reasons")}}))
    out(f"written: {cfg.state_path / live_gate.ATTESTATION_FILE}")
    st = live_gate.arm_status(cfg, now=float(time.time() if ctx.clock is None else ctx.clock.now()))
    out("live gate still missing: " + (", ".join(st["missing"]) or "nothing (flag + phrase at launch)"))
    return EXIT_OK if att["ok"] else EXIT_GATE


def cmd_canary(ctx: Ctx) -> int:
    from .canary import CanaryRefused, run_canary
    from .modes.live import build_canary_runtime
    cfg = _live_cfg(ctx)
    out("CANARY: places ONE real LIMIT BUY on USDTTMN at ~50% of the best bid (non-fillable by design), "
        "just above minNotional, then cancels it. Gated like live trading.")
    try:
        rt = build_canary_runtime(cfg, flag_ack=bool(ctx.args.i_understand_real_money), input_fn=ctx.input_fn,
                                  transport=ctx.transport, clock=ctx.clock)
    except LiveNotAllowedError as exc:
        err(str(exc))
        return EXIT_GATE
    except InstanceLockError as exc:
        err(f"canary: {exc}")
        return EXIT_REFUSED
    flag = StopFlag()                       # I5: same flag-only handling as the trading loop
    restore = _install_signals(flag)
    try:
        rep = rt.engine.reconcile_startup()
        if not rep.get("ok"):
            err(f"canary refused: startup reconciliation not ok (state {rep.get('state')}): "
                f"{rep.get('actions') or (rep.get('drift') or {}).get('text') or (rep.get('drift') or {}).get('reasons')}")
            return EXIT_STOPPED if rt.sm.state.value == "STOPPED" else EXIT_UNAVAILABLE
        try:
            doc = run_canary(rt, should_stop=lambda: flag.reason)
        except CanaryRefused as exc:
            err(f"canary refused (nothing sent): {exc}")
            return EXIT_INTERRUPTED if flag.stop_requested else EXIT_REFUSED
        out(_json({k: doc[k] for k in ("ok", "at", "base_url", "verified", "reasons", "observations")}))
        if rt.extras.get("canary_holdings"):
            err("!!! THE CANARY ORDER FILLED. Holdings now recorded in the live ledger: "
                + ", ".join(f"{v} {k}" for k, v in sorted(rt.extras["canary_holdings"].items())))
        out(f"written: {cfg.state_path / live_gate.CANARY_FILE}")
        if flag.stop_requested:
            err(f"{flag.reason} received: the canary order was cancelled (cancel only); run the canary again.")
            return EXIT_INTERRUPTED
        return EXIT_OK if doc["ok"] else EXIT_REFUSED
    finally:
        restore()
        rt.close()


def cmd_arm_live(ctx: Ctx) -> int:
    cfg = _live_cfg(ctx)
    now = float(time.time() if ctx.clock is None else ctx.clock.now())
    try:
        live_gate.check_live_allowed(cfg, flag_ack=bool(ctx.args.i_understand_real_money), typed_phrase=None,
                                     input_fn=ctx.input_fn, now=now)
    except LiveNotAllowedError as exc:
        err(str(exc))
        return EXIT_GATE
    doc = live_gate.write_arming(cfg, now=now)
    path = cfg.state_path / live_gate.ARMING_FILE
    out(f"armed: {path} written (bound to this source tree, {cfg.api_base}, {cfg.data_dir}; expires "
        f"{doc['expires_at']}).\n"
        "It lets `live --resume` (systemd) CONTINUE a competition that you started interactively with "
        "`live`; it can never start a fresh live run. Delete the file to disarm.")
    return EXIT_OK


def cmd_live(ctx: Ctx) -> int:
    from .modes.live import build_live
    a = ctx.args
    cfg = _live_cfg(ctx)
    try:
        rt = build_live(cfg, flag_ack=bool(a.i_understand_real_money), resume=bool(a.resume),
                        input_fn=ctx.input_fn, transport=ctx.transport, clock=ctx.clock)
    except LiveNotAllowedError as exc:
        err(str(exc))
        return EXIT_GATE
    except InstanceLockError as exc:
        err(f"live: {exc}")
        return EXIT_REFUSED
    runner = AgentRunner(rt, stage=a.stage)
    title = f"LIVE ({'conversion stage only' if a.stage == 'convert' else 'full'})"
    out(f"{title}: run {rt.run_id}. REAL MONEY. Kill switch: `wallex-agent kill` (or create {cfg.kill_file}).")
    return _run(rt, runner, title)


# --------------------------------------------------------------------------- commands: inspection
def _open_run(ctx: Ctx, run_id: str | None = None) -> tuple[Mode, str, Path, Any]:
    from .modes import open_snapshot, run_paths
    mode = ctx.mode()
    rid, rdir, db = run_paths(ctx.config, mode, run_id)
    if rid is None or db is None or not db.is_file():
        raise FileNotFoundError(f"no {mode.value} run found under {ctx.config.state_path / mode.value}")
    return mode, rid, rdir, open_snapshot(db)


def cmd_status(ctx: Ctx) -> int:
    cfg = ctx.config
    now = float(time.time() if ctx.clock is None else ctx.clock.now())
    info: dict[str, Any] = {"mode": ctx.mode().value, "data_dir": str(cfg.state_path),
                            "kill_file_present": cfg.kill_file.exists()}
    try:
        mode, rid, rdir, snap = _open_run(ctx, getattr(ctx.args, "run", None))
    except FileNotFoundError as exc:
        info["run"] = None
        info["note"] = str(exc)
    else:
        try:
            st = snap.kv_get("bot_state") or {"state": "UNFUNDED"}
            start = snap.kv_get(K_START)
            hours = cfg.competition_hours
            eq = None
            for ev in snap.iter_events("equity_point"):
                eq = ev
            info.update({
                "run": rid, "run_dir": str(rdir), "state": st.get("state"), "state_since": _iso(st.get("since")),
                "competition_start": _iso(start), "time_left_h": None if start is None else
                round(max(0.0, float(start) + hours * 3600 - now) / 3600, 3),
                "equity_tmn_net": None if eq is None else eq.payload.get("equity_tmn_net"),
                "equity_at": None if eq is None else _iso(eq.ts),
                "open_orders": [{"client_id": o.client_id, "exchange_id": o.exchange_client_order_id,
                                 "symbol": o.symbol, "side": o.side.value, "lifecycle": o.lifecycle.value,
                                 "price": plain(o.price), "quantity": plain(o.quantity),
                                 "executed_qty": plain(o.executed_qty)} for o in snap.unresolved_orders()],
                "orders_total": len(snap.list_orders()), "fills_total": snap.count_fills(),
                "stop": snap.kv_get("exec.stop")})
            hist = [ev.payload for ev in snap.iter_events("state_transition")]
            if hist:
                info["last_transition"] = {k: hist[-1].get(k) for k in ("from", "to", "reason")}
        finally:
            snap.close()
    arm = live_gate.arm_status(replace(cfg, mode=Mode.LIVE), now=now)
    info["arm_status"] = {"missing": arm["missing"], "arming_file_present": arm["arming_file_present"],
                          "arming": arm["arming"],
                          "unverified_api_items": arm["unverified_api_items"],
                          "canary_verified_api_items": arm["canary_verified_api_items"],
                          "required_at_launch": arm["required_at_launch"]}
    out(_json(info))
    return EXIT_OK


def agent_footprint(cfg: Config) -> dict[str, Any]:
    """Which KILL-obeying agent state (live store, paper runs) exists under ``cfg.data_dir``, and
    whether an agent holds its instance lock right now. Creates nothing."""
    from .modes import live_store_path, runs_dir
    live_db = live_store_path(cfg)
    live_lock = probe_lock(live_db.parent)
    paper_runs = sorted(p for p in runs_dir(cfg, Mode.PAPER).glob("*") if (p / "state.db").is_file()) \
        if runs_dir(cfg, Mode.PAPER).is_dir() else []
    paper_running = [p.name for p in paper_runs if probe_lock(p) is True]
    return {"data_dir": str(cfg.state_path), "live_store": str(live_db), "live_store_exists": live_db.is_file(),
            "live_agent_running": live_lock is True, "live_lock_file": live_lock is not None,
            "paper_runs": len(paper_runs), "paper_running": paper_running,
            "any": live_db.is_file() or live_lock is not None or bool(paper_runs)}


def cmd_kill(ctx: Ctx) -> int:
    cfg = ctx.config
    a = ctx.args
    kill_path = Path(cfg.kill_file).absolute()
    fp = agent_footprint(cfg)
    if not fp["any"] and not a.force_write:
        err(f"!!! kill: NO wallex-agent live store, paper run or instance lock under {cfg.state_path}.\n"
            f"!!! A KILL file at {kill_path} would most likely NOT be seen by your agent, so it was NOT written.\n"
            "    The agent reads <its DATA_DIR>/KILL. Re-run with the agent's own settings, e.g.\n"
            "    `wallex-agent --env-file <the agent's env file> kill` or `wallex-agent --data-dir <dir> kill`.\n"
            "    On a VPS the emergency stop is `sudo systemctl stop wallex-agent` (then `kill` with the\n"
            "    service's env file). Use --force-write to write the KILL file here anyway (e.g. to block a\n"
            "    future start from this DATA_DIR).")
        return EXIT_REFUSED
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    live_gate.atomic_write_json(kill_path, {"reason": a.reason, "at": _iso(time.time())})
    out(f"KILL file written: {kill_path}")
    if not fp["any"]:
        err(f"WARNING: no agent state was found under {cfg.state_path} (written anyway: --force-write).")
    out(_json({k: fp[k] for k in ("data_dir", "live_store_exists", "live_agent_running", "paper_runs",
                                  "paper_running")}))
    out("Paper and live agents obey KILL: a running agent cancels its own open orders (cancel only, never "
        "sells) and stops at its next check, and no new paper/live run, canary or arm-live starts while the file "
        "exists. demo/backtest runs do NOT read this file.\n"
        "To trade again: delete the KILL file; if a LIVE agent already acted on it (its ledger is now STOPPED), "
        "check the account and then run `wallex-agent unstop --i-understand-real-money`.")
    if not a.cancel_open:
        return EXIT_OK
    from .modes import live_store_path
    from .modes.live import build_cancel_only_runtime
    live_db = live_store_path(cfg)
    if not live_db.is_file():
        out(f"--cancel-open: no live store at {live_db}: no real order of this bot can be open there "
            "(paper orders are simulated in-process; a running paper agent cancels its own).")
        return EXIT_OK
    if probe_lock(live_db.parent):
        out("--cancel-open: a LIVE agent is running on this store; it cancels its own open orders at its next "
            "kill check (within about a second while it waits between cycles).")
        return EXIT_OK
    try:
        rt = build_cancel_only_runtime(cfg, transport=ctx.transport, clock=ctx.clock)
    except InstanceLockError:
        out("--cancel-open: a LIVE agent started meanwhile; it cancels its own open orders at its next kill check.")
        return EXIT_OK
    except (LiveNotAllowedError, ConfigError) as exc:
        err(f"--cancel-open: {exc}")
        return EXIT_REFUSED
    try:
        res = rt.engine.kill(cancel_open=True, reason=f"operator kill --cancel-open: {a.reason}")
    finally:
        rt.close()
    out(_json({"cancelled": res["cancelled"], "still_open": res["still_open"], "unresolved": res["unresolved"]}))
    out("Only THIS bot's recorded LIVE orders were touched; nothing was sold. The live ledger is now STOPPED.")
    return EXIT_OK if not res["still_open"] else EXIT_STOPPED


def cmd_unstop(ctx: Ctx) -> int:
    """F2: leave STOPPED after a human checked the account - only if a read-only reconcile is clean."""
    cfg = ctx.config
    kill = Path(cfg.kill_file)
    if kill.exists():
        err(f"unstop refused: the KILL file {kill} exists. Delete it yourself first (deliberately: nothing "
            "removes it automatically), then run `unstop` again.")
        return EXIT_REFUSED
    mode = ctx.mode()
    if mode is Mode.BACKTEST:
        err("unstop: backtest runs are disposable; start a new one.")
        return EXIT_USAGE
    return _unstop_paper(ctx) if mode is Mode.PAPER else _unstop_live(ctx)


def _unstop_live(ctx: Ctx) -> int:
    from .modes import live_store_path
    from .modes.live import build_unstop_runtime
    cfg = _live_cfg(ctx)
    if not ctx.args.i_understand_real_money:
        err("unstop (live) needs --i-understand-real-money: it re-enables a REAL-money ledger (it never trades "
            "by itself; you start `live` afterwards).")
        return EXIT_GATE
    db = live_store_path(cfg)
    if not db.is_file():
        err(f"unstop: no live store at {db}: nothing to unstop.")
        return EXIT_REFUSED
    try:
        rt = build_unstop_runtime(cfg, flag_ack=True, input_fn=ctx.input_fn, transport=ctx.transport,
                                  clock=ctx.clock)
    except LiveNotAllowedError as exc:
        err(str(exc))
        return EXIT_GATE
    except InstanceLockError as exc:
        err(f"unstop: {exc}. Stop the running agent first.")
        return EXIT_REFUSED
    try:
        return _unstop_with(rt, ctx)
    finally:
        rt.close()


def _unstop_with(rt: Any, ctx: Ctx) -> int:
    from .types import BotState
    if rt.sm.state is not BotState.STOPPED:
        out(f"the {rt.mode.value} ledger is {rt.sm.state.value}, not STOPPED: nothing to do.")
        return EXIT_OK
    now = float(rt.clock.now())
    start = rt.store.kv_get(K_START)
    if start is not None and now >= float(start) + float(rt.config.competition_hours) * 3600:
        err(f"unstop refused: the competition window ended at {_iso(float(start) + rt.config.competition_hours * 3600)}"
            " (STOPPED is the normal end of the run).")
        return EXIT_REFUSED
    stop = rt.store.kv_get("exec.stop") or {}
    hist = rt.sm.history()
    out(f"STOPPED because: {stop.get('reason') or (hist[-1].get('reason') if hist else 'unknown')}")
    out("Read-only reconcile (orders, abandoned-order watch, open orders, ledger vs exchange)...")
    check = rt.engine.unstop_check()
    out(_json({"drifts": check["drifts"], "problems": check["problems"], "derived_state": check["derived"],
               "why": check["why"]}))
    if not check["ok"]:
        err("unstop REFUSED: the reconcile is not clean (see 'problems' above). Nothing was changed; fix the "
            "cause (check the account in the Wallex app) and run `unstop` again.")
        return EXIT_STOPPED
    to = rt.engine.operator_unstop(check, note="wallex-agent unstop")
    out(f"unstopped: STOPPED -> {to.value} ({check['why']}). Nothing else was changed: KILL, LIVE_ARMED, the "
        "evidence files, quarantines and the abandoned-order watch list are untouched. Trading resumes only "
        "when you start the agent again.")
    return EXIT_OK


def _unstop_paper(ctx: Ctx) -> int:
    """Paper: the simulated exchange of a finished session no longer exists, so only the store itself
    can be checked (no unresolved orders, no negative ledger); `paper` always starts a NEW session."""
    from .modes import AUDIT_NAME, run_paths
    from .modes.backtest import REFERENCE_RULES
    from .portfolio import Portfolio
    from .reporting import AuditLog
    from .state import StateMachine, derive_state_explained
    from .store import SqliteStore
    from .types import Balance, BotState
    cfg = ctx.config
    rid, rdir, db = run_paths(cfg, Mode.PAPER, getattr(ctx.args, "run", None))
    if rid is None or db is None or not db.is_file():
        err(f"unstop: no paper run found under {cfg.state_path / 'paper'}")
        return EXIT_REFUSED
    try:
        store = SqliteStore(db)
    except InstanceLockError as exc:
        err(f"unstop: {exc}")
        return EXIT_REFUSED
    try:
        sm = StateMachine(store)
        if sm.state is not BotState.STOPPED:
            out(f"paper run {rid} is {sm.state.value}, not STOPPED: nothing to do.")
            return EXIT_OK
        pf = Portfolio(store, Decimal(str(store.kv_get(INITIAL_TMN_KEY) or cfg.initial_tmn)),
                       btc_symbol=cfg.btc_symbol, usdt_symbol=cfg.usdt_symbol)
        ledger = pf.expected_balances()
        problems = [f"order {o.client_id} is {o.lifecycle.value}" for o in store.unresolved_orders()]
        problems += [f"{a} ledger is negative ({plain(v)})" for a, v in ledger.items() if v < 0]
        derived, why = derive_state_explained({a: Balance(a, v) for a, v in ledger.items() if v >= 0}, [],
                                              REFERENCE_RULES, {}, btc_symbol=cfg.btc_symbol,
                                              usdt_symbol=cfg.usdt_symbol)
        if derived is BotState.STOPPED:
            problems.append(f"no state can be derived: {why}")
        out(_json({"run": rid, "ledger": ledger, "problems": problems, "derived_state": derived.value, "why": why,
                   "note": "paper: ledger-only check (the session's simulated exchange no longer exists)"}))
        if problems:
            err("unstop REFUSED: see 'problems'. Nothing was changed.")
            return EXIT_STOPPED
        prev = store.kv_get("exec.stop")
        with store.transaction():
            sm.transition(derived, f"operator_unstop (paper, ledger-only): {why}", {"previous_stop": prev},
                          force=True)
            store.kv_set("exec.stop", None)
            store.append("operator_unstop", {"to": derived.value, "previous_stop": prev, "why": why})
        audit = AuditLog(rdir / AUDIT_NAME, run_id=rid, secrets=[cfg.api_key] if cfg.api_key else [])
        try:
            audit.write("operator_unstop", {"to": derived.value, "previous_stop": prev, "why": why})
        finally:
            audit.close()
        out(f"unstopped paper run {rid}: STOPPED -> {derived.value}. `paper` always starts a NEW session; this "
            "only repairs the stored state.")
        return EXIT_OK
    finally:
        store.close()


def cmd_report(ctx: Ctx) -> int:
    from .modes import mode_dir
    try:
        mode, rid, rdir, snap = _open_run(ctx, ctx.args.run)
    except FileNotFoundError as exc:
        err(f"report: {exc}")
        return EXIT_REFUSED
    try:
        cfg = ctx.config
        initial = Decimal(str(snap.kv_get(INITIAL_TMN_KEY) or cfg.initial_tmn))
        pf = Portfolio(snap, initial, btc_symbol=cfg.btc_symbol, usdt_symbol=cfg.usdt_symbol)
        books, books_ts = books_from_kv(snap.kv_get(K_BOOKS))
        fees = fees_from_kv(snap.kv_get(K_FEES))
        start = snap.kv_get(K_START)
        events = list(snap.iter_events())
        end_ts = events[-1].ts if events else time.time()
        caveats = [f"Rebuilt offline from the store; valued at the last books observed by the agent "
                   f"({_iso(books_ts) or 'none recorded'}). The runner's own final_report.json (if any) is "
                   "left untouched; this file is final_report.rebuilt.json."]
        if mode is not Mode.LIVE:
            caveats.append(f"Fees ASSUMED at {cfg.paper_fee_rate} ({mode.value}).")
        state = (snap.kv_get("bot_state") or {}).get("state")
        rep = build_final_report(config=replace(cfg, mode=mode, initial_tmn=initial), mode=mode, run_id=rid,
                                 started_ts=float(start if start is not None else (events[0].ts if events else end_ts)),
                                 ended_ts=float(end_ts), portfolio=pf, engine_status={"state": state},
                                 store=snap, memory_snapshot=AgentMemory(snap).snapshot(), books_at_end=books,
                                 fees=fees, caveats=caveats, stop_reason=f"rebuilt by `report` (state {state})",
                                 synthetic_data=bool((snap.kv_get(K_LABELS) or {}).get("synthetic")),
                                 exchange_check={"verified": None, "detail": "offline rebuild: no exchange "
                                                 "balances were read"} if mode is Mode.LIVE else None)
    finally:
        snap.close()
    rdir.mkdir(parents=True, exist_ok=True)
    # never overwrite the runner's final_report.json (F3): the rebuild is a separate file
    path = write_final_report(rdir / "final_report.rebuilt.json", rep,
                              secrets=[cfg.api_key] if cfg.api_key else [])
    out(_json({k: rep.get(k) for k in ("run_id", "mode", "labels", "started_at", "ended_at", "stop_reason")}
              | {"value": rep["value"].get("final_tmn_net_of_exit_costs"), "pnl": rep["pnl"],
                 "orders": rep["orders"]["count"], "fills": rep["fills"]["count"]}))
    out(f"written: {path}")
    return EXIT_OK


def cmd_trace(ctx: Ctx) -> int:
    from .modes import AUDIT_NAME, run_paths
    a = ctx.args
    rid, rdir, _ = run_paths(ctx.config, ctx.mode(), a.run)
    if rdir is None or not (rdir / AUDIT_NAME).is_file():
        err(f"trace: no audit log for the {ctx.mode().value} run {rid or '(none)'}")
        return EXIT_REFUSED
    sys.stdout.write(render_trace(rdir / AUDIT_NAME, last=a.last, decision_id=a.decision))
    sys.stdout.flush()
    return EXIT_OK


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(prog="wallex-agent", description=DESCRIPTION, epilog=EXIT_CODES_HELP,
                                formatter_class=fmt)
    p.add_argument("--version", action="version", version=f"wallex-agent {__version__}")
    p.add_argument("--data-dir", help="state directory (default: DATA_DIR, else ./state of the CURRENT "
                                      "directory; resolved to an absolute path; live needs it explicitly)")
    p.add_argument("--env-file", default=".env",
                   help="dotenv file to read (default: .env in the current directory; on a VPS pass the "
                        "service's file, e.g. /etc/wallex-agent/wallex-agent.env)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help_: str, desc: str | None = None) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, description=textwrap.fill(desc or help_, 92),
                              epilog=EXIT_CODES_HELP, formatter_class=fmt)

    s = add("demo", "offline 24 h backtest on SYNTHETIC data (no network, no key)",
            "Runs the full agent (convert -> decide -> risk -> simulated execution -> reflect) over a seeded "
            "SYNTHETIC 24 h market. Prints a short summary and the paths of final_report.json, trace.txt "
            "and audit.jsonl. Results are labelled SYNTHETIC and say nothing about real performance.")
    s.add_argument("--seed", type=int, default=None, help="market/agent seed (default: SEED or 42)")
    s.add_argument("--hours", type=float, default=24.0, help="simulated hours (default 24)")
    s.set_defaults(func=cmd_demo)

    s = add("backtest", "backtest over a candle file or a synthetic market (no network)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--data", help="candles: CSV time,open,high,low,close,volume or Wallex UDF JSON "
                                  "(see fetch-history)")
    g.add_argument("--synthetic", action="store_true", help="seeded SYNTHETIC market")
    s.add_argument("--seed", type=int, default=None, help="seed (default: SEED or 42)")
    s.add_argument("--hours", type=float, default=None, help="hours (synthetic length / competition window)")
    s.add_argument("--step", type=int, default=60, help="synthetic step seconds (default 60)")
    s.set_defaults(func=cmd_backtest)

    s = add("fetch-history", "download public candles to a local CSV/JSON (public GET only)")
    s.add_argument("--symbol", default="BTCUSDT", choices=["BTCUSDT", "USDTTMN"])
    s.add_argument("--resolution", default="1", choices=sorted(RES_SECONDS), help="UDF resolution (1 = 1 min)")
    s.add_argument("--hours", type=float, default=24.0, help="how far back (default 24)")
    s.add_argument("--out", required=True, help="output file (.csv, or .json for UDF JSON)")
    s.set_defaults(func=cmd_fetch_history)

    s = add("diagnose", "public check: rules, books, spread/depth, TMN->USDT math, feasibility, unknowns",
            "Public endpoints only (works without a key): market rules, current USDTTMN/BTCUSDT books, "
            "spread and depth, how much USDT the initial TMN buys net of an ASSUMED fee, whether trading "
            "looks feasible now, and what is still unknown (the account fee, unverified API behaviour).")
    s.add_argument("--tmn", help="TMN amount for the conversion math (default INITIAL_TMN)")
    s.set_defaults(func=cmd_diagnose)

    s = add("paper", "paper trading: live public books, simulated orders (never sends an order)",
            "Paper mode: the agent sees real public Wallex books but every order, fill and balance is "
            "simulated in-process. The only network client is public-only (no key, GET of public "
            "endpoints only). 5-minute smoke test: `wallex-agent paper --seconds 300 --interval 10`.")
    s.add_argument("--seconds", type=float, default=300.0, help="session length in seconds (default 300)")
    s.add_argument("--interval", type=float, default=10.0, help="seconds between cycles (default 10)")
    s.set_defaults(func=cmd_paper)

    s = add("check-live", "GET-only probe of the private API -> live_schema_attestation.json (needs key)",
            "Reads balances/fee/open orders/trades shapes with a READ-ONLY keyed client (POST/DELETE are "
            "impossible), and writes a secret-free attestation (no amounts) to the state dir.")
    s.set_defaults(func=cmd_check_live)

    s = add("canary", "ONE real, tiny, non-fillable order: place, observe, cancel -> live_canary.json",
            "Settles the API behaviours a GET-only probe cannot (client_id echo, lookup by id, cancel "
            "answer, locked balance). Places ONE LIMIT BUY on USDTTMN at ~50% of the best bid with a "
            "quantity just above minNotional, then cancels it. Needs ENABLE_LIVE=true, the key, a passing "
            "selftest and check-live, no KILL file, --i-understand-real-money and the typed phrase "
            "'RUN CANARY <INITIAL_TMN>'. Needs spendable TMN >= 1.3 x the USDTTMN minNotional.")
    s.add_argument("--i-understand-real-money", action="store_true", help="required: this sends a REAL order")
    s.set_defaults(func=cmd_canary)

    s = add("selftest", "run the test suite and write selftest.json (required for live)")
    s.set_defaults(func=cmd_selftest)

    s = add("arm-live", "after all gates pass interactively, write the <state>/LIVE_ARMED record for systemd",
            "Checks every live gate (incl. the typed phrase 'START LIVE <INITIAL_TMN>' at a TTY) and writes a "
            "structured arming record (phrase, source hash, base URL, DATA_DIR, expiry = now + "
            "COMPETITION_HOURS + 24 h) that the systemd unit requires (ConditionPathExists) and that "
            "`live --resume` reads instead of prompting. --resume only CONTINUES a competition started "
            "interactively with `live`; it never starts one. Delete the file to disarm.")
    s.add_argument("--i-understand-real-money", action="store_true", help="required")
    s.set_defaults(func=cmd_arm_live)

    s = add("live", "LIVE trading with real money (all gates required)",
            "Gates: ENABLE_LIVE=true, WALLEX_API_KEY, an explicit DATA_DIR, --i-understand-real-money, fresh "
            "passing selftest (full suite) and check-live attestation for this source tree (<= 24 h; <= 48 h "
            "when resuming a started run), a passing canary (<= 72 h), no KILL file, and the typed phrase "
            "'START LIVE <INITIAL_TMN>' at a terminal. --resume (systemd) only CONTINUES a competition already "
            "started in this live store and reads the phrase from a valid, unexpired <state>/LIVE_ARMED "
            "record (no TTY needed). Startup always reconciles orders, fills and balances first and refuses "
            "to trade on unexplained drift.")
    s.add_argument("--stage", choices=["convert", "full"], default="full",
                   help="'convert': run ONLY the TMN->USDT conversion, then stop at READY; 'full' (default)")
    s.add_argument("--resume", action="store_true",
                   help="unattended restart (systemd) of a STARTED competition: phrase from LIVE_ARMED")
    s.add_argument("--i-understand-real-money", action="store_true", help="required")
    s.set_defaults(func=cmd_live)

    s = add("status", "state, gate status, open orders, equity, time left (no secrets)")
    s.add_argument("--mode", choices=[m.value for m in Mode], help="default: TRADING_MODE (paper)")
    s.add_argument("--run", help="run id (default: the mode's last run)")
    s.set_defaults(func=cmd_status)

    s = add("kill", "engage the kill switch (<DATA_DIR>/KILL); --cancel-open also cancels THIS bot's live orders",
            "Writes <DATA_DIR>/KILL (the absolute path is printed). PAPER and LIVE agents obey it: a running "
            "agent cancels its open orders (cancel only, never sells) and stops; demo/backtest runs do NOT read "
            "it. Refuses (exit 6, nothing written) when no live store, paper run or instance lock exists under "
            "DATA_DIR, because the agent then almost certainly uses another DATA_DIR: pass the agent's "
            "--env-file/--data-dir, or --force-write. On a VPS the emergency stop is `systemctl stop "
            "wallex-agent`. --cancel-open: whenever <DATA_DIR>/live/state.db exists and no live agent is "
            "running, cancels the open orders recorded in THIS bot's live store (never other orders, never "
            "sells) and leaves that ledger STOPPED. To trade again: delete KILL, then `unstop`.")
    s.add_argument("--cancel-open", action="store_true", help="also cancel this bot's recorded live open orders")
    s.add_argument("--reason", default="operator kill", help="free text stored in the KILL file")
    s.add_argument("--force-write", action="store_true",
                   help="write KILL even though no agent state exists under DATA_DIR")
    s.add_argument("--mode", choices=[m.value for m in Mode],
                   help="ignored (kept for old scripts): --cancel-open acts on the live store whenever it exists")
    s.set_defaults(func=cmd_kill)

    s = add("unstop", "leave STOPPED after a human check: read-only reconcile, then the true state",
            "For a STOPPED live (or paper) ledger. Refuses while the KILL file exists (delete it yourself first). "
            "Live needs --i-understand-real-money and the same gate evidence as `live --resume`, but uses a "
            "READ-ONLY client: it re-runs the startup reconcile without sending anything, prints every drift "
            "(asset, ledger, exchange, shortfall), and ONLY if nothing is unresolved, unexplained or short it "
            "moves the ledger to the state the facts imply and audit-logs operator_unstop. It removes nothing "
            "and never trades: start `live` afterwards. Exit 5 = reconcile not clean (nothing changed).")
    s.add_argument("--mode", choices=[m.value for m in Mode], help="default: TRADING_MODE (paper)")
    s.add_argument("--run", help="paper: run id (default: the last paper run)")
    s.add_argument("--i-understand-real-money", action="store_true", help="required for live")
    s.set_defaults(func=cmd_unstop)

    s = add("report", "rebuild the report from the store (offline) into final_report.rebuilt.json",
            "Rebuilds the final report offline from the run's store and writes final_report.rebuilt.json next "
            "to the runner's own final_report.json, which is never overwritten. Live values in a rebuild are "
            "ledger-based and NOT verified against the exchange.")
    s.add_argument("--mode", choices=[m.value for m in Mode], help="default: TRADING_MODE (paper)")
    s.add_argument("--run", help="run id (default: the mode's last run)")
    s.set_defaults(func=cmd_report)

    s = add("trace", "human-readable observe -> plan -> tool -> action -> verify -> reflect trace")
    s.add_argument("--mode", choices=[m.value for m in Mode], help="default: TRADING_MODE (paper)")
    s.add_argument("--run", help="run id (default: the mode's last run)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--last", type=int, help="only the last N cycles")
    g.add_argument("--decision", help="only cycles mentioning this decision id")
    s.set_defaults(func=cmd_trace)
    return p


# --------------------------------------------------------------------------- config
def _default_mode(env: Mapping[str, str] | None, dotenv_path: str | None) -> Mode:
    merged: dict[str, str] = {}
    if dotenv_path and Path(dotenv_path).is_file():
        merged.update(parse_dotenv(Path(dotenv_path).read_text(encoding="utf-8")))
    merged.update(dict(os.environ if env is None else env))
    raw = merged.get("TRADING_MODE", "").strip().lower()
    try:
        return Mode(raw) if raw else Mode.PAPER
    except ValueError as exc:
        raise ConfigError(f"bad value for TRADING_MODE: {raw!r}") from exc


def _secrets_from(env: Mapping[str, str] | None, dotenv_path: str | None) -> list[str]:
    vals = []
    src = os.environ if env is None else env
    if src.get("WALLEX_API_KEY"):
        vals.append(src["WALLEX_API_KEY"])
    if dotenv_path and Path(dotenv_path).is_file():
        try:
            k = parse_dotenv(Path(dotenv_path).read_text(encoding="utf-8")).get("WALLEX_API_KEY")
        except OSError:
            k = None
        if k:
            vals.append(k)
    return vals


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, transport: Any = None,
         clock: Any = None, input_fn: Callable[[str], str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:          # --help / --version (0) or usage error (2)
        return int(exc.code or 0)
    scrub = Scrubber(_secrets_from(env, args.env_file))
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = ScrubbingStream(old_out, scrub), ScrubbingStream(old_err, scrub)
    try:
        if not getattr(args, "func", None):
            parser.print_help()
            return EXIT_USAGE
        try:
            default_mode = _default_mode(env, args.env_file)
            overrides: dict[str, Any] = {"mode": Mode.PAPER}   # the live builders switch to LIVE themselves
            if args.data_dir:
                overrides["data_dir"] = args.data_dir
            cfg = load_config(env=env, dotenv_path=args.env_file, overrides=overrides)
        except ConfigError as exc:
            err(f"configuration error: {exc}")
            return EXIT_USAGE
        ctx = Ctx(args, cfg, default_mode, transport=transport, clock=clock, input_fn=input_fn)
        try:
            return int(args.func(ctx))
        except KeyboardInterrupt:
            err("interrupted")
            return EXIT_INTERRUPTED
        except WallexAgentError as exc:
            err(f"error: {type(exc).__name__}: {exc}")
            return EXIT_ERROR
        except Exception as exc:  # noqa: BLE001 - last resort: scrubbed message, non-zero exit
            err(f"unexpected error: {type(exc).__name__}: {exc}")
            return EXIT_ERROR
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        sys.stdout, sys.stderr = old_out, old_err


__all__ = ["main", "build_parser", "ScrubbingStream", "EXIT_CODES_HELP"]
