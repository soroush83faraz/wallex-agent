"""Live-trading gate: every condition must hold before a single real order may be sent.

``check_live_allowed`` evaluates ALL conditions and raises ``LiveNotAllowedError`` listing every unmet
one (an operator fixing them one by one would otherwise loop). The evidence files live in the state
dir and are written atomically:

* ``selftest.json`` - result of running the project's own test suite against THIS source tree
  (``source_hash``), fresh (<= 24 h);
* ``live_schema_attestation.json`` - minimal, secret-free summary of a GET-only private schema probe
  (``check-live``) for the same ``base_url`` and source tree, fresh (<= 24 h);
* ``live_canary.json`` - result of ONE tiny, non-fillable real order that was placed, observed and
  cancelled (``canary``) against the same ``base_url``, ok and <= 72 h old. It settles the API
  behaviours a GET-only probe cannot (client_id echo, lookup by id, cancel answer, locked balance).

A resumed run (``live --resume``, systemd) may use selftest/attestation evidence up to 48 h old
(``max_evidence_age_s``); a fresh start always needs <= 24 h. Nothing here talks to the exchange.
The clock is injectable (``now`` = epoch seconds).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from .clock import Clock, RealClock
from .config import Config
from .errors import LiveNotAllowedError
from .types import Mode, plain

PACKAGE_DIR = Path(__file__).resolve().parent
SELFTEST_FILE = "selftest.json"
ATTESTATION_FILE = "live_schema_attestation.json"
KILL_FILE = "KILL"
ARMING_FILE = "LIVE_ARMED"
CANARY_FILE = "live_canary.json"
MAX_EVIDENCE_AGE_S = 24 * 3600.0
RESUME_MAX_EVIDENCE_AGE_S = 48 * 3600.0
MAX_CANARY_AGE_S = 72 * 3600.0
MAX_FUTURE_SKEW_S = 300.0          # evidence stamped in the future => clock trouble or tampering
SELFTEST_TIMEOUT_S = 900.0

Runner = Callable[[Sequence[str], Path], "tuple[int, str]"]
InputFn = Callable[[str], str]


# --------------------------------------------------------------------------- small helpers
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _parse_iso(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:          # naive timestamps are ambiguous => refuse
        return None
    return dt.timestamp()


def atomic_write_json(path: str | Path, obj: Any) -> Path:
    """Write JSON to ``path`` via temp file + fsync + ``os.replace`` (never a half-written file)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True,
                       default=lambda o: plain(o) if isinstance(o, Decimal) else str(o)) + "\n")
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name == "posix":         # make the rename itself durable
        dfd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return p


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"unreadable ({type(exc).__name__})"
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    return obj, None


def _norm_url(url: Any) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _amount_text(config: Config) -> str:
    amt = config.initial_tmn
    return str(int(amt)) if amt == amt.to_integral_value() else plain(amt)


def required_phrase(config: Config) -> str:
    """``START LIVE <initial_tmn>`` with the amount as a plain integer string (e.g. 5000000)."""
    return f"START LIVE {_amount_text(config)}"


def required_canary_phrase(config: Config) -> str:
    """``RUN CANARY <initial_tmn>``: the canary places a REAL order, so it has its own phrase."""
    return f"RUN CANARY {_amount_text(config)}"


# --------------------------------------------------------------------------- source hash
def source_hash(package_dir: str | Path | None = None) -> str:
    """SHA-256 over every ``*.py`` under the package (sorted relative paths, ``__pycache__``
    ignored). Each file contributes its relative POSIX path and its bytes with CRLF normalised to
    LF, so a Windows checkout and a Linux checkout of the same commit hash identically, while a
    rename, addition or deletion changes the hash."""
    root = Path(package_dir) if package_dir is not None else PACKAGE_DIR
    files = sorted((p for p in root.rglob("*.py")
                    if "__pycache__" not in p.relative_to(root).parts and p.is_file()),
                   key=lambda p: p.relative_to(root).as_posix())
    h = hashlib.sha256()
    for p in files:
        rel = p.relative_to(root).as_posix().encode("utf-8")
        body = p.read_bytes().replace(b"\r\n", b"\n")
        h.update(len(rel).to_bytes(4, "big") + rel)
        h.update(len(body).to_bytes(8, "big") + body)
    return h.hexdigest()


# --------------------------------------------------------------------------- selftest
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


def _parse_pytest_counts(output: str) -> dict[str, int]:
    """Counts from pytest's final summary line (``12 passed, 1 failed in 3.2s``)."""
    counts: dict[str, int] = {}
    lines = [ln for ln in output.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if _COUNT_RE.search(ln) and " in " in ln), "")
    for n, word in _COUNT_RE.findall(summary):
        key = "errors" if word.startswith("error") else word
        counts[key] = counts.get(key, 0) + int(n)
    return counts


def _parse_junit_counts(path: Path) -> dict[str, int] | None:
    """Counts from a pytest junit XML report, or None if absent/unreadable."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        return None
    tot = {k: sum(int(s.get(k, 0) or 0) for s in suites)
           for k in ("tests", "failures", "errors", "skipped")}
    counts = {"passed": tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"],
              "failed": tot["failures"], "errors": tot["errors"], "skipped": tot["skipped"]}
    return {k: v for k, v in counts.items() if v}


def _default_runner(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    env = dict(os.environ)
    src = str(cwd / "src")    # test exactly the tree that was hashed, not some installed copy
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        cp = subprocess.run(list(argv), cwd=str(cwd), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=SELFTEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124, f"selftest timed out after {SELFTEST_TIMEOUT_S:.0f}s"
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def project_root(package_dir: str | Path | None = None) -> Path | None:
    """Repo root (``<root>/src/wallex_agent`` layout with a ``tests`` dir), else None."""
    pkg = Path(package_dir) if package_dir is not None else PACKAGE_DIR
    root = pkg.parent.parent
    if pkg.parent.name != "src" or not (root / "tests").is_dir():
        return None
    return root


def run_selftest(state_dir: str | Path, *, runner: Runner | None = None, clock: Clock | None = None,
                 package_dir: str | Path | None = None) -> dict[str, Any]:
    """Run the project's pytest suite (``python -m pytest -q`` in the repo root) and write
    ``<state_dir>/selftest.json`` atomically. ``runner(argv, cwd) -> (returncode, output)`` is a
    test hook. Without a repo ``tests`` dir (installed package) the result is ``passed: False``."""
    clk = clock or RealClock()
    pkg = Path(package_dir) if package_dir is not None else PACKAGE_DIR
    at = float(clk.now())
    t0 = clk.monotonic()
    h_before = source_hash(pkg)
    root = project_root(pkg)
    result: dict[str, Any] = {"passed": False, "tests": 0, "failures": 0, "source_hash": h_before,
                              "at": _iso(at), "python": sys.version.split()[0], "duration_s": 0.0,
                              "returncode": None, "counts": {}, "reason": ""}
    if root is None:
        result["reason"] = ("repository tests directory not found (installed package?): the selftest "
                            "must be run from a source checkout")
    else:
        with tempfile.TemporaryDirectory(prefix="wa-selftest-") as tmp:
            junit = Path(tmp) / "junit.xml"
            # `-o addopts=` keeps ONE -q (the ini's -q plus ours would hide the summary line);
            # the junit XML is the primary, verbosity-independent source of the counts.
            argv = [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
                    f"--junitxml={junit}"]
            rc, output = (runner or _default_runner)(argv, root)
            counts = _parse_junit_counts(junit) or _parse_pytest_counts(output)
        h_after = source_hash(pkg)
        failures = counts.get("failed", 0) + counts.get("errors", 0)
        total = sum(counts.get(k, 0) for k in ("passed", "failed", "errors", "skipped", "xfailed",
                                               "xpassed"))
        result.update(returncode=rc, counts=counts, tests=total, failures=failures)
        if h_after != h_before:
            result["reason"] = "source tree changed while the selftest was running"
        elif rc != 0:
            result["reason"] = f"pytest exited with code {rc}"
        elif failures:
            result["reason"] = f"{failures} failing test(s)"
        elif counts.get("passed", 0) == 0:
            result["reason"] = "no passing tests found in the pytest output"
        else:
            result["passed"] = True
            result["reason"] = "all tests passed"
    result["duration_s"] = round(max(0.0, clk.monotonic() - t0), 3)
    atomic_write_json(Path(state_dir) / SELFTEST_FILE, result)
    return result


# --------------------------------------------------------------------------- schema attestation
def write_schema_attestation(state_dir: str | Path, report: Mapping[str, Any], base_url: str, *,
                             package_dir: str | Path | None = None) -> dict[str, Any]:
    """Store a minimal attestation derived from a ``SchemaReport``: ok/at/base_url/source_hash plus
    the NAMES of failed checks and unverified items. Never copies details, keys or balances. ``ok``
    also requires the report to be GET-only and for the same ``base_url``."""
    reasons: list[str] = []
    if not bool(report.get("ok")):
        reasons.append("schema probe reported failures")
    if report.get("get_only") is not True:
        reasons.append("report is not marked get_only")
    if _norm_url(report.get("base_url")) != _norm_url(base_url):
        reasons.append("report base_url differs from the configured base_url")
    at_ts = _parse_iso(report.get("at"))
    if at_ts is None:
        reasons.append("report has no valid UTC 'at' timestamp")
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    unverified = sorted(str(u.get("id"))[:80] for u in (report.get("undocumented") or [])
                        if isinstance(u, Mapping))
    # items a valid canary (ok, same base_url, <= 72 h at probe time) has already settled
    by_canary: list[str] = []
    if at_ts is not None:
        canary_cfg = Config(api_base=str(base_url))
        by_canary = [u for u in canary_verified_items(canary_cfg, state_dir, at_ts) if u in unverified]
    att = {
        "ok": not reasons,
        "at": _iso(at_ts) if at_ts is not None else None,
        "base_url": str(base_url),
        "source_hash": source_hash(package_dir),
        "failed_checks": sorted(str(c.get("name"))[:80] for c in checks
                                if isinstance(c, Mapping) and not c.get("ok")),
        "unverified": [u for u in unverified if u not in by_canary],
        "verified_by_canary": by_canary,
        "probe_version": report.get("probe_version"),
        "reasons": reasons,
    }
    atomic_write_json(Path(state_dir) / ATTESTATION_FILE, att)
    return att


# --------------------------------------------------------------------------- phrase input
def read_typed_phrase(config: Config, *, input_fn: InputFn | None = None,
                      stdin: TextIO | None = None, phrase: str | None = None) -> str:
    """Ask the operator to type the confirmation phrase (``phrase``, default ``START LIVE ...``).
    Refuses a non-interactive stdin (no TTY: pipes, systemd, CI) unless ``input_fn`` is given
    explicitly (test hook)."""
    expected = phrase if phrase is not None else required_phrase(config)
    prompt = (f"REAL MONEY. Type exactly '{expected}' to continue: ")
    if input_fn is None:
        stream = stdin if stdin is not None else sys.stdin
        try:
            interactive = bool(stream is not None and stream.isatty())
        except (AttributeError, ValueError, OSError):
            interactive = False
        if not interactive:
            raise LiveNotAllowedError("typed confirmation requires an interactive terminal "
                                      "(stdin is not a TTY)")
        input_fn = input
    try:
        return input_fn(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise LiveNotAllowedError("typed confirmation aborted") from exc


def read_arming_phrase(state_dir: str | Path) -> str | None:
    """Content of the operator-created ``<state_dir>/LIVE_ARMED`` file (first line), or None.
    For an unattended restart (systemd, no TTY) the CLI may pass this as ``typed_phrase``."""
    p = Path(state_dir) / ARMING_FILE
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    return lines[0] if lines else ""


# --------------------------------------------------------------------------- evaluation
def _cond(name: str, met: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "met": bool(met), "detail": detail}


def _evidence_checks(prefix: str, doc: dict[str, Any] | None, err: str | None, *, ok_key: str,
                     current_hash: str, now: float, max_age_s: float,
                     base_url: str | None = None) -> list[dict[str, Any]]:
    label = {"selftest": "selftest.json", "schema_attestation": ATTESTATION_FILE}[prefix]
    if doc is None:
        return [_cond(f"{prefix}_present", False, f"{label} {err}")]
    out = [_cond(f"{prefix}_present", True, f"{label} found")]
    passed = doc.get(ok_key) is True
    why = str(doc.get("reason") or "; ".join(map(str, doc.get("reasons") or [])))[:200]
    out.append(_cond(f"{prefix}_{ok_key}", passed,
                     f"{label}: {ok_key}={doc.get(ok_key)!r}" + (f" ({why})" if why and not passed else "")))
    hash_ok = isinstance(doc.get("source_hash"), str) and doc.get("source_hash") == current_hash
    out.append(_cond(f"{prefix}_source_hash", hash_ok,
                     f"{label} source_hash matches the current source tree" if hash_ok else
                     f"{label} source_hash does not match the current source tree "
                     "(code changed since it was produced: re-run it)"))
    ts = _parse_iso(doc.get("at"))
    if ts is None:
        out.append(_cond(f"{prefix}_fresh", False, f"{label} has no valid UTC 'at' timestamp"))
    else:
        age = now - ts
        if age < -MAX_FUTURE_SKEW_S:
            out.append(_cond(f"{prefix}_fresh", False, f"{label} is dated {-age / 3600:.2f} h in the future"))
        else:
            out.append(_cond(f"{prefix}_fresh", age <= max_age_s,
                             f"{label} age {max(age, 0) / 3600:.2f} h (max {max_age_s / 3600:.0f} h)"))
    if base_url is not None:
        same = _norm_url(doc.get("base_url")) == _norm_url(base_url)
        out.append(_cond(f"{prefix}_base_url", same,
                         f"{label} base_url {'matches' if same else 'differs from'} the configured "
                         f"api_base ({doc.get('base_url')!r} vs {base_url!r})"))
    return out


def canary_status(config: Config, state_dir: str | Path, now: float,
                  max_age_s: float = MAX_CANARY_AGE_S) -> tuple[bool, str, dict[str, Any] | None]:
    """``(met, detail, doc)`` for ``live_canary.json``: present, ``ok: true``, same base_url as the
    config, a valid UTC timestamp at most ``max_age_s`` old (and not in the future)."""
    doc, err = _read_json(Path(state_dir) / CANARY_FILE)
    if doc is None:
        return False, f"{CANARY_FILE} {err} (run `wallex-agent canary` first)", None
    problems: list[str] = []
    if doc.get("ok") is not True:
        problems.append("ok is not true" + (f" ({'; '.join(map(str, doc.get('reasons') or []))[:160]})"
                                            if doc.get("reasons") else ""))
    if _norm_url(doc.get("base_url")) != _norm_url(config.api_base):
        problems.append(f"base_url {doc.get('base_url')!r} differs from {config.api_base!r}")
    ts = _parse_iso(doc.get("at"))
    if ts is None:
        problems.append("no valid UTC 'at' timestamp")
    elif now - ts < -MAX_FUTURE_SKEW_S:
        problems.append(f"dated {(ts - now) / 3600:.2f} h in the future")
    elif now - ts > max_age_s:
        problems.append(f"age {(now - ts) / 3600:.2f} h > {max_age_s / 3600:.0f} h")
    if problems:
        return False, f"{CANARY_FILE}: " + "; ".join(problems), doc
    return True, f"{CANARY_FILE} ok, age {max(now - ts, 0) / 3600:.2f} h (max {max_age_s / 3600:.0f} h)", doc


def canary_verified_items(config: Config, state_dir: str | Path, now: float) -> list[str]:
    """API items a valid (ok, same base_url, fresh) canary verified; [] otherwise."""
    met, _, doc = canary_status(config, state_dir, now)
    if not met or doc is None:
        return []
    return sorted(str(v)[:80] for v in (doc.get("verified") or []) if isinstance(v, str))


def _evaluate(config: Config, state_dir: Path, now: float, *,
              max_evidence_age_s: float = MAX_EVIDENCE_AGE_S,
              require_canary: bool = True) -> list[dict[str, Any]]:
    current = source_hash()
    conds = [
        _cond("mode_live", config.mode is Mode.LIVE,
              f"config.mode is {getattr(config.mode, 'value', config.mode)!r}"),
        _cond("enable_live", bool(config.enable_live), f"ENABLE_LIVE={bool(config.enable_live)}"),
        _cond("api_key_set", bool(config.api_key and config.api_key.strip()),
              "WALLEX_API_KEY is " + ("set" if config.api_key and config.api_key.strip() else "empty")),
    ]
    explicit = bool(getattr(config, "data_dir_explicit", True)) and Path(config.data_dir).is_absolute()
    conds.append(_cond("data_dir_explicit", explicit,
                       f"DATA_DIR is {config.data_dir}" if explicit else
                       f"DATA_DIR not set explicitly (would be {Path(config.data_dir).absolute()}): set DATA_DIR "
                       "in the environment/.env or pass --data-dir; live never runs on the working-directory "
                       "default, because another working directory would mean another ledger and KILL file"))
    kill_paths = {state_dir / KILL_FILE, config.kill_file}
    killed = sorted(str(p) for p in kill_paths if p.exists())
    conds.append(_cond("no_kill_file", not killed,
                       f"KILL file present: {', '.join(killed)}" if killed else "no KILL file"))
    st, st_err = _read_json(state_dir / SELFTEST_FILE)
    conds += _evidence_checks("selftest", st, st_err, ok_key="passed", current_hash=current, now=now,
                              max_age_s=max_evidence_age_s)
    at, at_err = _read_json(state_dir / ATTESTATION_FILE)
    conds += _evidence_checks("schema_attestation", at, at_err, ok_key="ok", current_hash=current,
                              now=now, max_age_s=max_evidence_age_s, base_url=config.api_base)
    if require_canary:
        met, detail, _ = canary_status(config, state_dir, now)
        conds.append(_cond("canary_ok", met, detail))
    return conds


def check_live_allowed(config: Config, *, flag_ack: bool, typed_phrase: str | None,
                       now: float | None = None, state_dir: str | Path | None = None,
                       input_fn: InputFn | None = None, require_canary: bool = True,
                       expected_phrase: str | None = None,
                       max_evidence_age_s: float = MAX_EVIDENCE_AGE_S) -> dict[str, Any]:
    """Raise ``LiveNotAllowedError`` (message + ``.unmet`` list) unless EVERY live condition holds.

    If ``typed_phrase`` is None the operator is prompted via ``read_typed_phrase`` (TTY only, or
    ``input_fn``) - but only when every other condition already holds. Returns a secret-free summary
    of the satisfied gate for the audit log.

    ``require_canary=False`` + ``expected_phrase=required_canary_phrase(config)`` is the gate of the
    canary order itself (everything else identical). ``max_evidence_age_s`` may be raised to
    ``RESUME_MAX_EVIDENCE_AGE_S`` only for resuming an already started run; it is capped there."""
    t = float(time.time() if now is None else now)
    sdir = Path(state_dir) if state_dir is not None else config.state_path
    max_age = min(float(max_evidence_age_s), RESUME_MAX_EVIDENCE_AGE_S)
    conds = _evaluate(config, sdir, t, max_evidence_age_s=max_age, require_canary=require_canary)
    conds.insert(3, _cond("flag_ack", flag_ack is True,
                          "--i-understand-real-money " + ("given" if flag_ack is True else "NOT given")))
    expected = expected_phrase if expected_phrase is not None else required_phrase(config)
    phrase = typed_phrase
    if phrase is None:
        if all(c["met"] for c in conds):
            try:
                phrase = read_typed_phrase(config, input_fn=input_fn, phrase=expected)
            except LiveNotAllowedError as exc:
                conds.append(_cond("typed_phrase", False, str(exc)))
        else:
            conds.append(_cond("typed_phrase", False,
                               "confirmation phrase not entered (prompt skipped: other gates unmet)"))
    if phrase is not None:
        # never echo what was typed (it could be a pasted secret)
        conds.append(_cond("typed_phrase", phrase == expected,
                           "typed phrase matches" if phrase == expected else
                           f"typed phrase does not match exactly '{expected}'"))
    unmet = [c for c in conds if not c["met"]]
    if unmet:
        lines = "\n".join(f"  - {c['name']}: {c['detail']}" for c in unmet)
        err = LiveNotAllowedError(f"live trading NOT allowed; {len(unmet)} unmet condition(s):\n{lines}")
        err.unmet = unmet  # type: ignore[attr-defined]
        raise err
    return {"allowed": True, "at": _iso(t), "source_hash": source_hash(), "base_url": config.api_base,
            "initial_tmn": plain(config.initial_tmn), "max_evidence_age_h": max_age / 3600,
            "require_canary": require_canary, "conditions": conds}


def arm_status(config: Config, *, now: float | None = None,
               state_dir: str | Path | None = None) -> dict[str, Any]:
    """What is met / missing for live trading (for ``status`` / ``check-live``). No secrets."""
    t = float(time.time() if now is None else now)
    sdir = Path(state_dir) if state_dir is not None else config.state_path
    conds = _evaluate(config, sdir, t)
    att, _ = _read_json(sdir / ATTESTATION_FILE)
    missing = [c["name"] for c in conds if not c["met"]]
    canary_verified = canary_verified_items(config, sdir, t)
    return {
        "mode": getattr(config.mode, "value", str(config.mode)),
        "base_url": config.api_base,
        "state_dir": str(sdir),
        "source_hash": source_hash(),
        "conditions": conds,
        "missing": missing,
        "ready_except_launch_inputs": not missing,
        "required_at_launch": ["--i-understand-real-money flag",
                               f"typed phrase '{required_phrase(config)}'"],
        "arming_file_present": (sdir / ARMING_FILE).is_file(),
        "unverified_api_items": [u for u in (att or {}).get("unverified") or []
                                 if u not in canary_verified],
        "canary_verified_api_items": canary_verified,
        "failed_probe_checks": list((att or {}).get("failed_checks") or []),
        "at": _iso(t),
    }
