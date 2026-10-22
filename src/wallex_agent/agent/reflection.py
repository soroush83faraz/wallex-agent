"""Reflection: compare a hypothesis with what happened, write a lesson, and emit the memory updates.

Two kinds:
  * ``round_trip`` — an executed entry closed. success = net return after all fees > 0.
    Updates: bandit(regime at entry, strategy) +1 success/failure; calibration(strategy) with the
    RAW predicted move vs the realised market move over the holding period.
  * ``abstention`` — a HOLD decision, scored after its horizon with later prices: "would buying the
    best candidate at decision time and selling now have paid after costs?" (walked through the
    recorded entry price and today's bids, both taker fees). Updates: stand_aside(regime) success
    when abstaining was right; each long candidate's bandit at half weight (off-policy evidence);
    calibration for every candidate with a non-zero prediction.

Also builds the printable observe -> plan -> tool -> action -> verify -> reflect trace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

OFF_POLICY_WEIGHT = 0.5


@dataclass
class Reflection:
    decision_id: str
    kind: str                       # 'round_trip' | 'abstention'
    ts: float
    hypothesis: dict[str, Any]
    result: dict[str, Any]
    error_bps: float
    lesson: str
    updates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"decision_id": self.decision_id, "kind": self.kind, "ts": self.ts,
                "hypothesis": self.hypothesis, "result": self.result, "error_bps": round(self.error_bps, 2),
                "lesson": self.lesson, "updates": self.updates}


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def reflect(decision: Mapping[str, Any], outcome: Mapping[str, Any]) -> Reflection:
    """``decision``: a ``Decision.to_dict()``. ``outcome``: ``{"kind": "round_trip", "ts",
    "realized_move_bps", "net_return_bps", "pnl_usdt", "exit_reason"}`` or ``{"kind": "abstention",
    "ts", "realized_move_bps", "candidates": [{"strategy", "cf_net_bps"}...], "best_cf_net_bps"}``."""
    hyp = dict(decision.get("hypothesis") or {})
    kind = str(outcome["kind"])
    ts = _f(outcome.get("ts"))
    realized = _f(outcome.get("realized_move_bps"))
    did = str(decision["decision_id"])
    if kind == "round_trip":
        strat = str(hyp.get("strategy", "unknown"))
        regime = str(hyp.get("regime", "unknown"))
        predicted = _f(hyp.get("expected_move_bps"))
        raw = _f(hyp.get("raw_move_bps")) or predicted
        net = _f(outcome.get("net_return_bps"))
        won = net > 0
        err = realized - predicted
        lesson = (f"{strat} in {regime}: predicted {predicted:+.1f} bps over {int(_f(hyp.get('horizon_s')))}s, "
                  f"market moved {realized:+.1f} bps (error {err:+.1f}); net after fees {net:+.1f} bps -> "
                  f"{'paid' if won else 'did not pay'}"
                  + (f" (exit: {outcome.get('exit_reason')})" if outcome.get("exit_reason") else "")
                  + ("; calibration shrinks" if abs(realized) < abs(predicted) or realized * predicted < 0
                     else "; prediction held up"))
        updates = [{"kind": "bandit", "regime": regime, "strategy": strat,
                    "success": 1.0 if won else 0.0, "failure": 0.0 if won else 1.0},
                   {"kind": "calibration", "strategy": strat, "regime": regime, "predicted_bps": raw,
                    "realized_bps": realized, "weight": 1.0}]
        return Reflection(did, kind, ts, hyp, dict(outcome), err, lesson, updates)

    if kind != "abstention":
        raise ValueError(f"unknown outcome kind {kind!r}")
    cf = dict(hyp.get("counterfactual") or {})
    regime = str(cf.get("regime") or hyp.get("regime", "unknown"))
    cands: Sequence[Mapping[str, Any]] = cf.get("candidates") or []
    results = {str(c["strategy"]): _f(c.get("cf_net_bps")) for c in outcome.get("candidates") or []}
    best = str(cf.get("best") or "")
    best_net = _f(outcome.get("best_cf_net_bps"))
    abstain_right = best_net <= 0
    updates: list[dict[str, Any]] = [{"kind": "bandit", "regime": regime, "strategy": "stand_aside",
                                      "success": 1.0 if abstain_right else 0.0,
                                      "failure": 0.0 if abstain_right else 1.0}]
    errs = []
    for c in cands:
        s = str(c["strategy"])
        raw = _f(c.get("raw_move_bps"))
        if raw != 0:
            updates.append({"kind": "calibration", "strategy": s, "regime": regime, "predicted_bps": raw,
                            "realized_bps": realized, "weight": 1.0})
            errs.append(realized - _f(c.get("expected_move_bps")))
        if c.get("direction") == "up" and s in results:
            paid = results[s] > 0
            updates.append({"kind": "bandit", "regime": regime, "strategy": s,
                            "success": OFF_POLICY_WEIGHT if paid else 0.0,
                            "failure": 0.0 if paid else OFF_POLICY_WEIGHT})
    err = errs[0] if errs else realized
    lesson = (f"stood aside in {regime} (best candidate {best or 'none'}); over {int(_f(cf.get('horizon_s')))}s "
              f"the market moved {realized:+.1f} bps; buying would have returned {best_net:+.1f} bps after "
              f"costs -> abstaining was {'right' if abstain_right else 'wrong'}")
    return Reflection(did, kind, ts, hyp, dict(outcome), err, lesson, updates)


# --------------------------------------------------------------------------- trace
def trace_record(decision: Mapping[str, Any], outcomes: Sequence[Mapping[str, Any]] = (),
                 reflections: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Structured ``observe -> plan -> tool -> action -> verify -> reflect`` record of one decision."""
    hyp = decision.get("hypothesis") or {}
    sf = decision.get("state_features") or {}
    return {
        "decision_id": decision.get("decision_id"), "cycle": decision.get("cycle"), "ts": decision.get("ts"),
        "observe": {"prices": decision.get("observed_prices", {}),
                    "features": {k: sf.get(k) for k in ("state", "regime", "n_points", "vol_1m_bps", "ret_60m_bps",
                                                        "zscore_60m", "imbalance", "spread_bps", "fx_spread_bps",
                                                        "time_left_frac") if k in sf}},
        "plan": {"hypothesis": {k: hyp.get(k) for k in ("strategy", "direction", "expected_move_bps",
                                                         "horizon_s", "invalidation") if k in hyp},
                 "alternatives": decision.get("alternatives", [])},
        "tool": {"calls": decision.get("tool_calls", []), "chosen_tool": decision.get("chosen_tool")},
        "action": {"action": decision.get("action"), "confidence": decision.get("confidence"),
                   "estimated_net_edge": decision.get("estimated_net_edge"), "intent": decision.get("intent"),
                   "rationale": decision.get("rationale")},
        "verify": {"risk_verdict": decision.get("risk_verdict"), "order_ids": decision.get("order_ids", []),
                   "outcomes": list(outcomes)},
        "reflect": [{"kind": r.get("kind"), "error_bps": r.get("error_bps"), "lesson": r.get("lesson")}
                    for r in reflections],
    }


def render_trace(record: Mapping[str, Any]) -> str:
    """Human-readable trace of ``trace_record`` output."""
    obs, plan, tool, act, ver = (record[k] for k in ("observe", "plan", "tool", "action", "verify"))
    lines = [f"=== decision {record['decision_id']} (cycle {record['cycle']}, t={record['ts']}) ==="]
    px = ", ".join(f"{k}={v}" for k, v in obs["prices"].items())
    feats = ", ".join(f"{k}={round(v, 3) if isinstance(v, float) else v}" for k, v in obs["features"].items())
    lines.append(f"OBSERVE  {px}")
    lines.append(f"         {feats}")
    h = plan["hypothesis"]
    lines.append(f"PLAN     {h.get('strategy')} {h.get('direction', '')} move {h.get('expected_move_bps')} bps "
                 f"/ {h.get('horizon_s')}s; invalidation {h.get('invalidation')}")
    for a in plan["alternatives"]:
        lines.append(f"         alt {a.get('strategy')}: net {a.get('net_edge_bps')} bps conf "
                     f"{a.get('confidence')} -> {a.get('rejected_because') or 'chosen'}")
    for c in tool["calls"]:
        if c.get("called"):
            lines.append(f"TOOL     {c['tool']} [{'ok' if c.get('ok') else 'FAIL'} {c.get('latency_ms')}ms] "
                         f"{c.get('summary') or c.get('error')}  <- {c.get('why')}")
        else:
            lines.append(f"TOOL     (skipped {c['tool']})  <- {c.get('why')}")
    lines.append(f"ACTION   {act['action']} conf={act['confidence']} edge={act['estimated_net_edge']} "
                 f"(decisive tool: {tool.get('chosen_tool')})")
    lines.append(f"         {act['rationale']}")
    if act.get("intent"):
        i = act["intent"]
        lines.append(f"         intent {i.get('purpose')} {i.get('side')} {i.get('quantity')} {i.get('symbol')} "
                     f"@ref {i.get('reference_price')} slip<= {i.get('max_slippage_bps')} bps")
    if ver.get("risk_verdict") is not None:
        lines.append(f"VERIFY   risk {ver['risk_verdict']}")
    for o in ver["outcomes"]:
        lines.append(f"VERIFY   {o.get('kind')}: {o.get('result')} {o.get('verify', '')}".rstrip())
    if not ver["outcomes"] and ver.get("risk_verdict") is None:
        lines.append("VERIFY   (no order sent)")
    for r in record["reflect"]:
        lines.append(f"REFLECT  [{r['kind']}] {r['lesson']}")
    if not record["reflect"]:
        lines.append("REFLECT  (pending: scored after the hypothesis horizon / round trip close)")
    return "\n".join(lines)
