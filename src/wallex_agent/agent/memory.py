"""Event-sourced agent memory: ``decision``, ``outcome`` and ``reflection`` events in the EventStore.

Nothing is kept only in RAM: bandit posteriors, calibration and lessons change ONLY by applying the
``updates`` carried by ``reflection`` events, so replaying the store on start rebuilds exactly the
same state (restart-safe, auditable).

Bandit: for each (regime, strategy) a Beta(alpha, beta) posterior over "this choice pays off after
costs" (for ``stand_aside``: "abstaining was right"), starting at Beta(1, 1).

Calibration (per strategy, pooled over regimes): least-squares slope through the origin of the
realised move on the RAW predicted move, shrunk toward a prior slope ``k0``::

    k = (sum w*p*r + k0 * W0) / (sum w*p^2 + W0),   W0 = n0 * P0^2
    k = clamp(k, 0, 1)                      # never amplify a model, never invert it
    calibrated_move = k * raw_move

with ``k0 = 1`` (trust the raw model until evidence arrives), ``n0 = 5`` pseudo-observations at
``P0 = 20`` bps. When realised moves are smaller than predicted (or of the wrong sign) ``k`` falls
and future ``expected_move_bps`` shrink toward 0.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..store import dumps
from ..types import Decision

EV_DECISION = "decision"
EV_OUTCOME = "outcome"
EV_REFLECTION = "reflection"

CAL_PRIOR_SLOPE = 1.0
CAL_PRIOR_OBS = 5.0
CAL_PRIOR_SCALE_BPS = 20.0
CF_SPACING_S = 600.0      # at most one scored abstention per 10 minutes (avoid flooding the bandit)


@dataclass
class BetaPosterior:
    alpha: float = 1.0
    beta: float = 1.0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def n(self) -> float:
        """Effective number of observations beyond the Beta(1,1) prior."""
        return self.alpha + self.beta - 2.0

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(self.alpha, self.beta)

    def to_dict(self) -> dict[str, float]:
        return {"alpha": round(self.alpha, 4), "beta": round(self.beta, 4), "mean": round(self.mean, 4),
                "n": round(self.n, 4)}


@dataclass
class Calibration:
    spr: float = 0.0     # sum w * predicted * realised
    spp: float = 0.0     # sum w * predicted^2
    n: float = 0.0
    abs_err: float = 0.0  # sum w * |realised - predicted_raw|

    def slope(self) -> float:
        w0 = CAL_PRIOR_OBS * CAL_PRIOR_SCALE_BPS ** 2
        k = (self.spr + CAL_PRIOR_SLOPE * w0) / (self.spp + w0)
        return max(0.0, min(1.0, k))

    def to_dict(self) -> dict[str, float]:
        return {"slope": round(self.slope(), 4), "n": round(self.n, 4),
                "mean_abs_err_bps": round(self.abs_err / self.n, 2) if self.n else 0.0}


def _norm(payload: Mapping[str, Any]) -> dict[str, Any]:
    """JSON round trip so the live copy equals what a replay will load (lists, str Decimals)."""
    return json.loads(dumps(dict(payload)))


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class AgentMemory:
    """Rebuilds itself from ``store`` on construction (``replay``)."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._reset()
        self.replay()

    # ------------------------------------------------------------------ state
    def _reset(self) -> None:
        self.decisions: dict[str, dict[str, Any]] = {}     # id -> decision event payload
        self.order: list[str] = []
        self.outcomes: dict[str, list[dict[str, Any]]] = {}
        self.reflections: dict[str, list[dict[str, Any]]] = {}
        self._bandit: dict[tuple[str, str], BetaPosterior] = {}
        self._cal: dict[str, Calibration] = {}
        self._cal_regime: dict[tuple[str, str], Calibration] = {}
        self._lessons: list[str] = []
        self._pending_cf: list[str] = []
        self._last_cf_ts: float | None = None
        self._executed: dict[str, float] = {}               # BUY decision id -> filled qty
        self._closed: set[str] = set()
        self.convert_wait_streak = 0
        self.last_event_id = 0

    def replay(self) -> None:
        self._reset()
        for ev in self.store.iter_events():
            if ev.kind in (EV_DECISION, EV_OUTCOME, EV_REFLECTION):
                self._apply_event(ev.kind, ev.payload)
            self.last_event_id = ev.id

    def _apply_event(self, kind: str, payload: Mapping[str, Any]) -> None:
        if kind == EV_DECISION:
            self._on_decision(dict(payload))
        elif kind == EV_OUTCOME:
            self._on_outcome(dict(payload))
        else:
            self._on_reflection(dict(payload))

    def _on_decision(self, p: dict[str, Any]) -> None:
        d = p["decision"]
        did = d["decision_id"]
        self.decisions[did] = p
        self.order.append(did)
        hyp = d.get("hypothesis") or {}
        self.convert_wait_streak = self.convert_wait_streak + 1 if hyp.get("strategy") == "convert_wait" else 0
        cf = hyp.get("counterfactual")
        if cf and (self._last_cf_ts is None or d["ts"] - self._last_cf_ts >= CF_SPACING_S):
            self._pending_cf.append(did)
            self._last_cf_ts = float(d["ts"])

    def _on_outcome(self, p: dict[str, Any]) -> None:
        did = p.get("decision_id", "")
        self.outcomes.setdefault(did, []).append(p)
        if p.get("kind") == "order":
            d = self.decisions.get(did, {}).get("decision", {})
            filled = _f((p.get("result") or {}).get("filled_qty"))
            if d.get("action") == "BUY_BTC" and filled > 0:
                self._executed[did] = self._executed.get(did, 0.0) + filled
        elif p.get("kind") == "round_trip":
            self._closed.add(did)

    def _on_reflection(self, p: dict[str, Any]) -> None:
        did = p.get("decision_id", "")
        self.reflections.setdefault(did, []).append(p)
        if did in self._pending_cf and p.get("kind") == "abstention":
            self._pending_cf.remove(did)
        self.apply_updates(p.get("updates") or [])
        if p.get("lesson"):
            self._lessons.append(str(p["lesson"]))

    def apply_updates(self, updates: Iterable[Mapping[str, Any]]) -> None:
        for u in updates:
            if u.get("kind") == "bandit":
                post = self._bandit.setdefault((str(u["regime"]), str(u["strategy"])), BetaPosterior())
                post.alpha += _f(u.get("success"))
                post.beta += _f(u.get("failure"))
            elif u.get("kind") == "calibration":
                p, r, w = _f(u.get("predicted_bps")), _f(u.get("realized_bps")), _f(u.get("weight"), 1.0)
                for cal in (self._cal.setdefault(str(u["strategy"]), Calibration()),
                            self._cal_regime.setdefault((str(u.get("regime", "?")), str(u["strategy"])),
                                                        Calibration())):
                    cal.spr += w * p * r
                    cal.spp += w * p * p
                    cal.n += w
                    cal.abs_err += w * abs(r - p)

    # ------------------------------------------------------------------ writes
    def record_decision(self, decision: Decision | Mapping[str, Any], *, extra: Mapping[str, Any] | None = None) -> int:
        d = decision.to_dict() if isinstance(decision, Decision) else dict(decision)
        payload = _norm({"decision": d, **dict(extra or {})})
        eid = self.store.append(EV_DECISION, payload, ts=float(d["ts"]))
        self._on_decision(payload)
        self.last_event_id = eid
        return eid

    def record_outcome(self, decision_id: str, result: Mapping[str, Any], *, kind: str = "order",
                       ts: float | None = None, **fields: Any) -> int:
        payload = _norm({"decision_id": decision_id, "kind": kind, "result": dict(result), **fields})
        eid = self.store.append(EV_OUTCOME, payload, ts=ts)
        self._on_outcome(payload)
        self.last_event_id = eid
        return eid

    def record_reflection(self, reflection: Any) -> int:
        payload = _norm(reflection.to_dict() if hasattr(reflection, "to_dict") else dict(reflection))
        eid = self.store.append(EV_REFLECTION, payload, ts=payload.get("ts"))
        self._on_reflection(payload)
        self.last_event_id = eid
        return eid

    # ------------------------------------------------------------------ reads
    @property
    def n_decisions(self) -> int:
        return len(self.order)

    def decision(self, decision_id: str) -> dict[str, Any] | None:
        p = self.decisions.get(decision_id)
        return p["decision"] if p else None

    def iter_decision_payloads(self) -> Iterable[dict[str, Any]]:
        return (self.decisions[i] for i in self.order)

    def posterior(self, regime: str, strategy: str) -> BetaPosterior:
        return self._bandit.get((regime, strategy), BetaPosterior())

    def posteriors(self) -> dict[str, dict[str, dict[str, float]]]:
        out: dict[str, dict[str, dict[str, float]]] = {}
        for (regime, strat), post in sorted(self._bandit.items()):
            out.setdefault(regime, {})[strat] = post.to_dict()
        return out

    def calibration(self, strategy: str) -> Calibration:
        return self._cal.get(strategy, Calibration())

    def calibration_factor(self, strategy: str) -> float:
        return self.calibration(strategy).slope()

    def lessons(self, n: int = 5) -> list[str]:
        return self._lessons[-n:] if n > 0 else []

    def pending_abstentions(self) -> list[dict[str, Any]]:
        return [self.decisions[i]["decision"] for i in self._pending_cf]

    def open_entry(self) -> dict[str, Any] | None:
        """Last BUY decision with a recorded fill whose round trip has not been closed."""
        for did in reversed(self.order):
            if did in self._executed and did not in self._closed:
                return self.decisions[did]["decision"]
        return None

    def executed_entries(self) -> list[str]:
        return [d for d in self.order if d in self._executed]

    def snapshot(self) -> dict[str, Any]:
        return {
            "decisions": self.n_decisions,
            "actions": self._action_counts(),
            "posteriors": self.posteriors(),
            "calibration": {k: v.to_dict() for k, v in sorted(self._cal.items())},
            "calibration_by_regime": {f"{r}/{s}": v.to_dict() for (r, s), v in sorted(self._cal_regime.items())},
            "reflections": sum(len(v) for v in self.reflections.values()),
            "pending_abstention_scores": len(self._pending_cf),
            "open_entry": (self.open_entry() or {}).get("decision_id"),
            "lessons": self.lessons(5),
        }

    def _action_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for did in self.order:
            a = self.decisions[did]["decision"].get("action", "?")
            out[a] = out.get(a, 0) + 1
        return out
