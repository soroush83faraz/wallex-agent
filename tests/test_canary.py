"""Canary: one real (here: simulated) non-fillable order, observed and cancelled; plus the
``canary_ok`` live-gate condition and its effect on the unverified-API list."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from tests._runner_support import SENTINEL, USD, live_like_runtime
from wallex_agent import live_gate as lg
from wallex_agent.canary import CanaryRefused, id_pattern, run_canary
from wallex_agent.clock import SimClock
from wallex_agent.config import Config
from wallex_agent.errors import LiveNotAllowedError
from wallex_agent.exchange.probe import CANARY_VERIFIABLE, UNVERIFIED, unverified_after_canary
from wallex_agent.reporting import read_audit
from wallex_agent.store import OrderRecord
from wallex_agent.types import BotState, Mode, OrderLifecycle, Side

NOW = 1_800_000_000.0
BASE = "https://api.wallex.ir"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canary_file(tmp_path):
    return json.loads((tmp_path / lg.CANARY_FILE).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- branches
def test_happy_path_echo_places_observes_and_cancels(tmp_path):
    rt, sim, _ = live_like_runtime(tmp_path)
    doc = run_canary(rt)
    assert doc["ok"] is True and doc["reasons"] == []
    assert set(doc["verified"]) == set(CANARY_VERIFIABLE)
    o = doc["observations"]
    assert o["post_outcome"] == "acknowledged" and o["client_id_echo"] is True
    assert o["get_order_by_our_client_id"] is True and o["in_open_orders"] is True
    assert o["balance_locked_semantics"] == "value_includes_locked"      # SimExchange semantics
    assert o["final_lifecycle"] == "CANCELED" and o["final_active"] is False and o["unexpected_fill"] is False
    # exactly one order, far below the bid, just above minNotional, now cancelled
    (info,) = sim.all_orders()
    assert info.status.value == "CANCELED" and info.side is Side.BUY and info.symbol == USD
    assert info.price <= sim.book_source.get_book(USD).best_bid / 2
    assert D("50000") <= info.price * info.orig_qty <= D("52000")
    assert sim.max_posts_per_client_id() == 1
    rec = rt.store.list_orders()[0]
    assert rec.purpose == "canary" and rec.lifecycle is OrderLifecycle.CANCELED
    assert rt.sm.state is BotState.UNFUNDED                     # the canary never moves the bot state
    # file on disk: booleans/strings only, no balances
    disk = canary_file(tmp_path)
    assert disk["ok"] is True and disk["base_url"] == BASE and disk["source_hash"] == lg.source_hash()
    assert "5000000" not in json.dumps(disk["observations"])
    kinds = [r["kind"] for r in read_audit(rt.audit.path)]
    assert "canary" in kinds and "order_sending" in kinds


def test_no_echo_records_the_alias_pattern(tmp_path):
    rt, sim, _ = live_like_runtime(tmp_path, sim_kw={"echo_client_id": False})
    doc = run_canary(rt)
    o = doc["observations"]
    assert o["client_id_echo"] is False and o["exchange_id_pattern"] == "LIMIT-<hex16>"
    assert o["get_order_by_our_client_id"] is True and o["get_order_by_exchange_id"] is True
    assert doc["ok"] is True and "client_id_echo" in doc["verified"]


def test_lookup_by_our_id_404_is_recorded_as_a_fact(tmp_path):
    rt, sim, _ = live_like_runtime(tmp_path, sim_kw={"echo_client_id": False, "lookup_by_client_id": False})
    doc = run_canary(rt)
    o = doc["observations"]
    assert o["get_order_by_our_client_id"] is False and o["get_order_by_exchange_id"] is True
    assert "get_order_by_client_id" in doc["verified"]           # behaviour known: lookups need the alias
    assert o["final_lifecycle"] == "CANCELED" and doc["ok"] is True


def test_cancel_race_fill_is_recorded_alerted_and_not_ok(tmp_path):
    rt, sim, _ = live_like_runtime(tmp_path)
    sim.inject("cancel_race_filled")
    doc = run_canary(rt)
    assert doc["ok"] is False and doc["observations"]["unexpected_fill"] is True
    assert any("FILLED" in r for r in doc["reasons"])
    led = rt.portfolio.expected_balances()
    assert led["USDT"] > 0 and led["USDT"] == sim.total("USDT")    # normal fill path, ledger == exchange
    assert rt.extras["canary_holdings"]["USDT"]
    alerts = [r["payload"] for r in read_audit(rt.audit.path) if r["kind"] == "alert"]
    assert any(a.get("alert") == "canary_filled" for a in alerts)
    assert not lg.canary_status(rt.config, tmp_path, rt.clock.now())[0]


def test_post_timeout_after_accept_is_resolved_never_resent(tmp_path):
    rt, sim, _ = live_like_runtime(tmp_path)
    sim.inject("post_timeout_after_accept")
    doc = run_canary(rt)
    o = doc["observations"]
    assert o["post_outcome"] == "unknown_then_adopted" and o["post_ack_parsed"] is False
    assert "post_order_response_shape" not in doc["verified"] and doc["ok"] is False
    assert o["final_lifecycle"] == "CANCELED"
    assert sim.max_posts_per_client_id() == 1 and sim.order_count() == 1


def test_post_timeout_before_accept_abandons_and_quarantines(tmp_path):
    rt, sim, _ = live_like_runtime(tmp_path)
    sim.inject("post_timeout_before_accept")
    doc = run_canary(rt)
    assert doc["ok"] is False and doc["observations"]["post_outcome"] == "unknown_then_not_found"
    assert sim.order_count() == 0 and sim.max_posts_per_client_id() == 1
    assert (USD, Side.BUY) in rt.engine.quarantined()


@pytest.mark.parametrize("case", ["balance", "kill", "unresolved", "state"])
def test_refusals_send_nothing(tmp_path, case):
    bal = {"TMN": D("60000"), "USDT": D(0), "BTC": D(0)} if case == "balance" else None
    rt, sim, _ = live_like_runtime(tmp_path, balances=bal, initial_tmn=D("60000") if bal else D("5000000"))
    if case == "kill":
        (tmp_path / "KILL").write_text("x", encoding="utf-8")
    if case == "unresolved":                     # an order whose POST outcome is still unknown
        rt.store.insert_order_intent(OrderRecord(client_id="wa-x-1-abc", symbol=USD, side=Side.BUY,
                                                 price=D("272000"), quantity=D("1"), purpose="convert"))
        rt.store.update_order("wa-x-1-abc", lifecycle=OrderLifecycle.SENDING)
    if case == "state":
        rt.sm.transition(BotState.STOPPED, "test")
    posts = sum(sim.post_counts.values())
    with pytest.raises(CanaryRefused):
        run_canary(rt)
    assert sum(sim.post_counts.values()) == posts


def test_interrupt_before_sending_sends_nothing(tmp_path):
    """I5: SIGINT/SIGTERM before the POST => refused, nothing sent."""
    rt, sim, _ = live_like_runtime(tmp_path)
    with pytest.raises(CanaryRefused, match="interrupted"):
        run_canary(rt, should_stop=lambda: "SIGINT")
    assert sum(sim.post_counts.values()) == 0


def test_interrupt_after_placement_cancels_the_resting_order_at_once(tmp_path):
    rt, sim, _ = live_like_runtime(tmp_path)
    doc = run_canary(rt, should_stop=lambda: "SIGTERM" if sim.order_count() else None)
    assert doc["ok"] is False and any("interrupted (SIGTERM)" in r for r in doc["reasons"])
    (info,) = sim.all_orders()
    assert info.active is False and info.status.value == "CANCELED"
    assert rt.store.list_orders()[0].lifecycle is OrderLifecycle.CANCELED
    assert sim.max_posts_per_client_id() == 1


def test_an_exception_after_placement_still_cancels_the_canary(tmp_path):
    """I5: whatever escapes after the order rests, a finally cancels it (cancel only)."""
    rt, sim, _ = live_like_runtime(tmp_path)
    real = sim.get_balances
    calls = {"n": 0}

    def balances_then_interrupt():
        calls["n"] += 1
        if sim.order_count():                     # the locked-balance observation after placement
            raise KeyboardInterrupt
        return real()

    sim.get_balances = balances_then_interrupt
    with pytest.raises(KeyboardInterrupt):
        run_canary(rt)
    sim.get_balances = real
    (info,) = sim.all_orders()
    assert info.active is False and rt.store.list_orders()[0].lifecycle is OrderLifecycle.CANCELED


def test_cli_canary_routes_signals_to_the_canary(monkeypatch, tmp_path, capsys):
    """I5: `canary` installs the flag-only handlers; a signal during the canary is seen by it."""
    import signal as _signal
    from wallex_agent import canary as canary_mod
    from wallex_agent import cli
    from wallex_agent.modes import live as live_mode
    rt, sim, _ = live_like_runtime(tmp_path)
    monkeypatch.setattr(live_mode, "build_canary_runtime", lambda *a, **k: rt)
    seen = {}

    def fake_run_canary(rt_, *, should_stop=None, now=None):
        before = should_stop()
        _signal.getsignal(_signal.SIGINT)(_signal.SIGINT, None)      # what Ctrl+C does
        seen.update(before=before, after=should_stop())
        raise CanaryRefused(f"interrupted ({should_stop()}) before sending: nothing was sent")

    monkeypatch.setattr(canary_mod, "run_canary", fake_run_canary)
    default = _signal.getsignal(_signal.SIGINT)
    code = cli.main(["--env-file", str(tmp_path / "none.env"), "canary", "--i-understand-real-money"],
                    env={"DATA_DIR": str(tmp_path), "WALLEX_API_KEY": SENTINEL})
    assert seen == {"before": None, "after": "SIGINT"} and code == 130
    assert _signal.getsignal(_signal.SIGINT) is default                   # restored afterwards
    assert SENTINEL not in capsys.readouterr().err


def test_id_pattern_hides_values():
    assert id_pattern("LIMIT-0123456789abcdef") == "LIMIT-<hex16>"
    assert id_pattern("12345678") == "<hex8>"
    assert id_pattern("wa-x") == "wa-x"


# --------------------------------------------------------------------------- live gate: canary_ok
def live_cfg(tmp_path, **kw):
    return replace(Config(mode=Mode.LIVE, enable_live=True, api_key=SENTINEL, data_dir=str(tmp_path)), **kw)


def ok_runner(argv, cwd):
    return 0, f"{lg.MIN_SELFTEST_TESTS + 100} passed in 0.5s\n"


def evidence(tmp_path, *, age_h=1.0):
    lg.run_selftest(tmp_path, runner=ok_runner, clock=SimClock(NOW - age_h * 3600))
    rep = {"ok": True, "get_only": True, "base_url": BASE, "at": iso(NOW - age_h * 3600), "probe_version": 1,
           "checks": [], "undocumented": [{"id": k} for k, _ in UNVERIFIED]}
    lg.write_schema_attestation(tmp_path, rep, BASE)


def write_canary(tmp_path, **kw):
    doc = {"ok": True, "at": iso(NOW - 600), "base_url": BASE, "observations": {},
           "verified": ["client_id_echo", "cancel_response"]}
    doc.update(kw)
    lg.atomic_write_json(tmp_path / lg.CANARY_FILE, doc)


def unmet(exc):
    return {c["name"] for c in exc.value.unmet}


@pytest.mark.parametrize("kw,why", [({"ok": False}, "ok is not true"), ({"at": iso(NOW - 73 * 3600)}, "age"),
                                    ({"base_url": "https://other.test"}, "base_url"), ({"at": "x"}, "timestamp"),
                                    ({"at": iso(NOW + 3600)}, "future")])
def test_canary_condition_is_required_and_checked(tmp_path, kw, why):
    evidence(tmp_path)
    cfg = live_cfg(tmp_path)
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(cfg, flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW)
    assert unmet(ei) == {"canary_ok"} and "canary" in str(ei.value)
    write_canary(tmp_path, **kw)
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(cfg, flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW)
    assert unmet(ei) == {"canary_ok"} and why in str(ei.value)


def test_canary_condition_met_up_to_72h(tmp_path):
    evidence(tmp_path)
    write_canary(tmp_path, at=iso(NOW - 71 * 3600))
    res = lg.check_live_allowed(live_cfg(tmp_path), flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW)
    assert res["allowed"] and any(c["name"] == "canary_ok" and c["met"] for c in res["conditions"])


def test_canary_gate_uses_its_own_phrase_and_skips_the_canary_condition(tmp_path):
    evidence(tmp_path)
    cfg = live_cfg(tmp_path)
    prompts = []
    res = lg.check_live_allowed(cfg, flag_ack=True, typed_phrase=None, now=NOW, require_canary=False,
                                expected_phrase=lg.required_canary_phrase(cfg),
                                input_fn=lambda p: prompts.append(p) or "RUN CANARY 5000000")
    assert res["allowed"] and "RUN CANARY 5000000" in prompts[0]
    with pytest.raises(LiveNotAllowedError) as ei:       # the live phrase does not start a canary
        lg.check_live_allowed(cfg, flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW,
                              require_canary=False, expected_phrase=lg.required_canary_phrase(cfg))
    assert unmet(ei) == {"typed_phrase"}


def test_resume_accepts_48h_evidence_but_never_more(tmp_path):
    evidence(tmp_path, age_h=30)
    write_canary(tmp_path)
    cfg = live_cfg(tmp_path)
    with pytest.raises(LiveNotAllowedError) as ei:
        lg.check_live_allowed(cfg, flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW)
    assert unmet(ei) == {"selftest_fresh", "schema_attestation_fresh"}
    res = lg.check_live_allowed(cfg, flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW,
                                max_evidence_age_s=lg.RESUME_MAX_EVIDENCE_AGE_S)
    assert res["allowed"] and res["max_evidence_age_h"] == 48
    evidence(tmp_path, age_h=50)
    with pytest.raises(LiveNotAllowedError) as ei:       # a larger value is capped at 48 h
        lg.check_live_allowed(cfg, flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW,
                              max_evidence_age_s=10 * 24 * 3600)
    assert unmet(ei) == {"selftest_fresh", "schema_attestation_fresh"}


def test_canary_verified_items_leave_the_unverified_list(tmp_path):
    evidence(tmp_path)
    att = json.loads((tmp_path / lg.ATTESTATION_FILE).read_text(encoding="utf-8"))
    assert "client_id_echo" in att["unverified"] and att["verified_by_canary"] == []
    write_canary(tmp_path, at=iso(NOW - 3600))
    rep = {"ok": True, "get_only": True, "base_url": BASE, "at": iso(NOW - 600), "probe_version": 1,
           "checks": [], "undocumented": [{"id": k} for k, _ in UNVERIFIED]}
    att = lg.write_schema_attestation(tmp_path, rep, BASE)
    assert att["verified_by_canary"] == ["cancel_response", "client_id_echo"]
    assert "client_id_echo" not in att["unverified"] and "partial_fill_status" in att["unverified"]
    st = lg.arm_status(live_cfg(tmp_path), now=NOW)
    assert st["canary_verified_api_items"] == ["cancel_response", "client_id_echo"]
    assert "client_id_echo" not in st["unverified_api_items"]
    left = dict(unverified_after_canary(["client_id_echo", "bogus"]))
    assert "client_id_echo" not in left and "partial_fill_status" in left


def test_a_real_canary_run_satisfies_the_gate(tmp_path):
    rt, sim, clock = live_like_runtime(tmp_path)
    run_canary(rt, now=NOW - 60)
    evidence(tmp_path)
    res = lg.check_live_allowed(live_cfg(tmp_path), flag_ack=True, typed_phrase="START LIVE 5000000", now=NOW)
    assert res["allowed"]
