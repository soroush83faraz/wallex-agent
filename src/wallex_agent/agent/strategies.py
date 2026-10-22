"""Hypothesis generators. Each turns features into a falsifiable ``Hypothesis`` (direction, calibrated
expected move, horizon, invalidation). They never trade; the planner prices them against costs.

Raw move models are deliberately simple, hand-set priors (NOT fitted to any data):
  * trend_follow   : half of the last L-minute move persists over the next L minutes (L = 60, or
                     30 while history is short); requires efficiency >= 0.2 and |t| >= 1.
  * mean_reversion : half of the deviation from the 60-min mean reverts within 30 minutes; needs
                     |z| >= 2.
  * book_pressure  : 0.5 * imbalance * vol * sqrt(5) over 5 minutes; needs |imbalance| >= 0.3.
  * stand_aside    : no position, no cost, zero edge. The default.
Each raw move is multiplied by memory's calibration factor (see ``memory`` for the formula).

Confidence (explicit formula, see ``confidence``): a blend of a model probability from signal
strength and the empirical hit-rate of this strategy in this regime::

    p_model = Phi((calibrated_move - cost_bps) / sigma_h)   sigma_h = max(vol_1m, 1) * sqrt(horizon_min)
    h       = Beta posterior mean for (regime, strategy), n = its effective observation count
    confidence = (N0 * p_model + n * h) / (N0 + n)          N0 = 4

``cost_bps`` is 0 until the planner has priced the trade, so the first value is a directional belief
and the planner replaces it with the after-cost belief.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

CONF_PRIOR_OBS = 4.0
STRATEGIES = ("trend_follow", "mean_reversion", "book_pressure", "stand_aside")
DEFAULT_HORIZON_S = 1800.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def confidence(calibrated_move_bps: float, cost_bps: float, sigma_h_bps: float,
               hit_mean: float, hit_n: float) -> float:
    """Documented confidence formula (module docstring). Long-only: a 'down' move gives p_model of
    the long trade, i.e. small."""
    p_model = norm_cdf((calibrated_move_bps - cost_bps) / max(sigma_h_bps, 1e-9))
    n = max(0.0, hit_n)
    return (CONF_PRIOR_OBS * p_model + n * hit_mean) / (CONF_PRIOR_OBS + n)


@dataclass(frozen=True)
class Hypothesis:
    name: str
    direction: str                 # 'up' | 'down' | 'flat'
    expected_move_bps: float       # calibrated, signed
    horizon_s: float
    confidence: float
    rationale: str
    invalidation: dict[str, Any] = field(default_factory=dict)
    raw_move_bps: float = 0.0
    calibration_factor: float = 1.0
    signal_strength: float = 0.0   # |t|-like statistic
    sigma_h_bps: float = 0.0
    regime: str = "unknown"

    def with_cost(self, cost_bps: float, hit_mean: float, hit_n: float) -> "Hypothesis":
        move = self.expected_move_bps if self.direction == "up" else -abs(self.expected_move_bps)
        return replace(self, confidence=confidence(move, cost_bps, self.sigma_h_bps, hit_mean, hit_n))

    def to_dict(self) -> dict[str, Any]:
        return {"strategy": self.name, "direction": self.direction,
                "expected_move_bps": round(self.expected_move_bps, 2), "raw_move_bps": round(self.raw_move_bps, 2),
                "calibration_factor": round(self.calibration_factor, 4), "horizon_s": self.horizon_s,
                "confidence": round(self.confidence, 4), "signal_strength": round(self.signal_strength, 3),
                "sigma_h_bps": round(self.sigma_h_bps, 2), "regime": self.regime,
                "rationale": self.rationale, "invalidation": dict(self.invalidation)}


def _fl(f: Mapping[str, Any], k: str, default: float = 0.0) -> float:
    v = f.get(k, default)
    return float(v) if isinstance(v, (int, float)) else default


def _make(name: str, f: Mapping[str, Any], memory: Any, raw: float, horizon_s: float, strength: float,
          rationale: str, invalidation: dict[str, Any]) -> Hypothesis:
    regime = str(f.get("regime", "unknown"))
    k = memory.calibration_factor(name) if memory is not None else 1.0
    move = k * raw
    direction = "flat" if raw == 0 else ("up" if move > 0 else "down") if k > 0 else "flat"
    sigma = max(_fl(f, "vol_1m_bps"), 1.0) * math.sqrt(horizon_s / 60.0)
    post = memory.posterior(regime, name) if memory is not None else None
    hm, hn = (post.mean, post.n) if post is not None else (0.5, 0.0)
    conf_move = move if direction == "up" else -abs(move)
    conf = confidence(conf_move, 0.0, sigma, hm, hn) if direction != "flat" else 0.0
    if raw != 0 and k <= 0:
        rationale += "; calibration factor 0 (realised moves have not followed this signal) -> no tradable move"
    return Hypothesis(name, direction, move, horizon_s, conf, rationale, invalidation, raw, k, strength,
                      sigma, regime)


def trend_follow(f: Mapping[str, Any], memory: Any = None) -> Hypothesis:
    n = _fl(f, "n_points")
    lb = 60 if n >= 61 else 30
    r = _fl(f, f"ret_{lb}m_bps")
    vol = max(_fl(f, "vol_1m_bps"), 1e-9)
    t = r / (vol * math.sqrt(lb))
    er = _fl(f, "efficiency_30m")
    mid = _fl(f, "mid")
    sigma = max(vol, 1.0) * math.sqrt(lb)
    if er < 0.2 or abs(t) < 1.0:
        return _make("trend_follow", f, memory, 0.0, lb * 60.0, abs(t),
                     f"no persistent trend: {lb}m return {r:+.1f} bps, t={t:+.2f}, efficiency {er:.2f}",
                     {"note": "no position thesis"})
    raw = 0.5 * r
    sign = 1 if r > 0 else -1
    level = mid * (1 - sign * sigma / 10000.0)
    inv = ({"price_below": round(level, 2)} if sign > 0 else {"price_above": round(level, 2)})
    inv["signal_flip"] = f"{lb}m trend t-stat changes sign"
    return _make("trend_follow", f, memory, raw, lb * 60.0, abs(t),
                 f"{lb}m return {r:+.1f} bps (t={t:+.2f}, efficiency {er:.2f}); assume half persists",
                 inv)


def mean_reversion(f: Mapping[str, Any], memory: Any = None) -> Hypothesis:
    z = _fl(f, "zscore_60m")
    dev = _fl(f, "sma_60m_dev_bps")
    mid = _fl(f, "mid")
    if abs(z) < 2.0:
        return _make("mean_reversion", f, memory, 0.0, 1800.0, abs(z),
                     f"price within 2 sd of its 60m mean (z={z:+.2f}, dev {dev:+.1f} bps)",
                     {"note": "no position thesis"})
    raw = -0.5 * dev
    level = mid * (1 + dev / 10000.0)     # deviation doubling
    inv = {"price_below": round(level, 2)} if dev < 0 else {"price_above": round(level, 2)}
    inv["signal_flip"] = "z-score beyond 3.5 sd (stretch, not reversion)"
    return _make("mean_reversion", f, memory, raw, 1800.0, abs(z),
                 f"stretched {dev:+.1f} bps from 60m mean (z={z:+.2f}); assume half reverts in 30m", inv)


def book_pressure(f: Mapping[str, Any], memory: Any = None) -> Hypothesis:
    imb = _fl(f, "imbalance")
    vol = _fl(f, "vol_1m_bps")
    if abs(imb) < 0.3:
        return _make("book_pressure", f, memory, 0.0, 300.0, abs(imb),
                     f"balanced near-touch book (imbalance {imb:+.2f})", {"note": "no position thesis"})
    raw = 0.5 * imb * max(vol, 1.0) * math.sqrt(5)
    return _make("book_pressure", f, memory, raw, 300.0, abs(imb),
                 f"near-touch depth imbalance {imb:+.2f}: short-lived pressure", {"signal_flip": "imbalance changes sign"})


def stand_aside(f: Mapping[str, Any], memory: Any = None, reason: str = "") -> Hypothesis:
    regime = str(f.get("regime", "unknown"))
    post = memory.posterior(regime, "stand_aside") if memory is not None else None
    return Hypothesis("stand_aside", "flat", 0.0, DEFAULT_HORIZON_S,
                      post.mean if post is not None else 0.5,
                      reason or "no hypothesis clears costs + hurdle with enough confidence",
                      {"condition": "a candidate clears the net-edge hurdle and the confidence gate"},
                      regime=regime)


GENERATORS = {"trend_follow": trend_follow, "mean_reversion": mean_reversion, "book_pressure": book_pressure}
