"""CLI: help texts, exit codes, offline demo (socket guard), paper/diagnose over a recorded fake
transport, inspection commands, and the API-key scrubbing of every output."""
from __future__ import annotations

import io
import json
import socket
from pathlib import Path

import pytest

from tests._runner_support import SENTINEL, recording_public_transport
from wallex_agent import cli
from wallex_agent import live_gate as lg
from wallex_agent.clock import SimClock
from wallex_agent.reporting import Scrubber, verify_chain
from wallex_agent.runner import (EXIT_GATE, EXIT_INTERRUPTED, EXIT_KILLED, EXIT_OK, EXIT_REFUSED,
                                 EXIT_STOPPED, EXIT_UNAVAILABLE, EXIT_USAGE)

COMMANDS = ["demo", "backtest", "fetch-history", "diagnose", "paper", "check-live", "canary", "selftest",
            "arm-live", "live", "status", "kill", "report", "trace"]


def env_for(tmp_path, **kw):
    e = {"DATA_DIR": str(tmp_path / "state"), "WALLEX_API_KEY": SENTINEL}
    e.update(kw)
    return e


def run_cli(capsys, argv, tmp_path, **kw):
    env = kw.pop("env", None) or env_for(tmp_path)
    code = cli.main(["--env-file", str(tmp_path / "none.env")] + list(argv), env=env, **kw)
    o = capsys.readouterr()
    return code, o.out, o.err


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# --------------------------------------------------------------------------- help / usage
@pytest.mark.parametrize("cmd", COMMANDS)
def test_every_subcommand_has_help_with_exit_codes(capsys, cmd):
    code = cli.main([cmd, "--help"])
    out = capsys.readouterr().out
    assert code == 0 and "exit codes:" in out and "130" in out


def test_top_level_help_lists_commands(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for c in COMMANDS:
        assert c in out


def test_paper_help_mentions_the_smoke_test(capsys):
    cli.main(["paper", "--help"])
    assert "paper --seconds 300 --interval 10" in capsys.readouterr().out


def test_usage_errors(capsys, tmp_path):
    assert cli.main(["nonsense"]) == EXIT_USAGE
    code, _, err = run_cli(capsys, ["backtest"], tmp_path)
    assert code == EXIT_USAGE and "--data" in err
    assert cli.main([]) == EXIT_USAGE


# --------------------------------------------------------------------------- demo / backtest
def test_demo_runs_offline_and_labels_synthetic(capsys, tmp_path, no_network):
    code, out, err = run_cli(capsys, ["demo", "--hours", "3"], tmp_path)
    assert code == EXIT_OK, err
    assert "SYNTHETIC" in out and "final_report.json" in out and "hash chain OK" in out
    report = next(Path(line.split(maxsplit=1)[1]) for line in out.splitlines() if line.startswith("report:"))
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert "synthetic_data" in doc["labels"] and doc["run"]["status"] == "deadline"
    assert SENTINEL not in out + err


def test_report_status_trace_after_a_backtest(capsys, tmp_path):
    assert run_cli(capsys, ["backtest", "--synthetic", "--hours", "1", "--seed", "4"], tmp_path)[0] == EXIT_OK
    code, out, _ = run_cli(capsys, ["status", "--mode", "backtest"], tmp_path)
    st = json.loads(out)
    assert code == 0 and st["state"] == "STOPPED" and st["open_orders"] == [] and st["time_left_h"] == 0.0
    assert "arm_status" in st and SENTINEL not in out
    runs = list((tmp_path / "state" / "backtest" / "runs").glob("*/final_report.json"))
    assert len(runs) == 1
    original = runs[0].read_bytes()
    code, out, _ = run_cli(capsys, ["report", "--mode", "backtest"], tmp_path)
    assert code == 0 and "final_report.rebuilt.json" in out and '"value"' in out
    assert runs[0].read_bytes() == original                    # F3: the runner's report is untouched
    rebuilt = json.loads((runs[0].parent / "final_report.rebuilt.json").read_text(encoding="utf-8"))
    assert rebuilt["clock"] == "simulated" and "simulated_clock" in rebuilt["labels"]
    code, out, _ = run_cli(capsys, ["trace", "--mode", "backtest", "--last", "2"], tmp_path)
    assert code == 0 and out.startswith("Audit trace") and "OBSERVE" in out


def test_report_and_trace_without_runs(capsys, tmp_path):
    assert run_cli(capsys, ["report", "--mode", "paper"], tmp_path)[0] == EXIT_REFUSED
    assert run_cli(capsys, ["trace", "--mode", "live"], tmp_path)[0] == EXIT_REFUSED
    code, out, _ = run_cli(capsys, ["status"], tmp_path)
    assert code == 0 and json.loads(out)["run"] is None


def test_backtest_from_a_csv_file(capsys, tmp_path):
    csv = tmp_path / "c.csv"
    rows = ["time,open,high,low,close,volume"] + [f"{1_700_000_000 + 60 * i},81000,81010,80990,{81000 + i},1"
                                                  for i in range(90)]
    csv.write_text("\n".join(rows), encoding="utf-8")
    code, out, err = run_cli(capsys, ["backtest", "--data", str(csv)], tmp_path)
    assert code == EXIT_OK, err
    assert "BACKTEST over c.csv" in out
    assert run_cli(capsys, ["backtest", "--data", str(tmp_path / "missing.csv")], tmp_path)[0] == EXIT_REFUSED


# --------------------------------------------------------------------------- paper / public
def test_paper_session_uses_only_public_gets(capsys, tmp_path):
    tr = recording_public_transport()
    code, out, err = run_cli(capsys, ["paper", "--seconds", "120", "--interval", "30"], tmp_path, transport=tr,
                             clock=SimClock(1_700_000_000.0))
    assert code == EXIT_OK, err
    assert "PAPER" in out and "session_end" in out
    assert {r["method"] for r in tr.records} == {"GET"} and not any(r["with_key"] for r in tr.records)
    assert SENTINEL not in out + err


def test_paper_unavailable_network(capsys, tmp_path):
    from wallex_agent.modes import RecordingTransport

    def down(*a):
        from wallex_agent.errors import TransportError
        raise TransportError("no route")
    code, _, err = run_cli(capsys, ["paper", "--seconds", "10"], tmp_path,
                           transport=RecordingTransport(responder=down), clock=SimClock())
    assert code == EXIT_UNAVAILABLE and "unavailable" in err


def test_diagnose_public_math(capsys, tmp_path):
    code, out, err = run_cli(capsys, ["diagnose"], tmp_path, transport=recording_public_transport(),
                             clock=SimClock(1_700_000_000.0))
    assert code == EXIT_OK, err
    doc = json.loads(out[: out.rindex("}") + 1])
    assert doc["conversion"]["tmn"] == "5000000" and doc["conversion"]["book_can_fill"] is True
    assert 17 < float(doc["conversion"]["usdt_net_of_assumed_fee"]) < 19
    assert any("fee" in u for u in doc["unknown"]) and any("client_id_echo" in u for u in doc["unknown"])
    assert "Trading feasible now" in out and SENTINEL not in out


def test_fetch_history_writes_candles(capsys, tmp_path):
    dest = tmp_path / "h.csv"
    code, out, _ = run_cli(capsys, ["fetch-history", "--symbol", "BTCUSDT", "--hours", "1", "--out", str(dest)],
                           tmp_path, transport=recording_public_transport(), clock=SimClock(1_792_670_000.0))
    assert code == EXIT_OK and dest.is_file() and "candles" in out
    assert dest.read_text(encoding="utf-8").startswith("time,open,high,low,close,volume")


# --------------------------------------------------------------------------- live gates
def test_live_canary_arm_refuse_without_gates(capsys, tmp_path):
    for argv in (["live"], ["live", "--i-understand-real-money"], ["canary"], ["arm-live"],
                 ["live", "--resume", "--i-understand-real-money"]):
        code, out, err = run_cli(capsys, argv, tmp_path, input_fn=lambda p: "START LIVE 5000000")
        assert code == EXIT_GATE, (argv, err)
        assert SENTINEL not in out + err
        assert "unmet" in err or "LIVE_ARMED" in err or "first live start must be interactive" in err
    assert not (tmp_path / "state" / "live" / "state.db").exists()


def test_check_live_needs_a_key(capsys, tmp_path):
    code, _, err = run_cli(capsys, ["check-live"], tmp_path, env={"DATA_DIR": str(tmp_path / "s")})
    assert code == EXIT_REFUSED and "WALLEX_API_KEY" in err


def test_arm_live_writes_the_arming_file_when_all_gates_pass(capsys, tmp_path, monkeypatch):
    sd = tmp_path / "state"
    sd.mkdir()
    lg.run_selftest(sd, runner=lambda a, c: (0, f"{lg.MIN_SELFTEST_TESTS} passed in 0.1s"))
    import time
    now = time.time()
    iso = lg._iso(now - 60)
    lg.write_schema_attestation(sd, {"ok": True, "get_only": True, "base_url": "https://api.wallex.ir", "at": iso,
                                     "checks": [], "undocumented": []}, "https://api.wallex.ir")
    lg.atomic_write_json(sd / lg.CANARY_FILE, {"ok": True, "at": iso, "base_url": "https://api.wallex.ir"})
    env = env_for(tmp_path, ENABLE_LIVE="true")
    code, out, err = run_cli(capsys, ["arm-live", "--i-understand-real-money"], tmp_path, env=env,
                             input_fn=lambda p: "START LIVE 5000000")
    assert code == EXIT_OK, err
    doc, why = lg.read_arming(sd)
    assert why is None and doc["phrase"] == "START LIVE 5000000" and doc["source_hash"] == lg.source_hash()
    assert "never start a fresh live run" in out and SENTINEL not in json.dumps(doc)


# --------------------------------------------------------------------------- kill
def test_kill_writes_the_kill_file_and_blocks_runs(capsys, tmp_path):
    # nothing under DATA_DIR yet: refused, nothing written (it would probably miss the agent)
    code, out, err = run_cli(capsys, ["kill", "--reason", "test"], tmp_path)
    assert code == EXIT_REFUSED and not (tmp_path / "state" / "KILL").exists()
    assert str(tmp_path / "state") in err and "systemctl stop wallex-agent" in err
    code, out, err = run_cli(capsys, ["kill", "--reason", "test", "--force-write"], tmp_path)
    kill = tmp_path / "state" / "KILL"
    assert code == EXIT_OK and kill.is_file() and f"KILL file written: {kill}" in out
    assert "demo/backtest runs do NOT read" in out and "unstop" in out and "WARNING" in err
    code, out, _ = run_cli(capsys, ["paper", "--seconds", "30"], tmp_path, transport=recording_public_transport(),
                           clock=SimClock(1_700_000_000.0))
    assert code == EXIT_KILLED and "killed" in out
    code, out, _ = run_cli(capsys, ["kill", "--cancel-open", "--mode", "live"], tmp_path)
    assert code == EXIT_OK and "no live store" in out          # a paper run exists now: no refusal
    code, out, _ = run_cli(capsys, ["status"], tmp_path)
    assert json.loads(out)["kill_file_present"] is True


def test_kill_from_another_working_directory_does_not_pretend(capsys, tmp_path, monkeypatch):
    """M3 / R2: the operator's shell lacks the agent's DATA_DIR: kill must not silently write a KILL
    file the agent never reads, and every path it prints is absolute."""
    agent_state = tmp_path / "var-lib" / "state"
    from wallex_agent.store import SqliteStore
    SqliteStore(agent_state / "live" / "state.db").close()         # the agent's real store
    operator_cwd = tmp_path / "home-operator"
    operator_cwd.mkdir()
    monkeypatch.chdir(operator_cwd)
    shell = {"OPERATOR_SHELL": "1"}                    # no DATA_DIR, no TRADING_MODE, no key
    code, out, err = run_cli(capsys, ["kill", "--cancel-open", "--reason", "emergency"], tmp_path, env=shell)
    assert code == EXIT_REFUSED and not list(tmp_path.rglob("KILL"))
    assert str(operator_cwd / "state") in err
    code, out, err = run_cli(capsys, ["--data-dir", str(agent_state), "kill", "--reason", "emergency"],
                             tmp_path, env=shell)
    assert code == EXIT_OK and (agent_state / "KILL").is_file() and str(agent_state / "KILL") in out
    assert json.loads(out[out.index("{"): out.index("}") + 1])["live_store_exists"] is True


def test_config_data_dir_is_absolute_and_explicitness_is_recorded(tmp_path, monkeypatch):
    from wallex_agent.config import load_config
    monkeypatch.chdir(tmp_path)
    c = load_config(env={}, dotenv_path=None)
    assert c.data_dir == str(tmp_path / "state") and c.data_dir_explicit is False
    c = load_config(env={"DATA_DIR": "rel/dir"}, dotenv_path=None)
    assert c.data_dir == str(tmp_path / "rel" / "dir") and c.data_dir_explicit is True
    c = load_config(env={}, dotenv_path=None, overrides={"data_dir": "x"})
    assert c.data_dir == str(tmp_path / "x") and c.data_dir_explicit is True


def test_live_refuses_the_working_directory_default_data_dir(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for argv in (["live", "--i-understand-real-money"], ["arm-live", "--i-understand-real-money"],
                 ["canary", "--i-understand-real-money"]):
        code, out, err = run_cli(capsys, argv, tmp_path, env={"WALLEX_API_KEY": SENTINEL, "ENABLE_LIVE": "true"})
        assert code == EXIT_GATE and "data_dir_explicit" in err and SENTINEL not in out + err


def _cancel_responder(order_id: str, symbol: str = "USDTTMN"):
    """Fake keyed Wallex for one resting order of ours: GET shows it, DELETE cancels it."""
    from tests._runner_support import public_responder
    pub = public_responder()
    state = {"active": True, "deletes": 0}

    def order_doc():
        return {"symbol": symbol, "type": "LIMIT", "side": "BUY", "price": "135000", "origQty": "0.38",
                "origSum": "51300", "executedQty": "0", "executedSum": "0", "executedPercent": 0,
                "status": "NEW" if state["active"] else "CANCELED", "active": state["active"],
                "clientOrderId": order_id, "created_at": "2026-10-22T10:00:00Z"}

    def respond(method, url, headers, body):
        from urllib.parse import urlsplit
        path = urlsplit(url).path
        if method == "GET" and path == f"/v1/account/orders/{order_id}":
            return 200, {}, json.dumps({"success": True, "message": "ok", "result": order_doc()}).encode()
        if method == "DELETE" and path == "/v1/account/orders":
            state["active"] = False
            state["deletes"] += 1
            return 200, {}, b""
        return pub(method, url, headers, body)
    return respond, state


def test_kill_cancel_open_acts_on_live_whenever_the_live_store_exists(capsys, tmp_path):
    """M3: --cancel-open reaches the LIVE store even though TRADING_MODE says paper."""
    from decimal import Decimal as D
    from wallex_agent.modes import RecordingTransport
    from wallex_agent.store import OrderRecord, SqliteStore
    from wallex_agent.types import OrderLifecycle, Side
    sd = tmp_path / "state"
    s = SqliteStore(sd / "live" / "state.db")
    s.insert_order_intent(OrderRecord(client_id="wa-live-1-abc", symbol="USDTTMN", side=Side.BUY,
                                      price=D("135000"), quantity=D("0.38"), purpose="canary"))
    s.update_order("wa-live-1-abc", lifecycle=OrderLifecycle.SENDING)
    s.update_order("wa-live-1-abc", lifecycle=OrderLifecycle.OPEN, exchange_client_order_id="wa-live-1-abc")
    s.close()
    env = env_for(tmp_path, TRADING_MODE="paper")
    responder, state = _cancel_responder("wa-live-1-abc")
    code, out, err = run_cli(capsys, ["kill", "--cancel-open", "--reason", "x"], tmp_path, env=env,
                             transport=RecordingTransport(responder=responder), clock=SimClock(1_800_000_000.0))
    assert code == EXIT_OK, err
    assert state["deletes"] == 1 and '"cancelled": [' in out and "wa-live-1-abc" in out
    from wallex_agent.modes import open_snapshot
    snap = open_snapshot(sd / "live" / "state.db")
    try:
        assert snap.get_order("wa-live-1-abc").lifecycle is OrderLifecycle.CANCELED
        assert snap.kv_get("bot_state")["state"] == "STOPPED"
    finally:
        snap.close()
    # without the key the live cancel path refuses loudly instead of pretending
    code, out, err = run_cli(capsys, ["kill", "--cancel-open"], tmp_path, env={"DATA_DIR": str(sd)})
    assert code == EXIT_REFUSED and "WALLEX_API_KEY" in err


# --------------------------------------------------------------------------- summary texts (F3)
def _summary(capsys, tmp_path, mode, verified):
    from types import SimpleNamespace
    from wallex_agent.config import Config
    from wallex_agent.reporting import AuditLog
    from wallex_agent.runner import RunResult
    from wallex_agent.types import Mode
    log = AuditLog(tmp_path / f"a-{mode}-{verified}.jsonl", run_id="r", clock=SimClock(1_700_000_000.0))
    log.write("observe", {})
    log.close()
    rt = SimpleNamespace(mode=Mode(mode), run_id="live-x", config=Config(), audit=log)
    rep = {"labels": [], "value": {"initial_tmn": "5000000", "final_tmn_net_of_exit_costs": "5100000",
                                   "verified_against_exchange": verified,
                                   "exchange_check": {"detail": "exchange balances unavailable (TransportError)"}},
           "pnl": {"abs_tmn": "100000", "pct": "2", "target_statement": "Target 2x NOT reached"},
           "end": {"holdings": {"USDT": "18.1"}}}
    cli.print_summary(RunResult("deadline", 0, 3, "deadline", report=rep), rt, "TITLE")
    return capsys.readouterr().out


def test_live_summary_is_not_simulated_and_flags_an_unverified_value(capsys, tmp_path):
    out = _summary(capsys, tmp_path, "live", None)
    assert "Past (simulated)" not in out and "REAL-MONEY run" in out
    assert "LEDGER-BASED, NOT verified against the exchange balances" in out and "(unverified)" in out
    out = _summary(capsys, tmp_path, "live", True)
    assert "NOT verified" not in out and "confirmed against the exchange balances" in out
    out = _summary(capsys, tmp_path, "paper", None)
    assert "Past (simulated) results" in out and "REAL-MONEY" not in out and "NOT verified" not in out


# --------------------------------------------------------------------------- signals
def test_signal_handler_only_sets_a_flag(monkeypatch):
    """L5: the handler runs between two bytecodes of the main loop (possibly inside a print or an
    audit write): it must do no I/O at all, only flag the stop."""
    import signal as _signal

    class Boom(io.StringIO):
        def write(self, s):
            raise AssertionError("I/O from inside the signal handler")

    flag = cli.StopFlag()
    restore = cli._install_signals(flag)
    try:
        handler = _signal.getsignal(_signal.SIGINT)
        monkeypatch.setattr("sys.stdout", Boom())
        monkeypatch.setattr("sys.stderr", Boom())
        handler(_signal.SIGINT, None)
        assert flag.stop_requested and flag.reason == "SIGINT"
    finally:
        restore()
    assert _signal.getsignal(_signal.SIGINT) is not handler


def test_runner_announces_the_stop_from_the_main_loop(tmp_path):
    from tests._runner_support import hold_script, sim_world
    from wallex_agent.runner import AgentRunner
    w = sim_world(tmp_path)
    rt = w.runtime(hold_script)
    r = AgentRunner(rt)
    said = []
    r.notify = said.append
    r.request_stop("SIGTERM")                       # what the handler does
    assert said == []                               # nothing printed by the handler path
    res = r.run()
    assert res.status == "interrupted" and len(said) == 1 and said[0].startswith("SIGTERM received")


# --------------------------------------------------------------------------- secrets
def test_scrubbing_stream_removes_the_key():
    buf = io.StringIO()
    s = cli.ScrubbingStream(buf, Scrubber([SENTINEL]))
    s.write(f"key={SENTINEL} and X-API-Key: {SENTINEL}\n")
    assert SENTINEL not in buf.getvalue() and "***redacted***" in buf.getvalue()


def test_secret_never_reaches_output_audit_report_or_db(capsys, tmp_path):
    outputs = []
    for argv, kw in ((["demo", "--hours", "2"], {}),
                     (["paper", "--seconds", "60", "--interval", "30"],
                      {"transport": recording_public_transport(), "clock": SimClock(1_700_000_000.0)}),
                     (["status"], {}), (["live", "--i-understand-real-money"], {}),
                     (["diagnose"], {"transport": recording_public_transport(), "clock": SimClock(1_700_000_000.0)})):
        code, out, err = run_cli(capsys, argv, tmp_path, **kw)
        outputs.append(out + err)
    assert all(SENTINEL not in o for o in outputs)
    files = [p for p in (tmp_path / "state").rglob("*") if p.is_file()]
    assert any(p.name == "audit.jsonl" for p in files) and any(p.name.endswith("report.json") for p in files)
    for p in files:
        assert SENTINEL.encode() not in p.read_bytes(), p
    for rep in (tmp_path / "state").rglob("final_report.json"):
        assert json.loads(rep.read_text(encoding="utf-8"))["config"]["api_key"] in ("***set***", "***redacted***")
    for audit in (tmp_path / "state").rglob("audit.jsonl"):
        assert verify_chain(audit) == (True, None)


def test_exit_code_constants_are_distinct():
    codes = [EXIT_OK, EXIT_USAGE, EXIT_GATE, EXIT_KILLED, EXIT_STOPPED, EXIT_REFUSED, EXIT_UNAVAILABLE,
             EXIT_INTERRUPTED]
    assert len(set(codes)) == len(codes)
