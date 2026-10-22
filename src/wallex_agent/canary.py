"""Canary: ONE real, tiny, deliberately non-fillable order that settles the API behaviours a GET-only
probe cannot (CONTRACT §1 "unverified").

A LIMIT BUY on USDTTMN at ~50 % of the best bid (tick-rounded down), quantity just above the market's
minNotional, sent through the engine's normal persist-before-send path (purpose ``canary``; kill switch,
rounding, persist-before-send and unknown-outcome handling all apply; only the strategy/edge gates do
not). It is then looked up, found in the open orders, its effect on ``locked`` observed, cancelled, and
looked up again. ``<state>/live_canary.json`` records booleans/strings only (no balances, no key).

If the order fills anyway (a crossing market, a cancel race), the fill goes through the normal fill
path into the live ledger, an ``alert`` is written, the result is ``ok: false``, and the operator is
told exactly which holdings resulted.

Interrupts (SIGINT/SIGTERM/SIGBREAK, via ``should_stop``): before the order is sent the canary is
refused and nothing is sent; once it rests, the remaining observations are skipped and it is
cancelled at once. Whatever happens after placement (even an exception), a ``finally`` cancels the
canary order if it is still open (cancel only).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from . import live_gate
from .errors import OrderNotFound, WallexAgentError
from .exchange.probe import CANARY_VERIFIABLE
from .reporting import Scrubber
from .types import BotState, OrderLifecycle as L, Side, TradeIntent, plain, quantize_up

PRICE_FRACTION = Decimal("0.5")          # of the best bid: far from any fill
NOTIONAL_MARGIN = Decimal("1.02")        # just above minNotional
MIN_BALANCE_FACTOR = Decimal("1.3")      # spendable quote needed, as a multiple of minNotional
LOCKED_TOLERANCE = Decimal("0.02")       # relative tolerance when classifying balance deltas


class CanaryRefused(WallexAgentError):
    """A precondition of the canary is not met; nothing was sent."""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def id_pattern(alias: str) -> str:
    """Shape of an exchange order id without its value: hex/digit runs -> ``<hexN>`` / ``<digitsN>``."""
    out = re.sub(r"[0-9a-fA-F]{6,}", lambda m: f"<hex{len(m.group(0))}>", alias)
    return re.sub(r"\d{3,}", lambda m: f"<digits{len(m.group(0))}>", out)[:80]


def _near(x: Decimal, target: Decimal) -> bool:
    return abs(x - target) <= abs(target) * LOCKED_TOLERANCE


def _lookup(ex: Any, oid: str) -> tuple[bool | None, Any, str | None]:
    try:
        return True, ex.get_order(oid), None
    except OrderNotFound:
        return False, None, "OrderNotFound"
    except Exception as exc:  # noqa: BLE001 - an error is an observation, not a verdict
        return None, None, type(exc).__name__


def _ensure_cancelled(rt: Any, client_id: str) -> None:
    """Safety net: the canary order must never be left resting (cancel only, best effort)."""
    try:
        rec = rt.store.get_order(client_id)
        if rec is not None and rec.lifecycle in (L.OPEN, L.PARTIAL):
            rec = rt.engine.cancel_and_verify(client_id)
            if rec.lifecycle in (L.OPEN, L.PARTIAL):
                rt.audit.write("alert", {"alert": "canary_still_open", "level": "critical", "client_id": client_id,
                                         "reason": "cancel could not be verified: check the Wallex app"})
    except Exception as exc:  # noqa: BLE001 - reported; the caller's error (if any) propagates
        try:
            rt.audit.write("alert", {"alert": "canary_cancel_failed", "level": "critical", "client_id": client_id,
                                     "error": type(exc).__name__})
        except Exception:  # noqa: BLE001
            pass


def run_canary(rt: Any, *, now: float | None = None,
               should_stop: Callable[[], str | None] | None = None) -> dict[str, Any]:
    """Place, observe and cancel the canary order through ``rt.engine``; write ``live_canary.json``.
    Raises ``CanaryRefused`` (nothing sent) when a precondition fails or a stop was requested before
    sending. ``should_stop()`` returns the reason of a pending interrupt (signal), or None."""
    stop = should_stop or (lambda: None)
    cfg, ex, eng, store = rt.config, rt.exchange, rt.engine, rt.store
    usd = cfg.usdt_symbol
    audit = rt.audit
    if eng.kill_requested():
        raise CanaryRefused("KILL switch present: no order may be sent")
    if rt.sm.state not in (BotState.UNFUNDED, BotState.READY):
        raise CanaryRefused(f"bot state is {rt.sm.state.value}: the canary needs UNFUNDED or READY")
    if store.unresolved_orders():
        raise CanaryRefused("an order of this bot is unresolved: resolve it first (status / live --resume)")
    rules = ex.get_markets()
    ur = rules.get(usd)
    if ur is None:
        raise CanaryRefused(f"market rules for {usd} unknown")
    book = ex.get_order_book(usd)
    bals = ex.get_balances()
    qb = bals.get(ur.quote)
    need = ur.min_notional * MIN_BALANCE_FACTOR
    if qb is None or qb.spendable < need:
        raise CanaryRefused(f"spendable {ur.quote} is below {plain(need)} ({MIN_BALANCE_FACTOR} x {usd} "
                            f"minNotional {plain(ur.min_notional)})")
    price = ur.round_price(book.best_bid * PRICE_FRACTION, Side.BUY)
    if price <= 0:
        raise CanaryRefused(f"{usd} best bid too low to price a canary")
    qty = max(quantize_up(ur.min_notional * NOTIONAL_MARGIN / price, ur.qty_decimals), ur.min_qty)
    if ur.violations(price, qty):
        raise CanaryRefused(f"canary order would violate {usd} rules: {ur.violations(price, qty)}")
    intent = TradeIntent(purpose="canary", side=Side.BUY, symbol=usd, quantity=qty, reference_price=price,
                         max_slippage_bps=Decimal(0), style="passive_limit",
                         note="canary: non-fillable API probe, cancelled right after placement")
    audit.write("canary", {"stage": "plan", "symbol": usd, "price_fraction_of_bid": PRICE_FRACTION,
                           "price": price, "qty": qty})
    pre = (qb.value, qb.locked)
    why = stop()
    if why:
        raise CanaryRefused(f"interrupted ({why}) before sending: nothing was sent")

    res = eng.submit(intent, decision_id="canary")
    obs: dict[str, Any] = {"submit_status": res.status, "submit_reason": res.reason[:200]}
    reasons: list[str] = []
    verified: list[str] = []
    rec = store.get_order(res.order_ids[-1]) if res.order_ids else None
    if rec is None or rec.lifecycle in (L.REJECTED, L.ABANDONED, L.UNKNOWN_OUTCOME, L.SENDING):
        reasons.append(f"order not placed/confirmed: submit status {res.status}"
                       + (f", lifecycle {rec.lifecycle.value}" if rec else ""))
        obs["post_outcome"] = {"declined": "not_sent", "blocked": "not_sent", "rejected": "rejected",
                               "abandoned": "unknown_then_not_found", "unknown": "unknown_unresolved",
                               "stopped": "unknown_contradiction"}.get(res.status, res.status)
        return _finish(rt, ok=False, obs=obs, verified=verified, reasons=reasons, now=now)
    try:
        return _observe_and_cancel(rt, rec, ur, obs, reasons, verified, pre, stop, now)
    finally:
        _ensure_cancelled(rt, rec.client_id)      # never leave the canary resting (also on errors)


def _observe_and_cancel(rt: Any, rec: Any, ur: Any, obs: dict[str, Any], reasons: list[str],
                        verified: list[str], pre: tuple[Decimal, Decimal], stop: Callable[[], str | None],
                        now: float | None) -> dict[str, Any]:
    ex, eng, store = rt.exchange, rt.engine, rt.store
    usd = rt.config.usdt_symbol
    rec_cid = rec.client_id
    why = stop()
    if why:   # interrupted right after placement: skip every observation, cancel at once
        reasons.append(f"interrupted ({why}) after placement: observations skipped, order cancelled at once")
        obs["interrupted"] = why
        if rec.lifecycle in (L.OPEN, L.PARTIAL):
            rec = eng.cancel_and_verify(rec.client_id)
        obs["final_lifecycle"] = rec.lifecycle.value
        if rec.lifecycle in (L.OPEN, L.PARTIAL):
            reasons.append("cancel could not be verified: the order may still be open (check `status`)")
        return _finish(rt, ok=False, obs=obs, verified=verified, reasons=reasons, now=now)
    unknown_first = "POST outcome unknown" in (rec.note or "")
    obs["post_outcome"] = "unknown_then_adopted" if unknown_first else "acknowledged"
    obs["post_ack_parsed"] = not unknown_first
    post_rec = rt.recorder.last("POST", "/v1/account/orders") if rt.recorder is not None else None
    if post_rec is not None:
        obs["post_http_status"] = post_rec["status"]
        obs["post_json_keys"] = post_rec["json_keys"]
        obs["post_result_keys"] = post_rec["result_keys"]
    else:
        obs["post_result_fields"] = sorted((rec.last_raw or {}).keys())
        obs["post_shape_source"] = "parsed_order_info"
    if not unknown_first:
        verified.append("post_order_response_shape")
    else:
        reasons.append("POST answer could not be interpreted (unknown outcome, adopted later)")

    alias = rec.exchange_client_order_id
    obs["client_id_echo"] = alias == rec.client_id if alias else None
    if alias and alias != rec.client_id:
        obs["exchange_id_pattern"] = id_pattern(alias)
    if alias:
        verified.append("client_id_echo")

    found_ours, _, err_ours = _lookup(ex, rec.client_id)
    obs["get_order_by_our_client_id"] = found_ours
    if err_ours and found_ours is None:
        obs["get_order_by_our_client_id_error"] = err_ours
    if alias and alias != rec.client_id:
        found_alias, _, _ = _lookup(ex, alias)
        obs["get_order_by_exchange_id"] = found_alias
    if found_ours is not None:
        verified.append("get_order_by_client_id")
    try:
        opens = ex.list_open_orders(usd)
        obs["in_open_orders"] = any(o.client_order_id in (rec.client_id, alias) for o in opens)
    except Exception as exc:  # noqa: BLE001
        obs["in_open_orders"] = None
        obs["open_orders_error"] = type(exc).__name__

    # locked-balance semantics, from deltas around the placement (verdict only, no amounts)
    rec = store.get_order(rec.client_id)
    why = stop()
    if why:
        reasons.append(f"interrupted ({why}): remaining observations skipped, order cancelled at once")
        obs["interrupted"] = why
    elif rec.lifecycle is L.OPEN and rec.executed_qty == 0:
        try:
            post = ex.get_balances().get(ur.quote)
            dv, dl = post.value - pre[0], post.locked - pre[1]
            notional = rec.price * rec.quantity
            locked_up = _near(dl, notional)
            obs["locked_increased_by_order_notional"] = locked_up
            obs["value_decreased_by_order_notional"] = _near(-dv, notional)
            obs["value_unchanged"] = abs(dv) <= notional * LOCKED_TOLERANCE
            verdict = ("value_includes_locked" if locked_up and obs["value_unchanged"] else
                       "value_excludes_locked" if locked_up and obs["value_decreased_by_order_notional"] else
                       "inconclusive")
            obs["balance_locked_semantics"] = verdict
            if verdict != "inconclusive":
                verified.append("balance_value_includes_locked")
        except Exception as exc:  # noqa: BLE001
            obs["balance_locked_semantics"] = f"unavailable ({type(exc).__name__})"

    # cancel (cancel only) and verify through the engine
    if rec.lifecycle in (L.OPEN, L.PARTIAL):
        rec = eng.cancel_and_verify(rec.client_id)
        obs["cancel_error"] = eng.last_cancel_error
        del_rec = rt.recorder.last("DELETE", "/v1/account/orders") if rt.recorder is not None else None
        if del_rec is not None:
            obs["cancel_http_status"] = del_rec["status"]
            obs["cancel_body_bytes"] = del_rec["bytes"]
            obs["cancel_json_keys"] = del_rec["json_keys"]
    oid = rec.exchange_client_order_id or rec.client_id
    found, info, err = _lookup(ex, oid)
    if info is not None:
        obs["final_status_raw"] = info.raw_status or info.status.value
        obs["final_active"] = info.active
    else:
        obs["final_lookup_error"] = err
    obs["final_lifecycle"] = rec.lifecycle.value
    if rec.lifecycle is L.CANCELED and rec.executed_qty == 0 and info is not None:
        verified.append("cancel_response")
        if info.active is False:
            verified.append("terminal_active_flag")
    elif rec.lifecycle in (L.OPEN, L.PARTIAL):
        reasons.append("cancel could not be verified: the order may still be open (check `status`)")

    if rec.executed_qty > 0:
        held = rt.portfolio.expected_balances()
        obs["unexpected_fill"] = True
        reasons.append("the canary order FILLED (fully or partly): see the alert and holdings")
        rt.audit.write("alert", {"alert": "canary_filled", "level": "critical", "client_id": rec_cid,
                                 "executed_qty": rec.executed_qty, "lifecycle": rec.lifecycle,
                                 "ledger_after": held})
        obs["holdings_note"] = "ledger updated through the normal fill path; run `status` for holdings"
        rt.extras["canary_holdings"] = {a: plain(v) for a, v in held.items() if v}
    else:
        obs["unexpected_fill"] = False
    ok = not reasons and "post_order_response_shape" in verified and "cancel_response" in verified
    return _finish(rt, ok=ok, obs=obs, verified=verified, reasons=reasons, now=now)


def _finish(rt: Any, *, ok: bool, obs: dict[str, Any], verified: list[str], reasons: list[str],
            now: float | None) -> dict[str, Any]:
    t = float(rt.clock.now() if now is None else now)
    doc = {"ok": bool(ok), "at": _iso(t), "base_url": rt.config.api_base,
           "observations": obs, "verified": sorted(v for v in set(verified) if v in CANARY_VERIFIABLE),
           "reasons": reasons, "source_hash": live_gate.source_hash()}
    rt.audit.write("canary", {"stage": "result", **doc})
    live_gate.atomic_write_json(rt.config.state_path / live_gate.CANARY_FILE, Scrubber(rt.secrets).tree(doc))
    return doc
