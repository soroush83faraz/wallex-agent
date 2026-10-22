"""In-process Wallex simulator for backtest/paper/tests. It NEVER touches the network.

Semantics (CONTRACT §1/§5): LIMIT orders only; Wallex-style 422 ``RejectedError`` codes (1001 price
precision, 1002 qty precision, 1004 below minimum qty/notional, 1006 insufficient balance); unique
client ids; immediate taker matching by walking the book up to the limit price, the remainder rests
with its funds ``locked`` and is matched against later book updates; fees are charged in the RECEIVED
asset (buy -> base, sell -> quote) at the taker rate (``resting_fee='maker'`` charges resting fills the
maker rate) and reported only through ``get_trades`` rows, which carry no order id. ``Balance.value`` here INCLUDES ``locked`` (so ``spendable = value - locked`` is the free amount).

Fault injection (``inject(kind, times=1, **kw)``) reproduces the dangerous exchange behaviours the
execution engine must survive. Everything is deterministic for a given ``rng`` seed.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from ..clock import Clock
from ..errors import (InvariantError, OrderNotFound, RateLimitError, RejectedError, SchemaError,
                      ServerError, TransportError)
from ..types import (BPS, ONE, ZERO, Balance, Candle, FeeRates, MarketRules, OrderBook, OrderInfo,
                     OrderRequest, OrderStatus, Side, Trade, dec, quantize_down)

CREATE_FAULTS = frozenset({"post_timeout_after_accept", "post_timeout_before_accept", "post_5xx",
                           "undecodable", "rate_limit", "reject"})
FAULT_KINDS = CREATE_FAULTS | frozenset({
    "partial_fill", "get_order_404", "get_order_error", "cancel_race_filled", "cancel_fail",
    "stale_book", "network_down", "balances_error", "trades_error"})
_CLIENT_ID_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class StaticBookSource:
    """Settable ``BookSource`` for tests/paper scaffolding. ``stamp_now`` re-stamps every read with the
    clock time (like a live feed stamping receipt time); the levels only change via ``set_book``."""

    def __init__(self, clock: Clock, books: Mapping[str, OrderBook] | None = None, *,
                 stamp_now: bool = True) -> None:
        self._clock = clock
        self._books: dict[str, OrderBook] = dict(books or {})
        self.stamp_now = stamp_now

    def set_book(self, symbol: str, bids: Iterable[tuple[Decimal, Decimal]],
                 asks: Iterable[tuple[Decimal, Decimal]]) -> OrderBook:
        book = OrderBook.build(symbol, bids, asks, self._clock.now())
        self._books[symbol] = book
        return book

    def put(self, book: OrderBook) -> None:
        self._books[book.symbol] = book

    def shift(self, symbol: str, bps: Decimal, decimals: int | None = None) -> OrderBook:
        """Move every level by ``bps`` (prices re-quantized to ``decimals`` if given)."""
        b = self._books[symbol]
        f = ONE + dec(bps) / BPS

        def mv(p: Decimal) -> Decimal:
            return quantize_down(p * f, decimals) if decimals is not None else p * f

        return self.set_book(symbol, [(mv(lv.price), lv.quantity) for lv in b.bids],
                             [(mv(lv.price), lv.quantity) for lv in b.asks])

    def get_book(self, symbol: str) -> OrderBook:
        book = self._books.get(symbol)
        if book is None:
            raise TransportError(f"no book for {symbol}")
        return replace(book, ts=self._clock.now()) if self.stamp_now else book

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        return []


@dataclass
class _Fault:
    kind: str
    remaining: int
    kw: dict[str, Any]


@dataclass
class _SimOrder:
    client_id: str
    alias: str
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    created_at: str
    executed_qty: Decimal = ZERO
    executed_sum: Decimal = ZERO
    status: OrderStatus = OrderStatus.NEW
    active: bool = True
    locked: Decimal = ZERO   # still-reserved amount (quote for BUY, base for SELL)
    hold_key: int | None = None  # injected partial fill: no further matching until the book changes

    def info(self) -> OrderInfo:
        status = self.status
        if self.active and 0 < self.executed_qty < self.qty:
            status = OrderStatus.PARTIALLY_FILLED
        return OrderInfo(
            client_order_id=self.alias, symbol=self.symbol, side=self.side, type="LIMIT",
            price=self.price, orig_qty=self.qty, executed_qty=self.executed_qty,
            executed_sum=self.executed_sum,
            executed_price=(self.executed_sum / self.executed_qty) if self.executed_qty > 0 else None,
            status=status, active=self.active, created_at=self.created_at, raw_status=self.status.value,
            orig_sum=self.price * self.qty)


class SimExchange:
    """Implements ``interfaces.Exchange`` entirely in memory."""

    def __init__(self, book_source: Any, clock: Clock, rules: Mapping[str, MarketRules],
                 fees: Mapping[str, FeeRates], balances: Mapping[str, Decimal | Balance], *,
                 rng: random.Random | None = None, latency_s: float = 0.0, echo_client_id: bool = True,
                 lookup_by_client_id: bool = True, resting_fee: str = "taker",
                 max_trades: int = 100) -> None:
        if resting_fee not in ("maker", "taker"):
            raise ValueError("resting_fee must be 'maker' or 'taker'")
        self.book_source = book_source
        self.clock = clock
        self.rules = dict(rules)
        self.fees = dict(fees)
        self.hidden_fees: set[str] = set()     # symbols whose fee get_fees() omits ("unknown fee")
        self.rng = rng or random.Random(0)
        self.latency_s = float(latency_s)
        self.echo_client_id = echo_client_id
        self.lookup_by_client_id = lookup_by_client_id
        self.resting_fee = resting_fee
        self.max_trades = max_trades
        self._total: dict[str, Decimal] = {}
        self._locked: dict[str, Decimal] = {}
        for asset, b in balances.items():
            v = b.value if isinstance(b, Balance) else dec(b)
            if v < 0:
                raise ValueError(f"negative initial balance for {asset}")
            self._total[asset] = v
            self._locked[asset] = ZERO
        self._orders: dict[str, _SimOrder] = {}     # by client_id
        self._by_alias: dict[str, _SimOrder] = {}
        self._seq: list[_SimOrder] = []
        self._trades: list[Trade] = []
        self._faults: list[_Fault] = []
        # liquidity already taken from a given book snapshot: (symbol, side) -> (levels key, {price: qty})
        self._consumed: dict[tuple[str, Side], tuple[int, dict[Decimal, Decimal]]] = {}
        self.post_counts: Counter[str] = Counter()   # create_order calls per client_id (incl. faulted)
        self.calls: Counter[str] = Counter()

    # ------------------------------------------------------------------ fault injection
    def inject(self, kind: str, times: int = 1, **kw: Any) -> None:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}")
        if times < 1:
            raise ValueError("times must be >= 1")
        self._faults.append(_Fault(kind, times, dict(kw)))

    def clear_faults(self) -> None:
        self._faults.clear()

    def pending_faults(self) -> list[str]:
        return [f.kind for f in self._faults]

    def _take(self, kinds: Iterable[str]) -> _Fault | None:
        ks = set(kinds)
        for f in self._faults:
            if f.kind in ks:
                f.remaining -= 1
                if f.remaining <= 0:
                    self._faults.remove(f)
                return f
        return None

    def _call(self, name: str) -> None:
        self.calls[name] += 1
        if self.latency_s > 0:
            self.clock.sleep(self.latency_s)
        if self._take(("network_down",)):
            raise TransportError(f"simulated network down during {name}")

    # ------------------------------------------------------------------ public data
    def get_markets(self) -> dict[str, MarketRules]:
        self._call("get_markets")
        return dict(self.rules)

    def get_order_book(self, symbol: str) -> OrderBook:
        self._call("get_order_book")
        book = self.book_source.get_book(symbol)
        f = self._take(("stale_book",))
        if f:
            book = replace(book, ts=book.ts - float(f.kw.get("age_s", 3600.0)))
        return book

    get_book = get_order_book

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        self._call("get_candles")
        return self.book_source.get_candles(symbol, resolution, start, end)

    # ------------------------------------------------------------------ account
    def get_balances(self) -> dict[str, Balance]:
        self._call("get_balances")
        self.match_resting()
        if self._take(("balances_error",)):
            raise ServerError(502, "simulated balances failure")
        return {a: Balance(a, self._total[a], self._locked.get(a, ZERO)) for a in sorted(self._total)}

    def get_fees(self) -> dict[str, FeeRates]:
        self._call("get_fees")
        return {s: f for s, f in self.fees.items() if s not in self.hidden_fees}

    def get_trades(self, symbol: str | None = None, side: Side | None = None) -> list[Trade]:
        self._call("get_trades")
        self.match_resting()
        if self._take(("trades_error",)):
            raise TransportError("simulated trades failure")
        rows = [t for t in reversed(self._trades)
                if (symbol is None or t.symbol == symbol) and (side is None or t.side is side)]
        return rows[: self.max_trades]

    # ------------------------------------------------------------------ orders
    def create_order(self, req: OrderRequest) -> OrderInfo:
        self._call("create_order")
        self.post_counts[req.client_id] += 1
        self.match_resting()
        partial = self._take(("partial_fill",))
        fault = self._take(CREATE_FAULTS)
        kind = fault.kind if fault else None
        if kind == "post_timeout_before_accept":
            raise TransportError("simulated timeout before the exchange accepted the order")
        if kind == "rate_limit":
            raise RateLimitError(float(fault.kw.get("retry_after", 1.0)))
        if kind == "reject":
            code = fault.kw.get("code", 1006)
            raise RejectedError(422, f"simulated rejection {code}", code)
        accepted = bool(fault.kw.get("accepted", True)) if fault else True
        if kind == "post_5xx" and not accepted:
            raise ServerError(int(fault.kw.get("status", 503)), "simulated 5xx (not accepted)")
        if kind == "undecodable" and not accepted:
            raise SchemaError("simulated undecodable response (not accepted)")
        self._validate(req)
        order = self._accept(req, partial_frac=dec(partial.kw.get("frac", "0.5")) if partial else None)
        if kind == "post_timeout_after_accept":
            raise TransportError("simulated timeout AFTER the exchange accepted the order")
        if kind == "post_5xx":
            raise ServerError(int(fault.kw.get("status", 503)), "simulated 5xx after accept")
        if kind == "undecodable":
            raise SchemaError("simulated undecodable 2xx body after accept")
        return order.info()

    def _validate(self, req: OrderRequest) -> None:
        r = self.rules.get(req.symbol)
        if r is None:
            raise RejectedError(422, f"invalid symbol {req.symbol}", None)
        if req.type != "LIMIT":
            raise RejectedError(422, "simulator supports LIMIT only", None)
        cid = req.client_id
        if not cid or len(cid) > 64 or set(cid) - _CLIENT_ID_OK:
            raise RejectedError(422, "invalid client_id", None)
        if cid in self._orders or cid in self._by_alias:
            raise RejectedError(422, "duplicate client_id", None)
        if req.price <= 0:
            raise RejectedError(422, "price required", 1008)
        if req.quantity <= 0:
            raise RejectedError(422, "quantity required", 1005)
        if quantize_down(req.price, r.price_decimals) != req.price:
            raise RejectedError(422, "price precision", 1001)
        if quantize_down(req.quantity, r.qty_decimals) != req.quantity:
            raise RejectedError(422, "quantity precision", 1002)
        if (req.quantity < r.min_qty or req.price * req.quantity < r.min_notional
                or (r.max_qty is not None and req.quantity > r.max_qty)):
            raise RejectedError(422, "quantity below minimum", 1004)
        if req.side is Side.BUY:
            if req.price * req.quantity > self.free(r.quote):
                raise RejectedError(422, "insufficient balance", 1006)
        elif req.quantity > self.free(r.base):
            raise RejectedError(422, "insufficient balance", 1006)

    def _accept(self, req: OrderRequest, partial_frac: Decimal | None) -> _SimOrder:
        r = self.rules[req.symbol]
        alias = req.client_id if self.echo_client_id else f"LIMIT-{self.rng.getrandbits(64):016x}"
        o = _SimOrder(client_id=req.client_id, alias=alias, symbol=req.symbol, side=req.side,
                      price=req.price, qty=req.quantity, created_at=self._iso(self.clock.now()))
        lock_asset, lock_amt = (r.quote, req.price * req.quantity) if req.side is Side.BUY else (r.base, req.quantity)
        self._locked[lock_asset] = self._locked.get(lock_asset, ZERO) + lock_amt
        o.locked = lock_amt
        self._orders[o.client_id] = o
        self._by_alias[o.alias] = o
        self._seq.append(o)
        cap = None
        if partial_frac is not None:
            cap = quantize_down(req.quantity * partial_frac, r.qty_decimals)
        self._match(o, taker=True, cap=cap)
        return o

    def _lookup(self, order_id: str) -> _SimOrder:
        o = self._by_alias.get(order_id)
        if o is None and self.lookup_by_client_id:
            o = self._orders.get(order_id)
        if o is None:
            raise OrderNotFound(f"order {order_id} not found")
        return o

    def get_order(self, client_order_id: str) -> OrderInfo:
        self._call("get_order")
        self.match_resting()
        f = self._take(("get_order_404", "get_order_error"))
        if f and f.kind == "get_order_404":
            raise OrderNotFound(f"simulated 404 for {client_order_id}")
        if f:
            raise TransportError("simulated get_order failure")
        return self._lookup(client_order_id).info()

    def list_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        self._call("list_open_orders")
        self.match_resting()
        return [o.info() for o in self._seq if o.active and (symbol is None or o.symbol == symbol)]

    def cancel_order(self, client_order_id: str) -> None:
        self._call("cancel_order")
        self.match_resting()
        f = self._take(("cancel_fail", "cancel_race_filled"))
        o = self._lookup(client_order_id)
        if f and f.kind == "cancel_fail":
            raise TransportError("simulated cancel failure (order untouched)")
        if f and f.kind == "cancel_race_filled":
            if o.active:
                self._fill(o, o.price, o.qty - o.executed_qty, taker=False)
            if f.kw.get("raise_error"):
                raise RejectedError(400, "order is not active", None)
            return None
        if not o.active:
            raise RejectedError(400, "order is not active", None)
        o.status, o.active = OrderStatus.CANCELED, False
        self._release(o)
        return None

    # ------------------------------------------------------------------ matching
    def match_resting(self) -> None:
        """Match resting orders against the CURRENT books (called on every private call)."""
        for o in list(self._seq):
            if o.active:
                self._match(o, taker=False)

    def _book_or_none(self, symbol: str) -> OrderBook | None:
        try:
            return self.book_source.get_book(symbol)
        except Exception:   # a feed outage just means no matching this tick
            return None

    def _match(self, o: _SimOrder, *, taker: bool, cap: Decimal | None = None) -> None:
        book = self._book_or_none(o.symbol)
        if book is None or not o.active:
            return
        r = self.rules[o.symbol]
        key = hash((book.bids, book.asks))
        ks = (o.symbol, o.side)
        if o.hold_key == key:
            return
        prev = self._consumed.get(ks)
        if prev is None or prev[0] != key:
            prev = (key, {})
            self._consumed[ks] = prev
        consumed = prev[1]
        limit_qty = o.qty if cap is None else min(o.qty, cap)
        remaining = limit_qty - o.executed_qty
        levels = book.asks if o.side is Side.BUY else book.bids
        for lv in levels:
            if remaining <= 0:
                break
            if (o.side is Side.BUY and lv.price > o.price) or (o.side is Side.SELL and lv.price < o.price):
                break
            avail = lv.quantity - consumed.get(lv.price, ZERO)
            take = quantize_down(min(remaining, avail), r.qty_decimals)
            if take <= 0:
                continue
            self._fill(o, lv.price, take, taker=taker)
            consumed[lv.price] = consumed.get(lv.price, ZERO) + take
            remaining -= take
        if cap is not None and o.active:
            o.hold_key = key   # the injected partial fill sticks until the book changes

    def _fill(self, o: _SimOrder, price: Decimal, qty: Decimal, *, taker: bool) -> None:
        r = self.rules[o.symbol]
        fr = self.fees[o.symbol]
        rate = fr.taker if taker or self.resting_fee == "taker" else fr.maker
        quote_sum = price * qty
        if o.side is Side.BUY:
            fee, fee_asset = qty * rate, r.base
            self._add(r.quote, -quote_sum)
            release = o.price * qty
            self._locked[r.quote] -= release
            o.locked -= release
            self._add(r.base, qty - fee)
        else:
            fee, fee_asset = quote_sum * rate, r.quote
            self._add(r.base, -qty)
            self._locked[r.base] -= qty
            o.locked -= qty
            self._add(r.quote, quote_sum - fee)
        o.executed_qty += qty
        o.executed_sum += quote_sum
        now = self.clock.now()
        self._trades.append(Trade(symbol=o.symbol, side=o.side, price=price, quantity=qty,
                                  quote_sum=quote_sum, fee=fee, fee_asset=fee_asset, fee_coefficient=rate,
                                  timestamp=self._iso(now), ts_epoch=now))
        if o.executed_qty >= o.qty:
            o.status, o.active = OrderStatus.FILLED, False
            self._release(o)
        self._check_non_negative()

    def _release(self, o: _SimOrder) -> None:
        r = self.rules[o.symbol]
        asset = r.quote if o.side is Side.BUY else r.base
        self._locked[asset] -= o.locked
        o.locked = ZERO

    def _add(self, asset: str, amount: Decimal) -> None:
        self._total[asset] = self._total.get(asset, ZERO) + amount
        self._locked.setdefault(asset, ZERO)

    def _check_non_negative(self) -> None:
        for a, v in self._total.items():
            lk = self._locked.get(a, ZERO)
            if v < 0 or lk < 0 or lk > v:
                raise InvariantError(f"simulator balance broken for {a}: total={v} locked={lk}")

    # ------------------------------------------------------------------ helpers (tests / paper)
    @staticmethod
    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def total(self, asset: str) -> Decimal:
        return self._total.get(asset, ZERO)

    def free(self, asset: str) -> Decimal:
        return self._total.get(asset, ZERO) - self._locked.get(asset, ZERO)

    def totals(self) -> dict[str, Decimal]:
        return dict(self._total)

    def order_count(self) -> int:
        return len(self._seq)

    def max_posts_per_client_id(self) -> int:
        return max(self.post_counts.values(), default=0)

    def all_orders(self) -> list[OrderInfo]:
        return [o.info() for o in self._seq]

    def account_value(self) -> dict[str, Any]:
        """Balances plus a gross mid-price valuation in the fiat of the conversion pair (for paper
        reports; NOT net of exit costs — the portfolio's ``liquidation_value`` is the honest number)."""
        out: dict[str, Any] = {"balances": {a: str(v) for a, v in sorted(self._total.items())},
                               "locked": {a: str(v) for a, v in sorted(self._locked.items()) if v}}
        mids: dict[str, Decimal] = {}
        for sym in self.rules:
            b = self._book_or_none(sym)
            if b is not None:
                mids[sym] = b.mid
        # value every asset in the quote of the pair whose base is that asset, chained once
        fiat_pairs = {r.base: (s, r.quote) for s, r in self.rules.items()}
        values: dict[str, Decimal] = {}
        quotes = {r.quote for r in self.rules.values()}
        fiat = next((q for q in quotes if q not in fiat_pairs), None)
        if fiat is None:
            out["value_mid"] = None
            return out

        def to_fiat(asset: str, amt: Decimal, depth: int = 0) -> Decimal | None:
            if asset == fiat:
                return amt
            if asset not in fiat_pairs or depth > 3:
                return None
            sym, q = fiat_pairs[asset]
            if sym not in mids:
                return None
            return to_fiat(q, amt * mids[sym], depth + 1)

        total = ZERO
        for a, v in self._total.items():
            fv = to_fiat(a, v)
            if fv is None and v != 0:
                out["value_mid"] = None
                out["unvalued"] = a
                return out
            values[a] = fv or ZERO
            total += fv or ZERO
        out["value_mid"] = str(total)
        out["value_asset"] = fiat
        return out
