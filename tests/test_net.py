"""Network route + failure diagnosis (``exchange/net.py``) and the HTTPS transport over REAL local TLS.

Covers: proxy URL parsing and credential hygiene, WALLEX_PROXY_URL > environment precedence and NO_PROXY,
the verifying TLS context (CERT_REQUIRED + hostname + OP_IGNORE_UNEXPECTED_EOF), the classification of
every failure category (injected exceptions and real sockets), HTTP framing checks (a cut-short body is
never a success; an EOF without close_notify after a COMPLETE body is), a fake CONNECT proxy (407 / 502 /
drop / plaintext / tunnel), and the API key never leaving the TLS tunnel. Everything runs on 127.0.0.1;
no Internet. tests/fixtures/tls/README.txt describes the TEST-ONLY certificates.
"""
from __future__ import annotations

import base64
import copy
import http.client
import io
import json
import pickle
import socket
import ssl
import threading
import urllib.error
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Any, Callable

import pytest

from wallex_agent.clock import SimClock
from wallex_agent.config import Config, load_config
from wallex_agent.errors import ConfigError, SchemaError, TransportError, error_label
from wallex_agent.exchange import net
from wallex_agent.exchange.wallex import UrllibTransport, WallexClient
from wallex_agent.modes import network_transport

FIX = Path(__file__).parent / "fixtures"
TLS_DIR = FIX / "tls"
KEY = "SENTINELKEY-Q7xZ9wPv3Lm8/Rk2+Tz=Hq4"
KEY_FRAGMENTS = frozenset(KEY[i:i + 8] for i in range(len(KEY) - 7))
SECRET = "S3cretPassw0rd"
MARKETS = (FIX / "markets_real_2026-10-22.json").read_bytes()
HAS_IGNORE_EOF = bool(getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0))


def no_key(text: str) -> bool:
    return not any(f in text for f in KEY_FRAGMENTS)


def trusting(**kw: Any) -> UrllibTransport:
    """The production transport (system trust, CERT_REQUIRED, hostname check, ignore-unexpected-EOF) whose
    context ALSO trusts the test CA - the only difference from production."""
    tr = UrllibTransport(**kw)
    tr._ctx.load_verify_locations(cafile=str(TLS_DIR / "test_ca.pem"))
    return tr


# =========================================================================== local servers
Handler = Callable[[ssl.SSLSocket, bytes], None]


class TLSServer:
    """HTTPS server on 127.0.0.1 (test certificate for wallex-test.localhost / 127.0.0.1). For every
    connection: read the request head, run ``handler``, then close the TCP connection WITHOUT a TLS
    close_notify (a raw shutdown, as a dropping proxy/peer would)."""

    def __init__(self, handler: Handler) -> None:
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(TLS_DIR / "test_server.pem"))
        self.handler = handler
        self.requests: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(raw,), daemon=True).start()

    def _one(self, raw: socket.socket) -> None:
        conn = None
        try:
            raw.settimeout(10)
            conn = self.ctx.wrap_socket(raw, server_side=True)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                req += chunk
            self.requests.append(req)
            self.handler(conn, req)
        except (OSError, ssl.SSLError):
            pass                      # e.g. the client refused our certificate: expected in some tests
        finally:
            target = conn if conn is not None else raw
            try:
                socket.socket.shutdown(target, socket.SHUT_RDWR)     # TCP FIN, no TLS close_notify
            except OSError:
                pass
            target.close()

    def close(self) -> None:
        self.sock.close()


class FakeProxy:
    """Plain-TCP HTTP CONNECT proxy on 127.0.0.1. Modes: ``tunnel`` (200, then relay to ``upstream``),
    ``407`` / ``502`` (that status, then close), ``drop`` (close without answering), ``plaintext`` (200,
    then a plaintext HTTP answer instead of TLS), ``drop_after_200`` (200, then close after the
    ClientHello: what a local proxy does when its own upstream fails - it answers CONNECT before it has
    connected). ``received`` = every byte the client sent it."""

    def __init__(self, mode: str, upstream: int | None = None) -> None:
        self.mode, self.upstream = mode, upstream
        self.received = bytearray()
        self.connects: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket, record: bool) -> None:
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                if record:
                    self.received += d
                dst.sendall(d)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _one(self, c: socket.socket) -> None:
        try:
            c.settimeout(10)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    return
                buf += chunk
            self.received += buf
            self.connects.append(buf.decode("latin-1"))
            if self.mode == "drop":
                return
            if self.mode == "407":
                c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"t\"\r\n"
                          b"Content-Length: 0\r\n\r\n")
                return
            if self.mode == "502":
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if self.mode == "plaintext":
                self.received += c.recv(65536)                       # the ClientHello
                c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            if self.mode == "drop_after_200":                        # "200", then the upstream is gone
                self.received += c.recv(65536)
                return
            u = socket.create_connection(("127.0.0.1", self.upstream), timeout=10)
            back = threading.Thread(target=self._pump, args=(u, c, False), daemon=True)
            back.start()
            self._pump(c, u, True)
            back.join(5)
            u.close()
        except OSError:
            pass
        finally:
            c.close()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def servers():
    made: list[Any] = []

    def make(kind: str, *args: Any) -> Any:
        s = TLSServer(*args) if kind == "tls" else FakeProxy(*args)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def head(extra: bytes = b"", status: bytes = b"200 OK") -> bytes:
    return b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nConnection: close\r\n" + extra + b"\r\n"


def content_length(body: bytes, *, declared: int | None = None) -> Handler:
    def h(conn: ssl.SSLSocket, req: bytes) -> None:
        conn.sendall(head(b"Content-Length: %d\r\n" % (len(body) if declared is None else declared)) + body)
    return h


def until_close(body: bytes) -> Handler:          # no Content-Length, not chunked: the end is the close
    def h(conn: ssl.SSLSocket, req: bytes) -> None:
        conn.sendall(head() + body)
    return h


def chunked(body: bytes, *, size: int = 500, cut: int | None = None) -> Handler:
    def h(conn: ssl.SSLSocket, req: bytes) -> None:
        wire = head(b"Transfer-Encoding: chunked\r\n")
        for i in range(0, len(body), size):
            part = body[i:i + size]
            wire += b"%x\r\n" % len(part) + part + b"\r\n"
        wire += b"0\r\n\r\n"
        conn.sendall(wire if cut is None else wire[:cut])
    return h


def get(tr: UrllibTransport, url: str, headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
    return tr.request("GET", url, headers or {"Accept": "application/json"}, None, 5.0)


def transport_error(fn: Callable[[], Any]) -> TransportError:
    with pytest.raises(TransportError) as ei:
        fn()
    e = ei.value
    assert e.__cause__ is None and e.__context__ is None        # raised outside the except block
    assert e.category in net.CATEGORIES and str(e).startswith(f"[{e.category}] ") and e.hint
    return e


# =========================================================================== proxy URLs
@pytest.mark.parametrize("value,hostport", [
    ("http://127.0.0.1:2080", "127.0.0.1:2080"), ("http://127.0.0.1:2080/", "127.0.0.1:2080"),
    ("HTTP://Proxy.Example:3128", "proxy.example:3128"), ("http://[::1]:8080", "[::1]:8080"),
    (f"http://user:{SECRET}@10.0.0.5:3128", "10.0.0.5:3128")])
def test_parse_proxy_url_accepts_http_connect_proxies(value, hostport):
    p = net.parse_proxy_url(value, source="WALLEX_PROXY_URL")
    assert p.hostport == hostport and p.source == "WALLEX_PROXY_URL"
    assert p.display == f"http://{'***@' if '@' in value else ''}{hostport}"


def test_proxy_credentials_never_appear_in_repr_display_or_copies():
    p = net.parse_proxy_url(f"http://op%40er:{SECRET}@127.0.0.1:2080", source="WALLEX_PROXY_URL")
    assert p.has_credentials
    assert p.authorization() == "Basic " + base64.b64encode(f"op@er:{SECRET}".encode()).decode()
    for text in (repr(p), str(p), p.display, f"{p}", repr(net.ProxySettings(explicit=p))):
        assert SECRET not in text and "op@er" not in text and "op%40er" not in text
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(p)
    assert p.scrub(f"echo {SECRET} and {p.authorization()}").count("***") == 2


@pytest.mark.parametrize("value,why", [
    (f"https://u:{SECRET}@127.0.0.1:443", "TLS to the proxy"), ("socks5://127.0.0.1:1080", "socks"),
    ("127.0.0.1:2080", "scheme is required"), ("http://127.0.0.1", "port is required"),
    ("http://127.0.0.1:0", "port is required"), ("http://127.0.0.1:99999", "not a valid proxy URL"),
    (f"http://127.0.0.1:{SECRET}", "not a valid proxy URL"), ("http://127.0.0.1:2080/path", "no path"),
    ("http://127.0.0.1:2080?x=1", "no path"), ("http://127.0.0.1:2080#f", "no path"),
    ("http://bad host:1", "printable ASCII"), ("http://pröxy:1", "printable ASCII"),
    ("http://under_score:1", "invalid proxy host"), ("", "printable ASCII"), (None, "must be a string")])
def test_parse_proxy_url_rejects_without_echoing_the_value(value, why):
    with pytest.raises(ConfigError) as ei:
        net.parse_proxy_url(value, source="WALLEX_PROXY_URL")
    msg = str(ei.value)
    assert why in msg and msg.startswith("WALLEX_PROXY_URL:") and SECRET not in msg
    assert ei.value.__context__ is None


def test_environment_proxies_may_omit_the_scheme_but_not_the_port():
    assert net.parse_proxy_url("127.0.0.1:2080", source="HTTPS_PROXY", default_scheme="http").hostport == \
        "127.0.0.1:2080"
    with pytest.raises(ConfigError, match="port is required"):
        net.parse_proxy_url("proxy.example", source="HTTPS_PROXY", default_scheme="http")


def test_credential_forms_for_output_scrubbers():
    forms = net.credential_forms(f"http://op%40er:{SECRET}@127.0.0.1:2080")
    assert SECRET in forms and "op@er" in forms and f"op%40er:{SECRET}" in forms
    assert base64.b64encode(f"op@er:{SECRET}".encode()).decode() in forms
    assert net.credential_forms("http://127.0.0.1:2080") == [] and net.credential_forms(None) == []
    assert net.scrub_url_credentials(f"x http://u:{SECRET}@h:1/y") == "x http://***@h:1/y"


# =========================================================================== precedence + NO_PROXY
P_ENV = {"HTTPS_PROXY": "http://10.1.1.1:3128", "HTTP_PROXY": "http://10.2.2.2:3128"}


def test_explicit_proxy_wins_and_no_proxy_never_bypasses_it():
    s = net.load_proxy_settings(proxy_url="http://127.0.0.1:2080", use_env_proxy=True,
                                environ={**P_ENV, "NO_PROXY": "api.wallex.ir"})
    assert s.route("api.wallex.ir", 443).hostport == "127.0.0.1:2080"
    d = s.describe("api.wallex.ir", 443)
    assert d["source"] == "WALLEX_PROXY_URL" and d["ignored_env"] == ["HTTPS_PROXY", "HTTP_PROXY"]


def test_environment_proxies_are_used_only_when_opted_in():
    off = net.load_proxy_settings(use_env_proxy=False, environ=P_ENV)
    assert off.route("api.wallex.ir", 443) is None
    assert off.describe("api.wallex.ir", 443)["ignored_env"] == ["HTTPS_PROXY", "HTTP_PROXY"]
    on = net.load_proxy_settings(use_env_proxy=True, environ=P_ENV)
    p = on.route("api.wallex.ir", 443)
    assert p.hostport == "10.1.1.1:3128" and p.source == "HTTPS_PROXY"
    assert on.describe("api.wallex.ir", 443)["ignored_env"] == ["HTTP_PROXY"]


def test_lowercase_wins_and_http_proxy_is_the_fallback_for_https_targets():
    env = {"https_proxy": "http://10.9.9.9:1", "HTTPS_PROXY": "http://10.1.1.1:3128"}
    assert net.load_proxy_settings(use_env_proxy=True, environ=env).route("api.wallex.ir", 443).hostport \
        == "10.9.9.9:1"
    only_http = net.load_proxy_settings(use_env_proxy=True, environ={"http_proxy": "10.2.2.2:8080"})
    assert only_http.route("api.wallex.ir", 443).hostport == "10.2.2.2:8080"
    assert net.load_proxy_settings(use_env_proxy=True, environ={}).route("api.wallex.ir", 443) is None


@pytest.mark.parametrize("no_proxy,bypass", [
    ("api.wallex.ir", True), ("wallex.ir", True), (".wallex.ir", True), ("*.wallex.ir", True),
    ("WALLEX.IR", True), (" other.com , wallex.ir ", True), ("*", True), ("api.wallex.ir:443", True),
    ("api.wallex.ir:8443", False), ("other.com", False), ("allex.ir", False), ("", False)])
def test_no_proxy_applies_to_environment_proxies(no_proxy, bypass):
    s = net.load_proxy_settings(use_env_proxy=True, environ={**P_ENV, "no_proxy": no_proxy})
    assert (s.route("api.wallex.ir", 443) is None) is bypass
    assert s.describe("api.wallex.ir", 443)["no_proxy_bypass"] is bypass


def test_a_malformed_environment_proxy_fails_closed_only_when_it_would_be_used():
    bad = {"HTTPS_PROXY": f"socks5://u:{SECRET}@127.0.0.1:1080"}
    with pytest.raises(ConfigError) as ei:
        net.load_proxy_settings(use_env_proxy=True, environ=bad)
    assert "HTTPS_PROXY" in str(ei.value) and SECRET not in str(ei.value)
    assert net.load_proxy_settings(use_env_proxy=False, environ=bad).route("api.wallex.ir", 443) is None
    with pytest.raises(ConfigError):
        UrllibTransport(use_env_proxy=True, environ=bad)


def test_config_proxy_url_is_validated_hidden_and_masked(tmp_path):
    cfg = load_config(env={"WALLEX_PROXY_URL": f"http://op:{SECRET}@127.0.0.1:2080", "DATA_DIR": str(tmp_path)},
                      dotenv_path=None)
    assert cfg.proxy_url and SECRET not in repr(cfg) and SECRET not in str(cfg)
    assert cfg.redacted()["proxy_url"] == "http://***@127.0.0.1:2080" == cfg.proxy_display
    with pytest.raises(ConfigError) as ei:
        load_config(env={"WALLEX_PROXY_URL": f"https://op:{SECRET}@127.0.0.1:2080", "DATA_DIR": str(tmp_path)},
                    dotenv_path=None)
    assert SECRET not in str(ei.value) and "WALLEX_PROXY_URL" in str(ei.value)


def test_network_transport_factory_routes_like_the_config(tmp_path):
    base = Config(data_dir=str(tmp_path))
    assert network_transport(base, environ=P_ENV).proxy_settings.route("api.wallex.ir", 443) is None
    env_on = network_transport(replace(base, use_env_proxy=True), environ=P_ENV)
    assert env_on.proxy_settings.route("api.wallex.ir", 443).source == "HTTPS_PROXY"
    explicit = network_transport(replace(base, use_env_proxy=True, proxy_url="http://127.0.0.1:2080"), environ=P_ENV)
    assert explicit.proxy_settings.route("api.wallex.ir", 443).source == "WALLEX_PROXY_URL"


# =========================================================================== TLS context
def test_tls_context_verifies_and_only_relaxes_the_missing_close_notify():
    for ctx in (net.tls_context(), UrllibTransport()._ctx):
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
        assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
        if HAS_IGNORE_EOF:
            assert ctx.options & ssl.OP_IGNORE_UNEXPECTED_EOF
    tr = UrllibTransport()
    https = [h for h in tr._opener.handlers if isinstance(h, net.TracingHTTPSHandler)]
    assert https and all(h._context is tr._ctx and h._debuglevel == 0 for h in https)


# =========================================================================== classification (injected)
class FakeOpener:
    def __init__(self, outcome: Any) -> None:
        self.outcome, self.requests = outcome, []

    def open(self, req: Any, timeout: float | None = None) -> Any:
        self.requests.append((req, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeResponse:
    def __init__(self, status: int = 200, headers: Any = None, body: bytes = b"") -> None:
        self.status, self.headers = status, headers if headers is not None else {}
        self._buf = io.BytesIO(body)
        self.fp = self._buf

    def read1(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        pass


PROXY = f"http://op:{SECRET}@127.0.0.1:2080"
URLE = urllib.error.URLError
CASES = [   # (id, exception raised by the opener, proxy?, category, hint fragment)
    ("proxy_refused", URLE(ConnectionRefusedError(10061, "refused")), True, net.PROXY_UNREACHABLE, "start the proxy"),
    ("direct_refused", URLE(ConnectionRefusedError(10061, "refused")), False, net.CONNECT_FAILED, "WALLEX_PROXY_URL"),
    ("connect_407", URLE(OSError("Tunnel connection failed: 407 Proxy Authentication Required")), True,
     net.PROXY_CONNECT_FAILED, "requires authentication"),
    ("connect_502", URLE(OSError("Tunnel connection failed: 502 Bad Gateway")), True, net.PROXY_CONNECT_FAILED,
     "cannot reach"),
    ("dns", URLE(socket.gaierror(11001, "getaddrinfo failed")), False, net.DNS_FAILURE, "cannot be resolved"),
    ("dns_proxy_host", URLE(socket.gaierror(11001, "getaddrinfo failed")), True, net.DNS_FAILURE, "proxy host"),
    ("connect_timeout", URLE(TimeoutError("timed out")), False, net.CONNECT_TIMEOUT, "in time"),
    ("connect_timeout_proxy", URLE(TimeoutError("timed out")), True, net.CONNECT_TIMEOUT, "tunnel through"),
    ("unreachable", URLE(OSError(10051, "network unreachable")), False, net.CONNECT_FAILED, "failed"),
    ("read_timeout", socket.timeout("timed out"), False, net.READ_TIMEOUT, "did not arrive in time"),
    ("tls_eof_handshake", URLE(ssl.SSLEOFError(8, "EOF occurred in violation of protocol")), True, net.TLS_EOF,
     "TLS handshake"),
    ("tls_eof_reading", ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), False, net.TLS_EOF,
     "before the response was complete"),
    ("cert", URLE(ssl.SSLCertVerificationError(1, "certificate verify failed")), True, net.TLS_CERT,
     "never disabled"),
    ("tls_other", URLE(ssl.SSLError(1, "[SSL: SOMETHING] odd")), False, net.TLS_OTHER, "stays ON"),
    ("remote_disconnected", http.client.RemoteDisconnected("closed"), False, net.CONNECTION_RESET, "never resent"),
    ("reset", ConnectionResetError(10054, "reset"), True, net.CONNECTION_RESET, "reset"),
    ("incomplete", http.client.IncompleteRead(b"x" * 10, 90), False, net.INCOMPLETE_READ, "never accepted"),
    ("bad_status", http.client.BadStatusLine("garbage"), False, net.PROTOCOL_ERROR, "valid HTTP"),
    ("value_error", ValueError(f"Invalid header value {KEY!r}"), False, net.TRANSPORT_OTHER, "net-check"),
]


@pytest.mark.parametrize("exc,proxied,category,hint", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_every_injected_failure_is_classified_with_a_hint(exc, proxied, category, hint):
    tr = UrllibTransport(opener=FakeOpener(exc), proxy_url=PROXY if proxied else None)
    e = transport_error(lambda: tr.request("GET", "https://api.wallex.ir/v1/account/balances",
                                           {"X-API-Key": KEY}, None, 5.0))
    assert e.category == category and hint in e.hint, (str(e), e.hint)
    text = f"{e} {e.hint} {tr.last_trace.to_dict()}"
    assert no_key(text) and SECRET not in text
    assert tr.last_trace.error["category"] == category
    if category == net.PROXY_CONNECT_FAILED:
        assert tr.last_trace.proxy_status in (407, 502) and e.phase == "proxy_connect"   # only _tunnel says that
    else:      # the fake opener records no phase: a phase is reported only when it was really traced
        assert e.phase is None and tr.last_trace.error["phase"] is None


@pytest.mark.parametrize("exc,phase,proxied,category", [
    (TimeoutError(), "connect", True, net.PROXY_UNREACHABLE),
    (TimeoutError(), "proxy_connect", True, net.CONNECT_TIMEOUT),
    (TimeoutError(), "tls_handshake", False, net.CONNECT_TIMEOUT),
    (TimeoutError(), "response", False, net.READ_TIMEOUT),
    (TimeoutError(), "body", False, net.READ_TIMEOUT),
    (http.client.RemoteDisconnected("x"), "proxy_connect", True, net.PROXY_CONNECT_FAILED),
    (http.client.LineTooLong("x"), "proxy_connect", True, net.PROXY_CONNECT_FAILED),
    (ConnectionResetError(), "proxy_connect", True, net.PROXY_CONNECT_FAILED),
    (ConnectionResetError(), "tls_handshake", False, net.CONNECTION_RESET),
    (ssl.SSLZeroReturnError(6, "closed"), "tls_handshake", True, net.TLS_EOF),
    (ssl.SSLZeroReturnError(6, "closed"), "body", False, net.CONNECTION_RESET),
    (OSError("odd"), "tls_handshake", False, net.TLS_OTHER),
    (RuntimeError("bug"), "body", False, net.TRANSPORT_OTHER),
])
def test_the_traced_phase_refines_the_category(exc, phase, proxied, category):
    p = net.parse_proxy_url(PROXY, source="WALLEX_PROXY_URL") if proxied else None
    d = net.diagnose(exc, phase=phase, proxy=p)
    assert d.category == category and d.phase == phase
    assert SECRET not in d.message


def test_every_category_has_a_one_line_secret_free_hint():
    p = net.parse_proxy_url(PROXY, source="WALLEX_PROXY_URL")
    for cat in sorted(net.CATEGORIES):
        for proxy in (None, p):
            h = net.hint_for(cat, proxy=proxy, status=407)
            assert h and "\n" not in h and len(h) <= 300 and SECRET not in h, (cat, h)
    assert error_label(TransportError("x", category="read_timeout")) == "TransportError[read_timeout]"
    assert error_label(TransportError("x", category="Bad Cat!")) == "TransportError"
    assert error_label(ValueError("x")) == "ValueError"


def test_client_keeps_the_transport_diagnosis_and_scrubs_the_key():
    tr = UrllibTransport(opener=FakeOpener(URLE(OSError(f"Tunnel connection failed: 407 {KEY}"))),
                         proxy_url=PROXY)
    c = WallexClient(KEY, transport=tr, clock=SimClock(), max_read_retries=0)
    e = transport_error_client(lambda: c.get_balances())
    assert e.category == net.PROXY_CONNECT_FAILED and "requires authentication" in e.hint
    assert str(e).startswith("GET /v1/account/balances: transport failure [proxy_connect_failed]")
    assert no_key(str(e) + repr(e.args) + str(e.hint)) and SECRET not in str(e)
    assert str(e).count("[proxy_connect_failed]") == 1 and str(e).count("| hint:") == 1


def transport_error_client(fn: Callable[[], Any]) -> TransportError:
    with pytest.raises(TransportError) as ei:
        fn()
    assert ei.value.__cause__ is None and ei.value.__context__ is None
    return ei.value


def test_a_custom_transport_failure_is_classified_by_the_client_too():
    class Tr:
        def request(self, *a: Any, **k: Any) -> Any:
            raise socket.timeout("timed out")
    e = transport_error_client(lambda: WallexClient(None, public_only=True, transport=Tr(), clock=SimClock(),
                                                    max_read_retries=0).get_markets())
    assert e.category == net.READ_TIMEOUT and e.hint


# =========================================================================== request building (no I/O)
def test_proxy_route_is_a_connect_tunnel_with_credentials_only_for_the_proxy():
    opener = FakeOpener(FakeResponse(200, {"Content-Length": "2"}, b"{}"))
    tr = UrllibTransport(opener=opener, proxy_url=PROXY)
    assert tr.request("GET", "https://api.wallex.ir/v1/account/balances", {"X-API-Key": KEY}, None, 5.0)[2] == b"{}"
    req, _ = opener.requests[0]
    assert req.host == "127.0.0.1:2080" and req._tunnel_host == "api.wallex.ir"     # CONNECT api.wallex.ir:443
    assert req.unredirected_hdrs["Host"] == "api.wallex.ir"                        # the exchange's Host header
    token = base64.b64encode(f"op:{SECRET}".encode()).decode()
    assert req.unredirected_hdrs["Proxy-authorization"] == f"Basic {token}"        # moved into the CONNECT
    assert req.get_header("X-api-key") == KEY                                      # inner request (inside TLS)
    assert tr.last_trace.proxy == "http://***@127.0.0.1:2080" and tr.last_trace.status == 200


def test_direct_route_has_no_tunnel():
    opener = FakeOpener(FakeResponse(200, {}, b"{}"))
    tr = UrllibTransport(opener=opener)
    tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0)
    req, _ = opener.requests[0]
    assert req.host == "api.wallex.ir" and not req._tunnel_host and not req.has_header("Proxy-authorization")


@pytest.mark.parametrize("url", ["https://evil.example/v1/account/balances", "https://api.wallex.ir:8443/x",
                                 "https://api.wallex.ir.evil.example/x"])
def test_transport_sends_the_key_only_to_api_wallex_ir_443(url):
    opener = FakeOpener(FakeResponse())
    tr = UrllibTransport(opener=opener, proxy_url=PROXY)
    e = transport_error(lambda: tr.request("GET", url, {"X-API-Key": KEY}, None, 5.0))
    assert opener.requests == [] and no_key(str(e))


# =========================================================================== framing (no I/O)
def test_a_body_shorter_than_its_content_length_is_never_a_success():
    tr = UrllibTransport(opener=FakeOpener(FakeResponse(200, {"Content-Length": "100"}, b'{"a":1}')))
    e = transport_error(lambda: tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0))
    assert e.category == net.INCOMPLETE_READ and "Content-Length says 100" in str(e)
    assert tr.last_trace.bytes == 7 and tr.last_trace.framing == "content-length 100"


def test_conflicting_content_length_headers_are_a_protocol_error():
    m = Message()
    m["Content-Length"] = "7"
    m["Content-Length"] = "9"
    tr = UrllibTransport(opener=FakeOpener(FakeResponse(200, m, b'{"a":1}')))
    e = transport_error(lambda: tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0))
    assert e.category == net.PROTOCOL_ERROR


@pytest.mark.parametrize("status,headers,body", [(200, {"Content-Length": "7"}, b'{"a":1}'),
                                                 (200, {"Content-Length": "x"}, b'{"a":1}'),
                                                 (204, {"Content-Length": "9"}, b""),
                                                 (200, {"Transfer-Encoding": "chunked", "Content-Length": "99"}, b"{}")])
def test_exact_or_unframed_bodies_pass_the_framing_check(status, headers, body):
    tr = UrllibTransport(opener=FakeOpener(FakeResponse(status, headers, body)))
    assert tr.request("GET", "https://api.wallex.ir/v1/markets", {}, None, 5.0)[2] == body


# =========================================================================== real TLS (127.0.0.1)
def test_real_markets_fixture_over_tls_chunked_then_eof_without_close_notify(servers):
    srv = servers("tls", chunked(MARKETS, size=700))
    tr = trusting()
    c = WallexClient(None, public_only=True, base_url=f"https://127.0.0.1:{srv.port}", transport=tr,
                     clock=SimClock(), max_read_retries=0)
    rules = c.get_markets()
    usd, btc = rules["USDTTMN"], rules["BTCUSDT"]
    assert (usd.price_decimals, usd.qty_decimals, str(usd.min_qty), str(usd.min_notional)) == (0, 2, "0.01", "50000")
    assert (btc.price_decimals, btc.qty_decimals, btc.base, btc.quote) == (2, 8, "BTC", "USDT")
    t = tr.last_trace.to_dict()
    assert t["status"] == 200 and t["framing"] == "chunked" and t["bytes"] == len(MARKETS)
    assert t["tls_version"] in ("TLSv1.2", "TLSv1.3") and t["error"] is None and t["proxy"] is None
    assert {"connect", "tls_handshake", "request", "response", "body"} <= set(t["phases_ms"])
    assert b"Host: 127.0.0.1:%d" % srv.port in srv.requests[0]


def without_ignore_eof(tr: UrllibTransport) -> UrllibTransport:
    if HAS_IGNORE_EOF:
        tr._ctx.options &= ~ssl.OP_IGNORE_UNEXPECTED_EOF
    return tr


@pytest.mark.parametrize("option", [True, False], ids=["ignore_eof_on", "ignore_eof_off"])
def test_complete_unframed_body_then_eof_without_close_notify_is_a_success(servers, option):
    """An EOF without close_notify AFTER the body is not an error: Python's SSLSocket already reads it
    as a plain EOF (suppress_ragged_eofs=True), with or without OP_IGNORE_UNEXPECTED_EOF; whether the
    body is complete is decided by the framing / the JSON parse (next tests)."""
    if option and not HAS_IGNORE_EOF:
        pytest.skip("OP_IGNORE_UNEXPECTED_EOF needs OpenSSL 3 / Python >= 3.10")
    body = json.dumps({"success": True, "message": "ok", "result": {"x": 1}}).encode()
    srv = servers("tls", until_close(body))
    tr = trusting() if option else without_ignore_eof(trusting())
    assert get(tr, f"https://127.0.0.1:{srv.port}/v1/markets")[2] == body
    assert tr.last_trace.framing == "until close" and tr.last_trace.error is None


@pytest.mark.parametrize("option", [True, False], ids=["ignore_eof_on", "ignore_eof_off"])
def test_content_length_body_cut_by_the_peer_is_an_error(servers, option):
    """http.client returns a SHORT body (no IncompleteRead) when a length-framed body ends early; the
    transport's framing check turns that into incomplete_read."""
    body = b'{"success":true,"message":"ok","result":{"pad":"' + b"x" * 900 + b'"}}'
    srv = servers("tls", content_length(body[:400], declared=len(body)))
    tr = trusting() if option else without_ignore_eof(trusting())
    e = transport_error(lambda: get(tr, f"https://127.0.0.1:{srv.port}/v1/markets"))
    assert e.category == net.INCOMPLETE_READ and f"Content-Length says {len(body)}" in str(e)
    assert tr.last_trace.bytes == 400


def test_chunked_body_cut_by_the_peer_is_an_error(servers):
    srv = servers("tls", chunked(MARKETS, size=700, cut=1200))
    e = transport_error(lambda: get(trusting(), f"https://127.0.0.1:{srv.port}/v1/markets"))
    assert e.category == net.INCOMPLETE_READ and e.phase == "body"


def test_unframed_body_cut_by_the_peer_is_rejected_by_the_client(servers):
    srv = servers("tls", until_close(MARKETS[: len(MARKETS) // 2]))
    c = WallexClient(None, public_only=True, base_url=f"https://127.0.0.1:{srv.port}", transport=trusting(),
                     clock=SimClock(), max_read_retries=0)
    with pytest.raises(SchemaError, match="not valid JSON"):      # never rules parsed from half a body
        c.get_markets()


@pytest.mark.parametrize("option", [True, False], ids=["ignore_eof_on", "ignore_eof_off"])
def test_a_proxy_dropping_the_tunnel_during_the_handshake_is_the_operators_eof_error(servers, option):
    """The reported ``UNEXPECTED_EOF_WHILE_READING``: a local proxy answers CONNECT with 200 before its
    own upstream connection exists, then drops the tunnel. Without the option OpenSSL 3 raises
    SSLEOFError, with it SSLZeroReturnError - both during the HANDSHAKE (no byte of a response exists),
    so it stays an error either way, classified with the proxy hint (GETs are retried by the client)."""
    if option and not HAS_IGNORE_EOF:
        pytest.skip("OP_IGNORE_UNEXPECTED_EOF needs OpenSSL 3 / Python >= 3.10")
    px = servers("proxy", "drop_after_200")
    tr = UrllibTransport(proxy_url=f"http://127.0.0.1:{px.port}")
    if not option:
        without_ignore_eof(tr)
    e = transport_error(lambda: get(tr, "https://api.wallex.ir/v1/markets"))
    assert e.category == net.TLS_EOF and e.phase == "tls_handshake"
    assert "TLS handshake" in e.hint and tr.last_trace.proxy_status == 200


def test_an_untrusted_certificate_stays_a_hard_error_with_the_option_on(servers):
    srv = servers("tls", content_length(b"{}"))
    tr = UrllibTransport()                                     # production trust store: test CA unknown
    if HAS_IGNORE_EOF:
        assert tr._ctx.options & ssl.OP_IGNORE_UNEXPECTED_EOF
    e = transport_error(lambda: get(tr, f"https://127.0.0.1:{srv.port}/v1/markets"))
    assert e.category == net.TLS_CERT and e.phase == "tls_handshake" and srv.requests == []


# =========================================================================== fake CONNECT proxy
@pytest.mark.parametrize("mode,status,hint", [("407", 407, "requires authentication"), ("502", 502, "cannot reach"),
                                              ("drop", None, "dropped the tunnel request")])
def test_proxy_refusals_are_classified(servers, mode, status, hint):
    px = servers("proxy", mode)
    tr = UrllibTransport(proxy_url=f"http://op:{SECRET}@127.0.0.1:{px.port}")
    e = transport_error(lambda: get(tr, "https://api.wallex.ir/v1/markets"))
    assert e.category == net.PROXY_CONNECT_FAILED and e.phase == "proxy_connect" and hint in e.hint
    assert tr.last_trace.proxy_status == status and SECRET not in f"{e} {e.hint} {tr.last_trace.to_dict()}"
    assert px.connects[0].startswith("CONNECT api.wallex.ir:443 HTTP/1.1\r\n")


def test_a_proxy_that_answers_plaintext_after_connect_is_a_tls_failure(servers):
    px = servers("proxy", "plaintext")
    e = transport_error(lambda: get(UrllibTransport(proxy_url=f"http://127.0.0.1:{px.port}"),
                                    "https://api.wallex.ir/v1/markets"))
    assert e.category == net.TLS_OTHER and e.phase == "tls_handshake"


def test_nothing_listening_at_the_proxy_is_proxy_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                   # a port where nothing listens
    tr = UrllibTransport(proxy_url=f"http://127.0.0.1:{port}")
    e = transport_error(lambda: get(tr, "https://api.wallex.ir/v1/markets"))
    assert e.category == net.PROXY_UNREACHABLE and e.phase == "connect" and f"127.0.0.1:{port}" in e.hint


def test_tunnel_reaches_the_target_and_proxy_credentials_go_only_into_the_connect(servers):
    srv = servers("tls", chunked(MARKETS))
    px = servers("proxy", "tunnel", srv.port)
    tr = trusting(proxy_url=f"http://op:{SECRET}@127.0.0.1:{px.port}")
    status, _, body = get(tr, f"https://wallex-test.localhost:{srv.port}/v1/markets")
    assert status == 200 and body == MARKETS
    connect = px.connects[0]
    assert connect.startswith(f"CONNECT wallex-test.localhost:{srv.port} HTTP/1.1\r\n")
    assert f"Proxy-Authorization: Basic {base64.b64encode(f'op:{SECRET}'.encode()).decode()}" in connect
    inner = srv.requests[0]
    assert b"Host: wallex-test.localhost:%d" % srv.port in inner and b"Proxy-Authorization" not in inner
    t = tr.last_trace.to_dict()
    assert t["proxy_status"] == 200 and "proxy_connect" in t["phases_ms"] and t["proxy"].endswith(f"***@127.0.0.1:{px.port}")
    assert SECRET not in json.dumps(t)


def test_the_key_never_leaves_the_tunnel_and_the_exchange_host_is_what_tls_verifies(servers):
    """A proxy (here one that sends the tunnel to a server with a TRUSTED certificate for another name)
    can neither read the key nor impersonate api.wallex.ir: TLS checks api.wallex.ir and fails before
    any request is sent."""
    srv = servers("tls", content_length(b"{}"))
    px = servers("proxy", "tunnel", srv.port)
    c = WallexClient(KEY, transport=trusting(proxy_url=f"http://127.0.0.1:{px.port}"), clock=SimClock(),
                     max_read_retries=0)
    e = transport_error_client(lambda: c.get_balances())
    assert e.category == net.TLS_CERT and e.phase == "tls_handshake"
    assert px.connects[0].startswith("CONNECT api.wallex.ir:443 HTTP/1.1\r\n")
    seen = bytes(px.received).decode("latin-1")
    assert no_key(seen) and "X-API-Key" not in seen and srv.requests == []
    assert no_key(f"{e} {e.hint}")
