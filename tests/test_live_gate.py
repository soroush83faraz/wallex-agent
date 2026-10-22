import io
import json
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from wallex_agent import live_gate as lg
from wallex_agent.clock import SimClock
from wallex_agent.config import Config
from wallex_agent.errors import LiveNotAllowedError
from wallex_agent.types import Mode

NOW = 1_800_000_000.0
SENTINEL_KEY = "SENTINEL-live-key-9f8e7d"
PHRASE = "START LIVE 5000000"
BASE = "https://api.wallex.ir"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


FULL = lg.MIN_SELFTEST_TESTS + 241          # a full-suite-sized run


def ok_runner(argv, cwd):
    return 0, f"........\n{FULL} passed in 0.50s\n"


def probe_report(**kw):
    rep = {"ok": True, "get_only": True, "base_url": BASE, "at": iso(NOW - 600), "probe_version": 1,
           "checks": [{"name": "private.balances", "ok": True,
                       "detail": {"assets": ["BTC", "USDT"], "value": "123.45"}},
                      {"name": "private.fee", "ok": True, "detail": {"shape": "mapping"}}],
           "undocumented": [{"id": "client_id_echo", "detail": "x", "status": "UNVERIFIED"}]}
    rep.update(kw)
    return rep


def live_config(tmp_path, **kw):
    return replace(Config(mode=Mode.LIVE, enable_live=True, api_key=SENTINEL_KEY, data_dir=str(tmp_path)), **kw)


def write_canary(sdir, **kw):
    doc = {"ok": True, "at": iso(NOW - 600), "base_url": BASE, "observations": {}, "verified": []}
    doc.update(kw)
    lg.atomic_write_json(Path(sdir) / lg.CANARY_FILE, doc)


@pytest.fixture
def armed(tmp_path):
    """A state dir with fresh, passing evidence for the current source tree."""
    cfg = live_config(tmp_path)
    lg.run_selftest(tmp_path, runner=ok_runner, clock=SimClock(NOW - 3600))
    lg.write_schema_attestation(tmp_path, probe_report(), BASE)
    write_canary(tmp_path)
    return cfg


def unmet_names(excinfo):
    return {c["name"] for c in excinfo.value.unmet}


def edit_json(path, **kw):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc.update(kw)
    Path(path).write_text(json.dumps(doc), encoding="utf-8")


# --------------------------------------------------------------------------- happy path
def test_happy_path_passes(armed):
    res = lg.check_live_allowed(armed, flag_ack=True, typed_phrase=PHRASE, now=NOW)
    assert res["allowed"] is True and res["source_hash"] == lg.source_hash()
    assert all(c["met"] for c in res["conditions"])
    assert SENTINEL_KEY not in json.dumps(res)


def test_happy_path_with_prompt_hook(armed):
    prompts = []
    res = lg.check_live_allowed(armed, flag_ack=True, typed_phrase=None, now=NOW,
                                input_fn=lambda p: prompts.append(p) or PHRASE)
    assert res["allowed"] and len(prompts) == 1 and PHRASE in prompts[0]


def test_phrase_uses_plain_integer(tmp_path):
    assert lg.required_phrase(live_config(tmp_path, initial_tmn=D("5000000.00"))) == "START LIVE 5000000"
    assert lg.required_phrase(live_config(tmp_path, initial_tmn=D("5E+6"))) == "START LIVE 5000000"


# --------------------------------------------------------------------------- each condition alone
def _break(case, cfg, sdir):
    st, at = sdir / lg.SELFTEST_FILE, sdir / lg.ATTESTATION_FILE
    kw = {"flag_ack": True, "typed_phrase": PHRASE}
    if case == "mode_live":
        cfg = replace(cfg, mode=Mode.PAPER)
    elif case == "enable_live":
        cfg = replace(cfg, enable_live=False)
    elif case == "flag_ack":
        kw["flag_ack"] = False
    elif case == "api_key_set":
        cfg = replace(cfg, api_key="")
    elif case == "no_kill_file":
        (sdir / "KILL").write_text("stop", encoding="utf-8")
    elif case == "selftest_present":
        st.unlink()
    elif case == "selftest_passed":
        edit_json(st, passed=False, reason="1 failing test(s)")
    elif case == "selftest_fresh":
        edit_json(st, at=iso(NOW - 24 * 3600 - 1))
    elif case == "selftest_test_count":
        edit_json(st, counts={"passed": 12})
    elif case == "selftest_source_hash":
        edit_json(st, source_hash="0" * 64)
    elif case == "schema_attestation_present":
        at.unlink()
    elif case == "schema_attestation_ok":
        edit_json(at, ok=False)
    elif case == "schema_attestation_fresh":
        edit_json(at, at=iso(NOW - 25 * 3600))
    elif case == "schema_attestation_base_url":
        cfg = replace(cfg, api_base="https://api.example.test")
    elif case == "schema_attestation_source_hash":
        edit_json(at, source_hash="f" * 64)
    elif case == "typed_phrase":
        kw["typed_phrase"] = "START LIVE 50000000"
    elif case == "data_dir_explicit":
        cfg = replace(cfg, data_dir_explicit=False)
    return cfg, kw


CASES = ["mode_live", "enable_live", "flag_ack", "api_key_set", "data_dir_explicit", "no_kill_file",
         "selftest_present", "selftest_test_count",
         "selftest_passed", "selftest_fresh", "selftest_source_hash", "schema_attestation_present",
         "schema_attestation_ok", "schema_attestation_fresh", "schema_attestation_base_url",
         "schema_attestation_source_hash", "typed_phrase"]


@pytest.mark.parametrize("case", CASES)
def test_each_unmet_condition_alone_blocks(armed, tmp_path, case):
    cfg, kw = _break(case, armed, tmp_path)
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(cfg, now=NOW, **kw)
    # the canary evidence is bound to the base_url too, so a different api_base also unmeets it
    assert unmet_names(ei) == ({case, "canary_ok"} if case == "schema_attestation_base_url" else {case})
    assert case in str(ei.value)
    assert SENTINEL_KEY not in str(ei.value)


@pytest.mark.parametrize("phrase", ["start live 5000000", "START LIVE 5000000 ", " START LIVE 5000000",
                                    "START LIVE 5,000,000", "START LIVE 5000000.0", ""])
def test_phrase_must_match_exactly(armed, phrase):
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(armed, flag_ack=True, typed_phrase=phrase, now=NOW)
    assert unmet_names(ei) == {"typed_phrase"}


def test_wrong_phrase_is_not_echoed(armed):
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(armed, flag_ack=True, typed_phrase="oops-pasted-SECRET-123", now=NOW)
    assert "oops-pasted-SECRET-123" not in str(ei.value)


def test_all_unmet_conditions_are_listed_and_no_prompt(tmp_path):
    asked = []
    cfg = Config(data_dir=str(tmp_path))     # paper, live disabled, no key, no evidence
    (tmp_path / "KILL").write_text("", encoding="utf-8")
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(cfg, flag_ack=False, typed_phrase=None, now=NOW,
                              input_fn=lambda p: asked.append(p) or PHRASE)
    assert unmet_names(ei) == {"mode_live", "enable_live", "flag_ack", "api_key_set", "no_kill_file",
                               "selftest_present", "schema_attestation_present", "canary_ok",
                               "typed_phrase"}
    assert asked == []     # never ask for the phrase while other gates are unmet
    assert "9 unmet condition(s)" in str(ei.value)


def test_expired_attestation_and_base_url_mismatch_together(armed, tmp_path):
    edit_json(tmp_path / lg.ATTESTATION_FILE, at=iso(NOW - 30 * 3600))
    cfg = replace(armed, api_base="https://evil.example.test")
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(cfg, flag_ack=True, typed_phrase=PHRASE, now=NOW)
    assert unmet_names(ei) == {"schema_attestation_fresh", "schema_attestation_base_url", "canary_ok"}


def test_freshness_boundary_and_future_dates(armed, tmp_path):
    # selftest was produced at NOW-3600: valid until exactly NOW-3600+24h
    lg.check_live_allowed(armed, flag_ack=True, typed_phrase=PHRASE, now=NOW - 3600 + 24 * 3600 - 600)
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(armed, flag_ack=True, typed_phrase=PHRASE, now=NOW - 3600 + 24 * 3600 + 1)
    assert unmet_names(ei) == {"selftest_fresh"}
    edit_json(tmp_path / lg.SELFTEST_FILE, at=iso(NOW + 3600))
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(armed, flag_ack=True, typed_phrase=PHRASE, now=NOW)
    assert unmet_names(ei) == {"selftest_fresh"} and "future" in str(ei.value)


def test_corrupt_evidence_fails_closed(armed, tmp_path):
    (tmp_path / lg.SELFTEST_FILE).write_text("{not json", encoding="utf-8")
    edit_json(tmp_path / lg.ATTESTATION_FILE, at="2026-10-22T10:00:00")   # naive => refused
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(armed, flag_ack=True, typed_phrase=PHRASE, now=NOW)
    assert unmet_names(ei) == {"selftest_present", "schema_attestation_fresh"}


def test_kill_file_in_config_dir_counts_when_state_dir_differs(armed, tmp_path):
    other = tmp_path / "other"
    shutil.copytree(tmp_path, other, ignore=shutil.ignore_patterns("other"))
    (tmp_path / "KILL").write_text("", encoding="utf-8")       # config.kill_file
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(armed, flag_ack=True, typed_phrase=PHRASE, now=NOW, state_dir=other)
    assert unmet_names(ei) == {"no_kill_file"}


# --------------------------------------------------------------------------- tampered source
def _copy_pkg(tmp_path):
    dst = tmp_path / "repo" / "src" / "wallex_agent"
    shutil.copytree(lg.PACKAGE_DIR, dst, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "repo" / "tests").mkdir()
    return dst


def test_tampered_source_blocks_live(tmp_path, monkeypatch):
    pkg = _copy_pkg(tmp_path)
    sdir = tmp_path / "state"
    monkeypatch.setattr(lg, "PACKAGE_DIR", pkg)
    cfg = live_config(sdir)
    lg.run_selftest(sdir, runner=ok_runner, clock=SimClock(NOW - 60))
    lg.write_schema_attestation(sdir, probe_report(), BASE)
    write_canary(sdir)
    lg.check_live_allowed(cfg, flag_ack=True, typed_phrase=PHRASE, now=NOW)       # untouched: ok
    risk = pkg / "risk.py"
    risk.write_bytes(risk.read_bytes() + b"\n# bypass the hurdle\n")
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(cfg, flag_ack=True, typed_phrase=PHRASE, now=NOW)
    assert unmet_names(ei) == {"selftest_source_hash", "schema_attestation_source_hash"}


def test_source_hash_properties(tmp_path):
    a = _copy_pkg(tmp_path / "a")
    b = _copy_pkg(tmp_path / "b")
    for p in b.rglob("*.py"):                      # Windows checkout: CRLF line endings
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    (b / "__pycache__").mkdir(exist_ok=True)
    (b / "__pycache__" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    (b / "exchange" / "__pycache__").mkdir(exist_ok=True)
    (b / "exchange" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0compiled")
    h = lg.source_hash(a)
    assert h == lg.source_hash(b) and len(h) == 64   # CRLF and __pycache__ do not count
    (a / "types.py").rename(a / "types2.py")
    assert lg.source_hash(a) != h                  # renames count
    (a / "types2.py").rename(a / "types.py")
    assert lg.source_hash(a) == h
    (a / "extra.py").write_text("", encoding="utf-8")
    assert lg.source_hash(a) != h                  # additions count (even empty files)


@pytest.mark.parametrize("name,data", [("notes.txt", b"data\n"), ("rules.json", b'{"x": 1}'),
                                       ("_speedups.pyd", b"MZ\0\0binary"), ("fast.so", b"\x7fELF\0\0"),
                                       ("sub/extra.dat", b"\0\1\2")])
def test_source_hash_covers_every_package_file_not_only_py(tmp_path, name, data):
    """M1(c): a native extension or a data file next to the code can change behaviour too."""
    a = _copy_pkg(tmp_path)
    h = lg.source_hash(a)
    (a / name).parent.mkdir(parents=True, exist_ok=True)
    (a / name).write_bytes(data)
    assert lg.source_hash(a) != h


def test_binary_files_are_not_crlf_normalised(tmp_path):
    a = _copy_pkg(tmp_path / "a")
    (a / "x.pyd").write_bytes(b"\0\r\n")
    h = lg.source_hash(a)
    (a / "x.pyd").write_bytes(b"\0\n")
    assert lg.source_hash(a) != h


def test_source_hash_covers_pyproject_and_tests(tmp_path):
    pkg = _copy_pkg(tmp_path)
    root = tmp_path / "repo"
    h0 = lg.source_hash(pkg)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts='-q'\n", encoding="utf-8")
    h1 = lg.source_hash(pkg)
    (root / "tests" / "conftest.py").write_text("import pytest\n", encoding="utf-8")
    h2 = lg.source_hash(pkg)
    (root / "tests" / "sub").mkdir()
    (root / "tests" / "sub" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    h3 = lg.source_hash(pkg)
    (root / "tests" / "fixture.json").write_text("{}", encoding="utf-8")       # tests: *.py only
    assert len({h0, h1, h2, h3}) == 4 and lg.source_hash(pkg) == h3
    labels = [label for label, _ in lg.source_files(pkg)]
    assert "project/pyproject.toml" in labels and "project/tests/conftest.py" in labels
    assert "project/tests/sub/test_x.py" in labels
    # the real checkout: its own tests are covered
    real = [label for label, _ in lg.source_files()]
    assert "project/tests/test_live_gate.py" in real and "project/pyproject.toml" in real


# --------------------------------------------------------------------------- selftest
def test_run_selftest_writes_attestation(tmp_path):
    seen = []

    def runner(argv, cwd):
        seen.append((list(argv), cwd))
        return 0, f"............\n=== {FULL} passed, 2 skipped in 0.50s ===\n"

    res = lg.run_selftest(tmp_path, runner=runner, clock=SimClock(NOW))
    argv, cwd = seen[0]
    assert argv[1:4] == ["-m", "pytest", "-q"] and Path(cwd) == lg.project_root()
    doc = json.loads((tmp_path / lg.SELFTEST_FILE).read_text(encoding="utf-8"))
    assert doc == res
    assert doc["passed"] is True and doc["tests"] == FULL + 2 and doc["failures"] == 0
    assert doc["source_hash"] == lg.source_hash() and doc["at"] == iso(NOW)
    assert set(doc) >= {"passed", "tests", "failures", "source_hash", "at", "python", "duration_s"}
    assert [p.name for p in tmp_path.iterdir()] == [lg.SELFTEST_FILE]    # no temp files left


@pytest.mark.parametrize("rc,out,failures", [
    (1, f"F.....\n1 failed, {FULL} passed in 1.00s\n", 1),
    (1, f"E\n{FULL} passed, 2 errors in 1.00s\n", 2),
    (0, f"{FULL} passed in 1.00s\n", 0),          # rc lies? no: passes
    (5, "no tests ran in 0.01s\n", 0),
    (0, "garbage without a summary\n", 0),
])
def test_run_selftest_failure_modes(tmp_path, rc, out, failures):
    res = lg.run_selftest(tmp_path, runner=lambda a, c: (rc, out), clock=SimClock(NOW))
    assert res["failures"] == failures
    assert res["passed"] is (rc == 0 and failures == 0 and "passed" in out)
    if not res["passed"]:
        assert res["reason"]


@pytest.mark.parametrize("n", [1, 12, lg.MIN_SELFTEST_TESTS - 1])
def test_a_partial_test_run_attests_nothing(tmp_path, n):
    """M1(d): `-k`/PYTEST_ADDOPTS or a trimmed tests dir must not produce a passing selftest."""
    res = lg.run_selftest(tmp_path, runner=lambda a, c: (0, f"{n} passed in 0.1s\n"), clock=SimClock(NOW))
    assert res["passed"] is False and "not the project's full suite" in res["reason"]
    res = lg.run_selftest(tmp_path, runner=lambda a, c: (0, f"{lg.MIN_SELFTEST_TESTS} passed in 1s\n"),
                          clock=SimClock(NOW))
    assert res["passed"] is True


def test_selftest_subprocess_gets_a_scrubbed_environment(monkeypatch):
    """M1(d): the test run never sees the key, the live switches, the data dir or proxies."""
    dirty = {"PATH": "/usr/bin", "SYSTEMROOT": "C:\\Windows", "TEMP": "/tmp", "LC_ALL": "C.UTF-8",
             "WALLEX_API_KEY": SENTINEL_KEY, "MY_EXCHANGE_SECRET_TOKEN": "tok", "DATA_DIR": "/var/lib/x",
             "ENABLE_LIVE": "true", "TRADING_MODE": "live", "PROFILE": "competition", "INITIAL_TMN": "1",
             "HTTPS_PROXY": "http://proxy:3128", "http_proxy": "http://p", "NO_PROXY": "*",
             "WALLEX_USE_ENV_PROXY": "true", "PYTEST_ADDOPTS": "-k nothing", "PYTHONPATH": "/evil",
             "PYTHONSTARTUP": "/evil.py", "API_BASE": "https://evil.test"}
    env = lg.selftest_env(dirty, "/repo/src")
    assert env == {"PATH": "/usr/bin", "SYSTEMROOT": "C:\\Windows", "TEMP": "/tmp", "LC_ALL": "C.UTF-8",
                   "PYTHONPATH": "/repo/src"}
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return lg.subprocess.CompletedProcess(argv, 0, f"{FULL} passed in 1s\n", "")
    monkeypatch.setattr(lg.subprocess, "run", fake_run)
    for k, v in dirty.items():
        monkeypatch.setenv(k, v)
    rc, out = lg._default_runner(["python", "-m", "pytest"], Path("/repo"))
    assert rc == 0 and SENTINEL_KEY not in json.dumps(seen["env"])
    assert not {"WALLEX_API_KEY", "DATA_DIR", "ENABLE_LIVE", "TRADING_MODE", "HTTPS_PROXY",
                "PYTEST_ADDOPTS"} & set(seen["env"])
    assert seen["env"]["PYTHONPATH"] == str(Path("/repo") / "src")


def test_run_selftest_prefers_junit_counts(tmp_path):
    def runner(argv, cwd):
        assert "addopts=" in argv                     # one -q only, so a summary line exists
        junit = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
        Path(junit).write_text('<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="40" '
                               'failures="1" errors="0" skipped="3"/></testsuites>', encoding="utf-8")
        return 1, ""                                  # e.g. -qq: no summary line at all

    res = lg.run_selftest(tmp_path, runner=runner, clock=SimClock(NOW))
    assert res["counts"] == {"passed": 36, "failed": 1, "skipped": 3}
    assert res["tests"] == 40 and res["failures"] == 1 and res["passed"] is False


def test_run_selftest_without_tests_dir_fails(tmp_path):
    pkg = tmp_path / "site-packages" / "wallex_agent"
    shutil.copytree(lg.PACKAGE_DIR, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    called = []
    res = lg.run_selftest(tmp_path / "state", runner=lambda a, c: called.append(1) or (0, "1 passed in 0s"),
                          clock=SimClock(NOW), package_dir=pkg)
    assert res["passed"] is False and "tests directory" in res["reason"] and called == []
    assert (tmp_path / "state" / lg.SELFTEST_FILE).is_file()


def test_run_selftest_detects_source_change_during_run(tmp_path):
    pkg = _copy_pkg(tmp_path)

    def runner(argv, cwd):
        (pkg / "config.py").write_text("# changed\n", encoding="utf-8")
        return 0, "3 passed in 0.1s"

    res = lg.run_selftest(tmp_path / "state", runner=runner, clock=SimClock(NOW), package_dir=pkg)
    assert res["passed"] is False and "changed" in res["reason"]


# --------------------------------------------------------------------------- schema attestation
def test_attestation_is_minimal_and_secret_free(tmp_path):
    rep = probe_report()
    rep["checks"][0]["detail"]["note"] = f"X-API-Key: {SENTINEL_KEY}"
    rep["observations"] = {"balances": {"USDT": "999.99"}}
    att = lg.write_schema_attestation(tmp_path, rep, BASE)
    text = (tmp_path / lg.ATTESTATION_FILE).read_text(encoding="utf-8")
    assert SENTINEL_KEY not in text and "999.99" not in text and "123.45" not in text
    assert att["ok"] is True and att["at"] == iso(NOW - 600) and att["base_url"] == BASE
    assert att["source_hash"] == lg.source_hash() and att["unverified"] == ["client_id_echo"]


@pytest.mark.parametrize("kw,why", [({"ok": False}, "failures"), ({"get_only": None}, "get_only"),
                                    ({"base_url": "https://other.test"}, "base_url"),
                                    ({"at": "yesterday"}, "timestamp")])
def test_attestation_not_ok_when_report_is_not_clean(tmp_path, kw, why):
    rep = probe_report(**kw)
    if kw.get("ok") is False:
        rep["checks"][1]["ok"] = False
    att = lg.write_schema_attestation(tmp_path, rep, BASE)
    assert att["ok"] is False and any(why in r for r in att["reasons"])
    if kw.get("ok") is False:
        assert att["failed_checks"] == ["private.fee"]


# --------------------------------------------------------------------------- phrase reader
class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_phrase_reader_refuses_non_tty(tmp_path, monkeypatch):
    cfg = live_config(tmp_path)
    with pytest.raises(LiveNotAllowedError, match="interactive"):
        lg.read_typed_phrase(cfg, stdin=io.StringIO(PHRASE + "\n"))
    monkeypatch.setattr("builtins.input", lambda p: PHRASE)
    assert lg.read_typed_phrase(cfg, stdin=FakeTTY()) == PHRASE


def test_gate_prompt_without_tty_is_an_unmet_condition(armed, monkeypatch):
    monkeypatch.setattr(lg.sys, "stdin", io.StringIO(PHRASE + "\n"))
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(armed, flag_ack=True, typed_phrase=None, now=NOW)
    assert unmet_names(ei) == {"typed_phrase"} and "TTY" in str(ei.value)


def test_phrase_reader_eof_is_refusal(tmp_path):
    def eof(prompt):
        raise EOFError

    with pytest.raises(LiveNotAllowedError, match="aborted"):
        lg.read_typed_phrase(live_config(tmp_path), input_fn=eof)


def test_arming_record_is_structured_and_bound(tmp_path):
    """M1(b): LIVE_ARMED binds the phrase, source tree, base URL, data dir and an expiry."""
    cfg = live_config(tmp_path, competition_hours=24.0)
    assert lg.read_arming(tmp_path) == (None, "missing")
    (tmp_path / lg.ARMING_FILE).write_text(PHRASE + "\r\n", encoding="utf-8")      # legacy / hand-written
    doc, why = lg.read_arming(tmp_path)
    assert doc is None and "not a structured arming record" in why
    doc = lg.write_arming(cfg, now=NOW)
    assert lg.read_arming(tmp_path) == (doc, None)
    assert doc["phrase"] == PHRASE and doc["source_hash"] == lg.source_hash() and doc["base_url"] == BASE
    assert doc["armed_at"] == iso(NOW) and doc["expires_at"] == iso(NOW + 24 * 3600 + lg.ARMING_GRACE_S)
    assert lg.arming_problems(doc, cfg, NOW + 3600) == []
    assert SENTINEL_KEY not in json.dumps(doc)


@pytest.mark.parametrize("change,why", [
    ({"phrase": "START LIVE 6000000"}, "phrase"), ({"initial_tmn": "6000000"}, "initial_tmn"),
    ({"source_hash": "0" * 64}, "source_hash"), ({"base_url": "https://evil.test"}, "base_url"),
    ({"data_dir": "/elsewhere"}, "DATA_DIR"), ({"version": 0}, "version"),
    ({"armed_at": "yesterday"}, "armed_at"), ({"expires_at": iso(NOW + 30 * 24 * 3600)}, "longer"),
    ({"armed_at": iso(NOW + 3600)}, "future"),
])
def test_arming_record_problems(tmp_path, change, why):
    cfg = live_config(tmp_path)
    doc = dict(lg.arming_record(cfg, now=NOW), **change)
    problems = lg.arming_problems(doc, cfg, NOW + 60)
    assert problems and any(why in p for p in problems), problems


def test_arming_record_expires(tmp_path):
    cfg = live_config(tmp_path, competition_hours=24.0)
    doc = lg.arming_record(cfg, now=NOW)
    assert lg.arming_problems(doc, cfg, NOW + 48 * 3600 - 1) == []
    assert any("expired" in p for p in lg.arming_problems(doc, cfg, NOW + 48 * 3600 + 1))
    other = replace(cfg, initial_tmn=D("6000000"))          # INITIAL_TMN changed after arming
    assert any("phrase" in p for p in lg.arming_problems(doc, other, NOW + 60))


# --------------------------------------------------------------------------- status
def test_arm_status_reports_met_and_missing_without_secrets(armed, tmp_path):
    st = lg.arm_status(armed, now=NOW)
    assert st["missing"] == [] and st["ready_except_launch_inputs"] is True
    assert st["unverified_api_items"] == ["client_id_echo"] and st["arming_file_present"] is False
    assert any(PHRASE in r for r in st["required_at_launch"])
    (tmp_path / "KILL").write_text("", encoding="utf-8")
    st = lg.arm_status(replace(armed, enable_live=False), now=NOW)
    assert st["missing"] == ["enable_live", "no_kill_file"] and not st["ready_except_launch_inputs"]
    assert SENTINEL_KEY not in json.dumps(st)
