"""Wallex REST adapter: ``interfaces.Exchange`` (+ ``get_book`` so it is a ``BookSource`` too).

Safety properties (each unit-tested in tests/test_wallex_client.py):

* Every request is checked against an explicit ``(method, path)`` allow-list BEFORE any I/O, and
  withdrawal / transfer / deposit / profile / bank / OTC paths are additionally deny-listed.
* The API key travels only in ``X-API-Key``, only on private routes, only to ``api.wallex.ir`` over
  verified TLS; it is scrubbed from every exception message, stored body and log line.
* Writes (POST/DELETE) are sent exactly once - never retried here. ``RejectedError`` is raised only
  for 4xx answers that prove the order was refused; every other non-success outcome of a POST is
  an *unknown outcome* (``TransportError`` / ``ServerError`` / ``SchemaError``).
* Every response is validated strictly: Decimal numbers (strings or JSON numbers), required keys,
  ranges; ``SchemaError`` messages carry a field path but never field values.
"""
from __future__ import annotations

import calendar
import email.utils
import functools
import http.client
import json
import logging
import math
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, TypeVar

from .. import __version__
from ..clock import Clock, RealClock
from ..errors import (AuthError, ConfigError, ForbiddenEndpointError, OrderNotFound, RateLimitError,
                      RejectedError, SchemaError, ServerError, TransportError)
from ..types import (Balance, Candle, FeeRates, MarketRules, OrderBook, OrderInfo, OrderRequest,
                     OrderStatus, Side, Trade, plain, quantize_down, validate_symbol)

__all__ = [
    "AmbiguousResponseError", "CANDLE_RESOLUTIONS", "DEFAULT_BASE_URL", "DEFAULT_SYMBOLS",
    "MAX_RESPONSE_BYTES", "ORDER_ID_RE", "PRIVATE_API_HOSTS", "TokenBucket", "Transport",
    "USER_AGENT", "UrllibTransport", "WallexClient", "decode_json_bytes", "fee_entries",
    "map_order_status", "parse_balances", "parse_candles", "parse_depth", "parse_fees",
    "parse_iso8601", "parse_markets", "parse_open_orders", "parse_order", "parse_retry_after",
    "parse_trades",
]

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.wallex.ir"
PRIVATE_API_HOSTS = frozenset({"api.wallex.ir"})  # the key is never sent to any other host
USER_AGENT = f"wallex-agent/{__version__}"
DEFAULT_SYMBOLS = ("BTCUSDT", "USDTTMN")
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
EXCHANGE_MAX_RPS = 100.0         # documented global limit
EXCHANGE_MAX_ORDER_RPS = 10.0    # documented limit of POST /v1/account/orders
MAX_RETRY_AFTER_S = 10.0         # longer server back-off requests are surfaced, not slept through
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 4.0
MAX_FEE_RATE = Decimal("0.1")
CANDLE_RESOLUTIONS = frozenset({"1", "60", "180", "360", "720", "1D"})

ORDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")    # client_id and every id placed in a URL
_KEY_RE = re.compile(r"[A-Za-z0-9._~+/=|:-]{16,512}")
_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_-]+)+")
_PARAM_VALUE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SYMBOL_RE = re.compile(r"[A-Za-z0-9]{2,32}")
_ASSET_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
_TOKEN_RE = re.compile(r"[\x21-\x7e]+")              # printable ASCII, no spaces
_LINE_RE = re.compile(r"[\x20-\x7e]+")               # printable ASCII incl. spaces
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_NUM_STR_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?")
_INT_STR_RE = re.compile(r"-?[0-9]{1,18}")
_HOST_RE = re.compile(r"[a-z0-9.-]{1,253}")
_KEY_TEXT_RE = re.compile(r"(?i)((?:x[-_ ]?)?api[-_ ]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+")
_MAX_ABS_EXPONENT = 40
_TEXT_WINDOW = 16_000      # scrub this much text BEFORE truncating, so no partial key survives

# Path deny-list, checked before the allow-list: substrings anywhere, or whole path segments.
_DENY_SUBSTRINGS = ("withdraw", "transfer", "deposit", "wallgate")
_DENY_SEGMENTS = frozenset({
    "otc", "profile", "card-numbers", "card-number", "ibans", "iban", "kyc", "invite",
    "api-keys", "addresses", "address", "wallets", "wallet",
})
_AMBIGUOUS_4XX = frozenset({408, 409})   # timeout / conflict (e.g. duplicate id): order may exist
_ORDER_BODY_KEYS = frozenset({"symbol", "type", "side", "price", "quantity", "client_id"})


# --------------------------------------------------------------------------- errors
class AmbiguousResponseError(ServerError):
    """A write got an HTTP status that proves neither acceptance nor refusal (1xx/3xx/408/409/...).

    Subclass of ``ServerError`` so callers treat it as an UNKNOWN outcome (reconcile, never resend).
    """

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(status, body)
        self.args = (f"HTTP {status} (ambiguous answer to a write: outcome unknown)",)


# --------------------------------------------------------------------------- secret handling
class _Secret:
    """Holds the API key; never reveals it through repr/str/format/pickle/copy."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<redacted>"

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("the API key holder cannot be pickled or copied")

    def __reduce__(self) -> Any:
        raise TypeError("the API key holder cannot be pickled or copied")


class _Scrubber:
    """Removes the key (raw, JSON-escaped, URL-encoded) and any ``X-API-Key: ...`` text."""

    def __init__(self, secret: str | None) -> None:
        variants: set[str] = set()
        if secret:
            js = json.dumps(secret)[1:-1]
            variants = {secret, js, js.replace("/", "\\/"), secret.replace("/", "\\/"),
                        urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret),
                        secret.replace("+", " ")}         # echoed after form-decoding
        self._variants = tuple(sorted((v for v in variants if v), key=len, reverse=True))

    def __call__(self, text: Any) -> str:
        s = text if isinstance(text, str) else _safe_str(text)
        for v in self._variants:
            if v in s:
                s = s.replace(v, "***")
        return _KEY_TEXT_RE.sub(r"\1***", s)

    def contains_secret(self, text: str) -> bool:
        return any(v in text for v in self._variants)

    def __repr__(self) -> str:
        return "<scrubber>"

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("the key scrubber cannot be pickled or copied")


class _RedactedHeaders(dict):  # type: ignore[type-arg]
    """Request headers whose repr/str hide the key (they may surface in debug output)."""

    def __repr__(self) -> str:
        return repr({k: ("<redacted>" if str(k).lower() == "x-api-key" else v)
                     for k, v in self.items()})

    __str__ = __repr__


def _safe_str(obj: Any) -> str:
    try:
        return str(obj)
    except Exception:  # noqa: BLE001 - a broken __str__ must not mask the real failure
        return f"<unprintable {type(obj).__name__}>"


# --------------------------------------------------------------------------- transport
class Transport(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                timeout: float) -> tuple[int, Mapping[str, str], bytes]:
        """ONE HTTP exchange; returns (status, headers, body) for EVERY HTTP status (no raise on
        4xx/5xx). Raises ``TransportError`` on network failure. Must not retry or redirect."""
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a 3xx reaches the client as-is and is treated as an error."""

    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any,
                         newurl: Any) -> None:
        return None


class UrllibTransport:
    """Default stdlib transport: verified TLS >= 1.2, no redirects, no environment proxies (unless
    asked), no retries, response body capped (reads at most ``max_response_bytes + 1`` bytes so
    the client can detect the overflow), and a total deadline of ``timeout`` for reading."""

    def __init__(self, *, max_response_bytes: int = MAX_RESPONSE_BYTES,
                 use_env_proxy: bool = False, opener: Any = None) -> None:
        ctx = ssl.create_default_context()
        ctx.minimum_version = max(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:  # pragma: no cover
            raise ConfigError("TLS certificate and hostname verification must be enabled")
        self._ctx = ctx
        self._max = int(max_response_bytes)
        if opener is None:   # ``opener`` exists only so tests can run without any network
            opener = urllib.request.build_opener(   # debuglevel pinned: a wire dump shows the key
                urllib.request.ProxyHandler(None if use_env_proxy else {}),
                _NoRedirect(), urllib.request.HTTPSHandler(debuglevel=0, context=ctx))
            opener.addheaders = []
        self._opener = opener

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                timeout: float) -> tuple[int, Mapping[str, str], bytes]:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise TransportError("refusing a non-HTTPS URL")
        failure: str | None = None
        resp: Any = None
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            resp = self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as err:   # any non-2xx (incl. refused redirects)
            resp = err
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            failure = f"{type(exc).__name__}: {_safe_str(exc)}"
        except ValueError:                        # message may echo a header value: keep it out
            failure = "invalid request (ValueError)"
        if failure is not None:
            raise TransportError(failure)
        try:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            raw_headers = getattr(resp, "headers", None)
            hdrs = {str(k).lower(): str(v) for k, v in raw_headers.items()} if raw_headers else {}
            data = self._read_bounded(resp, time.monotonic() + timeout)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        return int(status), hdrs, data

    def _read_bounded(self, resp: Any, deadline: float) -> bytes:
        if getattr(resp, "fp", True) is None:    # HTTPError without a body
            return b""
        chunks: list[bytes] = []
        total = 0
        failure: str | None = None
        try:
            while total <= self._max:
                if time.monotonic() > deadline:
                    failure = "response read deadline exceeded"
                    break
                chunk = resp.read(min(65536, self._max + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except (http.client.HTTPException, OSError, ValueError) as exc:
            failure = f"{type(exc).__name__} while reading the response"
        if failure is not None:
            raise TransportError(failure)
        return b"".join(chunks)


# --------------------------------------------------------------------------- rate limiting
class TokenBucket:
    """Token bucket on the injected ``Clock``. ``acquire`` reserves the tokens at once (the balance
    may go negative = debt) and then sleeps off the deficit, so callers queue in arrival order."""

    def __init__(self, rate: float, capacity: float | None = None, *,
                 clock: Clock | None = None) -> None:
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) \
                or not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be a finite number > 0")
        cap = max(1.0, float(rate)) if capacity is None else capacity
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) \
                or not math.isfinite(cap) or cap < 1:
            raise ValueError("capacity must be a finite number >= 1")
        self.rate = float(rate)
        self.capacity = float(cap)
        self._clock: Clock = clock if clock is not None else RealClock()
        self._tokens = self.capacity
        self._last = self._clock.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        if now > self._last:
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now

    def try_acquire(self, n: float = 1.0) -> bool:
        with self._lock:
            self._refill(self._clock.monotonic())
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def acquire(self, n: float = 1.0) -> float:
        """Blocks (via ``clock.sleep``) until ``n`` tokens are available; returns seconds waited."""
        if not 0 < n <= self.capacity:
            raise ValueError(f"n must be in (0, {self.capacity}]")
        with self._lock:
            self._refill(self._clock.monotonic())
            self._tokens -= n
            wait = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if wait > 0:
            self._clock.sleep(wait)
        return wait


# --------------------------------------------------------------------------- routes
class _Route:
    __slots__ = ("name", "method", "pattern", "private", "params", "required", "envelope")

    def __init__(self, name: str, method: str, pattern: str, private: bool,
                 params: Iterable[str] = (), required: Iterable[str] = (),
                 envelope: bool = True) -> None:
        self.name = name
        self.method = method
        self.pattern = re.compile(pattern)
        self.private = private
        self.params = frozenset(params)
        self.required = frozenset(required)
        self.envelope = envelope

    @property
    def write(self) -> bool:
        return self.method != "GET"


_ID = ORDER_ID_RE.pattern
_ROUTES: tuple[_Route, ...] = (
    _Route("markets", "GET", r"/v1/markets", False),
    _Route("depth", "GET", r"/v1/depth", False, {"symbol"}, {"symbol"}),
    _Route("public_trades", "GET", r"/v1/trades", False, {"symbol"}, {"symbol"}),
    _Route("udf_history", "GET", r"/v1/udf/history", False, {"symbol", "resolution", "from", "to"},
           {"symbol", "resolution", "from", "to"}, envelope=False),
    _Route("balances", "GET", r"/v1/account/balances", True),
    _Route("fee", "GET", r"/v1/account/fee", True),
    _Route("create_order", "POST", r"/v1/account/orders", True),
    _Route("get_order", "GET", rf"/v1/account/orders/{_ID}", True),
    _Route("cancel_order", "DELETE", r"/v1/account/orders", True, {"clientOrderId"},
           {"clientOrderId"}),
    _Route("open_orders", "GET", r"/v1/account/openOrders", True, {"symbol"}),
    _Route("account_trades", "GET", r"/v1/account/trades", True, {"symbol", "side"}),
)


class _Prepared:
    __slots__ = ("route", "method", "path", "url", "headers", "body")

    def __init__(self, route: _Route, method: str, path: str, url: str,
                 headers: _RedactedHeaders, body: bytes | None) -> None:
        self.route, self.method, self.path = route, method, path
        self.url, self.headers, self.body = url, headers, body

    def __repr__(self) -> str:
        return f"<request {self.method} {self.path}>"


class _Response:
    __slots__ = ("status", "headers", "raw")

    def __init__(self, status: int, headers: dict[str, str], raw: bytes) -> None:
        self.status, self.headers, self.raw = status, headers, raw


# --------------------------------------------------------------------------- client
_F = TypeVar("_F", bound=Callable[..., Any])


def _public(fn: _F) -> _F:
    """Scrubs the key from anything escaping a public method (safety net; messages are built
    scrubbed already) and re-raises it outside the ``except`` block."""

    @functools.wraps(fn)
    def wrapper(self: "WallexClient", *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged in type below
            caught = exc
        self._sanitize(caught)
        raise caught

    return wrapper  # type: ignore[return-value]


class WallexClient:
    """Wallex REST client implementing ``interfaces.Exchange`` (+ ``get_book`` for ``BookSource``).

    Extra keyword-only options beyond the contract (all default to the safe choice):
    ``read_only`` (forbid every non-GET), ``allowed_symbols`` (tradable/queryable symbols),
    ``max_response_bytes`` (response size cap).
    """

    def __init__(self, api_key: str | None, *, base_url: str = DEFAULT_BASE_URL,
                 transport: Transport | None = None, clock: Clock | None = None,
                 timeout: float = 10.0, max_rps: float = 5.0, order_rps: float = 2.0,
                 public_only: bool = False, max_read_retries: int = 3, read_only: bool = False,
                 allowed_symbols: Sequence[str] = DEFAULT_SYMBOLS,
                 max_response_bytes: int = MAX_RESPONSE_BYTES) -> None:
        if api_key is not None and not isinstance(api_key, str):
            raise ConfigError("api_key must be a string or None")
        self._public_only = bool(public_only)
        self._read_only = bool(read_only) or self._public_only
        key = None if (self._public_only or not api_key) else api_key
        if key is not None and not _KEY_RE.fullmatch(key):
            raise ConfigError("API key has an unexpected format "
                              "(expected 16-512 characters of [A-Za-z0-9._~+/=|:-])")
        self._key: _Secret | None = _Secret(key) if key is not None else None
        self._scrub = _Scrubber(key)
        self._base_url = _validate_base_url(base_url, keyed=self._key is not None)
        self._timeout = _num_param("timeout", timeout, 0.0, 120.0)
        rps = _num_param("max_rps", max_rps, 0.0, EXCHANGE_MAX_RPS)
        orps = _num_param("order_rps", order_rps, 0.0, EXCHANGE_MAX_ORDER_RPS)
        if isinstance(max_read_retries, bool) or not isinstance(max_read_retries, int) \
                or not 0 <= max_read_retries <= 10:
            raise ConfigError("max_read_retries must be an int in [0, 10]")
        self._max_read_retries = max_read_retries
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) \
                or not 1024 <= max_response_bytes <= 64 * 1024 * 1024:
            raise ConfigError("max_response_bytes must be an int in [1024, 64 MiB]")
        self._max_response_bytes = max_response_bytes
        symbols = tuple(allowed_symbols)
        if not symbols or not all(isinstance(s, str) and re.fullmatch(r"[A-Z0-9]{2,20}", s)
                                  for s in symbols):
            raise ConfigError("allowed_symbols must be a non-empty sequence of upper-case symbols")
        self._symbols = symbols
        self._clock: Clock = clock if clock is not None else RealClock()
        self._transport: Transport = transport if transport is not None else UrllibTransport(
            max_response_bytes=max_response_bytes)
        self._bucket = TokenBucket(rps, clock=self._clock)
        self._order_bucket = TokenBucket(orps, clock=self._clock)

    # ------------------------------------------------------------------ introspection (no key)
    def __repr__(self) -> str:
        return (f"WallexClient(base_url={self._base_url!r}, public_only={self._public_only}, "
                f"read_only={self._read_only}, api_key={'<set>' if self._key else '<none>'})")

    __str__ = __repr__

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def public_only(self) -> bool:
        return self._public_only

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def has_key(self) -> bool:
        return self._key is not None

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def allowed_symbols(self) -> tuple[str, ...]:
        return self._symbols

    def scrub(self, text: Any) -> str:
        """Returns ``text`` with the key and any ``X-API-Key: ...`` value replaced by ``***``."""
        return self._scrub(text)

    def as_read_only(self) -> "WallexClient":
        """Same key, transport, clock and rate limiters, but every non-GET is forbidden."""
        clone = object.__new__(WallexClient)
        clone.__dict__.update(self.__dict__)
        clone._read_only = True
        return clone

    # ------------------------------------------------------------------ Exchange: public data
    @_public
    def get_markets(self) -> dict[str, MarketRules]:
        doc = self._read("/v1/markets")
        return parse_markets(doc["result"], self._symbols)

    @_public
    def get_order_book(self, symbol: str) -> OrderBook:
        sym = validate_symbol(symbol, self._symbols)
        doc = self._read("/v1/depth", {"symbol": sym})
        return parse_depth(sym, doc["result"], self._clock.now())

    get_book = get_order_book   # BookSource

    @_public
    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        sym = validate_symbol(symbol, self._symbols)
        if isinstance(resolution, bool) or not isinstance(resolution, (str, int)):
            raise ValueError("resolution must be a string")
        res = str(resolution)
        if res not in CANDLE_RESOLUTIONS:
            raise ValueError(f"resolution must be one of {sorted(CANDLE_RESOLUTIONS)}")
        for name, v in (("start", start), ("end", end)):
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10 ** 11:
                raise ValueError(f"{name} must be epoch seconds (int)")
        if start > end:
            raise ValueError("start must be <= end")
        doc = self._read("/v1/udf/history",
                         {"symbol": sym, "resolution": res, "from": str(start), "to": str(end)})
        return parse_candles(doc)

    # ------------------------------------------------------------------ Exchange: account reads
    @_public
    def get_balances(self) -> dict[str, Balance]:
        doc = self._read("/v1/account/balances")
        return parse_balances(doc["result"])

    @_public
    def get_fees(self) -> dict[str, FeeRates]:
        """Fee rates for the allowed symbols; a symbol missing from the answer is OMITTED (fee
        unknown => the caller must not trade it)."""
        doc = self._read("/v1/account/fee")
        return parse_fees(doc["result"], self._symbols)

    @_public
    def get_order(self, client_order_id: str) -> OrderInfo:
        cid = self._check_id(client_order_id)
        doc = self._read(f"/v1/account/orders/{cid}", not_found=True)
        result = doc["result"]
        if result is None or (isinstance(result, (dict, list)) and not result):
            raise OrderNotFound(f"GET /v1/account/orders/{cid}: empty result")
        return parse_order(result, "result")

    @_public
    def list_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        params = {} if symbol is None else {"symbol": validate_symbol(symbol, self._symbols)}
        doc = self._read("/v1/account/openOrders", params)
        return parse_open_orders(doc["result"], symbol=params.get("symbol"))

    @_public
    def get_trades(self, symbol: str | None = None, side: Side | None = None) -> list[Trade]:
        params: dict[str, str] = {}
        if symbol is not None:
            params["symbol"] = validate_symbol(symbol, self._symbols)
        want_side = None if side is None else Side(side)
        if want_side is not None:
            params["side"] = want_side.value
        doc = self._read("/v1/account/trades", params)
        return parse_trades(doc["result"], symbol=params.get("symbol"), side=want_side)

    @_public
    def get_raw(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        """GET-only escape hatch for diagnostics/probing: same allow-list, retries and envelope
        check as the typed reads; returns the decoded JSON document (Decimal numbers)."""
        return self._read(path, params)

    # ------------------------------------------------------------------ Exchange: writes
    @_public
    def create_order(self, req: OrderRequest) -> OrderInfo:
        """POSTs a LIMIT order exactly once. ``ValueError`` => invalid request, nothing was sent.
        ``RejectedError`` => definitive 4xx refusal. ``TransportError`` / ``ServerError`` /
        ``SchemaError`` (and in practice ``RateLimitError`` / ``AuthError``) => treat as UNKNOWN
        outcome: reconcile with ``get_order`` / ``list_open_orders`` / ``get_trades``."""
        body = self._order_body(req)
        prep = self._prepare("POST", "/v1/account/orders", json_body=body)
        resp = self._send_once(prep)                 # never retried
        return self._after_write(prep, resp, lambda: self._accept_order_ack(prep, resp, body))

    @_public
    def cancel_order(self, client_order_id: str) -> None:
        """Sends one DELETE. ``None`` = cancel requested (verify with ``get_order``)."""
        cid = self._check_id(client_order_id)
        prep = self._prepare("DELETE", "/v1/account/orders", {"clientOrderId": cid})
        resp = self._send_once(prep)                 # never retried
        self._after_write(prep, resp, lambda: self._confirm_cancel(prep, resp))

    def _confirm_cancel(self, prep: _Prepared, resp: _Response) -> None:
        if not resp.raw.strip():
            return None
        doc = self._decode_json(resp, f"DELETE {prep.path}")
        if not isinstance(doc, dict) or ("success" in doc and doc["success"] is not True):
            msg = doc.get("message") if isinstance(doc, dict) else None
            detail = f" (message: {msg[:_TEXT_WINDOW]})" if isinstance(msg, str) else ""
            raise SchemaError(self._scrub(
                f"DELETE {prep.path}: 2xx answer does not confirm the cancel{detail}")[:300])
        return None

    def _after_write(self, prep: _Prepared, resp: _Response, finish: Callable[[], Any]) -> Any:
        """Classifies the answer to a write that HAS BEEN SENT. Only the intended outcome types may
        escape; anything else (a bug, an odd header) becomes an unknown-outcome ``SchemaError`` so
        it can never be mistaken for "nothing was sent" (``ValueError``) or a rejection."""
        problem: str | None = None
        try:
            self._raise_for_write_status(prep, resp)
            return finish()
        except (RejectedError, AuthError, RateLimitError, ServerError, SchemaError, TransportError,
                OrderNotFound):
            raise
        except Exception as exc:  # noqa: BLE001 - see docstring
            problem = (f"{prep.method} {prep.path}: could not interpret the answer "
                       f"({type(exc).__name__})")
        raise SchemaError(f"{problem} [outcome UNKNOWN: reconcile, never resend]")

    def _check_id(self, v: Any) -> str:
        cid = _check_order_id(v)
        if self._scrub.contains_secret(cid):
            raise ValueError("refusing an order id that contains the API key")
        return cid

    # ------------------------------------------------------------------ request pipeline
    def _prepare(self, method: str, path: str, params: Mapping[str, str] | None = None,
                 json_body: dict[str, str] | None = None) -> _Prepared:
        """Allow-list, deny-list, mode and parameter checks. Raises before any I/O."""
        m = method.upper() if isinstance(method, str) else ""
        if m not in ("GET", "POST", "DELETE"):
            raise ForbiddenEndpointError(f"forbidden HTTP method {ascii(method)[:20]}")
        if not isinstance(path, str) or len(path) > 200 or not _PATH_RE.fullmatch(path):
            raise ForbiddenEndpointError(f"forbidden or malformed path {ascii(path)[:120]}")
        low = path.lower()
        if any(s in low for s in _DENY_SUBSTRINGS) \
                or any(seg in _DENY_SEGMENTS for seg in low.split("/")):
            raise ForbiddenEndpointError(f"explicitly denied endpoint: {m} {path}")
        route = next((r for r in _ROUTES if r.method == m and r.pattern.fullmatch(path)), None)
        if route is None:
            raise ForbiddenEndpointError(f"endpoint not in allow-list: {m} {path}")
        if self._public_only and (route.private or m != "GET"):
            raise ForbiddenEndpointError(f"public-only client may not call {m} {path}")
        if self._read_only and m != "GET":
            raise ForbiddenEndpointError(f"read-only client may not call {m} {path}")
        query = dict(params or {})
        if set(query) - route.params or route.required - set(query):
            raise ForbiddenEndpointError(f"query parameters not allowed for {m} {path}: "
                                         f"{sorted(map(ascii, query))[:6]}")
        for k, v in query.items():
            if not isinstance(v, str) or not _PARAM_VALUE_RE.fullmatch(v):
                raise ForbiddenEndpointError(f"bad value for query parameter {k!r} on {path}")
        body: bytes | None = None
        if route.name == "create_order":
            if not isinstance(json_body, dict) or set(json_body) != _ORDER_BODY_KEYS \
                    or not all(isinstance(v, str) for v in json_body.values()):
                raise ForbiddenEndpointError("order body does not match the allowed shape")
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        elif json_body is not None:
            raise ForbiddenEndpointError(f"{m} {path} must not carry a body")
        headers = _RedactedHeaders({"User-Agent": USER_AGENT, "Accept": "application/json",
                                    "Accept-Encoding": "identity"})
        if body is not None:
            headers["Content-Type"] = "application/json"
        if route.private:
            if self._key is None:
                raise AuthError(f"no API key configured for private endpoint {m} {path}")
            headers["X-API-Key"] = self._key.reveal()
        url = self._base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(sorted(query.items()))
        return _Prepared(route, m, path, url, headers, body)

    def _send_once(self, prep: _Prepared) -> _Response:
        """Exactly one transport call. Any transport failure => ``TransportError`` with a scrubbed
        message, raised outside the ``except`` block so no original exception is chained."""
        self._bucket.acquire()
        if prep.route.write:
            self._order_bucket.acquire()
        started = self._clock.monotonic()
        failure: str | None = None
        result: Any = None
        try:
            result = self._transport.request(prep.method, prep.url, prep.headers, prep.body,
                                             self._timeout)
        except Exception as exc:  # noqa: BLE001 - for a write every failure is "outcome unknown"
            failure = f"{type(exc).__name__}: {_safe_str(exc)[:_TEXT_WINDOW]}"
        if failure is not None:
            raise TransportError(self._scrub(
                f"{prep.method} {prep.path}: transport failure: {failure}")[:500])
        resp = self._coerce(prep, result)
        _log.debug("%s %s -> HTTP %s in %.0f ms", prep.method, self._scrub(prep.path),
                   resp.status, (self._clock.monotonic() - started) * 1000.0)
        return resp

    def _coerce(self, prep: _Prepared, result: Any) -> _Response:
        """Validates the transport's return value. The request has been SENT at this point, so any
        problem must surface as an unknown-outcome ``TransportError``, never as anything else."""
        resp: _Response | None = None
        try:
            if isinstance(result, tuple) and len(result) == 3:
                status, headers, raw = result
                if not isinstance(status, bool) and isinstance(status, int) \
                        and 100 <= status <= 999 and isinstance(headers, Mapping) \
                        and isinstance(raw, (bytes, bytearray)):
                    hdrs = {_safe_str(k).lower(): _safe_str(v) for k, v in headers.items()}
                    resp = _Response(status, hdrs, bytes(raw))
        except Exception:  # noqa: BLE001 - malformed result => unknown outcome below
            resp = None
        if resp is None:
            raise TransportError(
                f"{prep.method} {prep.path}: transport returned a malformed result")
        return resp

    def _read(self, path: str, params: Mapping[str, str] | None = None, *,
              not_found: bool = False) -> Any:
        """GET with retries on TransportError / 5xx / 429 (honouring Retry-After); never on 4xx."""
        prep = self._prepare("GET", path, params)
        attempts = self._max_read_retries + 1
        for attempt in range(attempts):
            final = attempt == attempts - 1
            try:
                resp = self._send_once(prep)
            except TransportError:
                if final:
                    raise
                self._clock.sleep(self._backoff(attempt))
                continue
            status = resp.status
            if status == 429 or 500 <= status <= 599:
                retry_after = parse_retry_after(resp.headers.get("retry-after"), self._clock.now())
                if final or (retry_after is not None and retry_after > MAX_RETRY_AFTER_S):
                    if status == 429:
                        raise RateLimitError(retry_after)
                    raise ServerError(status, self._error_text(resp.raw))
                _log.debug("GET %s: HTTP %s, retry %d", self._scrub(prep.path), status,
                           attempt + 1)
                self._clock.sleep(retry_after if retry_after is not None
                                  else self._backoff(attempt))
                continue
            return self._accept_read(prep, resp, not_found=not_found)
        raise AssertionError("unreachable")  # pragma: no cover

    def _accept_read(self, prep: _Prepared, resp: _Response, *, not_found: bool) -> Any:
        status = resp.status
        ctx = f"GET {prep.path}"
        if 200 <= status <= 299:
            doc = self._decode_json(resp, ctx)
            if prep.route.envelope:
                self._check_envelope(doc, ctx)
            return doc
        if status in (401, 403):
            raise AuthError(self._scrub(f"HTTP {status} on {ctx}: {_message_of(resp.raw)}")[:300])
        if status == 404 and not_found:   # NB: a 404 cannot tell "no such order" from "no route"
            raise OrderNotFound(self._scrub(f"HTTP 404 on {ctx}: {_message_of(resp.raw)}")[:300])
        raise SchemaError(self._scrub(
            f"unexpected HTTP {status} on {ctx}: {_message_of(resp.raw)}")[:300])

    def _raise_for_write_status(self, prep: _Prepared, resp: _Response) -> None:
        """Returns only for 2xx. ``RejectedError`` only for definitive 4xx refusals."""
        s = resp.status
        ctx = f"{prep.method} {prep.path}"
        if 200 <= s <= 299:
            return
        if s in (401, 403):
            raise AuthError(self._scrub(f"HTTP {s} on {ctx}: {_message_of(resp.raw)}")[:300])
        if s == 429:
            raise RateLimitError(parse_retry_after(resp.headers.get("retry-after"),
                                                   self._clock.now()))
        if 400 <= s <= 499 and s not in _AMBIGUOUS_4XX:
            if s == 404 and prep.route.name == "cancel_order":
                raise OrderNotFound(self._scrub(
                    f"HTTP 404 on {ctx}: {_message_of(resp.raw)}")[:300])
            message, code = _rejection_details(resp.raw)
            raise RejectedError(s, self._scrub(message)[:500], code)
        if 500 <= s <= 599:
            raise ServerError(s, self._error_text(resp.raw))
        raise AmbiguousResponseError(s, self._error_text(resp.raw))

    def _accept_order_ack(self, prep: _Prepared, resp: _Response,
                          body: Mapping[str, str]) -> OrderInfo:
        ctx = f"POST {prep.path}"
        problem: str | None = None
        info: OrderInfo | None = None
        try:
            doc = self._decode_json(resp, ctx)
            self._check_envelope(doc, ctx)
            info = parse_order(doc["result"], "result")
        except SchemaError as exc:
            problem = _safe_str(exc)
        except Exception as exc:  # noqa: BLE001 - a parsing bug after a 2xx: never "definitive"
            problem = f"{ctx}: could not interpret the 2xx answer ({type(exc).__name__})"
        if problem is None and info is not None and (
                info.symbol != body["symbol"] or info.side.value != body["side"]
                or info.type != "LIMIT"):
            problem = f"{ctx}: acknowledged order does not match the request (symbol/side/type)"
        if problem is not None or info is None:
            raise SchemaError(self._scrub(
                f"{problem} [order outcome UNKNOWN: reconcile, never resend]")[:600])
        return info

    def _order_body(self, req: OrderRequest) -> dict[str, str]:
        """Validates the request locally (``ValueError``, nothing sent) and builds the JSON body."""
        if getattr(req, "type", None) != "LIMIT":
            raise ValueError("only LIMIT orders are supported (MARKET syntax is unverified)")
        symbol = validate_symbol(getattr(req, "symbol", None),  # type: ignore[arg-type]
                                 self._symbols)
        side = Side(getattr(req, "side", None))
        price = _order_decimal(getattr(req, "price", None), "price")
        qty = _order_decimal(getattr(req, "quantity", None), "quantity")
        cid = getattr(req, "client_id", None)
        if not isinstance(cid, str) or not ORDER_ID_RE.fullmatch(cid):
            raise ValueError("client_id must be 1-64 characters of [A-Za-z0-9_-]")
        if self._scrub.contains_secret(cid):
            raise ValueError("refusing a client_id that contains the API key")
        return {"symbol": symbol, "type": "LIMIT", "side": side.value, "price": plain(price),
                "quantity": plain(qty), "client_id": cid}

    # ------------------------------------------------------------------ response helpers
    def _decode_json(self, resp: _Response, ctx: str) -> Any:
        if len(resp.raw) > self._max_response_bytes:
            raise SchemaError(f"{ctx}: response body exceeds {self._max_response_bytes} bytes")
        enc = resp.headers.get("content-encoding", "").strip().lower()
        if enc not in ("", "identity"):
            raise SchemaError(f"{ctx}: unsupported content-encoding {ascii(enc)[:30]}")
        return decode_json_bytes(resp.raw, ctx)

    def _check_envelope(self, doc: Any, ctx: str) -> None:
        if not isinstance(doc, dict):
            raise SchemaError(f"{ctx}: top-level JSON is {_jtype(doc)}, expected object")
        if doc.get("success") is not True:
            msg = doc.get("message")
            detail = f" (message: {msg[:_TEXT_WINDOW]})" if isinstance(msg, str) else ""
            raise SchemaError(self._scrub(f"{ctx}: 'success' is not true{detail}")[:300])
        if "result" not in doc:
            raise SchemaError(f"{ctx}: missing 'result'")

    def _error_text(self, raw: bytes) -> str:
        return self._scrub(raw[:_TEXT_WINDOW].decode("utf-8", errors="replace"))[:500]

    def _backoff(self, attempt: int) -> float:
        return min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** attempt))

    def _sanitize(self, exc: BaseException) -> None:
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen and len(seen) < 10:
            seen.add(id(cur))
            try:
                cur.args = tuple(self._scrub(a) if isinstance(a, str) else a for a in cur.args)
                for attr in ("message", "body"):
                    val = getattr(cur, attr, None)
                    if isinstance(val, str):
                        setattr(cur, attr, self._scrub(val))
            except Exception:  # noqa: BLE001 - best effort; never mask the original error
                pass
            cur = cur.__cause__ or cur.__context__


# --------------------------------------------------------------------------- module helpers
def _validate_base_url(url: Any, *, keyed: bool) -> str:
    if not isinstance(url, str) or not _TOKEN_RE.fullmatch(url):
        raise ConfigError("base_url must be a printable ASCII string without spaces")
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port: int | None = None
    bad_port = False
    try:
        port = parts.port
    except ValueError:
        bad_port = True
    if parts.scheme != "https":
        raise ConfigError("base_url must use https://")
    if bad_port or "@" in parts.netloc or not _HOST_RE.fullmatch(host) \
            or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError("base_url must look like https://host[:port] (no credentials/path/query)")
    if keyed and (host not in PRIVATE_API_HOSTS or port not in (None, 443)):
        raise ConfigError(f"the API key may only be sent to {sorted(PRIVATE_API_HOSTS)} (port 443)")
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def _num_param(name: str, v: Any, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not lo < v <= hi:
        raise ConfigError(f"{name} must be a number in ({lo}, {hi}]")
    return float(v)


def _check_order_id(v: Any) -> str:
    if not isinstance(v, str) or not ORDER_ID_RE.fullmatch(v):
        raise ValueError(f"order id must be 1-64 characters of [A-Za-z0-9_-], got {ascii(v)[:80]}")
    return v


def _order_decimal(v: Any, name: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (Decimal, int)):
        raise ValueError(f"{name} must be a Decimal (floats are never used for money)")
    d = Decimal(v)
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{name} must be a finite number > 0")
    if abs(d.adjusted()) > 30 or len(plain(d)) > 40:
        raise ValueError(f"{name} is out of the supported range")
    return d


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def decode_json_bytes(raw: bytes, ctx: str = "response") -> Any:
    """Strict UTF-8 JSON with ``Decimal`` floats; NaN/Infinity rejected. ``SchemaError`` on failure
    (raised outside the ``except`` so the body is never chained into the exception)."""
    problem: str | None = None
    doc: Any = None
    try:
        text = raw.decode("utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        doc = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        problem = type(exc).__name__
    if problem is not None:
        raise SchemaError(f"{ctx}: body is not valid JSON ({problem})")
    return doc


def _try_json(raw: bytes) -> Any:
    if len(raw) > 65536:
        return None
    try:
        return decode_json_bytes(raw)
    except SchemaError:
        return None


def _message_of(raw: bytes) -> str:
    """Server-provided error text (JSON ``message`` if present), bounded; NOT yet scrubbed."""
    doc = _try_json(raw)
    if isinstance(doc, dict) and isinstance(doc.get("message"), str):
        return doc["message"][:_TEXT_WINDOW]
    return raw[:_TEXT_WINDOW].decode("utf-8", errors="replace")


def _rejection_details(raw: bytes) -> tuple[str, int | None]:
    doc = _try_json(raw)
    code: int | None = None
    if isinstance(doc, dict):
        result = doc.get("result")
        if isinstance(result, dict):
            code = _error_code(result.get("error_code"))
    return _message_of(raw), code


def _error_code(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and 0 <= v <= 10 ** 6:
        return v
    if isinstance(v, str) and re.fullmatch(r"[0-9]{1,6}", v):
        return int(v)
    return None


def parse_retry_after(value: str | None, now: float) -> float | None:
    """``Retry-After`` as seconds (delta-seconds or HTTP-date); ``None`` if absent/invalid."""
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,3})?", v):
        return float(v)
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:  # pragma: no cover - older Pythons returned None for garbage
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, dt.timestamp() - now)


# --------------------------------------------------------------------------- schema primitives
def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float, Decimal)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _kp(base: str, key: Any) -> str:
    if isinstance(key, str) and _SAFE_KEY_RE.fullmatch(key):
        return f"{base}.{key}"
    return f"{base}[{ascii(key)[:40]}]"


def _obj(v: Any, path: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise SchemaError(f"{path}: expected object, got {_jtype(v)}")
    return v


def _arr(v: Any, path: str, limit: int = 100_000) -> list[Any]:
    if not isinstance(v, list):
        raise SchemaError(f"{path}: expected array, got {_jtype(v)}")
    if len(v) > limit:
        raise SchemaError(f"{path}: more than {limit} elements")
    return v


def _req(obj: Any, key: str, path: str) -> Any:
    if key not in _obj(obj, path):
        raise SchemaError(f"{path}.{key}: missing")
    return obj[key]


def _num(v: Any, path: str) -> Decimal:
    """Finite, non-negative Decimal from a JSON number or a plain ASCII decimal string. Messages
    never include the value (it may be a balance)."""
    if isinstance(v, bool) or v is None:
        raise SchemaError(f"{path}: expected a number, got {_jtype(v)}")
    if isinstance(v, int):
        if abs(v) >= 10 ** _MAX_ABS_EXPONENT:
            raise SchemaError(f"{path}: number magnitude out of range")
        d = Decimal(v)
    elif isinstance(v, Decimal):
        d = v
    elif isinstance(v, str):
        if len(v) > 64 or not _NUM_STR_RE.fullmatch(v):
            raise SchemaError(f"{path}: not a plain decimal number string")
        d = Decimal(v)
    else:
        raise SchemaError(f"{path}: expected a number, got {_jtype(v)}")
    if not d.is_finite():
        raise SchemaError(f"{path}: non-finite number")
    if d.is_zero():
        return d.copy_abs()
    if abs(d.adjusted()) > _MAX_ABS_EXPONENT:
        raise SchemaError(f"{path}: number magnitude out of range")
    if d < 0:
        raise SchemaError(f"{path}: negative number")
    return d


def _pos(v: Any, path: str) -> Decimal:
    d = _num(v, path)
    if d <= 0:
        raise SchemaError(f"{path}: must be > 0")
    return d


def _int(v: Any, path: str, lo: int, hi: int) -> int:
    if isinstance(v, bool):
        raise SchemaError(f"{path}: expected an integer, got boolean")
    if isinstance(v, int):
        n = v
    elif isinstance(v, Decimal) and v.is_finite() and v == v.to_integral_value() \
            and abs(v) < 10 ** 18:
        n = int(v)
    elif isinstance(v, str) and _INT_STR_RE.fullmatch(v):
        n = int(v)
    else:
        raise SchemaError(f"{path}: expected an integer, got {_jtype(v)}")
    if not lo <= n <= hi:
        raise SchemaError(f"{path}: integer out of range [{lo}, {hi}]")
    return n


def _bool(v: Any, path: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):   # PHP-style 0/1 is unambiguous
        return bool(v)
    raise SchemaError(f"{path}: expected boolean, got {_jtype(v)}")


def _text(v: Any, path: str, *, max_len: int = 128, pattern: re.Pattern[str] = _TOKEN_RE) -> str:
    if not isinstance(v, str):
        raise SchemaError(f"{path}: expected string, got {_jtype(v)}")
    if not 1 <= len(v) <= max_len or not pattern.fullmatch(v):
        raise SchemaError(f"{path}: malformed string")
    return v


def _symbol(v: Any, path: str) -> str:
    return _text(v, path, max_len=32, pattern=_SYMBOL_RE)


def _asset(v: Any, path: str) -> str:
    return _text(v, path, max_len=32, pattern=_ASSET_RE)


_ISO_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:?[0-9]{2})")


def parse_iso8601(value: Any, path: str = "timestamp") -> float:
    """ISO-8601 with an explicit timezone (``Z`` or offset) -> epoch seconds. Naive => SchemaError
    (a wrong timezone would silently corrupt fill attribution)."""
    if not isinstance(value, str) or len(value) > 40:
        raise SchemaError(f"{path}: expected an ISO-8601 string")
    m = _ISO_RE.fullmatch(value)
    if m is None:
        raise SchemaError(f"{path}: not ISO-8601 with an explicit timezone")
    y, mo, d, hh, mi, ss = (int(m.group(i)) for i in range(1, 7))
    if not (1970 <= y <= 2200 and 1 <= mo <= 12 and 1 <= d <= calendar.monthrange(y, mo)[1]
            and hh < 24 and mi < 60 and ss < 60):
        raise SchemaError(f"{path}: invalid date/time")
    epoch = float(calendar.timegm((y, mo, d, hh, mi, ss, 0, 0, 0)))
    frac = m.group(7)
    if frac:
        epoch += int(frac) / 10 ** len(frac)
    tz = m.group(8)
    if tz != "Z":
        digits = tz[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:])
        if oh > 23 or om > 59:
            raise SchemaError(f"{path}: invalid timezone offset")
        epoch -= (1 if tz[0] == "+" else -1) * (oh * 3600 + om * 60)
    return epoch


# --------------------------------------------------------------------------- endpoint parsers
def parse_markets(result: Any, symbols: Sequence[str] = DEFAULT_SYMBOLS) -> dict[str, MarketRules]:
    """``GET /v1/markets`` -> rules for ``symbols`` only (each must be present and sane).
    ``stepSize`` / ``tickSize`` are DECIMAL-PLACE COUNTS (quantity / price)."""
    markets = _obj(_req(result, "symbols", "result"), "result.symbols")
    out: dict[str, MarketRules] = {}
    for sym in symbols:
        p = _kp("result.symbols", sym)
        if sym not in markets:
            raise SchemaError(f"{p}: missing")
        m = _obj(markets[sym], p)
        if _symbol(_req(m, "symbol", p), f"{p}.symbol") != sym:
            raise SchemaError(f"{p}.symbol: does not match its key")
        base = _asset(_req(m, "baseAsset", p), f"{p}.baseAsset")
        quote = _asset(_req(m, "quoteAsset", p), f"{p}.quoteAsset")
        if base + quote != sym:
            raise SchemaError(f"{p}: baseAsset + quoteAsset != symbol")
        qty_dec = _int(_req(m, "stepSize", p), f"{p}.stepSize", 0, 18)
        price_dec = _int(_req(m, "tickSize", p), f"{p}.tickSize", 0, 18)
        min_qty = _pos(_req(m, "minQty", p), f"{p}.minQty")
        min_notional = _num(_req(m, "minNotional", p), f"{p}.minNotional")
        max_qty = None
        if m.get("maxQty") is not None:
            max_qty = _pos(m["maxQty"], f"{p}.maxQty")
            if max_qty < min_qty:
                raise SchemaError(f"{p}.maxQty: below minQty")
        if quantize_down(min_qty, qty_dec) != min_qty:
            raise SchemaError(f"{p}.minQty: finer than stepSize ({qty_dec} decimals)")
        out[sym] = MarketRules(sym, base, quote, qty_dec, price_dec, min_qty, min_notional, max_qty)
    return out


def parse_depth(symbol: str, result: Any, ts: float) -> OrderBook:
    """``GET /v1/depth`` -> ``OrderBook`` (sorted defensively; empty/crossed => SchemaError)."""
    def side(key: str) -> list[tuple[Decimal, Decimal]]:
        levels = _arr(_req(result, key, "result"), f"result.{key}", limit=10_000)
        out = []
        for i, lv in enumerate(levels):
            p = f"result.{key}[{i}]"
            out.append((_num(_req(lv, "price", p), f"{p}.price"),
                        _num(_req(lv, "quantity", p), f"{p}.quantity")))
        return out

    bids, asks = side("bid"), side("ask")
    problem: str | None = None
    book: OrderBook | None = None
    try:
        book = OrderBook.build(symbol, bids, asks, ts)
    except ValueError as exc:
        problem = _safe_str(exc)
    if problem is not None or book is None:
        raise SchemaError(f"depth {symbol}: {problem}")
    return book


def parse_balances(result: Any) -> dict[str, Balance]:
    """``GET /v1/account/balances`` -> ``{asset: Balance}`` (value/locked >= 0; string or number)."""
    balances = _obj(_req(result, "balances", "result"), "result.balances")
    if len(balances) > 10_000:
        raise SchemaError("result.balances: too many entries")
    out: dict[str, Balance] = {}
    for asset, entry in balances.items():
        p = _kp("result.balances", asset)
        name = _asset(asset, f"{p}(key)")
        e = _obj(entry, p)
        if "asset" in e and _asset(e["asset"], f"{p}.asset") != name:
            raise SchemaError(f"{p}.asset: does not match its key")
        out[name] = Balance(name, _num(_req(e, "value", p), f"{p}.value"),
                            _num(_req(e, "locked", p), f"{p}.locked"))
    return out


def fee_entries(result: Any) -> tuple[str, dict[str, Any]]:
    """Normalises ``GET /v1/account/fee`` ``result`` to ``(shape, {symbol: entry})``: a mapping, or
    a list of objects each carrying ``symbol``. Anything else => SchemaError."""
    if isinstance(result, dict):
        return "mapping", result
    if isinstance(result, list):
        entries: dict[str, Any] = {}
        for i, e in enumerate(_arr(result, "result", limit=10_000)):
            p = f"result[{i}]"
            if not isinstance(e, dict) or "symbol" not in e:
                raise SchemaError(f"{p}: fee list entries must be objects with a 'symbol'")
            sym = _symbol(e["symbol"], f"{p}.symbol")
            if sym in entries:
                raise SchemaError(f"{p}.symbol: duplicate entry")
            entries[sym] = e
        return "list", entries
    raise SchemaError(f"result: fee data must be a mapping or a list of objects with 'symbol', "
                      f"got {_jtype(result)}")


def parse_fees(result: Any, symbols: Sequence[str] = DEFAULT_SYMBOLS) -> dict[str, FeeRates]:
    """Fee rates for ``symbols``; a symbol absent from the answer is omitted (fee UNKNOWN)."""
    _, entries = fee_entries(result)
    out: dict[str, FeeRates] = {}
    for sym in symbols:
        if sym not in entries:
            continue
        p = _kp("result", sym)
        e = _obj(entries[sym], p)
        if "symbol" in e and e["symbol"] != sym:
            raise SchemaError(f"{p}.symbol: does not match its key")
        rates = []
        for field in ("makerFeeRate", "takerFeeRate"):
            r = _num(_req(e, field, p), f"{p}.{field}")
            if r >= MAX_FEE_RATE:
                raise SchemaError(f"{p}.{field}: outside the sane range [0, {MAX_FEE_RATE})")
            rates.append(r)
        out[sym] = FeeRates(sym, rates[0], rates[1])
    return out


def map_order_status(raw: str, active: bool, executed: Decimal, orig: Decimal) -> OrderStatus:
    """Documented strings are NEW / FILLED / CANCELED. Contradictions with ``active`` => UNKNOWN.
    PARTIALLY_FILLED is derived from the quantities (the partial-fill string is undocumented)."""
    partial = 0 < executed < orig
    if raw == "FILLED":
        return OrderStatus.FILLED if not active else OrderStatus.UNKNOWN
    if raw == "CANCELED":
        return OrderStatus.CANCELED if not active else OrderStatus.UNKNOWN
    if raw == "NEW":
        if not active:
            return OrderStatus.UNKNOWN
        if executed == 0:
            return OrderStatus.NEW
        return OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.UNKNOWN
    # Undocumented string: the quantities can only classify it as "still working, part-filled".
    return OrderStatus.PARTIALLY_FILLED if (active and partial) else OrderStatus.UNKNOWN


def parse_order(obj: Any, path: str = "result") -> OrderInfo:
    """One order object (create / get / openOrders share the documented shape)."""
    o = _obj(obj, path)
    symbol = _symbol(_req(o, "symbol", path), f"{path}.symbol")
    otype = _text(_req(o, "type", path), f"{path}.type", max_len=32)
    side_raw = _req(o, "side", path)
    if not isinstance(side_raw, str) or side_raw not in ("BUY", "SELL"):
        raise SchemaError(f"{path}.side: expected 'BUY' or 'SELL'")
    price = _pos(_req(o, "price", path), f"{path}.price")
    orig_qty = _pos(_req(o, "origQty", path), f"{path}.origQty")
    executed_qty = _num(_req(o, "executedQty", path), f"{path}.executedQty")
    executed_sum = _num(_req(o, "executedSum", path), f"{path}.executedSum")
    if executed_qty > orig_qty:
        raise SchemaError(f"{path}.executedQty: exceeds origQty")
    if executed_qty == 0 and executed_sum > 0:   # (qty > 0 with sum 0 can be display rounding)
        raise SchemaError(f"{path}.executedSum: non-zero while executedQty is zero")
    active = _bool(_req(o, "active", path), f"{path}.active")
    raw_status = _text(_req(o, "status", path), f"{path}.status", max_len=32)
    coid = _text(_req(o, "clientOrderId", path), f"{path}.clientOrderId", max_len=128)
    executed_price = None   # the documented sample carries garbage here while nothing executed
    if executed_qty > 0 and o.get("executedPrice") is not None:
        executed_price = _num(o["executedPrice"], f"{path}.executedPrice")
    orig_sum = None
    if o.get("origSum") is not None:
        orig_sum = _num(o["origSum"], f"{path}.origSum")
    created_at = o.get("created_at")
    if created_at is not None:
        created_at = _text(created_at, f"{path}.created_at", max_len=64, pattern=_LINE_RE)
    return OrderInfo(
        client_order_id=coid, symbol=symbol, side=Side(side_raw), type=otype, price=price,
        orig_qty=orig_qty, executed_qty=executed_qty, executed_sum=executed_sum,
        executed_price=executed_price,
        status=map_order_status(raw_status, active, executed_qty, orig_qty), active=active,
        created_at=created_at, raw_status=raw_status, orig_sum=orig_sum)


def parse_open_orders(result: Any, symbol: str | None = None) -> list[OrderInfo]:
    orders = _arr(_req(result, "orders", "result"), "result.orders", limit=10_000)
    out = [parse_order(o, f"result.orders[{i}]") for i, o in enumerate(orders)]
    if symbol is not None:
        for i, info in enumerate(out):
            if info.symbol != symbol:
                raise SchemaError(f"result.orders[{i}].symbol: does not match the requested symbol")
    return out


def parse_trades(result: Any, symbol: str | None = None,
                 side: Side | None = None) -> list[Trade]:
    """``GET /v1/account/trades`` -> ``Trade`` list. Rows not matching a requested filter =>
    SchemaError (the filter was not honoured, so the answer is not what we think it is)."""
    rows = _arr(_req(result, "AccountLatestTrades", "result"), "result.AccountLatestTrades")
    out: list[Trade] = []
    for i, row in enumerate(rows):
        p = f"result.AccountLatestTrades[{i}]"
        sym = _symbol(_req(row, "symbol", p), f"{p}.symbol")
        if symbol is not None and sym != symbol:
            raise SchemaError(f"{p}.symbol: does not match the requested symbol")
        tside = Side.BUY if _bool(_req(row, "isBuyer", p), f"{p}.isBuyer") else Side.SELL
        if side is not None and tside is not side:
            raise SchemaError(f"{p}.isBuyer: does not match the requested side")
        coeff = _num(_req(row, "feeCoefficient", p), f"{p}.feeCoefficient")
        if coeff >= 1:
            raise SchemaError(f"{p}.feeCoefficient: must be < 1")
        stamp = _text(_req(row, "timestamp", p), f"{p}.timestamp", max_len=40)
        out.append(Trade(
            symbol=sym, side=tside, price=_pos(_req(row, "price", p), f"{p}.price"),
            quantity=_pos(_req(row, "quantity", p), f"{p}.quantity"),
            quote_sum=_num(_req(row, "sum", p), f"{p}.sum"),
            fee=_num(_req(row, "fee", p), f"{p}.fee"),
            fee_asset=_asset(_req(row, "feeAsset", p), f"{p}.feeAsset"),
            fee_coefficient=coeff, timestamp=stamp,
            ts_epoch=parse_iso8601(stamp, f"{p}.timestamp")))
    return out


def parse_candles(doc: Any) -> list[Candle]:
    """UDF history (no envelope): ``{"s":"ok","t":[...],"o":[...],...}``; ``no_data`` => []."""
    d = _obj(doc, "udf")
    status = d.get("s")
    if status == "no_data":
        return []
    if status != "ok":
        shown = ascii(status)[:20] if isinstance(status, str) else _jtype(status)
        raise SchemaError(f"udf.s: expected 'ok' or 'no_data', got {shown}")
    cols = {k: _arr(_req(d, k, "udf"), f"udf.{k}") for k in "tohlcv"}
    n = len(cols["t"])
    if any(len(c) != n for c in cols.values()):
        raise SchemaError("udf: t/o/h/l/c/v arrays differ in length")
    candles = []
    for i in range(n):
        o, h, l, c = (_pos(cols[k][i], f"udf.{k}[{i}]") for k in "ohlc")
        if not (l <= min(o, c) and max(o, c) <= h):
            raise SchemaError(f"udf[{i}]: inconsistent OHLC")
        candles.append(Candle(_int(cols["t"][i], f"udf.t[{i}]", 0, 10 ** 11), o, h, l, c,
                              _num(cols["v"][i], f"udf.v[{i}]")))
    candles.sort(key=lambda x: x.ts)
    if any(a.ts == b.ts for a, b in zip(candles, candles[1:])):
        raise SchemaError("udf.t: duplicate timestamps")
    return candles
