"""Network route and failure diagnosis for the Wallex HTTPS transport (stdlib only).

* ``tls_context()``: the ONLY TLS context the transport uses: ``CERT_REQUIRED`` + hostname check
  (asserted), TLS >= 1.2, and ``OP_IGNORE_UNEXPECTED_EOF`` where OpenSSL 3 / Python >= 3.10 offer it.
  That option does not decide anything about completeness: CPython already reads an EOF without
  ``close_notify`` as a plain EOF on ``recv`` (``suppress_ragged_eofs``), and an EOF during the TLS
  HANDSHAKE stays an error with or without it (``SSLZeroReturnError`` instead of ``SSLEOFError``).
  Truncation is detected by the transport: the HTTP framing (``http.client.read1`` returns a SHORT body
  when a Content-Length body ends early, so the length is checked; a cut chunked body raises
  ``IncompleteRead``), then every body must parse as JSON and pass the schema. Certificate / hostname
  failures are never affected.
* ``ProxyURL`` / ``parse_proxy_url``: an HTTP CONNECT proxy ``http://[user:pass@]host:port``. The
  credentials never appear in a repr/str/message (``display`` shows ``http://***@host:port``); they
  are sent only to the proxy, in the CONNECT request (``Proxy-Authorization``).
* ``ProxySettings`` / ``load_proxy_settings``: the route of a request. ``WALLEX_PROXY_URL`` wins;
  otherwise, only when ``use_env_proxy`` is on, ``https_proxy``/``HTTPS_PROXY`` (then
  ``http_proxy``/``HTTP_PROXY``) of the PROCESS environment, honouring ``no_proxy``/``NO_PROXY``. The
  OS proxy settings (Windows registry, macOS) are never read, so the route is deterministic and can be
  printed. A malformed or unsupported proxy setting is a ``ConfigError``, never a silent direct route.
* ``RequestTrace``: the phases of ONE request (connect, proxy CONNECT, TLS handshake, request, response
  headers, body) with timings, HTTP status and byte count. It holds no header values and no body.
* ``diagnose``: exception (+ the phase it happened in) -> ``Diagnosis(category, detail, hint)``. The
  detail is built from exception types, errno values, TLS reason codes and status codes, scrubbed and
  truncated; ``ValueError`` texts (which can echo header values) are never used. The hint is a
  one-line operator instruction.
"""
from __future__ import annotations

import base64
import errno
import http.client
import ipaddress
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from ..errors import ConfigError, TransportError

__all__ = [
    "CATEGORIES", "CONNECTION_RESET", "CONNECT_FAILED", "CONNECT_TIMEOUT", "DNS_FAILURE", "Diagnosis",
    "HTTP_STATUS", "IGNORE_UNEXPECTED_EOF", "INCOMPLETE_READ", "PHASES",
    "PROTOCOL_ERROR", "PROXY_CONNECT_FAILED", "PROXY_UNREACHABLE", "ProxySettings", "ProxyURL",
    "READ_TIMEOUT", "RequestTrace", "TLS_CERT", "TLS_EOF", "TLS_OTHER", "TRANSPORT_OTHER",
    "TracingHTTPSHandler", "credential_forms", "diagnose", "hint_for", "http_status_hint",
    "load_proxy_settings", "no_proxy_matches", "parse_proxy_url", "scrub_url_credentials", "tls_context",
]

# --------------------------------------------------------------------------- categories
PROXY_UNREACHABLE = "proxy_unreachable"          # TCP connection to the proxy failed
PROXY_CONNECT_FAILED = "proxy_connect_failed"    # the proxy refused / dropped the CONNECT tunnel
DNS_FAILURE = "dns_failure"                      # name resolution failed (the first hop's host)
CONNECT_FAILED = "connect_failed"                # direct TCP connection refused / unreachable
CONNECT_TIMEOUT = "connect_timeout"              # connection (TCP / tunnel / TLS) not set up in time
READ_TIMEOUT = "read_timeout"                    # connected, but the response did not arrive in time
TLS_EOF = "tls_eof_no_close_notify"              # stream ended without TLS close_notify (too early)
TLS_CERT = "tls_cert_verify_failed"              # certificate or hostname verification failed
TLS_OTHER = "tls_other"                          # any other TLS failure
CONNECTION_RESET = "connection_reset"            # reset / closed by the peer mid-request
INCOMPLETE_READ = "incomplete_read"              # body shorter than its framing says
PROTOCOL_ERROR = "protocol_error"                # not valid HTTP from the peer
HTTP_STATUS = "http_status"                      # an HTTP status that is not a success (diagnostics)
TRANSPORT_OTHER = "transport_other"              # anything else
CATEGORIES = frozenset({
    PROXY_UNREACHABLE, PROXY_CONNECT_FAILED, DNS_FAILURE, CONNECT_FAILED, CONNECT_TIMEOUT, READ_TIMEOUT,
    TLS_EOF, TLS_CERT, TLS_OTHER, CONNECTION_RESET, INCOMPLETE_READ, PROTOCOL_ERROR, HTTP_STATUS,
    TRANSPORT_OTHER})

# --------------------------------------------------------------------------- phases
PH_CONNECT = "connect"           # DNS + TCP to the first hop (the proxy, or the server itself)
PH_PROXY = "proxy_connect"       # CONNECT host:port through the proxy
PH_TLS = "tls_handshake"         # TLS with the server (end-to-end, through the tunnel if any)
PH_REQUEST = "request"           # sending the request
PH_RESPONSE = "response"         # waiting for the status line + headers
PH_BODY = "body"                 # reading the body
PHASES = (PH_CONNECT, PH_PROXY, PH_TLS, PH_REQUEST, PH_RESPONSE, PH_BODY)
_SETUP_PHASES = frozenset({PH_CONNECT, PH_PROXY, PH_TLS})
_WHERE = {PH_CONNECT: "while connecting", PH_PROXY: "during the proxy CONNECT",
          PH_TLS: "during the TLS handshake", PH_REQUEST: "while sending the request",
          PH_RESPONSE: "while waiting for the response headers", PH_BODY: "while reading the response body"}

# --------------------------------------------------------------------------- TLS
IGNORE_UNEXPECTED_EOF: int = int(getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))


def tls_context() -> ssl.SSLContext:
    """Verifying client context: system trust store, CERT_REQUIRED, hostname check, TLS >= 1.2, and
    (when available) ``OP_IGNORE_UNEXPECTED_EOF`` (an EOF without ``close_notify`` reads as a plain EOF;
    the transport decides from the HTTP framing and the JSON parse whether the body was complete)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = max(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
    if IGNORE_UNEXPECTED_EOF:
        ctx.options |= IGNORE_UNEXPECTED_EOF
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:  # pragma: no cover
        raise ConfigError("TLS certificate and hostname verification must be enabled")
    return ctx


# --------------------------------------------------------------------------- scrubbing
_PRINTABLE_RE = re.compile(r"[\x21-\x7e]{1,512}")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,15}://)(?!\*{3}@)[^\s/]*@")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME_RE = re.compile(rf"(?=.{{1,253}}\Z){_LABEL}(?:\.{_LABEL})*\.?")


def scrub_url_credentials(text: str) -> str:
    """``scheme://user:pass@host`` -> ``scheme://***@host`` anywhere in ``text``."""
    return _URL_USERINFO_RE.sub(r"\1***@", text)


def credential_forms(value: Any) -> list[str]:
    """Literal forms of the credentials embedded in a (possibly malformed) proxy URL - userinfo,
    user, password (raw and percent-decoded) and the Basic token - for output scrubbers. Never raises;
    fragments shorter than 4 characters are left out (they would mangle every message)."""
    if not isinstance(value, str) or "@" not in value:
        return []
    v = value.strip()
    authority = (v.split("://", 1)[1] if "://" in v else v).split("/", 1)[0]
    if "@" not in authority:
        return []
    userinfo = authority.rsplit("@", 1)[0]
    user, _, password = userinfo.partition(":")
    forms: set[str] = set()
    for raw in (userinfo, user, password):
        forms.update({raw, urllib.parse.unquote(raw)})
    pair = f"{urllib.parse.unquote(user)}:{urllib.parse.unquote(password)}"
    forms.add(base64.b64encode(pair.encode("utf-8")).decode("ascii"))
    return sorted((f for f in forms if len(f) >= 4), key=len, reverse=True)


def _safe_str(obj: Any) -> str:
    try:
        return str(obj)
    except Exception:  # noqa: BLE001 - a broken __str__ must not mask the real failure
        return f"<unprintable {type(obj).__name__}>"


def _clean(text: str, scrub: Callable[[str], str] | None, limit: int = 220) -> str:
    """Printable, single-line, scrubbed IN FULL and only then truncated."""
    s = re.sub(r"\s+", " ", "".join(ch if ch.isprintable() else "?" for ch in text)).strip()
    s = scrub_url_credentials(s)
    if scrub is not None:
        s = scrub(s)
    return s[:limit]


# --------------------------------------------------------------------------- proxies
class ProxyURL:
    """An HTTP CONNECT proxy. ``display`` / repr / str never contain the credentials; the proxy can
    neither be pickled nor copied (it may hold a password)."""

    __slots__ = ("host", "port", "source", "_auth", "_secrets")

    def __init__(self, host: str, port: int, *, source: str, username: str = "",
                 password: str = "") -> None:
        self.host = host
        self.port = int(port)
        self.source = source
        self._auth: str | None = None
        self._secrets: tuple[str, ...] = ()
        if username or password:
            pair = f"{username}:{password}"
            token = base64.b64encode(pair.encode("utf-8")).decode("ascii")
            self._auth = "Basic " + token
            forms = {username, password, pair, token, urllib.parse.quote(username, safe=""),
                     urllib.parse.quote(password, safe=""), urllib.parse.quote(pair, safe=":")}
            # very short fragments would mangle every message; they are never printed anyway
            self._secrets = tuple(sorted((f for f in forms if len(f) >= 4), key=len, reverse=True))

    @property
    def hostport(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def has_credentials(self) -> bool:
        return self._auth is not None

    @property
    def display(self) -> str:
        return f"http://{'***@' if self._auth else ''}{self.hostport}"

    def authorization(self) -> str | None:
        """``Proxy-Authorization`` value (Basic) or None. Sent ONLY to the proxy, in the CONNECT."""
        return self._auth

    def scrub(self, text: str) -> str:
        for v in self._secrets:
            if v in text:
                text = text.replace(v, "***")
        return text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProxyURL) and (self.host, self.port, self.source, self._auth) == (
            other.host, other.port, other.source, other._auth)

    def __hash__(self) -> int:
        return hash((self.host, self.port, self.source))

    def __repr__(self) -> str:
        return f"<proxy {self.display} ({self.source})>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("a proxy setting (it may hold a password) cannot be pickled or copied")

    def __reduce__(self) -> Any:
        raise TypeError("a proxy setting (it may hold a password) cannot be pickled or copied")


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.fullmatch(host))


def parse_proxy_url(value: Any, *, source: str, default_scheme: str | None = None) -> ProxyURL:
    """``http://[user:pass@]host:port`` -> ``ProxyURL``. ``ConfigError`` messages name ``source``
    (e.g. ``WALLEX_PROXY_URL``) but never echo the value (it may contain a password). Without
    ``default_scheme`` the scheme is mandatory; the port is always mandatory."""
    if not isinstance(value, str):
        raise ConfigError(f"{source}: must be a string like http://127.0.0.1:2080")
    v = value.strip()
    if not _PRINTABLE_RE.fullmatch(v):
        raise ConfigError(f"{source}: must look like http://host:port (printable ASCII, no spaces, "
                          "at most 512 characters)")
    if "://" not in v:
        if default_scheme is None:
            raise ConfigError(f"{source}: the scheme is required, e.g. http://127.0.0.1:2080")
        v = f"{default_scheme}://{v}"
    parsed: tuple[Any, ...] | None = None
    try:
        parts = urllib.parse.urlsplit(v)
        parsed = (parts.scheme.lower(), parts.hostname, parts.port, parts.username, parts.password,
                  parts.path, parts.query, parts.fragment)
    except ValueError:      # bad port / IPv6 literal: the message could echo the value, so drop it
        parsed = None
    if parsed is None:
        raise ConfigError(f"{source}: not a valid proxy URL (expected http://host:port)")
    scheme, host, port, user, password, path, query, fragment = parsed
    if scheme == "https":
        raise ConfigError(
            f"{source}: https:// proxies (TLS to the proxy itself) are not supported: the CONNECT request "
            "and any credentials would reach the proxy unencrypted. Use http://host:port (the traffic to "
            "api.wallex.ir stays end-to-end TLS, verified, inside the tunnel)")
    if scheme != "http":
        raise ConfigError(f"{source}: unsupported proxy scheme (only http:// CONNECT proxies are "
                          "supported; socks is not)")
    if not host or not _valid_host(host):
        raise ConfigError(f"{source}: invalid proxy host (expected http://host:port)")
    if port is None or not 1 <= port <= 65535:
        raise ConfigError(f"{source}: the proxy port is required (http://host:port)")
    if path not in ("", "/") or query or fragment:
        raise ConfigError(f"{source}: must be http://host:port only (no path, query or fragment)")
    return ProxyURL(host, port, source=source,
                    username=urllib.parse.unquote(user) if user else "",
                    password=urllib.parse.unquote(password) if password else "")


def no_proxy_matches(host: str, port: int, no_proxy: str) -> bool:
    """curl/urllib-style ``NO_PROXY``: comma (or space) separated host names / domain suffixes
    (``example.com`` also matches ``api.example.com``; a leading ``.`` or ``*.`` is ignored), optional
    ``:port``, or ``*`` for every host."""
    h = host.lower().strip("[]").rstrip(".")
    for raw in re.split(r"[,\s]+", no_proxy or ""):
        e = raw.strip().lower()
        if not e:
            continue
        if e == "*":
            return True
        e_port: int | None = None
        m = re.fullmatch(r"(\[[0-9a-f:.]+\]|[^:]+):([0-9]{1,5})", e)
        if m:
            e, e_port = m.group(1), int(m.group(2))
        e = e.strip("[]")
        while e.startswith(("*", ".")):
            e = e[1:]
        e = e.rstrip(".")
        if not e or (e_port is not None and e_port != port):
            continue
        if h == e or h.endswith("." + e):
            return True
    return False


_ENV_ORDER = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")   # lowercase wins (urllib)
_REPORTED_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")


def _env_value(env: Mapping[str, str], name: str) -> str:
    v = env.get(name)
    return v.strip() if isinstance(v, str) else ""


class ProxySettings:
    """Which proxy (if any) a request uses. Immutable after construction."""

    __slots__ = ("explicit", "env", "no_proxy", "use_env", "ignored_env")

    def __init__(self, *, explicit: ProxyURL | None = None, env: ProxyURL | None = None,
                 no_proxy: str = "", use_env: bool = False, ignored_env: Iterable[str] = ()) -> None:
        self.explicit = explicit
        self.env = env
        self.no_proxy = no_proxy
        self.use_env = use_env
        self.ignored_env = tuple(ignored_env)

    def route(self, host: str, port: int) -> ProxyURL | None:
        """The explicit proxy always (NO_PROXY does not apply to it); an environment proxy unless
        NO_PROXY matches the host; else None (direct)."""
        if self.explicit is not None:
            return self.explicit
        if self.env is not None and not no_proxy_matches(host, port, self.no_proxy):
            return self.env
        return None

    def secrets(self) -> tuple[str, ...]:
        out: list[str] = []
        for p in (self.explicit, self.env):
            if p is not None:
                out.extend(p._secrets)       # noqa: SLF001 - same module
        return tuple(sorted(set(out), key=len, reverse=True))

    def scrub(self, text: str) -> str:
        for v in self.secrets():
            if v in text:
                text = text.replace(v, "***")
        return text

    def describe(self, host: str, port: int) -> dict[str, Any]:
        """Secret-free description for ``net-check`` (names of environment variables, never values)."""
        p = self.route(host, port)
        return {"route": "proxy" if p is not None else "direct",
                "proxy": p.display if p is not None else None,
                "source": p.source if p is not None else None,
                "target": f"{host}:{port}",
                "no_proxy_bypass": self.env is not None and p is None,
                "use_env_proxy": self.use_env,
                "ignored_env": list(self.ignored_env)}

    def __repr__(self) -> str:
        p = self.explicit or self.env
        return f"ProxySettings({p.display + ' from ' + p.source if p else 'direct'})"


def load_proxy_settings(*, proxy_url: str | None = None, use_env_proxy: bool = False,
                        environ: Mapping[str, str] | None = None) -> ProxySettings:
    """Explicit ``proxy_url`` > (only with ``use_env_proxy``) https_proxy / HTTPS_PROXY >
    http_proxy / HTTP_PROXY of ``environ`` (default: the process environment; NOT .env files, NOT the
    OS proxy settings). A proxy that is set but malformed is a ``ConfigError`` (fail closed: a typo
    must not silently send the traffic, and an IP-bound key, over another route)."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    explicit = parse_proxy_url(proxy_url, source="WALLEX_PROXY_URL") if proxy_url else None
    chosen: ProxyURL | None = None
    chosen_name: str | None = None
    if use_env_proxy and explicit is None:
        for name in _ENV_ORDER:
            value = _env_value(env, name)
            if value:
                # Windows environments are case-insensitive: name the variable as it is usually written
                label = name.upper() if _env_value(env, name.upper()) == value else name
                chosen = parse_proxy_url(value, source=label, default_scheme="http")
                chosen_name = name
                break
    no_proxy = _env_value(env, "no_proxy") or _env_value(env, "NO_PROXY")
    present = {n for n in _REPORTED_ENV if _env_value(env, n) or _env_value(env, n.lower())}
    used = {chosen_name.upper()} if chosen_name else set()
    return ProxySettings(explicit=explicit, env=chosen, no_proxy=no_proxy if chosen else "",
                         use_env=bool(use_env_proxy), ignored_env=sorted(present - used))


# --------------------------------------------------------------------------- tracing
class RequestTrace:
    """Phases of ONE HTTP exchange (filled by the transport and its connection class): durations in
    ms, HTTP status, CONNECT status, TLS version, bytes, framing, error. No header values, no body."""

    def __init__(self, *, method: str, path: str, host: str, port: int, proxy: ProxyURL | None,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.method = str(method).upper()
        self.path = path
        self.target = f"{host}:{port}"
        self.proxy = proxy.display if proxy is not None else None
        self.proxy_source = proxy.source if proxy is not None else None
        self.phase = "init"
        self.phases_ms: dict[str, float] = {}
        self.status: int | None = None
        self.proxy_status: int | None = None
        self.tls_version: str | None = None
        self.cipher: str | None = None
        self.bytes: int | None = None
        self.framing: str | None = None
        self.elapsed_ms: float | None = None
        self.error: dict[str, Any] | None = None
        self._clock = clock
        self._t0 = clock()
        self._tphase = self._t0

    def enter(self, phase: str) -> None:
        now = self._clock()
        if self.phase not in ("init", "done"):
            self.phases_ms[self.phase] = round(self.phases_ms.get(self.phase, 0.0)
                                               + (now - self._tphase) * 1000.0, 1)
        self.phase = phase
        self._tphase = now

    def tls_established(self, sock: Any) -> None:
        try:
            self.tls_version = sock.version()
            c = sock.cipher()
            self.cipher = c[0] if c else None
        except Exception:  # noqa: BLE001 - informational only
            pass

    def finish(self, error: "Diagnosis | None" = None) -> None:
        failed_in = self.phase if self.phase not in ("init", "done") else None
        self.enter("done")
        self.elapsed_ms = round((self._clock() - self._t0) * 1000.0, 1)
        if error is not None:
            self.error = {"category": error.category, "detail": error.detail, "hint": error.hint,
                          "phase": error.phase or failed_in}

    @property
    def current_phase(self) -> str | None:
        return self.phase if self.phase in PHASES else None

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "path": self.path, "target": self.target, "proxy": self.proxy,
                "proxy_source": self.proxy_source, "phases_ms": dict(self.phases_ms),
                "proxy_status": self.proxy_status, "tls_version": self.tls_version, "cipher": self.cipher,
                "status": self.status, "bytes": self.bytes, "framing": self.framing,
                "elapsed_ms": self.elapsed_ms, "error": self.error}


class _TracedHTTPSConnection(http.client.HTTPSConnection):
    """``HTTPSConnection`` that records its phase on the request's ``RequestTrace``. Behaviour is the
    stdlib's: ``HTTPConnection.connect`` (TCP + the CONNECT tunnel via ``_tunnel``), then the socket is
    wrapped with the SAME verifying context and server name as ``HTTPSConnection.connect`` uses (the
    tunnel's target host when tunnelling, so the certificate is checked against api.wallex.ir, never
    against the proxy)."""

    def __init__(self, *args: Any, wa_trace: RequestTrace | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._wa_trace = wa_trace

    def connect(self) -> None:
        tr = self._wa_trace
        if tr is None:
            super().connect()
            return
        tr.enter(PH_CONNECT)
        http.client.HTTPConnection.connect(self)
        tr.enter(PH_TLS)
        server_hostname = self._tunnel_host if self._tunnel_host else self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)
        tr.tls_established(self.sock)
        tr.enter(PH_REQUEST)

    def _tunnel(self) -> None:
        tr = self._wa_trace
        if tr is not None:
            tr.enter(PH_PROXY)
        super()._tunnel()
        if tr is not None:
            tr.proxy_status = 200

    def getresponse(self) -> http.client.HTTPResponse:
        tr = self._wa_trace
        if tr is not None:
            tr.enter(PH_RESPONSE)
        resp = super().getresponse()
        if tr is not None:
            tr.status = resp.status
            tr.enter(PH_BODY)
        return resp


class TracingHTTPSHandler(urllib.request.HTTPSHandler):
    """``HTTPSHandler`` whose connections record their phases on ``request.wa_trace``."""

    def https_open(self, req: Any) -> Any:
        return self.do_open(_TracedHTTPSConnection, req, context=self._context,
                            wa_trace=getattr(req, "wa_trace", None))


# --------------------------------------------------------------------------- diagnosis
@dataclass(frozen=True)
class Diagnosis:
    category: str
    detail: str
    hint: str
    phase: str | None = None
    status: int | None = None        # HTTP status involved (CONNECT answer / response), if any

    @property
    def message(self) -> str:
        return f"[{self.category}] {self.detail} | hint: {self.hint}"


_TUNNEL_RE = re.compile(r"Tunnel connection failed: ([0-9]{3})")
_UNREACHABLE_ERRNOS = frozenset(e for e in (
    getattr(errno, "ENETUNREACH", None), getattr(errno, "EHOSTUNREACH", None),
    getattr(errno, "ENETDOWN", None), getattr(errno, "EHOSTDOWN", None),
    10050, 10051, 10065) if e is not None)        # 100xx: WSAENETDOWN / WSAENETUNREACH / WSAEHOSTUNREACH


def _where(phase: str | None, received: int | None) -> str:
    text = _WHERE.get(phase or "", "during the request")
    if phase == PH_BODY and received is not None:
        text += f" (after {received} bytes)"
    return text


def http_status_hint(status: int | None, *, private: bool = False) -> str:
    """One-line hint for a non-success HTTP status (``net-check``)."""
    if status in (401, 403):
        if private:
            return ("the exchange refused the key: wrong key, missing READ permission, or this route's exit "
                    "IP (the proxy's, when one is used) is not in the key's IP allow-list")
        return "the exchange denied access (IP/geo block or WAF?): try another route / proxy"
    if status == 429:
        return "rate limited: wait a minute and retry"
    if status is not None and 500 <= status <= 599:
        return "exchange-side or gateway error: retry later; if it persists, check the proxy's upstream"
    if status is not None and 300 <= status <= 399:
        return "unexpected redirect (redirects are never followed): wrong base URL or a captive portal?"
    return "unexpected HTTP status from the exchange"


def _hint(cat: str, *, phase: str | None, proxy: ProxyURL | None, target: str,
          status: int | None = None, reason: str | None = None) -> str:
    host = target.rsplit(":", 1)[0]
    if proxy is not None:
        p = proxy.display
        if cat == PROXY_UNREACHABLE:
            return (f"nothing accepts connections at the proxy {p} (from {proxy.source}): start the proxy "
                    "app or fix the address")
        if cat == PROXY_CONNECT_FAILED:
            if status == 407:
                return (f"the proxy {p} requires authentication: set WALLEX_PROXY_URL=http://user:pass@host:port "
                        "(credentials are never printed)")
            if status in (403, 405):
                return f"the proxy {p} refuses to tunnel to {target}: allow CONNECT to {target} in the proxy"
            if status in (502, 503, 504):
                return (f"the proxy {p} cannot reach {target} (its upstream is down or blocked): check the "
                        "proxy's own connection")
            if status is not None:
                return f"the proxy {p} refused the CONNECT tunnel to {target} (HTTP {status})"
            return (f"the proxy {p} dropped the tunnel request: check its log/upstream, or the port is not an "
                    "HTTP proxy port")
        if cat == DNS_FAILURE:
            return f"the proxy host cannot be resolved: fix {proxy.source}"
        if cat == CONNECT_TIMEOUT:
            return (f"the tunnel through {p} to {target} was not set up in time: the proxy's route is slow "
                    "or blocked")
    if cat == DNS_FAILURE:
        return (f"{host} cannot be resolved: check DNS/internet, or route through a proxy "
                "(WALLEX_PROXY_URL=http://host:port)")
    if cat == CONNECT_FAILED:
        return (f"direct connection to {target} failed: check the network; if this machine needs a proxy, "
                "set WALLEX_PROXY_URL=http://host:port")
    if cat == CONNECT_TIMEOUT:
        return (f"no connection to {target} in time (filtered route?): if this machine needs a proxy, set "
                "WALLEX_PROXY_URL=http://host:port")
    if cat == READ_TIMEOUT:
        return ("connected, but the response did not arrive in time (slow or stalled proxy/server): reads "
                "are retried; an order's outcome is reconciled, never resent")
    if cat == TLS_EOF:
        if phase in _SETUP_PHASES:
            return ("the connection was cut during the TLS handshake (filtering/DPI, or the proxy cannot "
                    "reach the server): try another route/proxy")
        return ("the proxy/server ended the stream before the response was complete: reads are retried; "
                "check the proxy's stability")
    if cat == TLS_CERT:
        return ("certificate check failed (TLS interception by an antivirus/corporate proxy, or a wrong "
                "system clock): fix the trust store/clock; verification is never disabled")
    if cat == TLS_OTHER:
        if reason in ("WRONG_VERSION_NUMBER", "UNKNOWN_PROTOCOL", "HTTP_REQUEST", "RECORD_LAYER_FAILURE"):
            return "the peer did not speak TLS: wrong port, or the proxy is not an HTTP CONNECT proxy"
        return "TLS failure on the route to the exchange; certificate verification stays ON"
    if cat == CONNECTION_RESET:
        return ("the proxy/server closed or reset the connection: reads are retried; an order's outcome is "
                "reconciled, never resent")
    if cat == INCOMPLETE_READ:
        return ("the response was cut short (the proxy/peer dropped the connection): reads are retried; a "
                "cut-short body is never accepted")
    if cat == PROTOCOL_ERROR:
        return "the peer did not answer with valid HTTP (wrong proxy port or protocol?)"
    if cat == HTTP_STATUS:
        return http_status_hint(status)
    if cat == PROXY_UNREACHABLE:       # (only reachable without a proxy object: defensive)
        return "the proxy is not reachable: start the proxy app or fix the proxy address"
    if cat == PROXY_CONNECT_FAILED:
        return "the proxy refused the CONNECT tunnel"
    return "unexpected network failure: run `wallex-agent net-check` for a step-by-step diagnosis"


def hint_for(category: str, *, phase: str | None = None, proxy: ProxyURL | None = None,
             target: str = "api.wallex.ir:443", status: int | None = None) -> str:
    """The one-line operator hint of ``category`` (secret-free: proxies appear as ``display``)."""
    return _hint(category, phase=phase, proxy=proxy, target=target, status=status)


def diagnose(exc: BaseException, *, phase: str | None = None, proxy: ProxyURL | None = None,
             target: str = "api.wallex.ir:443", received: int | None = None,
             scrub: Callable[[str], str] | None = None) -> Diagnosis:
    """Classify a transport failure. ``phase`` is where it happened (from a ``RequestTrace``); without
    it the wording guesses (urllib wraps connect/CONNECT/TLS/send failures in ``URLError``; anything
    else came while waiting for the response) but ``Diagnosis.phase`` stays None, and only failures that
    can happen at TCP-connect time alone (refused, DNS, unreachable) are attributed to the proxy. Never
    raises."""
    inner: BaseException = exc
    wrapped = False
    for _ in range(5):
        reason = getattr(inner, "reason", None)
        if isinstance(inner, urllib.error.URLError) and not isinstance(inner, urllib.error.HTTPError) \
                and isinstance(reason, BaseException):
            inner, wrapped = reason, True
        else:
            break
    # Without a trace the phase is only GUESSED for the wording (urllib wraps connect AND send failures
    # in URLError), so it is not reported: a reported phase is always a traced fact (e.g. a failure
    # reported in a setup phase really happened before any request byte was written).
    traced = phase in PHASES
    reported = phase if traced else None
    if not traced:
        phase = PH_CONNECT if wrapped else PH_RESPONSE
    via = proxy is not None
    where = _where(phase, received) if traced or not wrapped else "while setting up the connection"

    def mk(cat: str, detail: str, *, status: int | None = None, reason: str | None = None,
           at: str | None = None) -> Diagnosis:
        return Diagnosis(cat, _clean(detail, scrub), _clean(_hint(cat, phase=at or phase, proxy=proxy,
                                                                     target=target, status=status, reason=reason),
                                                               scrub, 300), at or reported, status)

    def txt(e: BaseException) -> str:
        return _safe_str(e)

    if isinstance(inner, TransportError):         # already a TransportError (another transport/layer)
        cat = getattr(inner, "category", None)
        if cat in CATEGORIES:
            text = txt(inner)
            m = re.fullmatch(r"\[[a-z_]{1,40}\] (.*?)(?: \| hint: .*)?", text, re.S)
            return Diagnosis(cat, _clean(m.group(1) if m else text, scrub), _clean(
                getattr(inner, "hint", None) or _hint(cat, phase=phase, proxy=proxy, target=target),
                scrub, 300), getattr(inner, "phase", None) or reported)
        return mk(TRANSPORT_OTHER, f"TransportError: {txt(inner)}")
    if isinstance(inner, ssl.SSLCertVerificationError):           # (also a ValueError: check first)
        msg = getattr(inner, "verify_message", None) or getattr(inner, "reason", None) or "verification failed"
        return mk(TLS_CERT, f"certificate verification failed for {target}: {msg}")
    if isinstance(inner, ssl.SSLEOFError):
        return mk(TLS_EOF, f"EOF without TLS close_notify {where}")
    if isinstance(inner, ssl.SSLZeroReturnError):
        if phase in _SETUP_PHASES:
            return mk(TLS_EOF, f"connection closed {where}")
        return mk(CONNECTION_RESET, f"the peer closed the TLS session {where}")
    if isinstance(inner, TimeoutError):                            # socket.timeout, WinError 10060
        if phase == PH_CONNECT and via and traced:                 # (untraced: TCP, CONNECT or TLS?)
            return mk(PROXY_UNREACHABLE, f"TCP connection to the proxy {proxy.display} timed out")
        if phase in _SETUP_PHASES:
            return mk(CONNECT_TIMEOUT, f"timed out {where}")
        return mk(READ_TIMEOUT, f"timed out {where}")
    if isinstance(inner, ssl.SSLError):
        reason = getattr(inner, "reason", None)
        reason = reason if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", reason) else None
        return mk(TLS_OTHER, f"TLS error {reason or type(inner).__name__} {where}", reason=reason)
    if isinstance(inner, socket.gaierror):
        what = f"the proxy host of {proxy.display}" if via else target.rsplit(":", 1)[0]
        return mk(DNS_FAILURE, f"cannot resolve {what}: {txt(inner)}")
    if isinstance(inner, ConnectionRefusedError):
        if via and phase == PH_CONNECT:
            return mk(PROXY_UNREACHABLE, f"connection to the proxy {proxy.display} refused")
        return mk(CONNECT_FAILED, f"connection to {target} refused")
    tunnel = _TUNNEL_RE.search(txt(inner)) if isinstance(inner, OSError) else None
    if tunnel is not None:              # http.client's _tunnel: "Tunnel connection failed: <code> <reason>"
        status = int(tunnel.group(1))
        return mk(PROXY_CONNECT_FAILED, f"the proxy answered CONNECT {target} with HTTP {status}", status=status,
                  at=PH_PROXY)
    if isinstance(inner, http.client.RemoteDisconnected):          # (a ConnectionResetError subclass)
        if phase == PH_PROXY:
            return mk(PROXY_CONNECT_FAILED, "the proxy closed the connection without answering the CONNECT")
        return mk(CONNECTION_RESET, f"the peer closed the connection without a response {where}")
    if isinstance(inner, http.client.IncompleteRead):
        partial = getattr(inner, "partial", b"") or b""
        expected = getattr(inner, "expected", None)
        got = received if received is not None else len(partial)
        more = f", {expected} more expected" if isinstance(expected, int) and expected else ""
        return mk(INCOMPLETE_READ, f"response body cut short ({got} bytes received{more})")
    if isinstance(inner, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        if phase == PH_CONNECT and via and traced:                 # (untraced: maybe while sending)
            return mk(PROXY_UNREACHABLE, f"the proxy {proxy.display} reset the connection")
        if phase == PH_PROXY:
            return mk(PROXY_CONNECT_FAILED, "the proxy reset the connection during the CONNECT")
        return mk(CONNECTION_RESET, f"connection reset {where}")
    if isinstance(inner, OSError) and getattr(inner, "errno", None) in _UNREACHABLE_ERRNOS:
        if via and phase == PH_CONNECT:
            return mk(PROXY_UNREACHABLE, f"the proxy {proxy.display} is unreachable ({txt(inner)})")
        return mk(CONNECT_FAILED, f"{target} is unreachable ({txt(inner)})")
    if isinstance(inner, http.client.HTTPException):
        if phase == PH_PROXY:
            return mk(PROXY_CONNECT_FAILED, f"the proxy did not answer the CONNECT with valid HTTP "
                                            f"({type(inner).__name__})")
        return mk(PROTOCOL_ERROR, f"invalid HTTP from the peer {where} ({type(inner).__name__})")
    if isinstance(inner, ValueError):        # its text can echo a header value: never used
        return mk(TRANSPORT_OTHER, f"invalid request or closed stream (ValueError) {where}")
    if isinstance(inner, OSError):
        detail = f"{type(inner).__name__}: {txt(inner)} {where}"
        if phase == PH_CONNECT and traced:
            return mk(PROXY_UNREACHABLE if via else CONNECT_FAILED, detail)
        if phase == PH_PROXY:
            return mk(PROXY_CONNECT_FAILED, detail)
        if phase == PH_TLS:
            return mk(TLS_OTHER, detail)
        return mk(TRANSPORT_OTHER, detail)
    if isinstance(inner, urllib.error.URLError):
        return mk(TRANSPORT_OTHER, f"URLError: {_safe_str(getattr(inner, 'reason', ''))} {where}")
    return mk(TRANSPORT_OTHER, f"{type(inner).__name__}: {txt(inner)} {where}")
