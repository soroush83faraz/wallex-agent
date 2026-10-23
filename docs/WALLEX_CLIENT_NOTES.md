# Wallex client notes: ambiguities found while implementing `exchange/wallex.py`

Sources: `docs/wallex_official_api_reference_snapshot_2026-10-22.txt` (the official reference), the
real public captures in `tests/fixtures/*_real*.json`, and a public-GET smoke run on 2026-10-22.
**No private endpoint was ever called** (no key exists). Items marked **UNVERIFIED** can only be settled
by a real (small) order and are listed in the `undocumented` section of every probe report.

## 1. Errors and contradictions in the official reference

| # | What the reference says | What is real / what the client does |
|---|---|---|
| 1 | Fee sample: `"result": [ "BTCTMN": {...} ]`. That is not valid JSON, and the text describes a mapping. | Accept a mapping or a list of objects carrying `symbol`. Anything else raises `SchemaError`. A symbol missing from the answer is **omitted** (fee unknown, so no trade). The probe records the live shape. |
| 2 | The markets table names `quoteAssetPrecision`, but the samples and live data use `quotePrecision`. | Neither field is used. |
| 3 | `stepSize` / `tickSize` = "number of decimals". The doc sample (BTCUSDT `stepSize 6`, `minQty 1e-06`) is stale; live on 2026-10-22 it is `8` / `1e-08`. | Parsed as integer decimal-place counts in [0, 18]. An increment-style value (`1e-08`) raises `SchemaError`. So does a `minQty` finer than `stepSize`. |
| 4 | Depth: `price` is a number-string and `quantity` a number. | Live sends **both as JSON numbers**, including integers (TMN prices, whole-dollar BTC prices). Strings and numbers are both accepted, parsed to `Decimal`. `sum` (a string) is ignored. Floats never appear because JSON is parsed with `parse_float=Decimal`; `NaN`/`Infinity` raise `SchemaError`. |
| 5 | Public trades: `quantity` is a "number", but the sample shows a string. | (Not used by the client.) |
| 6 | Account trades: `feeAsset` is a "number-string", but the sample shows an asset code (`"PAXG"`). The section description is copy-pasted from "open orders". | `feeAsset` is parsed as an asset code. The fee asset is always read from the field, never assumed. |
| 7 | Order sample: `executedPrice: "62999995…"` while `executedQty` is 0. | `executed_price` is `None` unless `executedQty > 0`. |
| 8 | The curl examples contain invalid JSON (missing comma after `"type"`, trailing commas). | The body is always built by `json.dumps`. |
| 9 | The only HTTP status table (401/402/403/404/405/409/422/429/500) is in the **Wallgate** section. For the spot API only `422 + result.error_code` (1001–1011) is documented. | See section 2, "Rejections". |
| 10 | `maxQty` is documented. | Absent live for BTCUSDT/USDTTMN, so it is optional (must be >= `minQty` when present). |

## 2. Undocumented behaviour and how the client fails closed

* **Rejections.** Any 4xx except 401/403/408/409/429 raises `RejectedError` (definitively not placed;
  `error_code` comes from `result.error_code`, else `None`).
  * **408 and 409** raise `AmbiguousResponseError`, a `ServerError` subclass, so the outcome is UNKNOWN.
    Wallgate documents 409 as "duplicate order id", which means an order with that id may exist.
  * **1xx/3xx/>=600** also raise `AmbiguousResponseError`.
  * **401/403** raise `AuthError`, and **429** raises `RateLimitError`. Neither is a `RejectedError`, so the
    execution engine should treat both as UNKNOWN outcome on `create_order` (fail closed).
* **Unknown outcome on POST.** Network errors, timeouts, 5xx, an oversized/undecodable/compressed 2xx
  body, `success` not exactly `true`, or an ack whose symbol/side/type differs from the request all raise
  `TransportError`, `ServerError` or `SchemaError`, never `RejectedError`. The client sends a POST
  exactly once, with no retries.
* **Partial-fill status string (UNVERIFIED).** Only `NEW`/`FILLED`/`CANCELED` are documented.
  * `PARTIALLY_FILLED` is derived when `active` and `0 < executedQty < origQty`. This applies to `NEW` and
    also to any unknown string (a quantity fact; such an order stays "working").
  * Contradictions map to `UNKNOWN`, keeping `raw_status`: `FILLED`/`CANCELED` with `active=true`,
    `NEW` with `active=false`, or `NEW` fully executed.
* **`active` for terminal orders (UNVERIFIED).** The client assumes `false` for FILLED/CANCELED; see above.
* **`client_id` echo (UNVERIFIED).** The doc example's `clientOrderId` is `LIMIT-<uuid>`. The client returns
  whatever `clientOrderId` comes back (printable ASCII, at most 128 chars) and does not require it to equal
  our `client_id`. It is also unverified whether `GET /v1/account/orders/{id}` finds an order by **our**
  id. Ids put into URLs must match `[A-Za-z0-9_-]{1,64}`.
* **Cancel answer (UNVERIFIED).** No body or status is documented. Outcomes:
  * 2xx with an empty body, or JSON without `success:false`: returns `None` ("cancel requested").
  * Any other 2xx body: `SchemaError`.
  * 404: `OrderNotFound`.
  * Never retried. The caller must verify with `get_order`.
* **"Order not found".** A 404, or a 2xx with an empty `result`, raises `OrderNotFound`. A 2xx with
  `success:false`, or another 4xx, raises `SchemaError`, never `OrderNotFound`. A bare 404 cannot tell
  "no such order" from "no such route", so the exception carries the (scrubbed) server wording, and the
  probe's `private.order_not_found` check records it for a human to read.
* **Retry-After.** Not documented. It is honoured for reads (delta-seconds or HTTP-date). A value above
  10 s is surfaced as an error instead of slept through. Reads retry on transport errors, 5xx and 429
  (default 3 retries, backoff 0.5/1/2 s) and never on 4xx.
* **Balances (UNVERIFIED).** Whether `value` includes `locked` is unknown. `Balance.spendable` is
  `max(0, value - locked)`, and the probe reports whether `locked <= value` everywhere.
* **Fee asset of SELL fills.** Presumably the quote asset. The probe classifies it from trade history
  when such trades exist; otherwise it is UNVERIFIED.
* **Timestamps.** `…Z` (trades) and `….000000Z` (orders) both parse. A naive timestamp raises
  `SchemaError`, because a wrong timezone would silently corrupt fill attribution.
* **Booleans.** `active`/`isBuyer` given as PHP-style `0`/`1` are accepted. Anything else raises
  `SchemaError`.
* **MARKET orders.** The parameters (`sum`?) are undocumented, so the client refuses every type but `LIMIT`.
  There is no time-in-force: a marketable LIMIT may rest and must be cancelled by the caller.
* **Filters.** It is unknown whether `symbol`/`side` filters on `openOrders`/`trades` are honoured.
  Mismatching rows raise `SchemaError`, and the probe reports `honoured: true/false/null` (null means
  nothing to compare).
* **POST parameters.** The docs allow query or body, as JSON or form encoding. The client sends a JSON body only.

## 3. Network and deployment

* The default `UrllibTransport` **ignores every proxy** (environment variables and the OS/Windows
  proxy settings): an implicit proxy changes the source IP, which breaks an IP-allow-listed key, and routes
  traffic through infrastructure nobody chose. The route is explicit (`exchange/net.py`,
  `modes.network_transport`): `WALLEX_PROXY_URL=http://[user:pass@]host:port` wins; otherwise, only with
  `WALLEX_USE_ENV_PROXY=true`, `https_proxy`/`HTTPS_PROXY` then `http_proxy`/`HTTP_PROXY` of the PROCESS
  environment, honouring `NO_PROXY`; the OS proxy settings are never read (behaviour change: before, the
  env opt-in also picked up the Windows registry proxy through `urllib.request.getproxies()`). A malformed
  proxy is a `ConfigError` (never a silent direct route); `https://` (TLS to the proxy, which the stdlib
  cannot do: the CONNECT and credentials would go out in clear) and `socks` are refused. Through a proxy the
  request is a CONNECT tunnel to the URL's host:port; TLS inside it is verified against that host (never the
  proxy), the `Host` header is the exchange's, proxy credentials go only into the CONNECT
  (`Proxy-Authorization`), and the transport refuses an `X-API-Key` request to anything but
  `api.wallex.ir:443`. Credentials are masked everywhere (`http://***@host:port`).
* `wallex-agent net-check` exercises exactly this transport (GET only, one attempt per request) and prints
  each request's phases (TCP connect, CONNECT status, TLS handshake + version, HTTP status, bytes, framing,
  elapsed), the USDTTMN/BTCUSDT rules and both books; with a key, the private GETs through a read-only client.
* Every transport failure is a `TransportError` with `category` (`proxy_unreachable`,
  `proxy_connect_failed`, `dns_failure`, `connect_failed`, `connect_timeout`, `read_timeout`,
  `tls_eof_no_close_notify`, `tls_cert_verify_failed`, `tls_other`, `connection_reset`, `incomplete_read`,
  `protocol_error`, `transport_other`), a one-line `hint` and the traced `phase` (None when the phase was not
  traced); the message is `[category] detail | hint: ...`, scrubbed (key, proxy credentials) before it is
  truncated, and raised outside `except` blocks. The engine's `unknown_outcome` alert carries
  `category`/`hint`, and decline reasons read e.g. `rules_unavailable: TransportError[read_timeout]`.
* `OP_IGNORE_UNEXPECTED_EOF` is set on the verifying context when available. It is NOT what decides
  completeness: CPython already reads an EOF without `close_notify` as a plain EOF on `recv`
  (`suppress_ragged_eofs=True`), and `http.client.read1()` returns a SHORT body (no `IncompleteRead`) when a
  Content-Length body ends early, so the transport checks the framing itself (`incomplete_read`); a cut
  chunked body raises `IncompleteRead`; an unframed body must still parse as JSON and pass the schema.
  Observed root cause of `SSLEOFError: UNEXPECTED_EOF_WHILE_READING` behind a local proxy (2026-10-23): the
  proxy answers `CONNECT` with 200 before its own upstream exists, then drops the tunnel DURING the TLS
  handshake (reproduced in `tests/test_net.py`); with the option it surfaces as `SSLZeroReturnError` - an
  error either way, classified `tls_eof_no_close_notify` / phase `tls_handshake`.
* Redirects are never followed; a 3xx is an error. TLS >= 1.2 with certificate and hostname
  verification. The wire debug level is pinned to 0. The response is capped at 5 MiB. The timeout applies
  per socket operation while connecting and reading the status line/headers, and as a TOTAL deadline for
  the body: each body read is one `read1` (at most one recv) with the socket timeout set to the time
  left, and a watchdog shuts the socket down at the deadline (also covers chunk-size lines read inside
  http.client). DNS resolution itself is **not** bounded by the timeout (stdlib limitation).
* Error texts (bodies, messages, transport failures) are scrubbed of the key in FULL before they are
  truncated, so a key straddling a cut can never leave its prefix behind.
* Public smoke, 2026-10-22, through the proxy:
  * markets: BTCUSDT (step 8, tick 2, minQty 1e-8, minNotional 1) and USDTTMN (2, 0, 0.01, 50000);
  * depth: 50/50 levels, prices as JSON numbers, no `Content-Encoding` (identity honoured);
  * UDF: 1-minute candles parse.
