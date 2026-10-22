"""Seeded fuzzing of random fault / crash / market sequences over many cycles.

Invariants checked on every cycle: at most one POST per client_id; simulator and ledger balances never
negative; a sell is never sent for more than the ledger AND the exchange say we hold; no order is sent
while another is unresolved. At the end (faults cleared, reconciled) the fill ledger equals the
simulator's balances (or, if some fee had to be estimated, never exceeds them).
"""
import random
from decimal import Decimal as D

import pytest

from wallex_agent.errors import KillSwitchActive
from wallex_agent.execution import SimulatedCrash
from wallex_agent.types import BotState as S, Side

from ._sim_support import BTC, RULES, make_world

STAGES = ["after_intent", "after_sending", "after_post", "after_ack"]
FAULTS = [
    ("post_timeout_after_accept", {}), ("post_timeout_before_accept", {}),
    ("post_5xx", {"accepted": True}), ("post_5xx", {"accepted": False}),
    ("undecodable", {"accepted": True}), ("rate_limit", {}),
    ("partial_fill", {"frac": D("0.3")}), ("partial_fill", {"frac": D("0.7")}),
    ("reject", {"code": 1001}), ("reject", {"code": 1002}), ("reject", {"code": 1004}),
    ("reject", {"code": 1006}), ("reject", {"code": 1010}),
    ("get_order_404", {}), ("get_order_error", {}), ("cancel_race_filled", {}), ("cancel_fail", {}),
    ("stale_book", {"age_s": 120}), ("network_down", {}), ("balances_error", {}), ("trades_error", {}),
]
SEEDS = range(24)
CYCLES = 60


class Violations(AssertionError):
    pass


def guard_sends(w):
    """Wrap the simulator's POST to check the send-time invariants."""
    real = w.sim.create_order

    def guarded(req):
        others = [o for o in w.store.unresolved_orders() if o.client_id != req.client_id]
        if others:
            raise Violations(f"sent {req.client_id} while {others[0].client_id} unresolved")
        if req.side is Side.SELL:
            base = RULES[req.symbol].base
            held = w.portfolio.expected_balances().get(base, D(0))
            if req.quantity > held or req.quantity > w.sim.free(base):
                raise Violations(f"sell {req.quantity} > held {held} / free {w.sim.free(base)}")
        return real(req)

    w.sim.create_order = guarded


def check_cycle(w):
    assert w.sim.max_posts_per_client_id() <= 1, "duplicate POST for a client_id"
    for a, v in w.sim.totals().items():
        assert v >= 0 and w.sim.free(a) >= 0, f"simulator {a} negative"
    for a, v in w.portfolio.expected_balances().items():
        assert v >= 0, f"ledger {a} negative: {v}"


def run(tmp_path, seed):
    rnd = random.Random(seed)
    w = make_world(tmp_path, sim_kw={"echo_client_id": rnd.random() < 0.5})
    guard_sends(w)
    stats = {"crashes": 0, "faults": 0, "submits": 0}
    for _ in range(CYCLES):
        if rnd.random() < 0.5:
            w.source.shift(BTC, D(rnd.randint(-25, 25)), 2)
        if rnd.random() < 0.4:
            kind, kw = rnd.choice(FAULTS)
            w.sim.inject(kind, **kw)
            stats["faults"] += 1
        if rnd.random() < 0.1:
            w.engine.crash_at = {rnd.choice(STAGES)}
        try:
            st = w.sm.state
            style = rnd.choice(["aggressive_limit", "aggressive_limit", "passive_limit"])
            if st is S.READY:
                usdt = w.portfolio.expected_balances()["USDT"]
                frac = D(rnd.choice(["0.2", "0.5", "1", "3"]))
                w.engine.submit(w.entry(qty=str((usdt * frac / D(81000)).quantize(D("1e-8"))), style=style))
                stats["submits"] += 1
            elif st is S.BTC_HELD:
                w.engine.submit(w.exit(style=style))
                stats["submits"] += 1
            elif st in (S.UNFUNDED, S.CONVERTING):
                w.engine.convert_tmn_to_usdt()
            else:
                w.engine.poll_and_manage()
        except SimulatedCrash:
            stats["crashes"] += 1
            w.restart()
            w.engine.reconcile_startup()
        except KillSwitchActive:   # pragma: no cover - never engaged here
            raise
        w.engine.crash_at = set()
        check_cycle(w)
        if w.sm.state is S.STOPPED:
            break
        w.clock.advance(rnd.choice([1, 5, 15, 30]))
        w.engine.poll_and_manage()
        check_cycle(w)
    # settle: no more faults, let timeouts/quarantines play out, then reconcile like a restart
    w.sim.clear_faults()
    for _ in range(4):
        w.clock.advance(60)
        w.engine.poll_and_manage()
    stopped = w.sm.state is S.STOPPED
    if not stopped:
        rep = w.restart().reconcile_startup()
        assert rep["ok"], (seed, rep)
        assert w.store.unresolved_orders() == []
    check_cycle(w)
    exp = w.portfolio.expected_balances()
    estimated = any(f.fee_source == "estimated" for f in w.store.list_fills())
    for a in ("BTC", "USDT", "TMN"):
        if estimated:
            assert exp.get(a, D(0)) <= w.sim.total(a), (seed, a)
        elif not stopped:
            assert exp.get(a, D(0)) == w.sim.total(a), (seed, a, exp, w.sim.totals())
    return stopped, stats, w


@pytest.mark.parametrize("seed", SEEDS)
def test_random_fault_sequences_keep_invariants(tmp_path, seed):
    stopped, stats, w = run(tmp_path, seed)
    assert stats["submits"] > 0
    if stopped:   # a stop must always be explained loudly
        assert any(p.get("alert") == "needs_manual_reconcile" for p in w.audit.of("ALERT"))


def test_fuzz_mostly_completes_without_stopping(tmp_path):
    """Faults that the engine can resolve must not routinely degrade into STOPPED."""
    results = [run(tmp_path / str(s), s) for s in range(100, 110)]
    assert sum(1 for stopped, _, _ in results if not stopped) >= 7
    assert sum(st["crashes"] for _, st, _ in results) > 0
