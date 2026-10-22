"""Mode wiring: separate stores, labels, paper is public-GET-only (recorded), the live builder is the
only path to a keyed write client and it is gated."""
from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from tests._runner_support import (FIXTURES, SENTINEL, ScriptedBrain, recording_public_transport, track,
                                   trader_script)
from wallex_agent import live_gate as lg
from wallex_agent.clock import SimClock
from wallex_agent.config import Config
from wallex_agent.errors import ForbiddenEndpointError, LiveNotAllowedError
from wallex_agent.exchange.wallex import WallexClient
from wallex_agent.modes import (RecordingTransport, build_runtime, last_run, live_store_path, open_snapshot,
                                runs_dir)
from wallex_agent.modes.live import CancelOnlyExchange, build_cancel_only_runtime, build_live
from wallex_agent.runner import K_START, AgentRunner
from wallex_agent.store import SqliteStore
from wallex_agent.types import Mode, OrderRequest, Side

SRC = Path(__file__).resolve().parents[1] / "src" / "wallex_agent"
NOW = 1_800_000_000.0
BASE = "https://api.wallex.ir"
PHRASE = "START LIVE 5000000"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- backtest
def test_backtest_runs_are_isolated_labelled_and_pointed_to(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    rt = track(build_runtime(cfg, "backtest", synthetic=True, seed=3, hours=0.5))
    assert rt.mode is Mode.BACKTEST and rt.config.mode is Mode.BACKTEST
    assert rt.run_dir.parent == runs_dir(cfg, Mode.BACKTEST) and (rt.run_dir / "state.db").is_file()
    assert last_run(cfg, Mode.BACKTEST) == rt.run_id
    assert {"synthetic_data", "assumed_fee", "backtest_simulated"} <= set(rt.labels) and rt.synthetic
    fees = rt.exchange.get_fees()
    assert {f.taker for f in fees.values()} == {cfg.paper_fee_rate}
    assert rt.audit.durable is False and rt.config.kill_file.parent == rt.run_dir   # disposable sim run
    res = AgentRunner(rt).run()
    assert res.status == "deadline" and res.report_path.name == "final_report.json"
    rt2 = track(build_runtime(cfg, "backtest", synthetic=True, seed=3, hours=0.5))
    assert rt2.run_dir != rt.run_dir and last_run(cfg, Mode.BACKTEST) == rt2.run_id


def test_backtest_over_a_recorded_candle_file(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    rt = track(build_runtime(cfg, "backtest", data=FIXTURES / "udf_btcusdt_1m_real.json"))
    assert "recorded_btc_candles_constant_fx" in rt.labels and not rt.synthetic
    res = AgentRunner(rt).run()
    assert res.status in ("data_end", "deadline")
    assert any("CONSTANT" in c for c in res.report["honest_caveats"])


def test_backtest_rejects_data_and_synthetic_together(tmp_path):
    with pytest.raises(ValueError):
        build_runtime(Config(data_dir=str(tmp_path)), "backtest", data="x.csv", synthetic=True)


# --------------------------------------------------------------------------- paper
def test_paper_sends_only_public_gets(tmp_path):
    tr = recording_public_transport()
    clock = SimClock(1_700_000_000.0)
    cfg = Config(data_dir=str(tmp_path), api_key=SENTINEL)        # a key in the env must not matter
    rt = track(build_runtime(cfg, "paper", transport=tr, clock=clock))
    rt.brain = ScriptedBrain(rt.config, rt.store, clock, trader_script(buy_every=2, hold_cycles=1))
    res = AgentRunner(rt, interval_s=60).run(max_cycles=8)
    assert res.status == "max_cycles"
    assert rt.exchange.order_count() >= 2                          # simulated orders did happen
    recs = list(tr.records)
    assert recs, "paper must have read public books"
    assert {r["method"] for r in recs} == {"GET"}
    assert {r["path"] for r in recs} <= {"/v1/markets", "/v1/depth", "/v1/udf/history"}
    assert not any(r["with_key"] for r in recs)
    assert rt.run_dir.parent == runs_dir(cfg, Mode.PAPER)
    assert "live_public_books" in rt.labels and "assumed_fee" in rt.labels
    assert rt.audit.durable is True and rt.config.kill_file == tmp_path / "KILL"


def test_paper_client_cannot_write_or_read_private(tmp_path):
    tr = recording_public_transport()
    rt = track(build_runtime(Config(data_dir=str(tmp_path)), "paper", transport=tr, clock=SimClock()))
    client = rt.extras["public_client"]
    assert client.public_only and not client.has_key
    n = len(tr.records)
    for call in (lambda: client.create_order(OrderRequest("wa-1", "USDTTMN", Side.BUY, D("1"), D("1"))),
                 lambda: client.cancel_order("wa-1"), client.get_balances, client.get_fees,
                 lambda: client.get_order("wa-1"), client.list_open_orders, client.get_trades):
        with pytest.raises(ForbiddenEndpointError):
            call()
    assert len(tr.records) == n                                     # refused before any I/O
    assert not isinstance(rt.exchange, WallexClient)                # orders go to the simulator


# --------------------------------------------------------------------------- the only keyed write client
def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def _kw(call: ast.Call, name: str) -> object:
    for k in call.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value
    return None


def test_keyed_write_client_is_built_only_in_the_gated_live_builders():
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree, "WallexClient"):
            safe = _kw(call, "public_only") is True or _kw(call, "read_only") is True
            first = call.args[0] if call.args else None
            keyless = isinstance(first, ast.Constant) and first.value is None
            rel = path.relative_to(SRC).as_posix()
            if not (safe or keyless):
                offenders.append((rel, call.lineno))
    # exactly one unrestricted constructor: modes/live.py::_keyed_write_client
    live = (SRC / "modes" / "live.py").read_text(encoding="utf-8")
    tree = ast.parse(live)
    fn = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    ctor_line = _calls(fn["_keyed_write_client"], "WallexClient")[0].lineno
    assert offenders == [("modes/live.py", ctor_line)]
    callers = sorted(name for name, f in fn.items() if name != "_keyed_write_client"
                     and _calls(f, "_keyed_write_client"))
    assert callers == ["build_canary_runtime", "build_cancel_only_runtime", "build_live"]
    for name in ("build_live", "build_canary_runtime"):          # gate strictly before the client
        gate = min(c.lineno for c in _calls(fn[name], "check_live_allowed"))
        build = min(c.lineno for c in _calls(fn[name], "_keyed_write_client"))
        assert gate < build, name
    cancel = fn["build_cancel_only_runtime"]
    assert _calls(cancel, "CancelOnlyExchange"), "cancel-only builder must wrap the client"
    # nobody else imports the private constructor
    for path in SRC.rglob("*.py"):
        if path.name != "live.py":
            assert "_keyed_write_client" not in path.read_text(encoding="utf-8"), path


def test_cancel_only_exchange_refuses_orders_locally():
    tr = RecordingTransport(responder=lambda *a: (500, {}, b""))
    ex = CancelOnlyExchange(WallexClient(SENTINEL, transport=tr))
    with pytest.raises(ForbiddenEndpointError):
        ex.create_order(OrderRequest("wa-1", "USDTTMN", Side.BUY, D("1"), D("1")))
    assert len(tr.records) == 0 and SENTINEL not in repr(ex)


def test_cancel_only_runtime_needs_a_key(tmp_path):
    with pytest.raises(LiveNotAllowedError):
        build_cancel_only_runtime(Config(data_dir=str(tmp_path)))


# --------------------------------------------------------------------------- live gate in the builder
def ok_runner(argv, cwd):
    return 0, "12 passed in 0.5s\n"


def arm(tmp_path, *, age_h=1.0, canary=True):
    lg.run_selftest(tmp_path, runner=ok_runner, clock=SimClock(NOW - age_h * 3600))
    lg.write_schema_attestation(tmp_path, {"ok": True, "get_only": True, "base_url": BASE,
                                           "at": iso(NOW - age_h * 3600), "checks": [], "undocumented": []}, BASE)
    if canary:
        lg.atomic_write_json(tmp_path / lg.CANARY_FILE, {"ok": True, "at": iso(NOW - 600), "base_url": BASE,
                                                          "verified": []})


def live_cfg(tmp_path, **kw):
    return replace(Config(data_dir=str(tmp_path), enable_live=True, api_key=SENTINEL), **kw)


CASES = ["enable_live", "flag", "no_key", "selftest", "attestation", "canary", "kill", "phrase", "no_phrase"]


@pytest.mark.parametrize("case", CASES)
def test_live_builder_refuses_every_unmet_condition_before_any_io(tmp_path, case):
    arm(tmp_path, canary=case != "canary")
    cfg = live_cfg(tmp_path)
    kw = {"flag_ack": True, "typed_phrase": PHRASE}
    if case == "enable_live":
        cfg = replace(cfg, enable_live=False)
    elif case == "flag":
        kw["flag_ack"] = False
    elif case == "no_key":
        cfg = replace(cfg, api_key="")
    elif case == "selftest":
        (tmp_path / lg.SELFTEST_FILE).unlink()
    elif case == "attestation":
        (tmp_path / lg.ATTESTATION_FILE).unlink()
    elif case == "kill":
        (tmp_path / "KILL").write_text("stop", encoding="utf-8")
    elif case == "phrase":
        kw["typed_phrase"] = "START LIVE 1"
    elif case == "no_phrase":
        kw["typed_phrase"] = None                 # no TTY in tests: the prompt itself is refused
    tr = RecordingTransport(responder=lambda *a: (500, {}, b""))
    with pytest.raises(LiveNotAllowedError) as ei:
        build_live(cfg, transport=tr, clock=SimClock(NOW), now=NOW, **kw)
    assert SENTINEL not in str(ei.value)
    assert len(tr.records) == 0
    assert not live_store_path(cfg).exists()


def test_live_builder_with_all_gates_builds_a_keyed_client(tmp_path):
    arm(tmp_path)
    tr = RecordingTransport(responder=lambda *a: (500, {}, b""))
    rt = track(build_live(live_cfg(tmp_path), flag_ack=True, typed_phrase=PHRASE, transport=tr,
                          clock=SimClock(NOW), now=NOW))
    assert isinstance(rt.exchange, WallexClient) and rt.exchange.has_key and not rt.exchange.read_only
    assert rt.mode is Mode.LIVE and rt.run_id.startswith("live-") and rt.gate["allowed"]
    assert rt.audit.durable is True and rt.config.kill_file == tmp_path / "KILL"
    assert live_store_path(rt.config).is_file() and len(tr.records) == 0
    text = rt.audit.path.read_text(encoding="utf-8")
    assert '"kind":"live_gate"' in text and SENTINEL not in text and SENTINEL not in repr(rt.exchange)


def test_resume_reads_the_arming_file_and_relaxes_freshness_only_for_a_started_run(tmp_path):
    arm(tmp_path, age_h=30)
    cfg = live_cfg(tmp_path)
    tr = RecordingTransport(responder=lambda *a: (500, {}, b""))
    with pytest.raises(LiveNotAllowedError, match="LIVE_ARMED"):
        build_live(cfg, flag_ack=True, resume=True, transport=tr, clock=SimClock(NOW), now=NOW)
    (tmp_path / lg.ARMING_FILE).write_text(PHRASE + "\n", encoding="utf-8")
    with pytest.raises(LiveNotAllowedError) as ei:               # nothing to resume: 24 h rule
        build_live(cfg, flag_ack=True, resume=True, transport=tr, clock=SimClock(NOW), now=NOW)
    assert {c["name"] for c in ei.value.unmet} == {"selftest_fresh", "schema_attestation_fresh"}
    s = SqliteStore(live_store_path(cfg))
    s.kv_set(K_START, NOW - 3600)
    s.close()
    rt = track(build_live(cfg, flag_ack=True, resume=True, transport=tr, clock=SimClock(NOW), now=NOW))
    assert rt.gate["resume"] is True and rt.gate["max_evidence_age_h"] == 48
    rt.close()
    (tmp_path / lg.ARMING_FILE).write_text("START LIVE 6000000\n", encoding="utf-8")
    with pytest.raises(LiveNotAllowedError) as ei:
        build_live(cfg, flag_ack=True, resume=True, transport=tr, clock=SimClock(NOW), now=NOW)
    assert {c["name"] for c in ei.value.unmet} == {"typed_phrase"}


def _private_responder(fee_symbols=("BTCUSDT",)):
    from tests._runner_support import public_responder
    pub = public_responder()
    docs = {
        "/v1/account/balances": {"balances": {"TMN": {"asset": "TMN", "faName": "t", "fiat": True,
                                                      "value": "5000000", "locked": "0"}}},
        "/v1/account/fee": {s: {"makerFeeRate": "0.0025", "takerFeeRate": "0.0025", "recent_days_sum": 0}
                            for s in fee_symbols},
        "/v1/account/openOrders": {"orders": []},
        "/v1/account/trades": {"AccountLatestTrades": []},
    }

    def respond(method, url, headers, body):
        from urllib.parse import urlsplit
        path = urlsplit(url).path
        if method == "GET" and path in docs:
            assert headers.get("X-API-Key") == SENTINEL
            return 200, {}, json.dumps({"success": True, "message": "ok", "result": docs[path]}).encode()
        return pub(method, url, headers, body)
    return respond


def test_live_runtime_uses_the_account_fee_and_refuses_to_trade_when_a_fee_is_unknown(tmp_path):
    arm(tmp_path)
    tr = RecordingTransport(responder=_private_responder(fee_symbols=("BTCUSDT",)))
    clock = SimClock(NOW)
    rt = track(build_live(live_cfg(tmp_path), flag_ack=True, typed_phrase=PHRASE, transport=tr, clock=clock,
                          now=NOW))
    rt.brain = ScriptedBrain(rt.config, rt.store, clock, trader_script())
    res = AgentRunner(rt, interval_s=30).run(max_cycles=3)
    assert res.status == "max_cycles", res.reason
    assert not [r for r in tr.records if r["method"] != "GET"]          # no order: USDTTMN fee unknown
    recs = [json.loads(line) for line in rt.audit.path.read_text(encoding="utf-8").splitlines()]
    obs = [r["payload"] for r in recs if r["kind"] == "observe"]
    assert obs[0]["fees_taker"] == {"BTCUSDT": "0.0025"} and "fee:USDTTMN" in obs[0]["unknown"]
    verdicts = [r["payload"] for r in recs if r["kind"] == "risk_verdict"]
    assert verdicts and all(v["blocked"] or not v["approved"] for v in verdicts)
    assert SENTINEL not in rt.audit.path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- helpers
def test_snapshot_reads_a_locked_store(tmp_path):
    s = SqliteStore(tmp_path / "live" / "state.db")
    s.kv_set("bot_state", {"state": "READY"})
    snap = open_snapshot(tmp_path / "live" / "state.db")
    assert snap.kv_get("bot_state") == {"state": "READY"}
    snap.close()
    s.close()


def test_recording_transport_keeps_metadata_only():
    tr = RecordingTransport(responder=lambda m, u, h, b: (200, {}, b'{"success":true,"result":{"a":"secret-amount"}}'))
    tr.request("POST", "https://api.wallex.ir/v1/account/orders?x=1", {"X-API-Key": SENTINEL}, b'{"q":"1"}', 5)
    (rec,) = tr.records
    assert rec["with_key"] and rec["method"] == "POST" and rec["query_keys"] == ["x"]
    assert rec["result_keys"] == ["a"]
    dumped = json.dumps(list(tr.records))
    assert SENTINEL not in dumped and "secret-amount" not in dumped and '"q"' not in dumped
