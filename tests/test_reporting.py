import json
import urllib.parse
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from wallex_agent import STRATEGY_VERSION
from wallex_agent.clock import SimClock
from wallex_agent.reporting import (GENESIS_HASH, REDACTED, AuditChainError, AuditLog, Scrubber,
                                    build_final_report, read_audit, record_hash, render_trace,
                                    verify_chain, write_final_report)
from wallex_agent.types import Action, Mode, Side

from ._sim_support import BTC, FEES, INITIAL, USD, make_world

SENTINEL = "SENTINEL-k3y/abc+123=="


def lines(path):
    return Path(path).read_bytes().splitlines(keepends=True)


def write_n(path, n, clock=None, run_id="run-a", **kw):
    log = AuditLog(path, run_id=run_id, clock=clock or SimClock(1_700_000_000.0), **kw)
    for i in range(n):
        log.write("observe", {"i": i, "px": D("81000.10"), "qty": D("0.00010000")})
    return log


# --------------------------------------------------------------------------- chain
def test_chain_integrity_and_record_shape(tmp_path):
    p = tmp_path / "audit.jsonl"
    write_n(p, 5).close()
    assert verify_chain(p) == (True, None)
    recs = read_audit(p)
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5]
    assert recs[0]["prev_hash"] == GENESIS_HASH
    for a, b in zip(recs, recs[1:]):
        assert b["prev_hash"] == a["hash"]
    r = recs[0]
    assert set(r) == {"seq", "ts", "run_id", "strategy_version", "kind", "payload", "prev_hash", "hash"}
    assert r["ts"] == "2023-11-14T22:13:20.000Z" and r["run_id"] == "run-a"
    assert r["strategy_version"] == STRATEGY_VERSION
    assert r["payload"] == {"i": 0, "px": "81000.1", "qty": "0.0001"}   # Decimal -> plain string
    assert r["hash"] == record_hash(GENESIS_HASH, {k: v for k, v in r.items() if k != "hash"})
    assert all(ln.endswith(b"\n") for ln in lines(p))


def test_tamper_detection(tmp_path):
    p = tmp_path / "audit.jsonl"
    write_n(p, 5).close()
    good = lines(p)

    def rewrite(ls):
        p.write_bytes(b"".join(ls))

    rec = json.loads(good[2])
    rec["payload"]["px"] = "1"                        # edit a value, keep the old hash
    rewrite(good[:2] + [(json.dumps(rec) + "\n").encode()] + good[3:])
    assert verify_chain(p) == (False, 3)

    rec["hash"] = record_hash(rec["prev_hash"], {k: v for k, v in rec.items() if k != "hash"})
    rewrite(good[:2] + [(json.dumps(rec) + "\n").encode()] + good[3:])
    assert verify_chain(p) == (False, 4)              # re-hashed line breaks its successor

    rewrite(good[:1] + good[2:])                      # deleted line
    assert verify_chain(p) == (False, 2)
    rewrite([good[1], good[0]] + good[2:])            # reordered
    assert verify_chain(p) == (False, 1)
    rewrite(good[:4])                                 # truncation at a line boundary is undetectable
    assert verify_chain(p) == (True, None)            # by design (no external anchor); seq shows it
    rewrite(good + [b"{\"seq\": 6}\n"])
    assert verify_chain(p) == (False, 6)
    assert verify_chain(tmp_path / "missing.jsonl") == (False, 0)


def test_restart_resumes_chain_and_seq(tmp_path):
    p = tmp_path / "audit.jsonl"
    clock = SimClock(1_700_000_000.0)
    write_n(p, 3, clock).close()
    clock.advance(60)
    log = AuditLog(p, run_id="run-b", clock=clock)
    assert log.seq == 3
    log.write("reconcile_startup", {"ok": True})
    log.write("observe", {"cycle": 1})
    log.close()
    recs = read_audit(p)
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5]
    assert [r["run_id"] for r in recs] == ["run-a"] * 3 + ["run-b"] * 2
    assert verify_chain(p) == (True, None)


def test_refuses_to_extend_a_broken_chain(tmp_path):
    p = tmp_path / "audit.jsonl"
    write_n(p, 3).close()
    ls = lines(p)
    p.write_bytes(ls[0] + ls[2])
    with pytest.raises(AuditChainError, match="line 2"):
        AuditLog(p, run_id="x", clock=SimClock())
    assert lines(p) == [ls[0], ls[2]]                 # untouched


def test_torn_tail_is_dropped_and_recorded(tmp_path):
    p = tmp_path / "audit.jsonl"
    write_n(p, 2).close()
    with open(p, "ab") as fh:
        fh.write(b'{"seq": 3, "ts": "2023-11-1')     # crash mid-write
    log = AuditLog(p, run_id="x", clock=SimClock())
    log.write("observe", {})
    log.close()
    recs = read_audit(p)
    assert [r["kind"] for r in recs] == ["observe", "observe", "audit_torn_tail_dropped", "observe"]
    assert recs[2]["payload"]["bytes"] == len(b'{"seq": 3, "ts": "2023-11-1')
    assert verify_chain(p) == (True, None)


def test_complete_record_missing_only_newline_is_kept(tmp_path):
    p = tmp_path / "audit.jsonl"
    write_n(p, 3).close()
    data = p.read_bytes()
    p.write_bytes(data[:-1])                          # crash after the record, before "\n"
    log = AuditLog(p, run_id="x", clock=SimClock())
    assert log.seq == 3
    log.write("observe", {})
    log.close()
    assert [r["seq"] for r in read_audit(p)] == [1, 2, 3, 4]
    assert verify_chain(p) == (True, None)


# --------------------------------------------------------------------------- secrets
def test_sentinel_secret_never_reaches_disk(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p, run_id="r", clock=SimClock(), secrets=[SENTINEL])
    log.write("tool_call", {"api_key": SENTINEL, "fill_key": "BTCUSDT|BUY|1#0"})
    log.write("order_sending", {"headers": {"X-API-Key": "unknown-other-key-777"},
                                "Authorization": "Bearer tok-888", "access_token": "tok-999"})
    log.write("read_failed", {"error": f"HTTP 500 for request with X-API-Key: {SENTINEL}",
                              "nested": [{"note": f"echo {SENTINEL} echo"}, json.dumps({"k": SENTINEL})],
                              "url": "https://api.wallex.ir/x?k=" + urllib.parse.quote(SENTINEL, safe=""),
                              "raw": f"x-api-key={'zz-plain-header-value'}; next"})
    log.write(f"kind-{SENTINEL}", {SENTINEL: 1, "bytes": SENTINEL.encode()})
    log.write("status", {"api_key_set": True, "token_bucket_rate": None})
    log.close()
    raw = p.read_bytes().decode("utf-8")
    for leaked in (SENTINEL, json.dumps(SENTINEL)[1:-1], urllib.parse.quote(SENTINEL, safe=""),
                   "unknown-other-key-777", "tok-888", "tok-999", "zz-plain-header-value"):
        assert leaked not in raw
    recs = read_audit(p)
    assert recs[0]["payload"] == {"api_key": REDACTED, "fill_key": "BTCUSDT|BUY|1#0"}  # not over-redacted
    assert recs[4]["payload"] == {"api_key_set": True, "token_bucket_rate": None}     # bools/None kept
    assert verify_chain(p) == (True, None)


def test_scrubber_converts_types():
    s = Scrubber(["sek"])
    out = s.tree({"d": D("1E-8"), "side": Side.BUY, "f": float("nan"), "t": (1, 2), "set": {"b", "a"}})
    assert out == {"d": "0.00000001", "side": "BUY", "f": "nan", "t": [1, 2], "set": ["a", "b"]}


# --------------------------------------------------------------------------- engine integration
def run_scenario(tmp_path, *, api_key=SENTINEL):
    """Convert TMN->USDT, buy then sell BTC through the real engine on SimExchange, with an AuditLog
    as the engine's sink and agent-style decision/outcome/reflection events in the store."""
    w = make_world(tmp_path, api_key=api_key)
    w.audit = AuditLog(tmp_path / "audit.jsonl", run_id="run-x", clock=w.clock, secrets=[api_key])
    w.build_engine()
    store, pf = w.store, w.portfolio
    assert w.engine.convert_tmn_to_usdt().status == "done"
    pf.record_equity(w.clock.now(), D("5000000"))
    store.append("tool_call", {"name": "read_book", "ok": True})
    store.append("tool_call", {"name": "trend_signal", "ok": True})
    store.append("decision", {"decision_id": "d-1", "action": Action.BUY_BTC.value, "confidence": 0.62,
                              "estimated_net_edge": "0.004", "rationale": "uptrend with bid pressure",
                              "hypothesis": {"name": "trend_follow", "direction": "up",
                                             "expected_move_bps": 80, "horizon_s": 1800},
                              "tool_calls": [{"name": "read_book"}, {"name": "trend_signal"}]})
    w.clock.advance(30)
    r = w.engine.submit(w.entry("0.0002"), decision_id="d-1")
    assert r.status == "filled"
    pf.record_equity(w.clock.now(), D("5100000"))
    w.clock.advance(600)
    pf.record_equity(w.clock.now(), D("4900000"))
    store.append("decision", {"decision_id": "d-2", "action": "HOLD", "confidence": 0.3,
                              "tool_calls": [{"name": "read_book"}]})
    store.append("decision", {"decision_id": "d-3", "action": Action.SELL_BTC.value, "confidence": 0.7,
                              "rationale": "time stop", "hypothesis": {"name": "trend_follow"}})
    r = w.engine.submit(w.exit(), decision_id="d-3")
    assert r.status == "filled"
    store.append("outcome", {"decision_id": "d-1", "result": "loss", "pnl_bps": -95})
    store.append("reflection", {"decision_id": "d-1", "hypothesis": "up 80bps", "result": "-95bps",
                                "error_bps": -175, "lesson": "spread+fees ate the move"})
    store.append("reflection", {"decision_id": "d-2", "lesson": "abstaining was right"})
    return w


def test_engine_writes_a_verifiable_secret_free_audit(tmp_path):
    w = run_scenario(tmp_path)
    w.audit.close()
    p = tmp_path / "audit.jsonl"
    assert verify_chain(p) == (True, None)
    kinds = [r["kind"] for r in read_audit(p)]
    assert "order_sending" in kinds and "submit_result" in kinds and "conversion" in kinds
    assert SENTINEL not in p.read_text(encoding="utf-8")
    trace = render_trace(p)
    entry = [ln for ln in trace.splitlines() if "submit_result" in ln and "decision_id=d-1" in ln]
    assert len(entry) == 1 and "purpose=entry" in entry[0] and "status=filled" in entry[0]
    assert "ACTION" in entry[0]


def report_for(w, **kw):
    books = {s: w.source.get_book(s) for s in (BTC, USD)}
    args = dict(config=w.cfg, mode=Mode.PAPER, run_id="run-x", started_ts=1_700_000_000.0,
                ended_ts=w.clock.now(), portfolio=w.portfolio, engine_status=w.engine.status(),
                store=w.store, memory_snapshot={"posteriors": {"chop|trend_follow": {"a": 1, "b": 2}}},
                books_at_end=books, fees=FEES, caveats=["demo run"], stop_reason="deadline",
                synthetic_data=True)
    args.update(kw)
    return build_final_report(**args)


def test_final_report_on_sim_scenario(tmp_path):
    w = run_scenario(tmp_path)
    rep = report_for(w)
    dumped = json.dumps(rep)
    assert SENTINEL not in dumped
    assert rep["config"]["api_key"] == REDACTED and rep["mode"] == "paper"
    assert {"paper_no_real_orders", "assumed_fee", "synthetic_data"} <= set(rep["labels"])
    assert "backtest_simulated" not in rep["labels"] and "valuation_incomplete" not in rep["labels"]

    orders, fills = w.store.list_orders(), w.store.list_fills()
    assert rep["orders"]["count"] == len(orders) == 3
    assert rep["orders"]["order_ids"] == [o.client_id for o in orders]
    assert rep["fills"]["count"] == len(fills) >= 3
    assert rep["fees_paid"] == {a: format(v, "f").rstrip("0").rstrip(".") for a, v in w.portfolio.fees_paid().items()}
    assert rep["fee_source_counts"] == {"exchange": len(fills)}

    eq = w.portfolio.liquidation_value({s: w.source.get_book(s) for s in (BTC, USD)}, FEES)
    v = rep["value"]
    assert D(v["initial_tmn"]) == INITIAL
    assert D(v["final_tmn_net_of_exit_costs"]) == eq.equity_tmn_net.quantize(D("0.01"))
    assert D(v["final_tmn_net_of_exit_costs"]) < D(v["final_tmn_at_mid"]) < INITIAL   # costs are real
    assert D(v["final_usdt_equivalent"]) > 0 and D(v["initial_usdt_equivalent"]) > 0
    pnl = rep["pnl"]
    assert D(pnl["abs_tmn"]) < 0 and D(pnl["pct"]) < 0 and D(pnl["multiple"]) < 1
    assert pnl["target_reached"] is False and "2x NOT reached" in pnl["target_statement"]

    ec = rep["equity_curve"]
    final = eq.equity_tmn_net
    expected_dd = (D("5100000") - min(D("4900000"), final)) / D("5100000") * 100
    assert D(ec["max_drawdown_pct"]) == expected_dd.quantize(D("0.0001"))
    assert ec["points"] == 4 and ec["includes_final_valuation"] is True

    rt = rep["round_trips"]
    assert rt["count"] == 1 and rt["losses"] == 1
    d1 = next(d for d in rt["decisions"] if d["decision_id"] == "d-1")
    assert d1["strategy"] == "trend_follow" and d1["hypothesis"]["expected_move_bps"] == 80
    assert d1["outcome"]["result"] == "loss" and d1["reflection"]["error_bps"] == -175
    assert [d["decision_id"] for d in rt["decisions"]] == ["d-1", "d-3"]   # HOLDs are counted, not listed
    assert rep["decisions_by_action"] == {"BUY_BTC": 1, "HOLD": 1, "SELL_BTC": 1}
    assert rep["hold_decisions_scored"] == 1
    assert rep["tool_usage"] == {"source": "tool_call events", "counts": {"read_book": 1, "trend_signal": 1}}
    assert rep["memory"]["posteriors"]["chop|trend_follow"] == {"a": 1, "b": 2}
    assert rep["end"]["final_policy"] == "report_only" and "nothing was sold" in rep["end"]["final_policy_applied"]
    assert set(rep["end"]["books"]) == {BTC, USD} and rep["stop_reason"] == "deadline"

    cav = " ".join(rep["honest_caveats"])
    for needle in ("not predictive", "NOT guaranteed", "SIMULATED", "UNVERIFIED", "client_id_echo",
                   "Fee sources", "ASSUMED rate", "SYNTHETIC", "demo run"):
        assert needle in cav, needle

    out = write_final_report(tmp_path / "final_report.json", rep, secrets=[SENTINEL])
    assert json.loads(out.read_text(encoding="utf-8")) == rep
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_report_live_mode_and_unknown_fee(tmp_path):
    w = run_scenario(tmp_path)
    rep = report_for(w, mode=Mode.LIVE, fees={BTC: FEES[BTC]}, synthetic_data=False)
    assert "paper_no_real_orders" not in rep["labels"] and "assumed_fee" not in rep["labels"]
    assert "valuation_incomplete" in rep["labels"]
    assert rep["pnl"]["target_reached"] is None and "UNKNOWN" in rep["pnl"]["target_statement"]
    assert rep["value"]["final_tmn_net_of_exit_costs"] is None
    assert any("unknown_fee:USDTTMN" in c for c in rep["honest_caveats"])
    assert any("This run was LIVE" in c for c in rep["honest_caveats"])


def test_report_flags_flatten_policy_with_btc_left(tmp_path):
    w = make_world(tmp_path, final_policy="flatten_to_tmn")
    assert w.engine.convert_tmn_to_usdt().status == "done"
    assert w.engine.submit(w.entry("0.0002")).status == "filled"
    rep = report_for(w, caveats=None)
    assert "BTC still held" in rep["end"]["final_policy_applied"]
    assert rep["tool_usage"] == {"source": "decision.tool_calls", "counts": {}}


# --------------------------------------------------------------------------- trace
def trace_log(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p, run_id="r", clock=SimClock(1_700_000_000.0))
    log.write("reconcile_startup", {"ok": True, "state": "READY"})
    log.write("observe", {"cycle": 1, "prices": {"BTCUSDT": "81000/81010"}, "regime": "trend_up"})
    log.write("tool_call", {"name": "read_book", "ok": True, "latency_ms": 3})
    log.write("decision", {"decision_id": "d1", "action": "BUY_BTC", "confidence": 0.6,
                           "estimated_net_edge": D("0.004"), "rationale": "momentum after breakout",
                           "hypothesis": {"name": "trend_follow", "direction": "up", "expected_move_bps": 80}})
    log.write("risk_verdict", {"decision_id": "d1", "approved": True, "reasons": []})
    log.write("order_sending", {"client_id": "wa-1-1-abc", "symbol": "BTCUSDT", "side": "BUY",
                                "price": D("81010"), "quantity": D("0.0002")})
    log.write("fill", {"client_id": "wa-1-1-abc", "symbol": "BTCUSDT", "side": "BUY", "quantity": "0.0002",
                       "price": "81010", "fee": "0.0000007", "fee_asset": "BTC", "fee_source": "exchange"})
    log.write("state_transition", {"from": "READY", "to": "BTC_HELD", "reason": "buy filled"})
    log.write("outcome", {"decision_id": "d1", "result": "win", "pnl_bps": 55})
    log.write("reflection", {"decision_id": "d1", "error_bps": -25, "lesson": "move smaller than forecast"})
    log.write("weird_kind", {"foo": 1})
    log.write("observe", {"cycle": 2})
    log.write("decision", {"decision_id": "d2", "action": "HOLD", "rationale": "no edge after costs"})
    log.write("ALERT", {"level": "critical", "alert": "unknown_outcome", "client_id": "wa-1-2-def"})
    log.close()
    return p


def test_render_trace_structure(tmp_path):
    p = trace_log(tmp_path)
    out = render_trace(p)
    assert out.splitlines()[0] == "Audit trace audit.jsonl: 14 record(s), chain OK"
    assert "=== records before the first observe ===" in out
    assert "=== cycle 1 @ 2023-11-14T22:13:20.000Z (decision d1) ===" in out
    body = out.split("=== cycle 1")[1].split("=== cycle 2")[0].splitlines()[1:]
    stages = [ln.split()[2] for ln in body]
    assert stages == ["OBSERVE", "TOOL", "PLAN", "PLAN", "ACTION", "VERIFY", "VERIFY", "REFLECT",
                      "REFLECT", "OTHER"]
    assert "hypothesis=trend_follow(up, 80bps)" in out and "estimated_net_edge=0.004" in out
    assert "| momentum after breakout" in out
    assert "BUY 0.0002 BTCUSDT @ 81010 fee 0.0000007 BTC (exchange)" in out
    assert "READY -> BTC_HELD: buy filled" in out
    assert "lesson=move smaller than forecast" in out and "foo=1" in out
    alert = [ln for ln in out.splitlines() if "ALERT " in ln and "unknown_outcome" in ln]
    assert len(alert) == 1


def test_render_trace_filters(tmp_path):
    p = trace_log(tmp_path)
    last = render_trace(p, last=1)
    assert "cycle 2" in last and "cycle 1" not in last and "before the first observe" not in last
    one = render_trace(p, decision_id="d1")
    assert "cycle 1" in one and "cycle 2" not in one
    assert "no records mention decision_id d9" in render_trace(p, decision_id="d9")
    assert "cycle 1" not in render_trace(p, decision_id="d")        # exact id match, not substring


def test_render_trace_on_tampered_or_garbled_log(tmp_path):
    p = trace_log(tmp_path)
    ls = lines(p)
    p.write_bytes(b"".join(ls[:3] + [b"%%% not json\n"] + ls[4:]))
    out = render_trace(p)
    assert "chain BROKEN at line 4" in out and "[line 4] UNREADABLE record" in out
    assert "READY -> BTC_HELD" in out                   # the rest still renders
