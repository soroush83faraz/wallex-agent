"""probe_private_schemas: GET-only, reports shapes, never secrets or amounts. No network."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from wallex_agent.clock import SimClock
from wallex_agent.exchange.probe import SchemaReport, UNVERIFIED, probe_private_schemas
from wallex_agent.exchange.wallex import WallexClient

FIX = Path(__file__).parent / "fixtures"
KEY = "SENTINELKEY-Q7xZ9wPv3Lm8/Rk2+Tz=Hq4"
_KEY_FORMS = (KEY, KEY.replace("/", "\\/"), urllib.parse.quote(KEY, safe=""))
KEY_FRAGMENTS = frozenset(f[i:i + 8] for f in _KEY_FORMS for i in range(len(f) - 7))
NOW = 1_792_700_000.0
# Distinctive amounts planted in balances / orders / trades: none may appear in a report.
AMOUNTS = ("987654321.123", "0.000777", "4242.4242", "0.00012345", "81234.56", "10.02847",
           "0.00000037", "18.37", "272111", "4998.71", "0.0459")


def fixture_json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"), parse_float=D)


def body(doc: Any) -> bytes:
    return json.dumps(doc, default=str, ensure_ascii=False).encode("utf-8")


def env(result: Any) -> dict[str, Any]:
    return {"success": True, "message": "ok", "result": result}


def assert_no_key(text: str) -> None:
    leaked = sorted(frag for frag in KEY_FRAGMENTS if frag in text)
    assert not leaked, f"key material leaked: {leaked[:3]}"


class Router:
    """Fake transport answering by path (+query); records every call."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method, url, headers, body_, timeout):
        self.calls.append((method, url, dict(headers)))
        parts = urllib.parse.urlsplit(url)
        candidates = [parts.path + ("?" + parts.query if parts.query else ""), parts.path]
        if parts.path.startswith("/v1/account/orders/"):
            candidates.append("/v1/account/orders/{id}")
        for key in candidates:
            if key in self.routes:
                r = self.routes[key]
                if isinstance(r, BaseException):
                    raise r
                return r(method, url) if callable(r) else r
        raise AssertionError(f"unrouted {method} {url}")


def trade(symbol: str, is_buyer: bool, fee_asset: str, qty: str, price: str, total: str,
          fee: str) -> dict[str, Any]:
    return {"symbol": symbol, "quantity": qty, "price": price, "sum": total, "fee": fee,
            "feeCoefficient": "0.0030000000000000", "feeAsset": fee_asset, "isBuyer": is_buyer,
            "timestamp": "2026-10-22T10:00:00Z"}


TRADES = [trade("BTCUSDT", True, "BTC", "0.00012345", "81234.56", "10.02847", "0.00000037"),
          trade("USDTTMN", False, "TMN", "18.37", "272111", "4998.71", "0.0459")]


def ok(result: Any) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "application/json"}, body(env(result))


def happy_routes() -> dict[str, Any]:
    balances = fixture_json("wallex_private_balances_documented_sample.json")
    balances["result"]["balances"]["TMN"].update(value="987654321.123", locked="0.000777")
    balances["result"]["balances"]["BTC"] = {"asset": "BTC", "faName": "x", "fiat": False,
                                             "value": "4242.4242", "locked": "0"}
    fee = fixture_json("wallex_private_fee_documented_sample.json")
    fee["result"]["USDTTMN"] = {"makerFeeRate": "0.00250000", "takerFeeRate": "0.00350000",
                                "recent_days_sum": 0}
    open_orders = fixture_json("wallex_private_open_orders_documented_sample.json")
    open_orders["result"]["orders"][0].update(price="81234.56", origQty="0.00012345",
                                              origSum="10.02847")
    oo = open_orders["result"]["orders"]
    return {
        "/v1/markets": (200, {}, (FIX / "markets_real_2026-10-22.json").read_bytes()),
        "/v1/depth?symbol=BTCUSDT": (200, {}, (FIX / "depth_btcusdt_real.json").read_bytes()),
        "/v1/depth?symbol=USDTTMN": (200, {}, (FIX / "depth_usdttmn_real.json").read_bytes()),
        "/v1/account/balances": (200, {}, body(balances)),
        "/v1/account/fee": (200, {}, body(fee)),
        "/v1/account/openOrders": (200, {}, body(open_orders)),
        "/v1/account/openOrders?symbol=BTCUSDT": ok({"orders": oo}),
        "/v1/account/openOrders?symbol=USDTTMN": ok({"orders": []}),
        "/v1/account/trades": ok({"AccountLatestTrades": TRADES}),
        "/v1/account/trades?symbol=BTCUSDT": ok({"AccountLatestTrades": TRADES[:1]}),
        "/v1/account/trades?symbol=USDTTMN": ok({"AccountLatestTrades": TRADES[1:]}),
        "/v1/account/trades?side=BUY": ok({"AccountLatestTrades": TRADES[:1]}),
        "/v1/account/trades?side=SELL": ok({"AccountLatestTrades": TRADES[1:]}),
        "/v1/account/orders/{id}": (404, {}, body({"success": False, "message": "not found"})),
    }


def run_probe(routes: dict[str, Any], **client_kw: Any) -> tuple[SchemaReport, Router]:
    router = Router(routes)
    key = client_kw.pop("key", KEY)
    client = WallexClient(key, transport=router, clock=SimClock(NOW), **client_kw)
    return probe_private_schemas(client), router


def check(report: SchemaReport, name: str) -> dict[str, Any]:
    [c] = [c for c in report.checks if c["name"] == name]
    return c


def test_happy_path_reports_shapes_without_secrets_or_amounts():
    report, router = run_probe(happy_routes())
    assert report.ok, report.to_json()
    text = json.dumps(report)                     # a plain dict: JSON-able as is
    assert_no_key(text)
    assert_no_key(report.to_json())
    for amount in AMOUNTS:
        assert amount not in text, amount
    assert {m for m, _, _ in router.calls} == {"GET"}
    for _, url, headers in router.calls:          # key only on private paths
        assert ("X-API-Key" in headers) == ("/v1/account/" in url)
    assert report["base_url"] == "https://api.wallex.ir" and report["get_only"] is True
    assert report["at"] == datetime.fromtimestamp(NOW, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    assert [c["name"] for c in report.checks] == [
        "public.markets", "public.depth.BTCUSDT", "public.depth.USDTTMN", "private.balances",
        "private.fee", "private.open_orders", "private.account_trades",
        "private.order_not_found"]
    obs = report["observations"]
    assert obs["fee_shape"] == "mapping"
    assert obs["fee_symbols_present"] == {"BTCUSDT": True, "USDTTMN": True}
    assert obs["balance_assets"] == ["BTC", "TMN", "USDT"]
    assert obs["balances_value_locked_consistent"] is True
    assert obs["fee_asset_by_side"] == {"BUY": {"base": 1}, "SELL": {"quote": 1}}
    bal = check(report, "private.balances")["detail"]
    assert bal["documented_fields"] == {f: "all" for f in ("asset", "faName", "fiat", "value",
                                                           "locked")}
    assert bal["value_types"] == {"string": 3} and bal["contains"]["BTC"] is True
    assert check(report, "private.order_not_found")["detail"] == {
        "behaviour": "http_404", "server_message": "not found"}
    assert check(report, "private.open_orders")["detail"]["raw_statuses"] == ["NEW"]
    trades = check(report, "private.account_trades")["detail"]
    assert all(f["honoured"] is True for f in trades["filters"].values())
    ids = {u["id"] for u in report.undocumented}
    assert {k for k, _ in UNVERIFIED} <= ids
    assert not {"buy_fee_asset", "sell_fee_asset", "trade_fields_and_filters",
                "open_order_fields"} & ids
    assert all(u["status"] == "UNVERIFIED" for u in report.undocumented)


def test_report_is_a_json_round_trippable_dict():
    report, _ = run_probe(happy_routes())
    assert isinstance(report, dict) and isinstance(report, SchemaReport)
    again = json.loads(report.to_json())
    assert again["ok"] is True and set(again) >= {"ok", "checks", "undocumented", "at", "base_url"}


def test_probe_never_writes_even_through_the_client_it_was_given():
    report, router = run_probe(happy_routes())
    assert router.calls and all(m == "GET" for m, _, _ in router.calls)
    assert not any("withdraw" in u or "/otc" in u or "profile" in u for _, u, _ in router.calls)


def test_probe_works_only_through_a_read_only_view(monkeypatch):
    """Even a future bug in the probe that tried to POST would hit ForbiddenEndpointError."""
    seen: list[tuple[str, bool]] = []
    for name in ("get_raw", "get_markets", "get_order"):
        original = getattr(WallexClient, name)

        def spy(self, *a, _name=name, _orig=original, **k):
            seen.append((_name, self.read_only))
            return _orig(self, *a, **k)

        monkeypatch.setattr(WallexClient, name, spy)
    for name in ("create_order", "cancel_order"):
        monkeypatch.setattr(WallexClient, name, lambda *a, **k: pytest.fail("probe wrote"))
    report, _ = run_probe(happy_routes())
    assert report.ok and seen and all(ro for _, ro in seen), seen


def test_list_shaped_fee_is_reported():
    routes = happy_routes()
    routes["/v1/account/fee"] = ok([
        {"symbol": "BTCUSDT", "makerFeeRate": "0.002", "takerFeeRate": "0.002"},
        {"symbol": "USDTTMN", "makerFeeRate": "0.002", "takerFeeRate": "0.002"}])
    report, _ = run_probe(routes)
    assert report.ok and report["observations"]["fee_shape"] == "list"


def test_missing_fee_entry_fails_the_fee_check():
    routes = happy_routes()
    routes["/v1/account/fee"] = (200, {}, (FIX / "wallex_private_fee_documented_sample.json")
                                 .read_bytes())        # documented sample has no USDTTMN
    report, _ = run_probe(routes)
    fee = check(report, "private.fee")
    assert not report.ok and not fee["ok"]
    assert fee["detail"]["symbols_present"] == {"BTCUSDT": True, "USDTTMN": False}


@pytest.mark.parametrize("result", ["flat", 5, [["BTCUSDT"]], None])
def test_invalid_fee_shape_is_reported_as_invalid(result):
    routes = happy_routes()
    routes["/v1/account/fee"] = ok(result)
    report, _ = run_probe(routes)
    assert not check(report, "private.fee")["ok"]
    assert report["observations"]["fee_shape"] == "invalid"


def test_auth_failure_echoing_the_key_is_reported_without_it():
    routes = happy_routes()
    denied = (401, {}, body({"success": False, "message": f"invalid api key {KEY} for this IP"}))
    for path in list(routes):
        if path.startswith("/v1/account/"):
            routes[path] = denied
    report, _ = run_probe(routes)
    assert not report.ok
    assert_no_key(json.dumps(report))
    for c in report.checks:
        if c["name"].startswith("private."):
            assert not c["ok"] and c["detail"]["error"] == "AuthError", c
        else:
            assert c["ok"], c


def test_server_errors_report_status_but_never_the_body():
    routes = happy_routes()
    routes["/v1/account/balances"] = (500, {}, b'{"balances": {"TMN": "987654321.123"}}')
    report, router = run_probe(routes, max_read_retries=0)
    bal = check(report, "private.balances")
    assert bal["detail"] == {"error": "ServerError", "status": 500}
    assert "987654321.123" not in json.dumps(report)


def test_public_only_client_fails_private_checks_without_sending_them():
    report, router = run_probe(happy_routes(), public_only=True)
    assert not report.ok
    assert all("/v1/account/" not in u for _, u, _ in router.calls)
    for c in report.checks:
        if c["name"].startswith("private."):
            assert c["detail"]["error"] == "ForbiddenEndpointError"


def test_balance_schema_violation_reports_path_not_value():
    routes = happy_routes()
    doc = fixture_json("wallex_private_balances_documented_sample.json")
    doc["result"]["balances"]["TMN"]["value"] = "-987654321.123"
    routes["/v1/account/balances"] = (200, {}, body(doc))
    report, _ = run_probe(routes)
    bal = check(report, "private.balances")
    assert not bal["ok"] and "result.balances.TMN.value" in bal["detail"]["parse_error"]
    assert "987654321.123" not in json.dumps(report)


def test_inconsistent_locked_is_observed_but_not_fatal():
    routes = happy_routes()
    doc = fixture_json("wallex_private_balances_documented_sample.json")
    doc["result"]["balances"]["USDT"].update(value="1", locked="4998.71")
    routes["/v1/account/balances"] = (200, {}, body(doc))
    report, _ = run_probe(routes)
    assert report["observations"]["balances_value_locked_consistent"] is False
    assert "4998.71" not in json.dumps(report)


def test_empty_history_leaves_items_unverified_but_passes():
    routes = happy_routes()
    for path in list(routes):
        if path.startswith("/v1/account/trades"):
            routes[path] = ok({"AccountLatestTrades": []})
        if path.startswith("/v1/account/openOrders"):
            routes[path] = ok({"orders": []})
    report, _ = run_probe(routes)
    assert report.ok, report.to_json()
    ids = {u["id"] for u in report.undocumented}
    assert {"buy_fee_asset", "sell_fee_asset", "trade_fields_and_filters",
            "open_order_fields"} <= ids
    trades = check(report, "private.account_trades")["detail"]
    assert all(f["honoured"] is None for f in trades["filters"].values())


def test_filter_not_honoured_fails_the_check():
    routes = happy_routes()
    routes["/v1/account/trades?symbol=BTCUSDT"] = ok({"AccountLatestTrades": TRADES})
    routes["/v1/account/openOrders?symbol=USDTTMN"] = routes["/v1/account/openOrders"]
    report, _ = run_probe(routes)
    assert not check(report, "private.account_trades")["ok"]
    assert not check(report, "private.open_orders")["ok"]
    assert check(report, "private.account_trades")["detail"]["filters"]["symbol=BTCUSDT"] == {
        "count": 2, "honoured": False}


@pytest.mark.parametrize("response,ok_,behaviour", [
    ((404, {}, b""), True, "http_404"),
    (ok(None), True, "empty_result"),
    (ok({}), True, "empty_result"),
    ((200, {}, body({"success": False, "message": "order not found"})), False, None),
    ((422, {}, body({"success": False})), False, None),
])
def test_order_not_found_behaviour_is_recorded(response, ok_, behaviour):
    routes = happy_routes()
    routes["/v1/account/orders/{id}"] = response
    report, router = run_probe(routes)
    c = check(report, "private.order_not_found")
    assert c["ok"] is ok_
    if behaviour:
        assert c["detail"]["behaviour"] == behaviour
    else:
        assert c["detail"]["error"] == "SchemaError"
    probed = [u for _, u, _ in router.calls if "/v1/account/orders/" in u]
    assert len(probed) == 1 and "/v1/account/orders/wa-probe-" in probed[0]


def test_order_not_found_server_message_is_kept_scrubbed():
    routes = happy_routes()
    routes["/v1/account/orders/{id}"] = (404, {}, body(
        {"success": False, "message": f"route not found (key {KEY})"}))
    report, _ = run_probe(routes)
    detail = check(report, "private.order_not_found")["detail"]
    assert detail["behaviour"] == "http_404" and detail["server_message"].startswith("route not")
    assert_no_key(json.dumps(report))


def test_an_order_returned_for_a_random_id_fails():
    routes = happy_routes()
    routes["/v1/account/orders/{id}"] = (200, {}, (
        FIX / "wallex_private_order_documented_sample.json").read_bytes())
    report, _ = run_probe(routes)
    assert not check(report, "private.order_not_found")["ok"]


def test_transport_failures_never_escape_the_probe():
    routes: dict[str, Any] = {k: OSError(f"network down, header X-API-Key: {KEY}")
                              for k in happy_routes()}
    report, _ = run_probe(routes, max_read_retries=0)
    assert not report.ok and len(report.checks) == 8
    assert all(c["detail"]["error"] == "TransportError" for c in report.checks)
    assert_no_key(json.dumps(report))
