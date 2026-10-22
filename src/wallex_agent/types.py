"""Core value types shared by every module. Pure, dependency-free, Decimal-based."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Context, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)
_CTX = Context(prec=60)


# --------------------------------------------------------------------------- numbers
def dec(value: Any) -> Decimal:
    """Strict conversion to a finite Decimal. bool/None/NaN/inf are rejected."""
    if isinstance(value, bool) or value is None:
        raise TypeError(f"not a number: {value!r}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    else:
        raise TypeError(f"not a number: {value!r}")
    if not d.is_finite():
        raise ValueError(f"non-finite number: {value!r}")
    return d


def quantize_down(x: Decimal, decimals: int) -> Decimal:
    return x.quantize(ONE.scaleb(-decimals), rounding=ROUND_DOWN, context=_CTX)


def quantize_up(x: Decimal, decimals: int) -> Decimal:
    return x.quantize(ONE.scaleb(-decimals), rounding=ROUND_UP, context=_CTX)


def plain(x: Decimal) -> str:
    """Plain (non-exponent) string for API params and logs, trailing zeros trimmed."""
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# --------------------------------------------------------------------------- enums
class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class OrderStatus(str, Enum):
    """Exchange-reported status. Unrecognised strings map to UNKNOWN (fail closed)."""

    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # derived: NEW/active with 0 < executed < orig
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


class OrderLifecycle(str, Enum):
    """Local, persisted lifecycle of an order intent (persist-before-send)."""

    INTENT = "INTENT"                    # written to DB, nothing sent yet
    SENDING = "SENDING"                  # about to POST / POST in flight (crash here => unknown)
    OPEN = "OPEN"                        # exchange acknowledged, active, no fill yet
    PARTIAL = "PARTIAL"                  # active with some fills
    FILLED = "FILLED"                    # terminal
    CANCELED = "CANCELED"                # terminal (may carry partial fills)
    REJECTED = "REJECTED"                # terminal: definitive exchange rejection, no order
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"  # POST outcome unknown; must be resolved, never re-sent
    ABANDONED = "ABANDONED"              # terminal: resolved as 'never reached exchange'


TERMINAL_LIFECYCLES = frozenset(
    {OrderLifecycle.FILLED, OrderLifecycle.CANCELED, OrderLifecycle.REJECTED, OrderLifecycle.ABANDONED}
)
UNRESOLVED_LIFECYCLES = frozenset(
    {OrderLifecycle.INTENT, OrderLifecycle.SENDING, OrderLifecycle.OPEN, OrderLifecycle.PARTIAL,
     OrderLifecycle.UNKNOWN_OUTCOME}
)
ORDER_TRANSITIONS: Mapping[OrderLifecycle, frozenset[OrderLifecycle]] = {
    OrderLifecycle.INTENT: frozenset({OrderLifecycle.SENDING, OrderLifecycle.ABANDONED}),
    OrderLifecycle.SENDING: frozenset(
        {OrderLifecycle.OPEN, OrderLifecycle.PARTIAL, OrderLifecycle.FILLED, OrderLifecycle.CANCELED,
         OrderLifecycle.REJECTED, OrderLifecycle.UNKNOWN_OUTCOME}),
    OrderLifecycle.OPEN: frozenset(
        {OrderLifecycle.PARTIAL, OrderLifecycle.FILLED, OrderLifecycle.CANCELED}),
    OrderLifecycle.PARTIAL: frozenset(
        {OrderLifecycle.PARTIAL, OrderLifecycle.FILLED, OrderLifecycle.CANCELED}),
    OrderLifecycle.UNKNOWN_OUTCOME: frozenset(
        {OrderLifecycle.OPEN, OrderLifecycle.PARTIAL, OrderLifecycle.FILLED, OrderLifecycle.CANCELED,
         OrderLifecycle.ABANDONED}),
    OrderLifecycle.FILLED: frozenset(),
    OrderLifecycle.CANCELED: frozenset(),
    OrderLifecycle.REJECTED: frozenset(),
    OrderLifecycle.ABANDONED: frozenset(),
}


class BotState(str, Enum):
    UNFUNDED = "UNFUNDED"
    CONVERTING = "CONVERTING"
    READY = "READY"
    BUY_PENDING = "BUY_PENDING"
    BTC_HELD = "BTC_HELD"
    SELL_PENDING = "SELL_PENDING"
    STOPPED = "STOPPED"


# UNFUNDED -> CONVERTING -> READY -> BUY_PENDING -> BTC_HELD -> SELL_PENDING -> READY/STOPPED
STATE_TRANSITIONS: Mapping[BotState, frozenset[BotState]] = {
    BotState.UNFUNDED: frozenset({BotState.CONVERTING, BotState.READY, BotState.STOPPED}),
    BotState.CONVERTING: frozenset(
        {BotState.CONVERTING, BotState.READY, BotState.UNFUNDED, BotState.STOPPED}),
    BotState.READY: frozenset({BotState.BUY_PENDING, BotState.CONVERTING, BotState.STOPPED}),
    BotState.BUY_PENDING: frozenset({BotState.BTC_HELD, BotState.READY, BotState.STOPPED}),
    BotState.BTC_HELD: frozenset({BotState.SELL_PENDING, BotState.STOPPED}),
    BotState.SELL_PENDING: frozenset({BotState.READY, BotState.BTC_HELD, BotState.STOPPED}),
    BotState.STOPPED: frozenset(),  # terminal for a run; operator `resume` may force-reconcile
}


class Action(str, Enum):
    CONVERT_TMN_USDT = "CONVERT_TMN_USDT"
    BUY_BTC = "BUY_BTC"
    SELL_BTC = "SELL_BTC"
    HOLD = "HOLD"          # deliberately do nothing (stand aside / keep position)
    CANCEL = "CANCEL"      # cancel a stale resting order
    STOP = "STOP"          # halt trading (deadline, drawdown, kill)


# --------------------------------------------------------------------------- market rules
@dataclass(frozen=True)
class MarketRules:
    """Per-symbol trading rules from ``GET /v1/markets``.

    NOTE: Wallex's ``stepSize`` / ``tickSize`` are NUMBERS OF DECIMAL PLACES (quantity / price),
    not increments. ``min_notional`` is in QUOTE currency (USDT for BTCUSDT, TMN for USDTTMN).
    """

    symbol: str
    base: str
    quote: str
    qty_decimals: int
    price_decimals: int
    min_qty: Decimal
    min_notional: Decimal
    max_qty: Decimal | None = None

    def round_qty(self, qty: Decimal) -> Decimal:
        """Always rounds DOWN (never ask to trade more than computed)."""
        return quantize_down(qty, self.qty_decimals)

    def round_price(self, price: Decimal, side: Side) -> Decimal:
        """BUY rounds down, SELL rounds up -> never worse than the caller's slippage cap."""
        if side is Side.BUY:
            return quantize_down(price, self.price_decimals)
        return quantize_up(price, self.price_decimals)

    def violations(self, price: Decimal, qty: Decimal) -> list[str]:
        out: list[str] = []
        if price <= 0:
            out.append("price<=0")
        if qty <= 0:
            out.append("qty<=0")
        if out:
            return out
        if quantize_down(price, self.price_decimals) != price:
            out.append(f"price precision>{self.price_decimals}")
        if quantize_down(qty, self.qty_decimals) != qty:
            out.append(f"qty precision>{self.qty_decimals}")
        if qty < self.min_qty:
            out.append(f"qty<{plain(self.min_qty)}")
        if self.max_qty is not None and qty > self.max_qty:
            out.append(f"qty>{plain(self.max_qty)}")
        if price * qty < self.min_notional:
            out.append(f"notional<{plain(self.min_notional)}")
        return out


# --------------------------------------------------------------------------- order book
@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class Walk:
    """Result of consuming book liquidity."""

    filled_qty: Decimal
    quote_sum: Decimal
    worst_price: Decimal | None
    complete: bool  # requested size fully available within the visible book

    @property
    def avg_price(self) -> Decimal | None:
        return self.quote_sum / self.filled_qty if self.filled_qty > 0 else None


@dataclass(frozen=True)
class OrderBook:
    """Snapshot. bids sorted best(highest)->worst, asks best(lowest)->worst. ``ts`` = local epoch
    seconds at which WE received it (not an exchange timestamp: Wallex depth carries none)."""

    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts: float

    @staticmethod
    def build(symbol: str, bids: Iterable[tuple[Decimal, Decimal]],
              asks: Iterable[tuple[Decimal, Decimal]], ts: float) -> "OrderBook":
        """Sorts, drops zero/negative levels, rejects empty or crossed books (ValueError)."""
        b = sorted(((p, q) for p, q in bids if p > 0 and q > 0), key=lambda x: -x[0])
        a = sorted(((p, q) for p, q in asks if p > 0 and q > 0), key=lambda x: x[0])
        if not b or not a:
            raise ValueError(f"{symbol}: empty book side")
        if b[0][0] >= a[0][0]:
            raise ValueError(f"{symbol}: crossed/locked book bid={b[0][0]} ask={a[0][0]}")
        return OrderBook(symbol, tuple(BookLevel(p, q) for p, q in b),
                         tuple(BookLevel(p, q) for p, q in a), ts)

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.best_ask - self.best_bid) / self.mid * BPS

    def age(self, now: float) -> float:
        return now - self.ts

    def walk_qty(self, side: Side, qty: Decimal) -> Walk:
        """Take ``qty`` base units: BUY consumes asks, SELL consumes bids."""
        levels = self.asks if side is Side.BUY else self.bids
        remaining, spent, worst = qty, ZERO, None
        for lv in levels:
            if remaining <= 0:
                break
            take = min(remaining, lv.quantity)
            spent += take * lv.price
            remaining -= take
            worst = lv.price
        return Walk(qty - remaining, spent, worst, remaining <= 0)

    def walk_quote(self, side: Side, quote_amount: Decimal) -> Walk:
        """Spend (BUY) / receive (SELL) up to ``quote_amount`` quote units walking the book."""
        levels = self.asks if side is Side.BUY else self.bids
        remaining_q, got, worst = quote_amount, ZERO, None
        for lv in levels:
            if remaining_q <= 0:
                break
            lv_quote = lv.price * lv.quantity
            if lv_quote <= remaining_q:
                got += lv.quantity
                remaining_q -= lv_quote
            else:
                got += remaining_q / lv.price
                remaining_q = ZERO
            worst = lv.price
        return Walk(got, quote_amount - remaining_q, worst, remaining_q <= 0)

    def depth_quote(self, side: Side, within_bps: Decimal) -> Decimal:
        """Quote-currency liquidity available within ``within_bps`` of the best price."""
        levels = self.asks if side is Side.BUY else self.bids
        best = levels[0].price
        lim = best * (ONE + within_bps / BPS) if side is Side.BUY else best * (ONE - within_bps / BPS)
        tot = ZERO
        for lv in levels:
            if (side is Side.BUY and lv.price > lim) or (side is Side.SELL and lv.price < lim):
                break
            tot += lv.price * lv.quantity
        return tot


# --------------------------------------------------------------------------- account types
@dataclass(frozen=True)
class Balance:
    """``value`` and ``locked`` as returned by Wallex. Whether ``value`` already EXCLUDES locked is
    not documented, so ``spendable`` is conservative: max(0, value - locked)."""

    asset: str
    value: Decimal
    locked: Decimal = ZERO

    @property
    def spendable(self) -> Decimal:
        return max(ZERO, self.value - self.locked)


@dataclass(frozen=True)
class FeeRates:
    symbol: str
    maker: Decimal
    taker: Decimal


@dataclass(frozen=True)
class OrderRequest:
    client_id: str
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    type: str = "LIMIT"  # only LIMIT is used: MARKET syntax is not verified (fail closed)


@dataclass(frozen=True)
class OrderInfo:
    client_order_id: str
    symbol: str
    side: Side
    type: str
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal
    executed_sum: Decimal       # quote currency actually traded
    executed_price: Decimal | None
    status: OrderStatus
    active: bool
    created_at: str | None = None
    raw_status: str = ""
    orig_sum: Decimal | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELED) or not self.active

    @property
    def remaining_qty(self) -> Decimal:
        return max(ZERO, self.orig_qty - self.executed_qty)


@dataclass(frozen=True)
class Trade:
    """One account fill (``GET /v1/account/trades``). NOTE: carries NO order id."""

    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    quote_sum: Decimal
    fee: Decimal
    fee_asset: str
    fee_coefficient: Decimal
    timestamp: str      # ISO-8601 as returned
    ts_epoch: float


@dataclass(frozen=True)
class Candle:
    ts: int  # epoch seconds, candle open
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


# --------------------------------------------------------------------------- agent contract
@dataclass(frozen=True)
class TradeIntent:
    """What the agent wants executed. Risk gate may shrink/deny; execution turns it into orders."""

    purpose: str                 # 'convert' | 'entry' | 'exit' | 'flatten'
    side: Side
    symbol: str
    quantity: Decimal            # base units (pre-rounding; execution rounds DOWN to step)
    reference_price: Decimal     # best bid/ask observed when the decision was made
    max_slippage_bps: Decimal    # cap vs reference_price for the limit price
    style: str = "aggressive_limit"   # 'aggressive_limit' (marketable) | 'passive_limit'
    note: str = ""


@dataclass
class Decision:
    """Auditable agent decision (spec item 4): action, rationale, confidence, estimated_net_edge,
    alternatives, state_features (+ tools, hypothesis, observed prices, later: orders & outcome)."""

    decision_id: str
    ts: float
    cycle: int
    strategy_version: str
    action: Action
    rationale: str
    confidence: float                      # 0..1 calibrated-ish belief the hypothesis pays off
    estimated_net_edge: Decimal            # fraction of notional AFTER all costs (0.004 = 40 bps)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    state_features: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    hypothesis: dict[str, Any] | None = None
    observed_prices: dict[str, str] = field(default_factory=dict)
    intent: TradeIntent | None = None
    risk_verdict: dict[str, Any] | None = None
    order_ids: list[str] = field(default_factory=list)
    chosen_tool: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def conv(o: Any) -> Any:
            if isinstance(o, Decimal):
                return plain(o)
            if isinstance(o, Enum):
                return o.value
            if isinstance(o, Mapping):
                return {str(k): conv(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [conv(v) for v in o]
            if hasattr(o, "__dataclass_fields__"):
                return {k: conv(getattr(o, k)) for k in o.__dataclass_fields__}
            return o

        return conv(self)


def validate_symbol(symbol: str, allowed: Sequence[str] = ("BTCUSDT", "USDTTMN")) -> str:
    if symbol not in allowed:
        raise ValueError(f"symbol {symbol!r} not allowed (allowed: {list(allowed)})")
    return symbol
