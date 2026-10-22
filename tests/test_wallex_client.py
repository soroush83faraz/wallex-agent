"""WallexClient: allow-list, key hygiene, POST outcome classification, retries, schema validation.

Every request goes through a fake Transport: no network. Time is a SimClock: no real sleeping.
"""
from __future__ import annotations

import copy
import email.utils
import http.client
import io
import json
import logging
import pickle
import socket
import ssl
import traceback
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from wallex_agent import __version__
from wallex_agent.clock import SimClock
from wallex_agent.errors import (AuthError, ConfigError, ExchangeError, ForbiddenEndpointError,
                                 OrderNotFound, RateLimitError, RejectedError, SchemaError,
                                 ServerError, TransportError)
from wallex_agent.exchange import wallex as wx
from wallex_agent.exchange.wallex import (AmbiguousResponseError, TokenBucket, UrllibTransport,
                                          WallexClient)
from wallex_agent.interfaces import BookSource, Exchange
from wallex_agent.types import Balance, FeeRates, MarketRules, OrderRequest, OrderStatus, Side

FIX = Path(__file__).parent / "fixtures"
# Sentinel, never a real key. Contains / + = so escaped and URL-encoded echoes are exercised.
KEY = "SENTINELKEY-Q7xZ9wPv3Lm8/Rk2+Tz=Hq4"
_KEY_FORMS = (KEY, KEY.replace("/", "\\/"), urllib.parse.quote(KEY, safe=""),
              KEY.replace("+", " "))
# Every 8-character window of every form: catches PARTIAL leaks (e.g. a key cut by truncation).
KEY_FRAGMENTS = frozenset(f[i:i + 8] for f in _KEY_FORMS for i in range(len(f) - 7))


def assert_no_key(text: str) -> None:
    leaked = sorted(frag for frag in KEY_FRAGMENTS if frag in text)
    assert not leaked, f"key material leaked: {leaked[:3]}"


def fixture_bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"), parse_float=D)


class FakeTransport:
    """Scripted transport: each item is (status, headers, body), an exception, or a callable."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "headers_repr": repr(headers), "body": body, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected extra request")
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(method, url, headers, body, timeout)
        return r


def env(result: Any, success: Any = True, message: str = "ok") -> dict[str, Any]:
    return {"success": success, "message": message, "result": result}


def jdump(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode()


def ok(result: Any, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json"}, jdump(env(result))


def resp(status: int, body: Any = b"", headers: dict[str, str] | None = None):
    if isinstance(body, (dict, list)):
        body = jdump(body)
    elif isinstance(body, str):
        body = body.encode()
    return status, headers or {}, body


def make(*responses: Any, key: str | None = KEY, clock: SimClock | None = None, **kw: Any):
    t = FakeTransport(*responses)
    clk = clock or SimClock(1_000_000.0)
    return WallexClient(key, transport=t, clock=clk, **kw), t, clk


ORDER = fixture_json("wallex_private_order_documented_sample.json")["result"]
BALANCES = fixture_bytes("wallex_private_balances_documented_sample.json")
DEPTH_BTC = fixture_bytes("depth_btcusdt_real.json")


def order(**over: Any) -> dict[str, Any]:
    d = dict(ORDER)
    d.update(over)
    return d


def req(**over: Any) -> OrderRequest:
    base: dict[str, Any] = dict(client_id="wa-run1-1-abc123", symbol="BTCUSDT", side=Side.BUY,
                                price=D("10000"), quantity=D("0.001"))
    base.update(over)
    return OrderRequest(**base)


OPS = {
    "get_balances": lambda c: c.get_balances(),
    "get_order": lambda c: c.get_order("wa-run1-1-abc123"),
    "create_order": lambda c: c.create_order(req()),
    "cancel_order": lambda c: c.cancel_order("wa-run1-1-abc123"),
}


# =========================================================================== protocol conformance
def test_client_implements_exchange_and_book_source():
    c, _, _ = make()
    assert isinstance(c, Exchange) and isinstance(c, BookSource)
    assert WallexClient.get_book is WallexClient.get_order_book


# =========================================================================== allow-list / deny-list
DENIED = [
    ("POST", "/v1/account/money-withdrawal"),
    ("POST", "/v1/account/crypto-withdrawal"),
    ("GET", "/v1/account/crypto-withdrawal"),
    ("GET", "/v1/account/crypto-deposit"),
    ("GET", "/v1/account/profile"),
    ("GET", "/v1/account/card-numbers"),
    ("GET", "/v1/account/ibans"),
    ("GET", "/v1/otc/markets"),
    ("GET", "/v1/account/otc/price"),
    ("POST", "/v1/account/otc/orders"),
    ("POST", "/v2/withdraw"),
    ("POST", "/v1/account/transfer"),
    ("GET", "/v1/Account/Crypto-Withdrawal"),
    ("DELETE", "/v1/account/ibans"),
    ("GET", "/v1/account/orders/withdraw-1"),     # the id slot cannot smuggle a denied word
]


@pytest.mark.parametrize("method,path", DENIED)
def test_denied_endpoints_raise_before_any_io(method, path):
    c, t, clock = make()
    with pytest.raises(ForbiddenEndpointError, match="denied"):
        c._prepare(method, path)
    if method == "GET":
        with pytest.raises(ForbiddenEndpointError):
            c.get_raw(path)
    assert t.calls == [] and clock.now() == 1_000_000.0


NOT_ALLOWED = [
    ("GET", "/v1/currencies/stats"),
    ("GET", "/v2/depth/all"),
    ("GET", "/v1/account/orders"),               # only by id
    ("POST", "/v1/markets"),
    ("PUT", "/v1/account/orders"),
    ("PATCH", "/v1/account/orders"),
    ("HEAD", "/v1/markets"),
    ("DELETE", "/v1/account/orders/abc"),
    ("GET", "/v1/account/balances/"),
    ("GET", "/v1/account/orders/../crypto-withdrawal"),
    ("GET", "/v1/account/orders/%2e%2e"),
    ("GET", "/v1/account//balances"),
    ("GET", "v1/markets"),
    ("GET", "/v1/markets?x=1"),
    ("GET", "/V1/MARKETS"),
    ("GET", "/v1/account/orders/" + "a" * 65),
    ("GET", "/v1/marketsé"),
    ("GET", "/v1/account/balances\n"),
    ("GET", ""),
    ("GET", "https://evil.example/v1/markets"),
    ("GET", None),
    (None, "/v1/markets"),
]


@pytest.mark.parametrize("method,path", NOT_ALLOWED)
def test_unlisted_endpoints_raise_before_any_io(method, path):
    c, t, _ = make()
    with pytest.raises(ForbiddenEndpointError):
        c._prepare(method, path)
    assert t.calls == []


@pytest.mark.parametrize("params", [
    {"symbol": "BTCUSDT", "extra": "1"},          # unknown parameter
    {},                                           # required parameter missing
    {"symbol": "BTC USDT"},                       # bad characters
    {"symbol": "BTCUSDT&x=1"},
    {"symbol": 5},
])
def test_query_parameters_are_allow_listed_too(params):
    c, t, _ = make()
    with pytest.raises(ForbiddenEndpointError):
        c.get_raw("/v1/depth", params)
    assert t.calls == []


def test_order_body_shape_is_enforced_at_the_choke_point():
    c, t, _ = make()
    good = {"symbol": "BTCUSDT", "type": "LIMIT", "side": "BUY", "price": "1", "quantity": "1",
            "client_id": "a"}
    for body in (None, {**good, "extra": "x"}, {k: v for k, v in good.items() if k != "client_id"},
                 {**good, "price": 1}):
        with pytest.raises(ForbiddenEndpointError):
            c._prepare("POST", "/v1/account/orders", json_body=body)
    with pytest.raises(ForbiddenEndpointError):
        c._prepare("GET", "/v1/markets", json_body=good)
    assert t.calls == []


def test_public_only_forbids_private_and_writes_and_never_holds_the_key():
    c, t, _ = make(public_only=True)
    assert not c.has_key and c.read_only and c.public_only
    for call in (c.get_balances, c.get_fees, c.list_open_orders, c.get_trades,
                 lambda: c.get_order("abc"), lambda: c.cancel_order("abc"),
                 lambda: c.create_order(req()), lambda: c.get_raw("/v1/account/fee")):
        with pytest.raises(ForbiddenEndpointError):
            call()
    assert t.calls == []
    t.responses.append((200, {}, DEPTH_BTC))
    c.get_order_book("BTCUSDT")
    assert "X-API-Key" not in t.calls[0]["headers"]
    assert_no_key(repr(t.calls) + repr(vars(c)))


def test_read_only_view_forbids_writes_but_keeps_reads():
    c, t, _ = make((200, {}, BALANCES))
    ro = c.as_read_only()
    assert ro.read_only and not c.read_only and ro.has_key
    with pytest.raises(ForbiddenEndpointError):
        ro.create_order(req())
    with pytest.raises(ForbiddenEndpointError):
        ro.cancel_order("abc")
    assert t.calls == []
    ro.get_balances()
    assert t.calls[0]["headers"]["X-API-Key"] == KEY


def test_private_call_without_key_is_auth_error_before_io():
    c, t, _ = make(key=None)
    assert not c.has_key
    with pytest.raises(AuthError):
        c.get_balances()
    with pytest.raises(AuthError):
        c.create_order(req())
    assert t.calls == []
    c2, _, _ = make(key="")
    assert not c2.has_key


def test_key_travels_only_in_x_api_key_on_private_routes():
    c, t, _ = make((200, {}, DEPTH_BTC), (200, {}, BALANCES), ok(ORDER))
    c.get_order_book("BTCUSDT")
    c.get_balances()
    c.create_order(req())
    pub, priv, post = t.calls
    assert "X-API-Key" not in pub["headers"]
    assert priv["headers"]["X-API-Key"] == KEY and post["headers"]["X-API-Key"] == KEY
    for call in t.calls:
        others = {k: v for k, v in call["headers"].items() if k != "X-API-Key"}
        assert_no_key(json.dumps(others) + call["url"] + repr(call["body"]) + call["headers_repr"])
        assert call["headers"]["User-Agent"] == f"wallex-agent/{__version__}"
        assert call["headers"]["Accept-Encoding"] == "identity"
        assert call["url"].startswith("https://api.wallex.ir/")


@pytest.mark.parametrize("url", [
    "http://api.wallex.ir", "https://user:pw@api.wallex.ir", "https://api.wallex.ir/v1",
    "https://api.wallex.ir?x=1", "https://api.wallex.ir#f", "https://evil.example",
    "https://api.wallex.ir.evil.example", "https://api.wallex.ir:8443", "ftp://api.wallex.ir",
    "https://api.wallex.ir\n.evil.example", " https://api.wallex.ir", "https://api.wallex.ir:x",
    "https://[::1]", 5,
])
def test_keyed_client_only_talks_to_api_wallex_ir_over_https(url):
    with pytest.raises(ConfigError):
        WallexClient(KEY, base_url=url, transport=FakeTransport())


def test_base_url_normalisation_and_public_only_hosts():
    assert WallexClient(KEY, base_url="https://API.wallex.ir:443/",
                        transport=FakeTransport()).base_url == "https://api.wallex.ir"
    c = WallexClient(None, base_url="https://mirror.example", public_only=True,
                     transport=FakeTransport())
    assert c.base_url == "https://mirror.example"
    with pytest.raises(ConfigError):
        WallexClient(None, base_url="http://mirror.example", public_only=True,
                     transport=FakeTransport())


@pytest.mark.parametrize("bad", ["short-key", "has space" + "x" * 20, "line\nbreak" + "x" * 20,
                                 "x" * 513, "unicodé" + "x" * 20, 'quote"' + "x" * 20,
                                 "angle<" + "x" * 20])
def test_bad_key_format_is_refused_without_echoing_it(bad):
    with pytest.raises(ConfigError) as ei:
        WallexClient(bad, transport=FakeTransport())
    assert bad not in str(ei.value)


@pytest.mark.parametrize("kw", [
    {"timeout": 0}, {"timeout": -1}, {"timeout": float("nan")}, {"timeout": 1000},
    {"max_rps": 0}, {"max_rps": 101}, {"order_rps": 11}, {"order_rps": True},
    {"max_read_retries": -1}, {"max_read_retries": 11}, {"max_read_retries": 1.5},
    {"max_response_bytes": 10}, {"allowed_symbols": ()}, {"allowed_symbols": "BTCUSDT"},
    {"allowed_symbols": ("btcusdt",)},
])
def test_constructor_validation(kw):
    with pytest.raises(ConfigError):
        WallexClient(KEY, transport=FakeTransport(), **kw)
    with pytest.raises(ConfigError):
        WallexClient(b"bytes-key-0123456789", transport=FakeTransport())  # type: ignore[arg-type]


# =========================================================================== key hygiene
def all_text(exc: BaseException) -> str:
    parts = [str(exc), repr(exc), repr(exc.args)]
    parts += [str(getattr(exc, a, "")) for a in ("body", "message")]
    parts += traceback.format_exception(type(exc), exc, exc.__traceback__)
    return "\n".join(parts)


def test_repr_str_vars_pickle_copy_never_expose_key():
    c, _, _ = make()
    for text in (repr(c), str(c), f"{c}", repr(vars(c)), repr(c.as_read_only()),
                 repr(c._key), str(c._key), f"{c._key}", repr(c._scrub)):
        assert_no_key(text)
    assert "api_key=<set>" in repr(c)
    for fn in (pickle.dumps, copy.deepcopy, copy.copy):
        with pytest.raises(TypeError):
            fn(c._key)
        with pytest.raises(TypeError):
            fn(c._scrub)
    with pytest.raises(TypeError):
        pickle.dumps(c)


ECHO_FORMS = {
    "json": lambda k: json.dumps({"success": False, "message": f"invalid key {k}"}),
    "php_escaped": lambda k: json.dumps({"success": False, "message": f"bad {k}"}).replace("/", "\\/"),
    "html_header": lambda k: f"<html><pre>X-API-Key: {k}</pre></html>",
    "urlencoded": lambda k: "denied for " + urllib.parse.quote(k, safe=""),
    "quote_plus": lambda k: "denied for " + urllib.parse.quote_plus(k),
    "form_decoded": lambda k: "denied for " + k.replace("+", " "),
    "nested": lambda k: json.dumps({"success": False, "result": {"headers": {"x-api-key": k}},
                                    "message": json.dumps({"api_key": k})}),
    "window_edge": lambda k: json.dumps({"success": False, "message": "x" * 15_990 + k + "y" * 50}),
    "tail": lambda k: "z" * 290 + k,
    # The key placed so that it straddles every truncation limit (200/300/500/600 chars after the
    # message prefix), in a raw body and inside a JSON "message".
    **{f"edge{n}": (lambda k, n=n: "z" * n + k + "!" * 40)
       for n in (150, 180, 190, 250, 270, 290, 450, 470, 490, 550, 580)},
    **{f"jedge{n}": (lambda k, n=n: json.dumps({"success": False, "message": "z" * n + k}))
       for n in (120, 150, 180, 230, 250, 270, 430, 450, 470, 530, 560)},
}


@pytest.mark.parametrize("form", sorted(ECHO_FORMS))
@pytest.mark.parametrize("status", [200, 302, 400, 401, 403, 404, 409, 422, 500, 503])
@pytest.mark.parametrize("op", sorted(OPS))
def test_key_echoed_by_the_server_never_leaks(op, status, form, caplog):
    caplog.set_level(logging.DEBUG)
    body = ECHO_FORMS[form](KEY)
    c, t, _ = make(resp(status, body), max_read_retries=0)
    with pytest.raises(ExchangeError) as ei:
        OPS[op](c)
    assert_no_key(all_text(ei.value))
    assert_no_key(caplog.text)
    assert len(t.calls) == 1


def test_fragment_detector_catches_partial_leaks():
    with pytest.raises(AssertionError):
        assert_no_key("z" * 290 + KEY[:10])
    with pytest.raises(AssertionError):
        assert_no_key(KEY[20:28].replace("/", "\\/"))      # escaped-slash form, partial


def test_other_api_key_header_text_is_scrubbed_too():
    c, _, _ = make(resp(500, "proxy dump: X-API-Key: someoneelseskey123 ; x_api_key=abc123def"),
                   max_read_retries=0)
    with pytest.raises(ServerError) as ei:
        c.get_balances()
    assert "someoneelseskey123" not in ei.value.body and "abc123def" not in ei.value.body
    assert c.scrub(f"X-API-Key: {KEY}") == "X-API-Key: ***"


@pytest.mark.parametrize("exc", [
    TransportError(f"boom X-API-Key: {KEY}"),
    ValueError(f"Invalid header value {KEY!r}"),
    OSError(f"connection reset while sending {KEY}"),
    Exception({"X-API-Key": KEY}),
    socket.timeout("timed out"),
])
@pytest.mark.parametrize("op", sorted(OPS))
def test_transport_failures_are_scrubbed_and_unchained(op, exc, caplog):
    caplog.set_level(logging.DEBUG)
    c, t, _ = make(exc, max_read_retries=0)
    with pytest.raises(TransportError) as ei:
        OPS[op](c)
    e = ei.value
    assert e.__cause__ is None and e.__context__ is None
    assert_no_key(all_text(e))
    assert_no_key(caplog.text)
    assert len(t.calls) == 1


def test_headers_mapping_handed_to_transport_redacts_key_in_repr():
    c, t, _ = make((200, {}, BALANCES))
    c.get_balances()
    assert_no_key(t.calls[0]["headers_repr"])
    assert "<redacted>" in t.calls[0]["headers_repr"]


def test_ids_containing_the_key_are_refused_before_io():
    alnum_key = "ALNUMSECRETKEY0123456789"
    c, t, _ = make(key=alnum_key)
    for call in (lambda: c.get_order(f"wa-{alnum_key}"), lambda: c.cancel_order(alnum_key),
                 lambda: c.create_order(req(client_id=f"x{alnum_key}"))):
        with pytest.raises(ValueError) as ei:
            call()
        assert alnum_key not in all_text(ei.value)
    assert t.calls == []


def test_unexpected_failure_after_a_write_is_sent_is_an_unknown_outcome(monkeypatch):
    def boom(*_a: Any, **_k: Any) -> Any:
        raise ValueError("bug while reading Retry-After")

    monkeypatch.setattr(wx, "parse_retry_after", boom)
    for op in ("create_order", "cancel_order"):
        c, t, _ = make(resp(429, "", {"Retry-After": "1"}))
        with pytest.raises(SchemaError, match="UNKNOWN"):    # never ValueError ("not sent")
            OPS[op](c)
        assert len(t.calls) == 1


def test_validation_errors_never_echo_the_key():
    c, _, _ = make()
    with pytest.raises(ValueError) as ei:
        c.get_order(KEY)                       # someone passes the key by mistake
    assert_no_key(all_text(ei.value))
    with pytest.raises(ValueError) as ei:
        c.get_order_book(KEY)
    assert_no_key(all_text(ei.value))


# =========================================================================== POST classification
def test_create_order_success_parses_ack_and_posts_once_with_plain_strings():
    c, t, _ = make(ok(ORDER))
    info = c.create_order(req(price=D("10000.000"), quantity=D("1E-3")))
    assert info.client_order_id == "LIMIT-670f7c34-d609-40d3-b98e-0150640d79cb"
    assert (info.symbol, info.side, info.type) == ("BTCUSDT", Side.BUY, "LIMIT")
    assert info.status is OrderStatus.NEW and info.active and info.raw_status == "NEW"
    assert info.price == D("10000") and info.orig_qty == D("0.001") and info.orig_sum == D("10")
    assert info.executed_qty == 0 and info.executed_price is None    # sample's garbage ignored
    assert info.created_at == "2022-06-18T13:45:08.000000Z"
    [call] = t.calls
    assert call["method"] == "POST" and call["url"] == "https://api.wallex.ir/v1/account/orders"
    assert call["headers"]["Content-Type"] == "application/json" and call["timeout"] == 10.0
    assert json.loads(call["body"]) == {"symbol": "BTCUSDT", "type": "LIMIT", "side": "BUY",
                                        "price": "10000", "quantity": "0.001",
                                        "client_id": "wa-run1-1-abc123"}


def test_order_numbers_are_sent_as_plain_decimal_strings():
    c, t, _ = make(ok(order(price="81000.1", origQty="0.00000001", origSum="0.00081")))
    c.create_order(req(price=D("81000.10"), quantity=D("1E-8"), client_id="a" * 64))
    body = json.loads(t.calls[0]["body"])
    assert body["price"] == "81000.1" and body["quantity"] == "0.00000001"
    assert body["client_id"] == "a" * 64


@pytest.mark.parametrize("code", [1001, 1002, 1004, 1006, 1011, "1006"])
def test_422_is_a_definitive_rejection_with_error_code(code):
    c, t, _ = make(resp(422, {"success": False, "message": "invalid", "result": {"error_code": code}}))
    with pytest.raises(RejectedError) as ei:
        c.create_order(req())
    assert ei.value.status == 422 and ei.value.error_code == int(code)
    assert ei.value.message == "invalid" and len(t.calls) == 1


@pytest.mark.parametrize("status", [400, 402, 404, 405, 410, 413, 418, 422, 451, 499])
def test_other_definitive_4xx_are_rejections(status):
    c, t, _ = make(resp(status, "<html>nope</html>"))
    with pytest.raises(RejectedError) as ei:
        c.create_order(req())
    assert ei.value.status == status and ei.value.error_code is None and len(t.calls) == 1


@pytest.mark.parametrize("bad_code", [True, -1, "10x", 10 ** 7, 1006.5, None])
def test_unparseable_error_codes_become_none(bad_code):
    c, _, _ = make(resp(422, {"success": False, "result": {"error_code": bad_code}}))
    with pytest.raises(RejectedError) as ei:
        c.create_order(req())
    assert ei.value.error_code is None


@pytest.mark.parametrize("status,exc_type", [(401, AuthError), (403, AuthError),
                                             (429, RateLimitError)])
def test_auth_and_rate_limit_answers_are_not_rejections(status, exc_type):
    c, t, clock = make(resp(status, {"success": False}, {"Retry-After": "3"}))
    with pytest.raises(exc_type) as ei:
        c.create_order(req())
    assert not isinstance(ei.value, RejectedError) and len(t.calls) == 1
    if status == 429:
        assert ei.value.retry_after == 3.0
    assert clock.now() == 1_000_000.0          # no retry, no sleeping


def _gzip_body() -> bytes:
    import gzip
    return gzip.compress(jdump(env(ORDER)))


UNKNOWN_OUTCOMES = [
    ("http500", resp(500, "oops"), ServerError),
    ("http502", resp(502, "<html>bad gateway</html>"), ServerError),
    ("http503", resp(503, "", {"Retry-After": "1"}), ServerError),
    ("http504", resp(504), ServerError),
    ("http599", resp(599), ServerError),
    ("http408", resp(408, {"success": False}), AmbiguousResponseError),
    ("http409_duplicate", resp(409, {"success": False, "message": "duplicate client id"}),
     AmbiguousResponseError),
    ("http302_redirect", resp(302, "", {"Location": "https://evil.example/"}), AmbiguousResponseError),
    ("http101", resp(101), AmbiguousResponseError),
    ("http600", resp(600), AmbiguousResponseError),
    ("timeout", socket.timeout("timed out"), TransportError),
    ("reset", ConnectionResetError("reset by peer"), TransportError),
    ("transport_error", TransportError("x"), TransportError),
    ("remote_disconnected", http.client.RemoteDisconnected("closed"), TransportError),
    ("incomplete_read", http.client.IncompleteRead(b"{"), TransportError),
    ("keyboard_free_runtime_error", RuntimeError("bug in transport"), TransportError),
    ("malformed_result", lambda *a: "garbage", TransportError),
    ("bool_status", (True, {}, b"{}"), TransportError),
    ("str_body", (200, {}, "{}"), TransportError),
    ("bad_headers", (200, None, b"{}"), TransportError),
    ("2xx_empty", resp(200, b""), SchemaError),
    ("2xx_html", resp(200, "<html>ok</html>"), SchemaError),
    ("2xx_truncated", resp(201, jdump(env(ORDER))[:40]), SchemaError),
    ("2xx_not_utf8", resp(200, b"\xff\xfe{}"), SchemaError),
    ("2xx_array", resp(200, [env(ORDER)]), SchemaError),
    ("2xx_success_false", resp(200, env(ORDER, success=False)), SchemaError),
    ("2xx_success_string", resp(200, env(ORDER, success="true")), SchemaError),
    ("2xx_success_int", resp(200, env(ORDER, success=1)), SchemaError),
    ("2xx_success_missing", resp(200, {"message": "ok", "result": ORDER}), SchemaError),
    ("2xx_result_missing", resp(200, {"success": True, "message": "ok"}), SchemaError),
    ("2xx_result_null", resp(200, env(None)), SchemaError),
    ("2xx_missing_client_order_id", resp(200, env({k: v for k, v in ORDER.items()
                                                    if k != "clientOrderId"})), SchemaError),
    ("2xx_wrong_symbol", resp(200, env(order(symbol="USDTTMN"))), SchemaError),
    ("2xx_wrong_side", resp(200, env(order(side="SELL"))), SchemaError),
    ("2xx_market_type", resp(200, env(order(type="MARKET"))), SchemaError),
    ("2xx_bad_number", resp(200, env(order(price="1,000"))), SchemaError),
    ("2xx_nan", resp(200, b'{"success":true,"result":{"price":NaN}}'), SchemaError),
    ("2xx_gzip", (200, {"Content-Encoding": "gzip"}, _gzip_body()), SchemaError),
    ("2xx_oversize", resp(200, b" " * 2000 + jdump(env(ORDER))), SchemaError),
    ("2xx_deep_nesting", resp(200, b"[" * 500 + b"]" * 500), SchemaError),
]


@pytest.mark.parametrize("name,response,exc_type", UNKNOWN_OUTCOMES,
                         ids=[u[0] for u in UNKNOWN_OUTCOMES])
def test_post_unknown_outcomes_are_never_rejections_and_never_retried(name, response, exc_type):
    c, t, clock = make(response, max_response_bytes=1024)
    with pytest.raises(ExchangeError) as ei:
        c.create_order(req())
    e = ei.value
    assert not isinstance(e, RejectedError)
    assert isinstance(e, exc_type) and isinstance(e, (TransportError, ServerError, SchemaError))
    assert len(t.calls) == 1 and clock.now() == 1_000_000.0


def test_status_sweep_only_definitive_4xx_are_rejections():
    body = jdump({"success": False, "message": "x", "result": {"error_code": 1006}})
    for status in range(100, 1000):
        c, t, _ = make((status, {}, body))
        try:
            c.create_order(req())
        except RejectedError:
            assert 400 <= status <= 499 and status not in (401, 403, 408, 409, 429), status
        except (TransportError, ServerError, SchemaError, AuthError, RateLimitError):
            pass
        else:  # pragma: no cover - would be a serious bug
            pytest.fail(f"HTTP {status} produced an OrderInfo")
        assert len(t.calls) == 1, status


@pytest.mark.parametrize("raw", [b"[" * 100_000 + b"]" * 100_000, b'{"a":1e5', b"\xef\xbb\xbf{}",
                                 b'{"n":' + b"9" * 5000 + b"}"],
                         ids=["deep_nesting", "truncated", "utf8_bom", "huge_int"])
def test_json_decoder_edge_cases(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        assert wx.decode_json_bytes(raw) == {}            # a UTF-8 BOM is tolerated
    else:
        with pytest.raises(SchemaError):
            wx.decode_json_bytes(raw)


def test_unknown_outcome_message_says_reconcile():
    c, _, _ = make(resp(200, env(order(side="SELL"))))
    with pytest.raises(SchemaError, match="UNKNOWN"):
        c.create_order(req())


@pytest.mark.parametrize("over", [
    {"type": "MARKET"}, {"type": "limit"}, {"type": "STOP_LIMIT"},
    {"client_id": ""}, {"client_id": "x" * 65}, {"client_id": "bad id"}, {"client_id": "a/b"},
    {"client_id": "ü"}, {"client_id": "a\n"}, {"client_id": None},
    {"price": D("0")}, {"price": D("-1")}, {"quantity": D("0")}, {"quantity": D("-0.1")},
    {"price": 81000.1}, {"quantity": "0.1"}, {"price": D("NaN")}, {"price": D("Infinity")},
    {"price": True}, {"price": D("1E+40")}, {"quantity": D("1E-35")},
    {"symbol": "ETHUSDT"}, {"symbol": "btcusdt"}, {"side": "buy"}, {"side": "HOLD"},
])
def test_invalid_order_requests_are_refused_locally_before_io(over):
    c, t, _ = make()
    with pytest.raises(ValueError):
        c.create_order(req(**over))
    assert t.calls == []


def test_order_posts_are_rate_limited_on_the_injected_clock():
    clock = SimClock(5_000.0)
    c, t, _ = make(ok(ORDER), ok(ORDER), ok(ORDER), ok(ORDER), clock=clock, order_rps=1.0)
    for _ in range(4):
        c.create_order(req())
    assert len(t.calls) == 4 and clock.now() - 5_000.0 == pytest.approx(3.0)


# =========================================================================== cancel_order
@pytest.mark.parametrize("body", [b"", b"  \n", jdump({"success": True, "message": "ok"}),
                                  jdump({"message": "ok"}), jdump(env(None))])
def test_cancel_success_returns_none(body):
    c, t, _ = make((200, {}, body))
    assert c.cancel_order("wa-run1-1-abc123") is None
    [call] = t.calls
    assert call["method"] == "DELETE" and call["body"] is None
    assert call["url"] == "https://api.wallex.ir/v1/account/orders?clientOrderId=wa-run1-1-abc123"
    assert call["headers"]["X-API-Key"] == KEY


@pytest.mark.parametrize("response,exc_type", [
    (resp(404, {"success": False}), OrderNotFound),
    (resp(500), ServerError),
    (resp(502), ServerError),
    (socket.timeout("timed out"), TransportError),
    (resp(422, {"success": False, "message": "cannot cancel", "result": {"error_code": 2001}}),
     RejectedError),
    (resp(409), AmbiguousResponseError),
    (resp(302), AmbiguousResponseError),
    (resp(200, env(None, success=False)), SchemaError),
    (resp(200, "<html>ok</html>"), SchemaError),
    (resp(200, [1]), SchemaError),
    (resp(401), AuthError),
    (resp(429, "", {"Retry-After": "1"}), RateLimitError),
])
def test_cancel_failures_raise_and_are_never_retried(response, exc_type):
    c, t, clock = make(response)
    with pytest.raises(exc_type):
        c.cancel_order("wa-run1-1-abc123")
    assert len(t.calls) == 1 and clock.now() == 1_000_000.0


@pytest.mark.parametrize("bad", ["../x", "a b", "", "x" * 65, "a&b=c", "a/b", "a%2Fb", None, 5,
                                 "LIMIT-٠"])
def test_order_ids_are_validated_before_io(bad):
    c, t, _ = make()
    with pytest.raises(ValueError):
        c.cancel_order(bad)
    with pytest.raises(ValueError):
        c.get_order(bad)
    assert t.calls == []


# =========================================================================== get_order
def test_get_order_parses_documented_sample():
    c, t, _ = make(ok(ORDER))
    info = c.get_order("LIMIT-670f7c34-d609-40d3-b98e-0150640d79cb")
    assert info.status is OrderStatus.NEW and info.client_order_id.startswith("LIMIT-")
    assert t.calls[0]["url"] == ("https://api.wallex.ir/v1/account/orders/"
                                 "LIMIT-670f7c34-d609-40d3-b98e-0150640d79cb")
    assert t.calls[0]["method"] == "GET"


def test_get_order_404_is_order_not_found_and_not_retried():
    c, t, _ = make(resp(404, {"success": False, "message": "not found"}))
    with pytest.raises(OrderNotFound):
        c.get_order("wa-1")
    assert len(t.calls) == 1


@pytest.mark.parametrize("result", [None, {}, []])
def test_get_order_empty_result_is_order_not_found(result):
    c, _, _ = make(ok(result))
    with pytest.raises(OrderNotFound):
        c.get_order("wa-1")


@pytest.mark.parametrize("response", [resp(400, {"success": False}), resp(422, {"success": False}),
                                      resp(200, env(ORDER, success=False)), resp(302)])
def test_get_order_other_failures_are_not_order_not_found(response):
    c, _, _ = make(response)
    with pytest.raises(SchemaError):
        c.get_order("wa-1")


def test_get_order_retries_5xx_with_backoff():
    c, t, clock = make(resp(500), resp(503), ok(ORDER))
    assert c.get_order("wa-1").status is OrderStatus.NEW
    assert len(t.calls) == 3 and clock.now() - 1_000_000.0 == pytest.approx(0.5 + 1.0)


E = OrderStatus


@pytest.mark.parametrize("status,active,executed,expected", [
    ("NEW", True, "0", E.NEW),
    ("NEW", True, "0.0004", E.PARTIALLY_FILLED),
    ("NEW", True, "0.001", E.UNKNOWN),           # fully executed yet NEW/active: contradiction
    ("NEW", False, "0", E.UNKNOWN),
    ("FILLED", False, "0.001", E.FILLED),
    ("FILLED", True, "0.001", E.UNKNOWN),
    ("CANCELED", False, "0.0004", E.CANCELED),
    ("CANCELED", False, "0", E.CANCELED),
    ("CANCELED", True, "0", E.UNKNOWN),
    ("PARTIALLY_FILLED", True, "0.0004", E.PARTIALLY_FILLED),   # derived from quantities
    ("PARTIALLY_FILLED", False, "0.0004", E.UNKNOWN),
    ("PARTIALLY_FILLED", True, "0", E.UNKNOWN),
    ("EXPIRED", False, "0", E.UNKNOWN),
    ("new", True, "0", E.UNKNOWN),
    ("WEIRD", True, "0", E.UNKNOWN),
])
def test_order_status_mapping(status, active, executed, expected):
    o = order(status=status, active=active, executedQty=executed,
              executedSum=str(D(executed) * 10000), executedPrice="10000")
    info = wx.parse_order(o)
    assert info.status is expected and info.raw_status == status
    if D(executed) > 0:
        assert info.executed_price == D("10000")


@pytest.mark.parametrize("over", [
    {"side": "buy"}, {"side": None}, {"side": ["BUY"]}, {"price": "0"}, {"price": "-1"},
    {"origQty": "abc"}, {"origQty": "0"}, {"executedQty": "0.002"}, {"active": "true"},
    {"active": 2}, {"active": None}, {"status": 5}, {"status": ""}, {"clientOrderId": ""},
    {"clientOrderId": None}, {"clientOrderId": "has space"}, {"clientOrderId": "x" * 129},
    {"symbol": ""}, {"symbol": "BTC/USDT"}, {"price": True}, {"price": 10000.0},
    {"executedSum": "1", "executedQty": "0"}, {"created_at": 5}, {"type": ""},
    {"executedQty": "-0.0001"}, {"origSum": "x"},
])
def test_order_schema_violations(over):
    with pytest.raises(SchemaError):
        wx.parse_order(order(**over))


@pytest.mark.parametrize("missing", ["symbol", "type", "side", "price", "origQty", "executedQty",
                                     "executedSum", "status", "active", "clientOrderId"])
def test_order_missing_required_field_reports_its_path(missing):
    o = order()
    del o[missing]
    with pytest.raises(SchemaError, match=f"result.{missing}: missing"):
        wx.parse_order(o)


def test_order_optional_fields_extra_keys_numbers_and_int_booleans_tolerated():
    o = order(price=D("10000"), origQty=D("0.001"), executedQty=0, executedSum=0, active=1)
    for k in ("origSum", "executedPrice", "executedPercent", "created_at"):
        del o[k]
    o["brandNewField"] = {"nested": [1, 2]}
    info = wx.parse_order(o)
    assert info.orig_sum is None and info.created_at is None and info.active is True


# =========================================================================== open orders / trades
OPEN_ORDERS = fixture_bytes("wallex_private_open_orders_documented_sample.json")
TRADES = fixture_bytes("wallex_private_account_trades_documented_sample.json")


def test_open_orders_documented_sample_and_symbol_filter():
    c, t, _ = make((200, {}, OPEN_ORDERS), (200, {}, OPEN_ORDERS))
    assert [o.client_order_id for o in c.list_open_orders()] == [ORDER["clientOrderId"]]
    assert c.list_open_orders("BTCUSDT")[0].symbol == "BTCUSDT"
    assert t.calls[0]["url"].endswith("/v1/account/openOrders")
    assert t.calls[1]["url"].endswith("/v1/account/openOrders?symbol=BTCUSDT")


def test_open_orders_filter_not_honoured_is_schema_error():
    c, _, _ = make(ok({"orders": [order(symbol="USDTTMN")]}))
    with pytest.raises(SchemaError, match="requested symbol"):
        c.list_open_orders("BTCUSDT")


@pytest.mark.parametrize("result", [{"orders": {}}, {"orders": None}, {}, [], {"orders": [5]}])
def test_open_orders_schema_violations(result):
    c, _, _ = make(ok(result))
    with pytest.raises(SchemaError):
        c.list_open_orders()


def test_open_orders_invalid_symbol_is_refused_locally():
    c, t, _ = make()
    with pytest.raises(ValueError):
        c.list_open_orders("ETHUSDT")
    assert t.calls == []


def test_trades_documented_sample():
    c, t, _ = make((200, {}, TRADES))
    [tr] = c.get_trades()
    assert tr.symbol == "PAXGTMN" and tr.side is Side.BUY and tr.fee_asset == "PAXG"
    assert tr.quantity == D("0.0032340000000000") and tr.quote_sum == D("197705.1989220000000000")
    assert tr.fee == D("0.0000097020000000") and tr.fee_coefficient == D("0.003")
    assert tr.timestamp == "2022-06-18T17:22:25Z" and tr.ts_epoch == 1655572945.0
    assert t.calls[0]["url"].endswith("/v1/account/trades")


def _trade(symbol="BTCUSDT", is_buyer=False, **over):
    row = {"symbol": symbol, "quantity": "0.0001", "price": "81000", "sum": "8.1",
           "fee": "0.02", "feeCoefficient": "0.0025", "feeAsset": "USDT", "isBuyer": is_buyer,
           "timestamp": "2026-10-22T10:00:00Z"}
    row.update(over)
    return row


def test_trades_filters_are_sent_and_enforced():
    c, t, _ = make(ok({"AccountLatestTrades": [_trade()]}),
                   ok({"AccountLatestTrades": [_trade(symbol="USDTTMN")]}),
                   ok({"AccountLatestTrades": [_trade(is_buyer=True)]}))
    [tr] = c.get_trades("BTCUSDT", Side.SELL)
    assert tr.side is Side.SELL
    assert t.calls[0]["url"].endswith("/v1/account/trades?side=SELL&symbol=BTCUSDT")
    with pytest.raises(SchemaError, match="requested symbol"):
        c.get_trades("BTCUSDT")
    with pytest.raises(SchemaError, match="requested side"):
        c.get_trades(side=Side.SELL)


@pytest.mark.parametrize("over", [
    {"timestamp": "2026-10-22T10:00:00"}, {"timestamp": "yesterday"}, {"timestamp": 1792668060},
    {"feeAsset": ""}, {"feeAsset": None}, {"isBuyer": "yes"}, {"fee": "-0.1"},
    {"feeCoefficient": "1.5"}, {"price": "0"}, {"quantity": "0"}, {"sum": None},
])
def test_trade_schema_violations(over):
    with pytest.raises(SchemaError):
        wx.parse_trades({"AccountLatestTrades": [_trade(**over)]})


@pytest.mark.parametrize("ts,epoch", [
    ("2022-06-18T17:22:25Z", 1655572945.0),
    ("2022-06-18T17:22:25.5Z", 1655572945.5),
    ("2022-06-18T13:45:08.000000Z", 1655559908.0),
    ("2022-06-18T20:52:25+03:30", 1655572945.0),
    ("2022-06-18 17:22:25+0000", 1655572945.0),
    ("2022-06-18T12:22:25-05:00", 1655572945.0),
])
def test_iso_timestamps(ts, epoch):
    assert wx.parse_iso8601(ts) == pytest.approx(epoch)


@pytest.mark.parametrize("bad", ["2022-06-18T17:22:25", "2022-13-01T00:00:00Z",
                                 "2022-02-30T00:00:00Z", "2022-06-18T17:22:60Z",
                                 "2022-06-18T17:22:25+25:00", "1900-01-01T00:00:00Z",
                                 "٢022-06-18T17:22:25Z", None, 5, "x" * 100])
def test_bad_timestamps(bad):
    with pytest.raises(SchemaError):
        wx.parse_iso8601(bad)


# =========================================================================== read retries
def test_read_retries_5xx_and_transport_errors_with_backoff():
    c, t, clock = make(resp(503), TransportError("reset"), resp(500, "x"), (200, {}, BALANCES))
    assert set(c.get_balances()) == {"TMN", "USDT"}
    assert len(t.calls) == 4
    assert clock.now() - 1_000_000.0 == pytest.approx(0.5 + 1.0 + 2.0)


def test_read_honours_retry_after_seconds_and_http_date():
    c, t, clock = make(resp(429, "", {"Retry-After": "2"}), (200, {}, BALANCES))
    c.get_balances()
    assert len(t.calls) == 2 and clock.now() - 1_000_000.0 == pytest.approx(2.0)
    date = email.utils.formatdate(1_000_000.0 + 5.0, usegmt=True)
    c2, t2, clock2 = make(resp(503, "", {"retry-after": date}), (200, {}, BALANCES))
    c2.get_balances()
    assert len(t2.calls) == 2 and clock2.now() - 1_000_000.0 == pytest.approx(5.0)


def test_read_surfaces_long_retry_after_instead_of_sleeping():
    c, t, clock = make(resp(429, "", {"Retry-After": "120"}))
    with pytest.raises(RateLimitError) as ei:
        c.get_balances()
    assert ei.value.retry_after == 120.0 and len(t.calls) == 1 and clock.now() == 1_000_000.0
    c2, t2, _ = make(resp(503, "", {"Retry-After": "3600"}))
    with pytest.raises(ServerError):
        c2.get_balances()
    assert len(t2.calls) == 1


@pytest.mark.parametrize("response,exc_type", [
    (TransportError("down"), TransportError),
    (resp(502, "bad gateway"), ServerError),
    (resp(429), RateLimitError),
])
def test_read_exhausts_retries_then_raises(response, exc_type):
    c, t, _ = make(response, response, response, response)
    with pytest.raises(exc_type):
        c.get_balances()
    assert len(t.calls) == 4                 # 1 + max_read_retries(3)


@pytest.mark.parametrize("response,exc_type", [
    (resp(400, {"success": False}), SchemaError),
    (resp(404), SchemaError),
    (resp(408), SchemaError),
    (resp(422), SchemaError),
    (resp(401, {"success": False, "message": "invalid token"}), AuthError),
    (resp(403), AuthError),
    (resp(200, "not json"), SchemaError),
    (resp(200, env({"balances": {}}, success=False)), SchemaError),
    (resp(302, "", {"Location": "https://evil.example/"}), SchemaError),
])
def test_read_never_retries_4xx_auth_or_schema_errors(response, exc_type):
    c, t, clock = make(response, (200, {}, BALANCES))
    with pytest.raises(exc_type):
        c.get_balances()
    assert len(t.calls) == 1 and clock.now() == 1_000_000.0


def test_auth_error_carries_scrubbed_server_message():
    c, _, _ = make(resp(401, {"success": False, "message": f"IP not allowed for {KEY}"}))
    with pytest.raises(AuthError, match="IP not allowed") as ei:
        c.get_balances()
    assert_no_key(all_text(ei.value))


@pytest.mark.parametrize("value,expected", [
    ("5", 5.0), ("1.5", 1.5), ("0", 0.0), ("", None), (None, None), ("abc", None), ("-1", None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
])
def test_retry_after_parser(value, expected):
    assert wx.parse_retry_after(value, 2_000_000_000.0) == expected


# =========================================================================== public data schemas
def test_depth_real_capture_parses():
    c, t, clock = make((200, {}, DEPTH_BTC))
    book = c.get_order_book("BTCUSDT")
    assert book.best_bid == D("81208.15") and book.best_ask == D("81248.55")
    assert len(book.bids) == 50 and len(book.asks) == 50 and book.ts == clock.now()
    assert all(a.price <= b.price for a, b in zip(book.asks, book.asks[1:]))
    assert all(a.price >= b.price for a, b in zip(book.bids, book.bids[1:]))
    assert t.calls[0]["url"] == "https://api.wallex.ir/v1/depth?symbol=BTCUSDT"
    tmn = WallexClient(None, public_only=True, clock=clock, transport=FakeTransport(
        (200, {}, fixture_bytes("depth_usdttmn_real.json")))).get_book("USDTTMN")
    assert tmn.best_bid == D("272479") and tmn.best_ask == D("272499")


def test_depth_documented_sample_strings_numbers_and_unsorted_levels():
    body = (b'{"success":true,"message":"x","result":{'
            b'"ask":[{"price":"21400.0000000000000000","quantity":0.005,"sum":"107"},'
            b'{"price":"21300.0000000000000000","quantity":0.005,"sum":"106.5"}],'
            b'"bid":[{"price":"20889.8100000000000000","quantity":0.001472,"sum":"30.7"},'
            b'{"price":"21100.0000000000000000","quantity":1e-06,"sum":"0.0211"},'
            b'{"price":"21000","quantity":0,"sum":"0"}]}}')
    c, _, _ = make((200, {}, body))
    book = c.get_order_book("BTCUSDT")
    assert book.best_ask == D("21300") and book.best_bid == D("21100")
    assert book.bids[0].quantity == D("0.000001") and len(book.bids) == 2   # zero level dropped


def lv(p: Any, q: Any) -> dict[str, Any]:
    return {"price": str(p), "quantity": str(q)}


@pytest.mark.parametrize("bad", [
    {"ask": [], "bid": [lv(1, 1)]},
    {"ask": [lv(100, 1)], "bid": [lv(100, 1)]},
    {"ask": [lv(99, 1)], "bid": [lv(100, 1)]},
    {"ask": [lv(101, -1)], "bid": [lv(100, 1)]},
    {"ask": [lv(101, 1)]},
    {"ask": "x", "bid": []},
    {"ask": [{"price": "1_000", "quantity": "1"}], "bid": [lv(1, 1)]},
    {"ask": [{"price": "۱۰۱", "quantity": "1"}], "bid": [lv(100, 1)]},
    {"ask": [{"price": True, "quantity": "1"}], "bid": [lv(100, 1)]},
    {"ask": [{"price": None, "quantity": "1"}], "bid": [lv(100, 1)]},
    {"ask": [{"price": "1e99999", "quantity": "1"}], "bid": [lv(100, 1)]},
    {"ask": [{"price": "1e9999", "quantity": "1"}], "bid": [lv(100, 1)]},
    {"ask": [{"price": " 101", "quantity": "1"}], "bid": [lv(100, 1)]},
    {"ask": [{"quantity": "1"}], "bid": [lv(100, 1)]},
    {"ask": [5], "bid": [lv(100, 1)]},
    [],
])
def test_depth_schema_violations(bad):
    with pytest.raises(SchemaError):
        wx.parse_depth("BTCUSDT", bad, 0.0)


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_json_constants_are_rejected(token):
    body = (b'{"success":true,"result":{"ask":[{"price":' + token +
            b',"quantity":1}],"bid":[{"price":1,"quantity":1}]}}')
    c, _, _ = make((200, {}, body))
    with pytest.raises(SchemaError):
        c.get_order_book("BTCUSDT")


def test_depth_symbol_is_validated_before_io():
    c, t, _ = make()
    for bad in ("ETHUSDT", "btcusdt", "BTCUSDT&x=1", None):
        with pytest.raises(ValueError):
            c.get_order_book(bad)
    assert t.calls == []


def test_markets_real_capture():
    c, t, _ = make((200, {}, fixture_bytes("markets_real_2026-10-22.json")))
    rules = c.get_markets()
    assert rules == {
        "BTCUSDT": MarketRules("BTCUSDT", "BTC", "USDT", 8, 2, D("1e-8"), D("1")),
        "USDTTMN": MarketRules("USDTTMN", "USDT", "TMN", 2, 0, D("0.01"), D("50000")),
    }
    assert "X-API-Key" not in t.calls[0]["headers"]


def _markets(mutate) -> Any:
    doc = fixture_json("markets_real_2026-10-22.json")
    mutate(doc["result"]["symbols"])
    return doc["result"]


@pytest.mark.parametrize("mutate", [
    lambda m: m["BTCUSDT"].update(stepSize=-1),
    lambda m: m["BTCUSDT"].update(stepSize=19),
    lambda m: m["BTCUSDT"].update(stepSize=D("1E-8")),      # an increment, not a decimal count
    lambda m: m["BTCUSDT"].update(tickSize="two"),
    lambda m: m["BTCUSDT"].update(stepSize=True),
    lambda m: m["BTCUSDT"].update(minQty=0),
    lambda m: m["BTCUSDT"].update(minQty="1e-9"),           # finer than stepSize
    lambda m: m["BTCUSDT"].update(minNotional=-1),
    lambda m: m["BTCUSDT"].update(minQty=1e-08),            # a float never reaches the parser
    lambda m: m["BTCUSDT"].update(symbol="ETHUSDT"),
    lambda m: m["BTCUSDT"].update(baseAsset="ETH"),
    lambda m: m["BTCUSDT"].update(maxQty=D("1E-9")),
    lambda m: m.pop("USDTTMN"),
    lambda m: m["BTCUSDT"].pop("tickSize"),
    lambda m: m.__setitem__("BTCUSDT", "x"),
])
def test_market_schema_violations(mutate):
    with pytest.raises(SchemaError):
        wx.parse_markets(_markets(mutate))


def test_market_tolerances():
    rules = wx.parse_markets(_markets(lambda m: m["BTCUSDT"].update(
        stepSize="8", tickSize=D("2.0"), maxQty="100", newField=[1])))
    assert rules["BTCUSDT"].qty_decimals == 8 and rules["BTCUSDT"].max_qty == D("100")
    assert wx.parse_markets(_markets(lambda m: m.update(ETHUSDT="garbage")))  # others ignored


def test_balances_documented_sample():
    c, t, _ = make((200, {}, BALANCES))
    bals = c.get_balances()
    assert bals == {"TMN": Balance("TMN", D("10000000"), D("0")),
                    "USDT": Balance("USDT", D("10"), D("0"))}
    assert t.calls[0]["url"] == "https://api.wallex.ir/v1/account/balances"


SECRET_AMOUNT = "987654321.123"


@pytest.mark.parametrize("entry", [
    {"asset": "BTC", "value": f"-{SECRET_AMOUNT}", "locked": "0"},
    {"asset": "BTC", "value": SECRET_AMOUNT},
    {"asset": "BTC", "value": None, "locked": SECRET_AMOUNT},
    {"asset": "ETH", "value": SECRET_AMOUNT, "locked": "0"},
    {"asset": "BTC", "value": SECRET_AMOUNT + "x", "locked": "0"},
    {"asset": "BTC", "value": SECRET_AMOUNT, "locked": "NaN"},
    "not-an-object",
])
def test_balance_schema_violations_never_echo_values(entry):
    result = {"balances": {"BTC": entry}}
    with pytest.raises(SchemaError) as ei:
        wx.parse_balances(result)
    assert SECRET_AMOUNT not in str(ei.value)


@pytest.mark.parametrize("result", [{"balances": []}, {"balances": None}, {}, [],
                                    {"balances": {"": {"value": "1", "locked": "0"}}},
                                    {"balances": {"B TC": {"value": "1", "locked": "0"}}}])
def test_balance_container_violations(result):
    with pytest.raises(SchemaError):
        wx.parse_balances(result)


def test_balances_accept_numbers_and_missing_optional_fields():
    bals = wx.parse_balances({"balances": {"BTC": {"value": D("0.5"), "locked": 0}}})
    assert bals["BTC"] == Balance("BTC", D("0.5"), D("0"))


def test_fee_mapping_documented_sample_and_missing_symbol_omitted():
    c, t, _ = make((200, {}, fixture_bytes("wallex_private_fee_documented_sample.json")))
    fees = c.get_fees()
    assert fees == {"BTCUSDT": FeeRates("BTCUSDT", D("0.002"), D("0.002"))}   # USDTTMN unknown
    assert t.calls[0]["url"] == "https://api.wallex.ir/v1/account/fee"


def test_fee_list_shape():
    result = [{"symbol": "BTCUSDT", "makerFeeRate": "0.002", "takerFeeRate": "0.0035"},
              {"symbol": "USDTTMN", "makerFeeRate": D("0.001"), "takerFeeRate": 0},
              {"symbol": "ETHUSDT", "makerFeeRate": "junk"}]
    fees = wx.parse_fees(result)
    assert fees["BTCUSDT"].taker == D("0.0035") and fees["USDTTMN"].taker == D("0")
    assert wx.fee_entries(result)[0] == "list"


@pytest.mark.parametrize("result", [
    "x", 5, None, True,
    [{"makerFeeRate": "0.1"}], [["BTCUSDT"]],
    [{"symbol": "BTCUSDT", "makerFeeRate": "0.002", "takerFeeRate": "0.002"}] * 2,
    {"BTCUSDT": {"makerFeeRate": "-0.001", "takerFeeRate": "0.002"}},
    {"BTCUSDT": {"makerFeeRate": "0.002", "takerFeeRate": "0.5"}},
    {"BTCUSDT": {"takerFeeRate": "0.002"}},
    {"BTCUSDT": "0.002"},
    {"BTCUSDT": {"symbol": "USDTTMN", "makerFeeRate": "0.002", "takerFeeRate": "0.002"}},
])
def test_fee_schema_violations(result):
    with pytest.raises(SchemaError):
        wx.parse_fees(result)


def test_candles_real_capture_and_query():
    c, t, _ = make((200, {}, fixture_bytes("udf_btcusdt_1m_real.json")))
    candles = c.get_candles("BTCUSDT", "1", 1792668060, 1792690000)
    assert len(candles) == 359 and candles[0].ts == 1792668060
    assert candles[0].close == D("81933.89") and candles[0].volume == 0
    assert all(a.ts < b.ts for a, b in zip(candles, candles[1:]))
    assert t.calls[0]["url"] == ("https://api.wallex.ir/v1/udf/history?"
                                 "from=1792668060&resolution=1&symbol=BTCUSDT&to=1792690000")


def test_candles_no_data_and_unsorted_input():
    c, _, _ = make(resp(200, {"s": "no_data"}))
    assert c.get_candles("USDTTMN", "60", 0, 10) == []
    doc = {"s": "ok", "t": [120, 60], "o": ["2", "1"], "h": ["2", "1"], "l": ["2", "1"],
           "c": ["2", "1"], "v": ["0", "0"]}
    assert [x.ts for x in wx.parse_candles(doc)] == [60, 120]


@pytest.mark.parametrize("doc", [
    {"s": "error", "errmsg": "x"}, {"t": [1]}, [], "ok",
    {"s": "ok", "t": [1], "o": ["1"], "h": ["1"], "l": ["1"], "c": ["1"]},
    {"s": "ok", "t": [1, 2], "o": ["1"], "h": ["1"], "l": ["1"], "c": ["1"], "v": ["1"]},
    {"s": "ok", "t": [1], "o": ["1"], "h": ["0.5"], "l": ["1"], "c": ["1"], "v": ["1"]},
    {"s": "ok", "t": [1, 1], "o": ["1", "1"], "h": ["1", "1"], "l": ["1", "1"], "c": ["1", "1"],
     "v": ["1", "1"]},
    {"s": "ok", "t": [1], "o": ["0"], "h": ["1"], "l": ["0"], "c": ["1"], "v": ["1"]},
    {"s": "ok", "t": ["x"], "o": ["1"], "h": ["1"], "l": ["1"], "c": ["1"], "v": ["1"]},
    {"s": "ok", "t": [1], "o": ["1"], "h": ["1"], "l": ["1"], "c": ["1"], "v": ["-1"]},
])
def test_candle_schema_violations(doc):
    with pytest.raises(SchemaError):
        wx.parse_candles(doc)


@pytest.mark.parametrize("args", [
    ("BTCUSDT", "5", 0, 10), ("BTCUSDT", "D", 0, 10), ("BTCUSDT", True, 0, 10),
    ("BTCUSDT", "1", 10, 0), ("BTCUSDT", "1", -1, 10), ("BTCUSDT", "1", 0.5, 10),
    ("ETHUSDT", "1", 0, 10), ("BTCUSDT", "1", 0, True),
])
def test_candle_arguments_are_validated_before_io(args):
    c, t, _ = make()
    with pytest.raises(ValueError):
        c.get_candles(*args)
    assert t.calls == []


def test_numeric_resolution_is_accepted():
    c, t, _ = make(resp(200, {"s": "no_data"}))
    assert c.get_candles("BTCUSDT", 60, 0, 10) == []        # type: ignore[arg-type]
    assert "resolution=60" in t.calls[0]["url"]


# =========================================================================== token bucket
def test_token_bucket_burst_then_steady_rate_on_sim_clock():
    clock = SimClock(0.0)
    b = TokenBucket(2.0, clock=clock)
    assert b.acquire() == 0 and b.acquire() == 0 and clock.now() == 0.0
    assert b.acquire() == pytest.approx(0.5) and clock.now() == pytest.approx(0.5)
    assert b.acquire() == pytest.approx(0.5) and clock.now() == pytest.approx(1.0)
    clock.advance(10.0)                       # idle time refills only up to capacity
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()


def test_token_bucket_never_hangs_on_a_clock_that_does_not_advance():
    class FrozenClock(SimClock):
        def sleep(self, seconds: float) -> None:
            self.slept = getattr(self, "slept", 0.0) + seconds

    clock = FrozenClock(0.0)
    b = TokenBucket(1.0, clock=clock)
    for _ in range(5):
        b.acquire()
    assert clock.slept == pytest.approx(1 + 2 + 3 + 4)


@pytest.mark.parametrize("rate,capacity", [(0, None), (-1, None), (float("nan"), None),
                                           (float("inf"), None), (True, None), (1.0, 0.5)])
def test_token_bucket_validation(rate, capacity):
    with pytest.raises(ValueError):
        TokenBucket(rate, capacity, clock=SimClock())


def test_token_bucket_acquire_bounds():
    b = TokenBucket(2.0, clock=SimClock())
    for n in (0, -1, 3):
        with pytest.raises(ValueError):
            b.acquire(n)


# =========================================================================== UrllibTransport
class FakeHTTPResponse:
    def __init__(self, status=200, headers=None, body=b"", fail_after=None):
        self.status = status
        self.headers = headers or {}
        self._buf = io.BytesIO(body)
        self.fp = self._buf
        self.closed = False
        self._fail_after = fail_after
        self._reads = 0

    def read(self, n=-1):
        self._reads += 1
        if self._fail_after is not None and self._reads > self._fail_after:
            raise http.client.IncompleteRead(b"")
        return self._buf.read(n)

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_default_transport_verifies_tls_and_never_redirects_or_uses_env_proxies(monkeypatch):
    # Someone enabling http.client wire dumps globally must not make the key appear on stdout.
    monkeypatch.setattr(http.client.HTTPSConnection, "debuglevel", 1)
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    tr = UrllibTransport()
    assert tr._ctx.verify_mode == ssl.CERT_REQUIRED and tr._ctx.check_hostname
    assert tr._ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    handlers = tr._opener.handlers
    assert any(isinstance(h, wx._NoRedirect) for h in handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers)
    https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert https and all(h._context is tr._ctx for h in https)
    assert all(h._debuglevel == 0 for h in https)     # no wire dump (it would print the key)
    assert all(not h.proxies for h in handlers if isinstance(h, urllib.request.ProxyHandler))
    assert wx._NoRedirect().redirect_request(None, None, 302, "Found", {},
                                             "https://evil.example/") is None


def test_transport_returns_status_headers_body_and_closes():
    r = FakeHTTPResponse(200, {"Content-Type": "application/json"}, b'{"a":1}')
    tr = UrllibTransport(opener=FakeOpener(r))
    status, headers, body = tr.request("GET", "https://api.wallex.ir/v1/markets",
                                       {"X-API-Key": KEY, "User-Agent": wx.USER_AGENT}, None, 5.0)
    assert (status, headers["content-type"], body) == (200, "application/json", b'{"a":1}')
    assert r.closed
    sent, timeout = tr._opener.requests[0]
    assert timeout == 5.0 and sent.get_method() == "GET"
    assert sent.get_header("X-api-key") == KEY and sent.get_header("User-agent") == wx.USER_AGENT


@pytest.mark.parametrize("code", [302, 307, 401, 422, 500])
def test_transport_turns_http_errors_and_refused_redirects_into_responses(code):
    err = urllib.error.HTTPError("https://api.wallex.ir/x", code, "msg",
                                 {"Location": "https://evil.example/"}, io.BytesIO(b"body"))
    tr = UrllibTransport(opener=FakeOpener(err))
    status, headers, body = tr.request("POST", "https://api.wallex.ir/x", {}, b"{}", 5.0)
    assert (status, body) == (code, b"body") and headers["location"] == "https://evil.example/"


def test_transport_http_error_without_body():
    err = urllib.error.HTTPError("https://api.wallex.ir/x", 500, "msg", {}, None)
    tr = UrllibTransport(opener=FakeOpener(err))
    assert tr.request("GET", "https://api.wallex.ir/x", {}, None, 5.0) == (500, {}, b"")


def test_transport_caps_the_body_and_client_rejects_it():
    tr = UrllibTransport(max_response_bytes=1024,
                         opener=FakeOpener(FakeHTTPResponse(200, {}, b"x" * 50_000)))
    _, _, body = tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0)
    assert len(body) == 1025
    c = WallexClient(None, public_only=True, max_response_bytes=1024, transport=tr,
                     clock=SimClock())
    with pytest.raises(SchemaError, match="exceeds"):
        c.get_markets()


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("dns failure"), socket.timeout("timed out"),
    ConnectionResetError("reset"), http.client.RemoteDisconnected("gone"),
    ssl.SSLCertVerificationError("certificate verify failed"),
    ValueError(f"Invalid header value {KEY!r}"),
])
def test_transport_failures_become_transport_errors_without_header_values(exc):
    tr = UrllibTransport(opener=FakeOpener(exc))
    with pytest.raises(TransportError) as ei:
        tr.request("POST", "https://api.wallex.ir/v1/account/orders", {"X-API-Key": KEY}, b"{}", 5.0)
    assert_no_key(str(ei.value))
    assert ei.value.__context__ is None


def test_transport_read_failure_midway():
    tr = UrllibTransport(opener=FakeOpener(FakeHTTPResponse(200, {}, b"x" * 200_000,
                                                            fail_after=1)))
    with pytest.raises(TransportError):
        tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0)


def test_transport_total_read_deadline(monkeypatch):
    ticks = iter([0.0, 0.0] + [1000.0] * 1000)        # deadline base, first chunk, then far later
    monkeypatch.setattr(wx.time, "monotonic", lambda: next(ticks))
    tr = UrllibTransport(opener=FakeOpener(FakeHTTPResponse(200, {}, b"x" * 200_000)))
    with pytest.raises(TransportError, match="deadline"):
        tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0)


class _FakeSock:
    def __init__(self):
        self.timeouts = []
        self.shutdowns = 0

    def settimeout(self, t):
        self.timeouts.append(t)


class _DripResponse:
    """Models http.client semantics on a peer that drips ONE byte every ``step`` seconds:
    ``read(n)`` blocks until n bytes / EOF (the old code path), ``read1(n)`` returns after one
    recv. Time is a fake monotonic clock advanced by each recv."""

    def __init__(self, body: bytes, clock: list, step: float):
        self.status, self.headers = 200, {}
        self._body, self._clock, self._step = body, clock, step
        self.sock = _FakeSock()
        self.fp = type("BR", (), {"raw": type("SIO", (), {"_sock": self.sock})()})()

    def _recv_one(self) -> bytes:
        if not self._body:
            return b""
        self._clock[0] += self._step
        b, self._body = self._body[:1], self._body[1:]
        return b

    def read1(self, n=-1):
        return self._recv_one()

    def read(self, n=-1):                     # blocking read: drains everything, however long it takes
        out = b""
        while True:
            b = self._recv_one()
            if not b:
                return out
            out += b

    def close(self):
        pass


def test_transport_bounds_the_total_read_time_against_a_dripping_peer(monkeypatch):
    """L2: each byte arrives before the per-socket timeout, so only a TOTAL bound stops it: one
    read1 per recv, the socket timeout set to the time left before each, the deadline checked."""
    clock = [0.0]
    monkeypatch.setattr(wx.time, "monotonic", lambda: clock[0])
    resp = _DripResponse(b" " * 1000 + b'{"success":true}', clock, step=0.6)
    tr = UrllibTransport(opener=FakeOpener(resp))
    with pytest.raises(TransportError, match="deadline"):
        tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0)
    assert clock[0] <= 5.0 + 0.6                       # stopped within one drip of the deadline
    assert resp.sock.timeouts and all(0 < t <= 5.0 for t in resp.sock.timeouts)
    assert resp.sock.timeouts == sorted(resp.sock.timeouts, reverse=True)   # always the time LEFT


def _local_http_response(head: bytes, drip: bytes, interval: float):
    """A real http.client response over a local socketpair (no network); the peer sends ``head``
    at once and then ``drip`` one byte every ``interval`` seconds from a thread."""
    import threading
    import time as _t
    a, b = socket.socketpair()

    def peer():
        try:
            b.sendall(head)
            for i in range(len(drip)):
                _t.sleep(interval)
                b.sendall(drip[i:i + 1])
        except OSError:
            pass
    threading.Thread(target=peer, daemon=True).start()
    resp = http.client.HTTPResponse(a, method="GET")
    resp.begin()
    return resp, a, b


@pytest.mark.parametrize("chunked", [False, True])
def test_transport_deadline_on_a_real_socket_dripping_body_or_chunk_size(chunked):
    """L2 on real http.client objects: a Content-Length body dripped byte by byte, and a chunked
    body whose chunk-size LINE is dripped (read inside http.client's readline: only the watchdog
    can bound that). Either way the read ends at the deadline with a TransportError."""
    import time as _t
    if chunked:
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        drip = b"0" * 200 + b"5\r\nhello\r\n0\r\n\r\n"
    else:
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n"
        drip = b" " * 200
    resp, a, b = _local_http_response(head, drip, interval=0.02)
    tr = UrllibTransport(opener=FakeOpener(resp))
    t0 = _t.monotonic()
    try:
        with pytest.raises(TransportError, match="deadline|reading"):
            tr._read_bounded(resp, _t.monotonic() + 0.4)
    finally:
        a.close()
        b.close()
    assert _t.monotonic() - t0 < 2.0                   # the peer would need ~4.3 s


def test_transport_reads_a_normal_real_response_completely():
    import time as _t
    body = b'{"success":true,"message":"ok","result":{}}'
    resp, a, b = _local_http_response(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body,
                                      b"", interval=0.0)
    try:
        assert UrllibTransport(opener=FakeOpener(resp))._read_bounded(resp, _t.monotonic() + 5) == body
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("key_len", [48, 512])
def test_no_key_prefix_survives_truncation_at_any_boundary(key_len):
    """I1 / R8: the key is scrubbed from the WHOLE text before it is cut; a key straddling the old
    16 000-char window used to leave its prefix in the kept message."""
    import random
    rnd = random.Random(key_len)
    key = "".join(rnd.choice("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789") for _ in range(key_len))
    body = (key * (16000 // key_len)).encode()
    body += b"." * max(0, 16000 - len(body) - key_len // 4) + key.encode() + b" tail"
    prefixes = {key[:n] for n in (8, 16, key_len // 4)}

    class Tr:
        def request(self, *a, **k):
            return 500, {}, body

    c = WallexClient(key, transport=Tr(), clock=SimClock(), max_read_retries=0)
    with pytest.raises(ServerError) as ei:
        c.create_order(OrderRequest("wa-1", "BTCUSDT", Side.BUY, D("1"), D("1")))
    for text in (ei.value.body, str(ei.value)):
        assert not any(p in text for p in prefixes), "key prefix survived the truncation"
    for status in (401, 422, 404):
        class Tr2:
            def request(self, *a, **k):
                return status, {}, json.dumps({"message": body.decode(), "result": {"error_code": 1006}}).encode()
        c2 = WallexClient(key, transport=Tr2(), clock=SimClock(), max_read_retries=0)
        with pytest.raises(ExchangeError) as ei:
            c2.create_order(OrderRequest("wa-2", "BTCUSDT", Side.BUY, D("1"), D("1")))
        assert not any(p in str(ei.value) for p in prefixes)
        assert not any(p in str(getattr(ei.value, "message", "")) for p in prefixes)


@pytest.mark.parametrize("url", ["http://api.wallex.ir/v1/markets", "file:///etc/passwd",
                                 "ftp://api.wallex.ir/", None])
def test_transport_refuses_non_https(url):
    opener = FakeOpener(FakeHTTPResponse())
    with pytest.raises(TransportError):
        UrllibTransport(opener=opener).request("GET", url, {}, None, 5.0)
    assert opener.requests == []


def test_client_end_to_end_through_urllib_transport_with_fake_opener():
    r = FakeHTTPResponse(200, {"Content-Type": "application/json"}, BALANCES)
    opener = FakeOpener(r)
    c = WallexClient(KEY, transport=UrllibTransport(opener=opener), clock=SimClock())
    assert set(c.get_balances()) == {"TMN", "USDT"}
    sent, timeout = opener.requests[0]
    assert sent.full_url == "https://api.wallex.ir/v1/account/balances" and timeout == 10.0
    assert sent.get_header("X-api-key") == KEY
