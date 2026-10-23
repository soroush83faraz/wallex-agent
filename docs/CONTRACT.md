# CONTRACT — module APIs and Wallex API facts (authoritative for all implementers)

Foundation already in the repo (do **not** change signatures without telling the orchestrator):
`types.py`, `errors.py`, `config.py`, `clock.py`, `interfaces.py`, `tests/test_contract.py`.
Python ≥ 3.10, **stdlib only** (pytest for tests). Money/quantities = `Decimal` (parse JSON with
`json.loads(..., parse_float=Decimal)`; never `float` for money). Analytics features may be float.
Run tests with `python -m pytest` from the repo root (package is installed editable).
Windows host: use `pathlib`, UTF-8 everywhere, no POSIX-only calls without a fallback.

## 1. Wallex facts (from the official reference snapshot in docs/ + live public captures)

Snapshot: `docs/wallex_official_api_reference_snapshot_2026-10-22.txt`. Real captures: `tests/fixtures/*_real*.json`.

* Base `https://api.wallex.ir`; JSON envelope `{"success": bool, "message": str, "result": ...}`.
* Auth: header `X-API-Key: <key>` only (no HMAC/signature). Keys: ≤90 days, ≤3 per account, may have
  IP allow-list; withdrawal permission is a separate permission (we never want it).
* Limits: 100 req/s default; `POST /v1/account/orders` 10 req/s. Our client defaults far lower.
* PUBLIC (no key): `GET /v1/markets`, `GET /v1/depth?symbol=`, `GET /v1/trades?symbol=`,
  `GET /v1/udf/history?symbol=&resolution=(1|60|180|360|720|1D)&from=&to=` (returns
  `{"s":"ok","t":[...],"o":[...],"h":[],"l":[],"c":[],"v":[]}` with *string* numbers, no envelope;
  `{"s":"no_data"}` when empty).
* PRIVATE (key): `GET /v1/account/balances`, `GET /v1/account/fee`, `POST /v1/account/orders`,
  `GET /v1/account/orders/{clientOrderId}`, `DELETE /v1/account/orders?clientOrderId=`,
  `GET /v1/account/openOrders[?symbol=]`, `GET /v1/account/trades[?symbol=&side=]`.
* **Never callable by this program** (hard deny-list, tested): `/v1/account/money-withdrawal`,
  `/v1/account/crypto-withdrawal`, `/v1/account/profile`, `/v1/account/card-numbers`,
  `/v1/account/ibans`, any `/otc/` path, anything not in the allow-list above.
* `/v1/markets` → `result.symbols[SYM]` with `baseAsset, quoteAsset, baseAssetPrecision,
  quotePrecision, stepSize, tickSize, minQty, minNotional, (maxQty)`; numbers may be JSON numbers
  (`1e-08`). **`stepSize` = quantity DECIMAL PLACES, `tickSize` = price DECIMAL PLACES.**
  Live 2026-10-22: BTCUSDT stepSize 8, tickSize 2, minQty 1e-08, minNotional 1 (USDT);
  USDTTMN stepSize 2, tickSize 0, minQty 0.01, minNotional 50000 (TMN). USDTTMN ≈ 272,000 TMN/USDT,
  so 5,000,000 TMN ≈ 18.3 USDT. **`stats.bidPrice/askPrice` in /markets are stale vs /depth: use depth.**
* `/v1/depth?symbol=` → `result.ask[]`/`result.bid[]` of `{price, quantity, sum}`; live captures show
  `price`/`quantity` as JSON **numbers**, docs show strings — accept both, via Decimal. 50 levels/side.
  No timestamp → we stamp receipt time. Sort defensively; reject crossed/empty books (OrderBook.build).
* `GET /v1/account/balances` → `result.balances[ASSET] = {asset, faName, fiat, value, locked}`
  (strings). Whether `value` includes `locked` is undocumented ⇒ `Balance.spendable = max(0, value-locked)`.
* `GET /v1/account/fee` → `result` is a **mapping** `SYMBOL -> {makerFeeRate, takerFeeRate,
  recent_days_sum}` (the doc sample is syntactically broken and shows `[` — accept a mapping, or a list of
  such objects with a `symbol` key; anything else ⇒ `SchemaError`). Missing symbol ⇒ fee UNKNOWN ⇒ no trade.
* `POST /v1/account/orders` body (JSON): `symbol, type("LIMIT"), side, price, quantity, client_id`
  (strings). Response `result`: `symbol,type,side,price,origQty,origSum,executedPrice,executedQty,
  executedSum,executedPercent,status(NEW|FILLED|CANCELED),active,clientOrderId,created_at`.
  `type: MARKET` exists but its parameter syntax (sum?) is **undocumented ⇒ we only use LIMIT**; there is
  no documented time-in-force: an "aggressive" (marketable) limit that is not fully filled RESTS (status
  NEW) and must be cancelled by us. Whether a supplied `client_id` is returned verbatim in
  `clientOrderId` (doc example shows a `LIMIT-<uuid>` form) is **unverified**: store whatever
  `clientOrderId` comes back and use it for later queries, and record both.
* HTTP 422 on invalid order ⇒ `result.error_code`: 1001 price precision, 1002 qty precision,
  1003 MARKET cannot have price, 1004 qty below minimum, 1005 qty or sum required, 1006 insufficient
  balance, 1007 stop price required, 1008 price required, 1009 stop precision, 1010 qty & sum together,
  1011 sum precision. These are definitive rejections (`RejectedError`).
* `GET /v1/account/orders/{clientOrderId}` same shape as create result. `DELETE` has **no documented
  response body** ⇒ treat 2xx as "cancel requested", then verify with GET. `openOrders` →
  `result.orders[]`. Partial-fill status string is undocumented: derive `PARTIALLY_FILLED` when
  `active and 0 < executedQty < origQty`; any unrecognised status ⇒ `OrderStatus.UNKNOWN`.
* `GET /v1/account/trades` → `result.AccountLatestTrades[]`: `symbol, quantity, price, sum, fee,
  feeCoefficient, feeAsset, isBuyer, timestamp(ISO)`. **No order id.** Buy fee asset in the doc sample is
  the *base* asset (fee deducted from what you receive); sell fee presumably *quote* — never assume:
  always read `feeAsset`.
* No server-time endpoint (use local clock). No ticker beyond depth. Persian/ASCII digits: not an issue
  in JSON.

Everything marked "undocumented/unverified" is an explicit **blocker for LIVE until probed**; the code must
fail closed and report it (see `check-live` in §9).

## 2. `exchange/wallex.py` + `exchange/probe.py`  (owner: security-executor)

```python
class Transport(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str,str], body: bytes|None,
                timeout: float) -> tuple[int, Mapping[str,str], bytes]: ...   # raises TransportError
class UrllibTransport: ...                      # default, stdlib urllib, verified TLS, no redirects; route =
                                                # WALLEX_PROXY_URL / opted-in env proxy / direct (net.py);
                                                # failures: TransportError(category, hint, phase)
class TokenBucket: ...                          # uses Clock; blocking acquire(n=1)
class WallexClient:                             # implements interfaces.Exchange
    def __init__(self, api_key: str|None, *, base_url="https://api.wallex.ir",
                 transport: Transport|None=None, clock: Clock|None=None, timeout: float=10.0,
                 max_rps: float=5.0, order_rps: float=2.0, public_only: bool=False,
                 max_read_retries: int=3)
    # + the 10 Exchange methods; plus get_book alias == get_order_book so it is a BookSource too
```
Rules: (1) allow-list of `(method, path-pattern)`; anything else raises `ForbiddenEndpointError`
*before* any network I/O; withdrawal/profile/bank/otc paths explicitly denied (unit-tested).
(2) `public_only=True` (used by paper/backtest-fetch/diagnose): private paths **and every non-GET** raise
`ForbiddenEndpointError`; the key is never attached. (3) Key only in the `X-API-Key` header; `repr`/`str`
of client, requests, errors and logs never contain it (scrub the key and any `X-API-Key` header text from
exception messages and from stored bodies). (4) Reads retry (backoff, honour `Retry-After`) on
TransportError/5xx/429, never on 4xx; **POST/DELETE are never retried by the client**. (5) POST outcome
classification: 2xx+`success:true` → OrderInfo; 4xx (incl. 422) → `RejectedError(status, message,
error_code)`; 401/403 → `AuthError`; 429 → `RateLimitError`; 5xx → `ServerError`; network error/timeout/
undecodable 2xx body/`success` not true on 2xx → treat as **unknown outcome** (`TransportError` or
`SchemaError`) — never `RejectedError`. (6) Strict schema validation (required keys, Decimal parsing,
side ∈ BUY/SELL, symbol match, qty/price positive) → `SchemaError` with a field path. Every response is
validated; unknown extra keys are tolerated. (7) Quantities/prices sent as plain decimal strings (use
`types.plain`), `client_id` ≤ 64 chars `[A-Za-z0-9_-]`. (8) Request timeout from config; max response size
cap (e.g. 5 MB). (9) `get_candles` parses the UDF shape (`no_data` → `[]`).

`exchange/probe.py`:
`probe_private_schemas(client, symbols=("BTCUSDT","USDTTMN")) -> SchemaReport` performs **GET-only**
calls (balances, fee, openOrders, trades) against a real key and returns a JSON-able report
`{ok: bool, checks: [{name, ok, detail}], undocumented: [...], at: iso, base_url}` containing **no secrets
and no balances values** (only shapes/booleans/counts). It also records the observed shape of `fee`
(mapping vs list), asset names present, and whether `value`/`locked` look consistent. Never POSTs.

## 3. `store.py`, `state.py`, `portfolio.py`  (owner: executor)

`SqliteStore(path: str|Path, *, clock: Clock|None=None)` (implements `EventStore`):
SQLite WAL, `synchronous=FULL`, foreign keys, integrity check on open, all Decimals stored as TEXT,
single-process **instance lock** (`acquire_instance_lock()` raising `InstanceLockError` (a
`RuntimeError`) when another process - or another store in this process - holds
`<dir>/instance.lock`: an OS advisory lock, `flock(LOCK_EX|LOCK_NB)` on POSIX / `msvcrt.locking` on one
fixed byte on Windows, held for the store's lifetime; the lock file is never deleted or renamed and
the kernel releases the lock when the holder dies, so there is no PID/staleness heuristic; the
file's pid/host JSON is informational; `probe_lock(dir)` tells whether a store is in use). API:
`append / iter_events` (EventStore) · `kv_get(key, default=None) / kv_set(key, jsonable)` ·
`transaction()` context manager (BEGIN IMMEDIATE) · orders: `insert_order_intent(OrderRecord)`,
`update_order(client_id, *, lifecycle=None, **fields)` (enforces `ORDER_TRANSITIONS`; illegal →
`InvariantError`), `get_order(client_id)`, `find_order_by_exchange_id(...)`,
`list_orders(lifecycles=None)`, `unresolved_orders()` · fills: `add_fill(FillRecord) -> bool`
(idempotent by `fill_key`; returns False if already present), `list_fills(client_id=None)`.
`OrderRecord` (dataclass): `client_id, exchange_client_order_id|None, symbol, side, price, quantity,
purpose, decision_id|None, lifecycle, executed_qty, executed_sum, fee_total, fee_asset|None,
fee_source ('exchange'|'estimated'|None), created_ts, updated_ts, last_raw: dict|None, note`.
`FillRecord`: `fill_key, client_id, symbol, side, price, quantity, quote_sum, fee, fee_asset,
fee_source, ts`.
`InMemoryStore` (same API, for tests/backtest speed) lives in `store.py` too.

`state.py`: `StateMachine(store, clock)`: `.state` (persisted in kv; default UNFUNDED),
`.transition(to, reason, evidence: dict|None=None, *, force=False)` validates against
`STATE_TRANSITIONS` (illegal → `InvariantError`; `force` only for the operator `resume` path),
appends a `state_transition` event, `.history()`. `derive_state(balances, open_orders, rules, books) ->
BotState` pure helper used by restart reconciliation (e.g. TMN ≥ conversion minimum & no USDT/BTC → UNFUNDED;
USDT sufficient & no BTC → READY; BTC ≥ min sell → BTC_HELD; open buy → BUY_PENDING …).

`portfolio.py`: `Portfolio(store, initial_tmn)` — Decimal ledger **driven only by recorded fills**:
`apply_fill(FillRecord)` (buy: quote −= sum, base += qty; sell: base −= qty, quote += sum; then the fee is
deducted from `fee_asset`, whatever it is) · `expected_balances() -> dict[str, Decimal]` ·
`reconcile(actual: dict[str, Balance], tolerance: Mapping[str,Decimal]) -> ReconcileReport(ok, drifts)` ·
`btc_position() -> Position(qty, avg_cost_usdt (fee-inclusive), entry_ts, realized_pnl_usdt, round_trips,
wins, losses)` · `liquidation_value(books, fees) -> EquityReport(equity_tmn_net, equity_tmn_mid,
breakdown…)`: BTC valued by *walking BTCUSDT bids* net of the sell fee, then all USDT (incl. proceeds)
valued by *walking USDTTMN bids* net of fee; TMN as is (**exit costs deducted**) ·
`record_equity(ts, equity_tmn_net)`, `peak_equity()`, `drawdown()`, `equity_curve()` · everything is
rebuilt from the store on start (event-sourced) so restart never loses the ledger. Also `record_conversion`
metadata (initial USDT cost basis in TMN) so P&L in TMN and in USDT are both reportable.

## 4. `market_data.py` and `agent/`  (owner: executor)

`market_data.py`: `LiveBookSource(client)` (public GET depth, stamps `ts=clock.now()`; implements
`BookSource`), `ReplayBookSource(series, clock)` (deterministic, advances with a `SimClock`),
`SyntheticMarket(seed, n_steps, step_s, regime_schedule)` generating **plausible BTCUSDT + USDTTMN** books
(spread ~5–10 bps, depth shaped like `tests/fixtures/depth_*_real.json`, regimes: trend-up, trend-down,
chop, spike, low-vol) with optional fat tails — labelled synthetic everywhere,
`candles_to_books(candles, depth_template)`, `load_candles(path)` (CSV `time,open,high,low,close,volume`
or Wallex UDF JSON), `save_candles`, `ensure_fresh(book, now, max_age) -> None|raises StaleQuote`.
`SyntheticMarket` must make the **demo backtest work with no network and no API key**.

`agent/` (no I/O to the exchange except through `AgentTools`; deterministic given a seed):
* `features.py` — `compute_features(history, books, position, clock_info) -> dict[str, float|str]`: returns
  over several windows, realized vol, z-score, range position, book imbalance (depth-weighted), spread bps,
  visible depth (quote), USDTTMN spread/drift, `time_left_frac`, regime label (`trend_up|trend_down|chop|
  volatile|quiet|unknown`), data-sufficiency flag. No look-ahead.
* `tools.py` — registry of tools the agent can call, each `Tool(name, description, cost, run(ctx) ->
  ToolResult(ok, data, error, latency_ms))`: `read_book`, `read_candles`, `estimate_round_trip_cost`
  (walk books + fees + slippage), `trend_signal`, `reversion_signal`, `book_pressure_signal`,
  `fx_check`, `risk_headroom`. The planner picks tools **adaptively** (cheap first; fetch candles/extra
  tools only when features are insufficient or uncertainty is high; record which tools were used and why).
* `strategies.py` — hypothesis generators (`trend_follow`, `mean_reversion`, `book_pressure`, `stand_aside`)
  each returning `Hypothesis(name, direction, expected_move_bps, horizon_s, confidence, rationale,
  invalidation)`; moves are *calibrated* by memory (shrink toward 0 when realized/predicted history is poor).
* `planner.py` — `Planner(memory, config, rng_seed)`: `decide(obs: Observation, ctx: PlanContext) ->
  Decision` (types.Decision) with `intent: TradeIntent|None`. Chooses strategy via contextual bandit
  (regime × strategy posteriors from memory, Thompson sampling for exploration *only among strategies whose
  estimated net edge is positive*; exploration never overrides the edge/confidence gates), computes
  `estimated_net_edge = calibrated_expected_move − (spread + slippage + buy fee + sell fee)` using
  `OrderBook.walk_*` for the intended size, lists **alternatives** with rejection reasons, can choose
  HOLD (the default), CONVERT/BUY/SELL/STOP. Exit decisions (take-profit / thesis-invalidation / time-stop)
  are also agent decisions; the *hard* stop-loss, deadline and drawdown exits belong to `risk.py`.
  No "always trade": if costs/edge/liquidity unknown ⇒ HOLD with the reason.
* `memory.py` — `AgentMemory(store)`: event-sourced (`decision`, `outcome`, `reflection` events); rebuilds
  bandit posteriors + per-strategy calibration + recent lessons by replaying events on start (restart-safe);
  `record_decision(Decision)`, `record_outcome(...)`, `snapshot()`, `posteriors()`, `lessons(n)`.
* `reflection.py` — `reflect(decision, outcome) -> Reflection(hypothesis, result, error_bps, lesson,
  updates)`; also scores HOLD/abstain decisions after their horizon (counterfactual "would it have paid
  after costs?") so memory learns when abstaining was right; produces a printable **trace** record
  (`observe → plan → tool → action → verify → reflect`) consumed by reporting.
Expose a small `AgentBrain` facade: `AgentBrain(config, store_or_memory, clock).step_decide(...)`,
`.on_outcome(...)`, `.reflect_pending(now, books)`.

## 5. `risk.py`, `exchange/sim.py`, `execution.py`  (owner: executor)

`risk.py` — `RiskGate(config, clock)`: `evaluate(intent: TradeIntent, ctx: RiskContext) -> RiskVerdict(
approved, reasons: list[str], adjusted: TradeIntent|None)`. Pure & deterministic; `RiskContext` carries
state, balances (spendable), fees (or None ⇒ unknown), rules, books, position, equity/peak, round trips
done, last-trade/last-loss timestamps, deadline, kill flag, unresolved-order flag. Hard gates (each with a
distinct reason string, each unit-tested): kill switch · deadline passed (only exits/cancels allowed) ·
unresolved/unknown orders · book stale/crossed/missing · fee unknown · market rules unknown · spread >
max · depth within slippage < min · after-rounding qty/notional violate rules · intended notional >
exposure cap · insufficient spendable balance (incl. fee margin) · sell qty > held · round-trip cap ·
cooldown / min interval · drawdown breach (⇒ only exits, then STOP) · net edge below hurdle (independent
recomputation) · state/intent mismatch. `forced_exit(ctx) -> TradeIntent|None` implements hard stop-loss
(mark-to-bid net of exit fee), drawdown stop, deadline flatten *only if* `final_policy` says so.

`exchange/sim.py` — `SimExchange(book_source, clock, rules, fees, balances, *, rng=None, latency_s=0)`
implements `Exchange` **entirely in-process** (never any network): validates orders like Wallex (422-style
`RejectedError` with `error_code` 1001/1002/1004/1006…), unique client ids, immediate taker matching by
walking the book to the limit price (partial fill if depth lacks), remainder rests and is matched on later
book updates, fee = taker rate charged in **received asset** (buy → base, sell → quote) reported via
`get_trades`, balances with `locked` for resting orders. Public methods delegate to `book_source`.
Fault injection for tests: `inject(kind, **kw)` with kinds `post_timeout_after_accept` (order created but
raise TransportError), `post_timeout_before_accept` (raise, nothing created), `post_5xx`, `rate_limit`,
`partial_fill(frac)`, `reject(code)`, `get_order_404`, `cancel_race_filled`, `stale_book`, `network_down`.
Also an `account_value()` helper for paper reports. Seeded/deterministic.

`execution.py` — `ExecutionEngine(exchange, store, portfolio, state_machine, config, clock, audit)`:
* `submit(intent, decision_id) -> ExecutionResult(order_ids, filled_qty, status, notes)`:
  (1) compute limit price from intent (aggressive: walk-to-slippage-cap; passive: best price), round with
  `MarketRules`, size from intent with fee headroom, re-validate `rules.violations`, (2) generate
  unique `client_id` (`wa-<run>-<seq>-<rand>`), (3) **persist `OrderRecord(INTENT)` then `SENDING` in one
  committed transaction BEFORE `create_order`**, (4) POST exactly once, (5) outcomes: ack → `OPEN/PARTIAL/
  FILLED` + fills recorded; `RejectedError` → `REJECTED` (definitive; may re-plan a *new* intent with
  adjusted size for 1004/1006/1002/1001 at most `max_orders_per_intent`); `TransportError/ServerError/
  SchemaError/timeout` → `UNKNOWN_OUTCOME` then `resolve_unknown` — **never blind retry**.
* `resolve_unknown(rec)`: poll `get_order(client_id)` (and the alias returned id if known), then
  `list_open_orders`, then `get_trades` + balance delta vs the pre-send snapshot, within
  `unknown_resolve_window_s`; found → adopt; "not found anywhere" + balances unchanged → `ABANDONED`
  but the engine enforces a quarantine (no new order on that symbol/side until a later re-check agrees); any
  contradiction → state `STOPPED` with reason `needs_manual_reconcile` (and a loud audit record).
* `poll_and_manage()`: for every unresolved order: refresh via `get_order`; record new fills
  (executed_qty delta ⇒ `FillRecord` with fee from matching `get_trades` rows by symbol/side/time/qty/
  not-already-attributed, falling back to `executed_sum × fee_rate` flagged `fee_source='estimated'`);
  cancel orders older than `order_timeout_s` (cancel → verify via `get_order`; handle "filled during
  cancel" race); partial fills handled (state stays `*_PENDING` only while an order is active; remainder
  after cancel becomes position or dust).
* `reconcile_startup()`: restart recovery — load unresolved orders, resolve each against the exchange,
  rebuild portfolio from fills, compare with real balances, derive/confirm `BotState`, refuse to proceed on
  unexplained drift above tolerance (→ STOPPED).
* Invariants (enforced + tested): one `client_id` ⇒ one POST ever; no sell qty > ledger-held ∧ spendable;
  no second entry while any order unresolved; after crash at any point, restart never creates a duplicate.
* Every state transition is derived from observed exchange facts, not from "I sent it".
* Kill switch: `kill_requested()` (file `<data_dir>/KILL` or in-process flag) checked before every send.
  On kill: cancel open orders (cancel only — never sells), state STOPPED. No withdrawal code exists.
* (2026-10-22 review fixes) Every ABANDONED order that reached the exchange stays on a permanent watch
  list (`watched_abandoned()`, derived from the orders table) re-checked by `check_abandoned()` on every
  poll, before every new order and at startup; found => cancel (cancel only) + STOPPED `late_accept`;
  a quarantine lifts only after `QUARANTINE_MIN_CHECKS` (3) verified-absent re-checks each
  >= `unknown_resolve_window_s` apart. `check_balances()`: an in-run shortfall (no order in flight)
  blocks new orders and, confirmed >= `SHORTFALL_CONFIRM_S` later, STOPS with the numbers.
  `convert_tmn_to_usdt(decision_id=, should_stop=)` runs the RiskGate check before EVERY round.
  `settle_for_shutdown()` adopts-or-leaves unknown orders, then cancels open ones. `unstop_check()` /
  `operator_unstop()` back the `unstop` command (read-only reconcile, forced transition only if clean).

## 6. `runner.py`, `modes/`, `live_gate.py`, `reporting.py`, `cli.py`  (owner: executor)

* `runner.py` — `AgentRunner`: loop **observe → reason/plan → tool/action → verify → reflect** each cycle:
  poll/reconcile first; `forced_exit` from RiskGate; `AgentBrain` decision; `RiskGate.evaluate`;
  `ExecutionEngine.submit`; verify by re-reading order/balances; reflect; write audit; sleep
  (`loop_interval_s`) with kill/deadline checks; stop at `competition_hours` after the confirmed start
  timestamp (stored in kv at first live fill-eligible cycle and on resume — a restart never resets it).
  Conversion TMN→USDT is its own resumable stage (state UNFUNDED→CONVERTING→READY).
* `modes/__init__.py`: `build_runtime(config, mode, **opts) -> Runtime` wiring per mode
  (**backtest**: SimExchange over `ReplayBookSource` + SimClock, no network, no key ·
  **paper**: SimExchange over `LiveBookSource(WallexClient(public_only=True))` — zero private/ non-GET
  calls, enforced by the client · **live**: `WallexClient` with key, only after `live_gate`).
* `live_gate.py`: `check_live_allowed(config, *, flag_ack: bool, typed_phrase: str|None, now)` raises
  `LiveNotAllowedError` unless **all** hold: `config.mode is LIVE` ∧ `config.enable_live` ∧ CLI flag
  `--i-understand-real-money` ∧ fresh passing `state/selftest.json` whose `source_hash` equals the current
  source tree hash ∧ fresh passing `state/live_schema_attestation.json` (from `check-live`, GET-only,
  ≤ 24 h old, same base_url) ∧ interactive typed phrase `START LIVE <initial_tmn>` ∧ no KILL file. Also
  provides `run_selftest()` (runs the project's pytest/unittest suite programmatically, writes the
  attestation with `source_hash`) and `source_hash()`.
  (2026-10-22 review fixes) + `data_dir_explicit` (Config field; `load_config` makes `data_dir`
  absolute) and `selftest_test_count` (>= `MIN_SELFTEST_TESTS` = 1000 passed) conditions; the selftest
  runs pytest with an allow-listed environment (`selftest_env`); `source_hash` covers every file of
  the package (any type, except `__pycache__`) plus `pyproject.toml` and `tests/**/*.py`;
  `LIVE_ARMED` is a JSON record (`arming_record`/`write_arming`/`read_arming`/`arming_problems`:
  phrase, source_hash, armed_at, expires_at = armed_at + competition_hours + 24 h, initial_tmn,
  base_url, data_dir) and `live --resume` uses it ONLY to continue a competition already started in
  the live store. MODEL: these gates stop accidents and automated misuse, not a deliberate attacker
  running as the same OS user (who can read the key and forge evidence).
* `reporting.py`: `AuditLog` (append-only `audit.jsonl`, fsync per line, each line has `seq`, UTC `ts`,
  `strategy_version`, `run_id`, `prev_hash`/`hash` chain; **never contains secrets**), `FinalReport` →
  `final_report.json` (config redacted, mode, start/end, initial vs final value TMN (net of exit costs) and
  USDT, P&L, fees paid by asset, trades, order ids, fills, drawdown, equity curve, per-decision hypothesis /
  result, tool-use stats, memory posteriors, **honest caveats** incl. synthetic/paper/assumed-fee labels and
  unverified API items) plus `render_trace(audit_path) -> str` (human-readable observe→…→reflect trace).
* `cli.py` (`wallex-agent`, also `python -m wallex_agent`): subcommands `demo` (offline synthetic backtest),
  `backtest --data FILE|--synthetic`, `fetch-history` (public GET → local CSV), `diagnose` (public
  endpoints, rules/fees-known check, prints USDT/TMN math), `paper`, `check-live` (GET-only schema
  attestation), `selftest`, `live` (all gates), `status`, `kill` (writes KILL file; optional
  `--cancel-open` which only cancels), `report`, `trace`. Default `TRADING_MODE=paper`. Exit codes
  meaningful; never prints the key. (2026-10-22: `kill` prints the absolute KILL path and refuses
  (exit 6) when no store/lock exists under DATA_DIR unless `--force-write`; `--cancel-open` acts on
  the live store whenever it exists; `report` writes `final_report.rebuilt.json`; new `unstop`.)
* Packaging extras (owner of §6): `README_FA.md` (Persian: install, config, demo, 5-min smoke test, 24-h
  run, VPS + systemd unit file in `deploy/wallex-agent.service`, emergency stop, known limitations &
  every untested/unverified API item), `docs/ARCHITECTURE_FA.md`, `docs/API_UNCERTAINTIES_FA.md`.

## 7. Cross-cutting rules for every implementer

1. Fail closed. Unknown/ambiguous ⇒ do nothing and say why.
2. No code path may call withdrawal/transfer endpoints; none exists.
3. Secrets never in logs/reports/exceptions/repr/test output.
4. Deterministic & testable: inject `Clock`, `Exchange`, `BookSource`, rng seeds. No `time.sleep` in
   tests (use `SimClock`). No real network in tests (use fixtures/fakes). Tests must be fast (<30 s total).
5. Match the foundation's style: type hints, short module docstrings, comments only for non-obvious "why".
6. Own only your files; don't edit another owner's files — ask the orchestrator via your final report.
7. Final report: files created, public API actually exposed (if it deviates from this contract, say
   exactly how), tests run + results, known gaps. No claims of live-exchange testing.
