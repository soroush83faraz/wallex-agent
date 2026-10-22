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
    code, out, _ = run_cli(capsys, ["report", "--mode", "backtest"], tmp_path)
    assert code == 0 and "final_report.json" in out and '"value"' in out
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
        assert "unmet" in err or "LIVE_ARMED" in err
    assert not (tmp_path / "state" / "live" / "state.db").exists()


def test_check_live_needs_a_key(capsys, tmp_path):
    code, _, err = run_cli(capsys, ["check-live"], tmp_path, env={"DATA_DIR": str(tmp_path / "s")})
    assert code == EXIT_REFUSED and "WALLEX_API_KEY" in err


def test_arm_live_writes_the_arming_file_when_all_gates_pass(capsys, tmp_path, monkeypatch):
    sd = tmp_path / "state"
    sd.mkdir()
    lg.run_selftest(sd, runner=lambda a, c: (0, "3 passed in 0.1s"))
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
    assert lg.read_arming_phrase(sd) == "START LIVE 5000000"


# --------------------------------------------------------------------------- kill
def test_kill_writes_the_kill_file_and_blocks_runs(capsys, tmp_path):
    code, out, _ = run_cli(capsys, ["kill", "--reason", "test"], tmp_path)
    assert code == EXIT_OK and (tmp_path / "state" / "KILL").is_file()
    code, out, _ = run_cli(capsys, ["paper", "--seconds", "30"], tmp_path, transport=recording_public_transport(),
                           clock=SimClock(1_700_000_000.0))
    assert code == EXIT_KILLED and "killed" in out
    code, out, _ = run_cli(capsys, ["kill", "--cancel-open", "--mode", "live"], tmp_path)
    assert code == EXIT_OK and "never placed a live order" in out
    code, out, _ = run_cli(capsys, ["status"], tmp_path)
    assert json.loads(out)["kill_file_present"] is True


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
