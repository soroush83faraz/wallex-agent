"""GET-only schema probe of the private Wallex endpoints (input for the ``check-live`` attestation).

The probe works on ``client.as_read_only()``, so a write is impossible (``ForbiddenEndpointError``
before any I/O). The report holds no secrets and no amounts (balances, orders, trades): only shapes,
field names, booleans, counts, symbol/asset names and raw status strings.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from ..errors import ExchangeError, OrderNotFound, RateLimitError, ServerError
from ..types import MarketRules, Side, plain
from .wallex import (DEFAULT_SYMBOLS, WallexClient, _jtype, fee_entries, parse_balances,
                     parse_depth, parse_fees, parse_order, parse_trades)

PROBE_VERSION = 1

DOCUMENTED_FIELDS: dict[str, tuple[str, ...]] = {
    "balance": ("asset", "faName", "fiat", "value", "locked"),
    "fee": ("makerFeeRate", "takerFeeRate", "recent_days_sum"),
    "order": ("symbol", "type", "side", "price", "origQty", "origSum", "executedPrice",
              "executedQty", "executedSum", "executedPercent", "status", "active",
              "clientOrderId", "created_at"),
    "trade": ("symbol", "quantity", "price", "sum", "fee", "feeCoefficient", "feeAsset",
              "isBuyer", "timestamp"),
}

# Facts a GET-only probe cannot establish: they need a real (small) order, observed by a human.
UNVERIFIED: tuple[tuple[str, str], ...] = (
    ("post_order_response_shape",
     "Shape/types of a real POST /v1/account/orders success answer (only the documented sample "
     "is known)."),
    ("client_id_echo",
     "Whether a supplied client_id comes back verbatim as clientOrderId (the doc example shows "
     "'LIMIT-<uuid>')."),
    ("get_order_by_client_id",
     "Whether GET /v1/account/orders/{id} finds an order by OUR client_id (vs a server id)."),
    ("partial_fill_status",
     "Raw status string of an active, partially filled order (undocumented; the client derives "
     "PARTIALLY_FILLED from active + quantities)."),
    ("terminal_active_flag",
     "That FILLED / CANCELED orders report active=false (contradictions map to UNKNOWN)."),
    ("cancel_response",
     "Status/body of a successful DELETE /v1/account/orders and the answer for an order that is "
     "already filled or cancelled."),
    ("reject_http_codes",
     "HTTP status/body of order rejections other than 422 + result.error_code (e.g. 400/409), and "
     "the 429 / Retry-After format."),
    ("balance_value_includes_locked",
     "Whether balances.value includes the locked amount (spendable is computed conservatively as "
     "value - locked)."),
)


class SchemaReport(dict):  # type: ignore[type-arg]
    """JSON-able dict ``{ok, checks: [{name, ok, detail}], undocumented: [...], at, base_url, ...}``
    with attribute shortcuts. ``json.dumps(report)`` works directly."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))

    @property
    def checks(self) -> list[dict[str, Any]]:
        return list(self.get("checks", []))

    @property
    def undocumented(self) -> list[dict[str, Any]]:
        return list(self.get("undocumented", []))

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self, indent=indent, ensure_ascii=True)


def probe_private_schemas(client: WallexClient,
                          symbols: Sequence[str] = DEFAULT_SYMBOLS) -> SchemaReport:
    """Runs GET-only checks (markets, depth, balances, fee, openOrders, trades, order-not-found)
    and reports what the live API actually looks like. Never raises for API failures: each one
    becomes a failed check."""
    ro = client.as_read_only()          # any write => ForbiddenEndpointError, before any I/O
    started = client.clock.now()        # attestation time = start (conservative for freshness)
    syms = tuple(symbols)
    checks: list[dict[str, Any]] = []
    obs: dict[str, Any] = {}
    pending = dict(UNVERIFIED)
    rules: dict[str, MarketRules] = {}

    def run(name: str, fn: Callable[[], tuple[bool, dict[str, Any]]]) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - every failure is reported, never raised
            ok, detail = False, _describe_error(exc, client)
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def markets() -> tuple[bool, dict[str, Any]]:
        rules.update(ro.get_markets())
        missing = [s for s in syms if s not in rules]
        shown = {s: {"base": r.base, "quote": r.quote, "qty_decimals": r.qty_decimals,
                     "price_decimals": r.price_decimals, "min_qty": plain(r.min_qty),
                     "min_notional": plain(r.min_notional)}
                 for s, r in rules.items() if s in syms}
        return not missing, {"symbols": shown, "missing": missing}

    def depth(sym: str) -> Callable[[], tuple[bool, dict[str, Any]]]:
        def go() -> tuple[bool, dict[str, Any]]:
            result = ro.get_raw("/v1/depth", {"symbol": sym})["result"]
            types = {f"{side}.{field}": _type_counts(lv.get(field) for lv in _dicts(
                result.get(side) if isinstance(result, dict) else None))
                for side in ("bid", "ask") for field in ("price", "quantity")}
            book = parse_depth(sym, result, ro.clock.now())
            return True, {"bid_levels": len(book.bids), "ask_levels": len(book.asks),
                          "spread_bps": f"{book.spread_bps:.2f}", "number_types": types}
        return go

    def balances() -> tuple[bool, dict[str, Any]]:
        result = ro.get_raw("/v1/account/balances")["result"]
        bal = result.get("balances") if isinstance(result, dict) else None
        detail: dict[str, Any] = {"result_type": _jtype(result), "balances_type": _jtype(bal)}
        if not isinstance(bal, dict):
            return False, detail
        entries = _dicts(list(bal.values()))
        assets = sorted(str(k)[:32] for k in bal)[:1000]
        detail.update({
            "asset_count": len(bal), "assets": assets,
            "contains": {a: a in bal for a in ("TMN", "USDT", "BTC")},
            "documented_fields": _presence(entries, DOCUMENTED_FIELDS["balance"]),
            "extra_fields": _extra_keys(entries, DOCUMENTED_FIELDS["balance"]),
            "value_types": _type_counts(e.get("value") for e in entries),
            "locked_types": _type_counts(e.get("locked") for e in entries)})
        obs["balance_assets"] = assets
        parsed, err = _attempt(lambda: parse_balances(result))
        detail["typed_parse_ok"] = err is None
        if err is not None:
            detail["parse_error"] = err
            return False, detail
        consistent = all(b.locked <= b.value for b in parsed.values())
        detail["locked_le_value_everywhere"] = consistent
        obs["balances_value_locked_consistent"] = consistent
        return True, detail

    def fee() -> tuple[bool, dict[str, Any]]:
        result = ro.get_raw("/v1/account/fee")["result"]
        normalised, err = _attempt(lambda: fee_entries(result))
        if err is not None:
            obs["fee_shape"] = "invalid"
            return False, {"shape": "invalid", "result_type": _jtype(result), "parse_error": err}
        shape, entries = normalised
        present = {s: s in entries for s in syms}
        obs["fee_shape"] = shape
        obs["fee_symbols_present"] = present
        rows = _dicts([entries[s] for s in syms if s in entries])
        _, err = _attempt(lambda: parse_fees(result, syms))
        detail: dict[str, Any] = {
            "shape": shape, "entry_count": len(entries), "symbols_present": present,
            "documented_fields": _presence(rows, DOCUMENTED_FIELDS["fee"]),
            "rate_types": _type_counts(e.get("takerFeeRate") for e in rows),
            "typed_parse_ok": err is None}
        if err is not None:
            detail["parse_error"] = err
        return err is None and all(present.values()), detail

    def open_orders() -> tuple[bool, dict[str, Any]]:
        result = ro.get_raw("/v1/account/openOrders")["result"]
        orders = result.get("orders") if isinstance(result, dict) else None
        detail: dict[str, Any] = {"orders_type": _jtype(orders)}
        if not isinstance(orders, list):
            return False, detail
        rows = _dicts(orders)
        _, err = _attempt(lambda: [parse_order(o, f"result.orders[{i}]")
                                   for i, o in enumerate(orders)])
        detail.update({
            "count": len(orders),
            "documented_fields": _presence(rows, DOCUMENTED_FIELDS["order"]),
            "extra_fields": _extra_keys(rows, DOCUMENTED_FIELDS["order"]),
            "raw_statuses": sorted({str(o.get("status"))[:32] for o in rows})[:20],
            "typed_parse_ok": err is None})
        if err is not None:
            detail["parse_error"] = err
        if not rows:
            pending["open_order_fields"] = ("Field types of openOrders entries (no open order "
                                            "existed during the probe).")
        filters = {s: _filter_check(ro, "/v1/account/openOrders", {"symbol": s}, "orders",
                                    lambda o, s=s: o.get("symbol") == s) for s in syms}
        detail["symbol_filter"] = filters
        return err is None and all(f["honoured"] is not False for f in filters.values()), detail

    def trades() -> tuple[bool, dict[str, Any]]:
        result = ro.get_raw("/v1/account/trades")["result"]
        rows_raw = result.get("AccountLatestTrades") if isinstance(result, dict) else None
        detail: dict[str, Any] = {"rows_type": _jtype(rows_raw)}
        if not isinstance(rows_raw, list):
            return False, detail
        rows = _dicts(rows_raw)
        parsed, err = _attempt(lambda: parse_trades(result))
        detail.update({
            "count": len(rows_raw),
            "documented_fields": _presence(rows, DOCUMENTED_FIELDS["trade"]),
            "extra_fields": _extra_keys(rows, DOCUMENTED_FIELDS["trade"]),
            "typed_parse_ok": err is None})
        if err is not None:
            detail["parse_error"] = err
        fee_side: dict[str, dict[str, int]] = {"BUY": {}, "SELL": {}}
        for t in parsed or []:
            r = rules.get(t.symbol)
            if r is not None:
                label = "base" if t.fee_asset == r.base else (
                    "quote" if t.fee_asset == r.quote else "other")
                fee_side[t.side.value][label] = fee_side[t.side.value].get(label, 0) + 1
        detail["fee_asset_by_side"] = fee_side
        obs["fee_asset_by_side"] = fee_side
        for side in ("BUY", "SELL"):
            if not fee_side[side]:
                pending[f"{side.lower()}_fee_asset"] = (
                    f"Fee asset of {side} fills in {list(syms)} (no such trade in history).")
        filters: dict[str, Any] = {
            f"symbol={s}": _filter_check(ro, "/v1/account/trades", {"symbol": s},
                                         "AccountLatestTrades", lambda t, s=s: t.get("symbol") == s)
            for s in syms}
        for side in (Side.BUY, Side.SELL):
            want = side is Side.BUY
            filters[f"side={side.value}"] = _filter_check(
                ro, "/v1/account/trades", {"side": side.value}, "AccountLatestTrades",
                lambda t, want=want: isinstance(t.get("isBuyer"), (bool, int))
                and t.get("isBuyer") == want)
        detail["filters"] = filters
        if not rows:
            pending["trade_fields_and_filters"] = (
                "Field types of account trades and whether symbol/side filters are honoured "
                "(no trade existed during the probe).")
        return err is None and all(f["honoured"] is not False for f in filters.values()), detail

    def order_not_found() -> tuple[bool, dict[str, Any]]:
        probe_id = "wa-probe-" + secrets.token_hex(8)
        try:
            ro.get_order(probe_id)
        except OrderNotFound as exc:
            text = client.scrub(str(exc))
            if not text.startswith("HTTP 404"):
                return True, {"behaviour": "empty_result"}
            # A 404 alone cannot tell "no such order" from "no such route": keep the server's
            # wording (scrubbed) so a human can check it before going live.
            return True, {"behaviour": "http_404", "server_message": text.split(": ", 1)[-1][:200]}
        return False, {"behaviour": "an order was returned for a random id"}

    run("public.markets", markets)
    for s in syms:
        run(f"public.depth.{s}", depth(s))
    run("private.balances", balances)
    run("private.fee", fee)
    run("private.open_orders", open_orders)
    run("private.account_trades", trades)
    run("private.order_not_found", order_not_found)

    report = {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "undocumented": [{"id": k, "detail": v, "status": "UNVERIFIED"}
                         for k, v in pending.items()],
        "at": _iso(started),
        "duration_s": round(max(0.0, client.clock.now() - started), 3),
        "base_url": client.base_url,
        "symbols": list(syms),
        "observations": obs,
        "get_only": True,
        "probe_version": PROBE_VERSION,
    }
    return SchemaReport(_scrub_tree(report, client.scrub))


# --------------------------------------------------------------------------- helpers
def _filter_check(ro: WallexClient, path: str, params: dict[str, str], key: str,
                  match: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Re-queries with a filter; ``honoured`` is None when nothing came back (unverifiable)."""
    result = ro.get_raw(path, params)["result"]
    rows = result.get(key) if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return {"count": None, "honoured": False}
    dict_rows = _dicts(rows)
    if not rows:
        return {"count": 0, "honoured": None}
    return {"count": len(rows),
            "honoured": len(dict_rows) == len(rows) and all(match(r) for r in dict_rows)}


def _attempt(fn: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except ExchangeError as exc:   # SchemaError messages carry field paths, never values
        return None, f"{type(exc).__name__}: {str(exc)[:300]}"


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def _presence(entries: list[dict[str, Any]], fields: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in fields:
        n = sum(1 for e in entries if f in e)
        out[f] = ("n/a" if not entries else
                  "all" if n == len(entries) else "some" if n else "none")
    return out


def _extra_keys(entries: list[dict[str, Any]], fields: Iterable[str]) -> list[str]:
    known = set(fields)
    return sorted({str(k)[:40] for e in entries for k in e if k not in known})[:50]


def _type_counts(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        t = _jtype(v)
        out[t] = out.get(t, 0) + 1
    return out


def _describe_error(exc: BaseException, client: WallexClient) -> dict[str, Any]:
    d: dict[str, Any] = {"error": type(exc).__name__}
    if isinstance(exc, ServerError):
        d["status"] = exc.status                    # never the body
    elif isinstance(exc, RateLimitError):
        d["retry_after"] = exc.retry_after
    elif isinstance(exc, (ExchangeError, ValueError)):
        d["message"] = client.scrub(str(exc))[:300]
    return d


def _scrub_tree(obj: Any, scrub: Callable[[Any], str]) -> Any:
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {scrub(str(k)): _scrub_tree(v, scrub) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_tree(v, scrub) for v in obj]
    return obj


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")
