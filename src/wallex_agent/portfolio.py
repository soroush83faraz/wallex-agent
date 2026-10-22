"""Decimal ledger driven ONLY by recorded fills, plus valuation and the persisted equity curve.

Nothing here is updated from "I sent an order": balances move only when a ``FillRecord`` exists
in the store. All state is rebuilt from the store on construction (and re-synced on every read),
so a restart never loses the ledger. If a read happens inside a store transaction the result is
provisional and the ledger is rebuilt once the transaction is over (it may have rolled back).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping

from .errors import InvariantError
from .interfaces import Event
from .store import FillRecord
from .types import ONE, ZERO, Balance, FeeRates, MarketRules, OrderBook, Side, dec

DEFAULT_PAIRS: dict[str, tuple[str, str]] = {"BTCUSDT": ("BTC", "USDT"), "USDTTMN": ("USDT", "TMN")}
INITIAL_TMN_KEY = "portfolio.initial_tmn"
EQUITY_EVENT = "equity_point"
CONVERSION_EVENT = "usdt_conversion"
LOCKED_SEMANTICS = ("unknown", "value_includes_locked", "value_excludes_locked")


@dataclass(frozen=True)
class Position:
    qty: Decimal
    avg_cost_usdt: Decimal               # fee-inclusive; 0 when flat
    entry_ts: float | None               # first buy of the currently open round trip
    realized_pnl_usdt: Decimal           # all exits so far (fees included)
    round_trips: int
    wins: int
    losses: int
    cost_usdt: Decimal = ZERO            # fee-inclusive cost of ``qty``
    realized_pnl_tmn: Decimal | None = None   # realized USDT P&L x USDT cost basis (TMN/USDT)
    open_trip_pnl_usdt: Decimal = ZERO   # realized so far on the open trip (partial exits)
    untracked_fee_assets: tuple[str, ...] = ()  # fees paid in assets not priced into avg cost


@dataclass(frozen=True)
class AssetDrift:
    asset: str
    expected: Decimal
    actual_value: Decimal
    actual_locked: Decimal
    drift: Decimal            # actual_total - expected under ``interpretation``
    tolerance: Decimal
    direction: str            # 'match' | 'short' (actual < expected: DANGEROUS) | 'excess'
    ok: bool
    interpretation: str       # 'value' | 'value+locked'


@dataclass(frozen=True)
class ReconcileReport:
    """``ok`` is False when any asset is SHORT beyond tolerance (we believe we hold more than we
    do), when the ledger itself is negative, or (``strict_excess``) on unexplained excess."""

    ok: bool
    drifts: dict[str, AssetDrift]
    reasons: list[str] = field(default_factory=list)
    excess: list[str] = field(default_factory=list)       # informational unless strict_excess
    anomalies: list[str] = field(default_factory=list)    # ledger history oddities (informational)


@dataclass(frozen=True)
class EquityReport:
    """``equity_tmn_net`` = what we would end with in TMN after selling BTC->USDT->TMN through the
    visible books net of taker fees (``None`` if a fee/book is unknown or the ledger is negative;
    a lower bound when depth is insufficient — see ``flags``). ``equity_tmn_mid`` is gross at mids."""

    equity_tmn_net: Decimal | None
    equity_tmn_mid: Decimal | None
    ok: bool
    flags: tuple[str, ...]
    breakdown: dict[str, Any]


def _taker(fees: Mapping[str, Any] | None, symbol: str) -> Decimal | None:
    f = (fees or {}).get(symbol)
    if f is None:
        return None
    rate = f.taker if isinstance(f, FeeRates) else dec(f)
    if rate < 0 or rate >= 1:
        raise ValueError(f"implausible taker fee for {symbol}: {rate}")
    return rate


def _in_tx(store: Any) -> bool:
    return bool(getattr(store, "in_transaction", False))


class Portfolio:
    def __init__(self, store: Any, initial_tmn: Decimal, *,
                 rules: Mapping[str, MarketRules] | None = None,
                 btc_symbol: str = "BTCUSDT", usdt_symbol: str = "USDTTMN",
                 dust_notional: Decimal | None = None) -> None:
        if not isinstance(initial_tmn, (Decimal, int)) or isinstance(initial_tmn, bool):
            raise TypeError("initial_tmn must be Decimal")
        self.initial_tmn = dec(initial_tmn)
        if self.initial_tmn <= 0:
            raise ValueError("initial_tmn must be > 0")
        self._store = store
        persisted = store.kv_get(INITIAL_TMN_KEY)
        if persisted is None:
            store.kv_set(INITIAL_TMN_KEY, str(self.initial_tmn))
        elif Decimal(persisted) != self.initial_tmn:
            raise InvariantError(
                f"initial_tmn {self.initial_tmn} differs from the persisted ledger's {persisted}")
        self._rules = dict(rules) if rules else None
        self._pairs = ({s: (r.base, r.quote) for s, r in rules.items()} if rules else dict(DEFAULT_PAIRS))
        for s in (btc_symbol, usdt_symbol):
            if s not in self._pairs:
                raise ValueError(f"no base/quote known for {s}")
        self.btc_symbol, self.usdt_symbol = btc_symbol, usdt_symbol
        self.base, self.quote = self._pairs[btc_symbol]
        self.fiat = self._pairs[usdt_symbol][1]
        if self._pairs[usdt_symbol][0] != self.quote:
            raise ValueError(f"{usdt_symbol} base must be {self.quote}")
        if dust_notional is None:
            dust_notional = rules[btc_symbol].min_notional if rules and btc_symbol in rules else Decimal(1)
        self.dust_notional = dec(dust_notional)
        self._reset_ledger()
        self._reset_equity()
        self._reset_conversions()
        self.sync()

    # ------------------------------------------------------------------ rebuild
    def _reset_ledger(self) -> None:
        self._applied: list[FillRecord] = []
        self._applied_keys: set[str] = set()
        self._ledger_dirty = False
        self._bal: dict[str, Decimal] = {self.fiat: self.initial_tmn, self.quote: ZERO, self.base: ZERO}
        self._fees: dict[str, Decimal] = {}
        self._anomalies: list[str] = []
        self._pos_qty = ZERO
        self._pos_cost = ZERO
        self._entry_ts: float | None = None
        self._realized = ZERO
        self._trip_open = False
        self._trip_pnl = ZERO
        self._round_trips = self._wins = self._losses = 0
        self._untracked_fee_assets: set[str] = set()
        self._fill_conv_usdt = ZERO   # USDT received via USDTTMN buys (net of fee)
        self._fill_conv_tmn = ZERO    # TMN paid for it (incl. fee)

    def _reset_equity(self) -> None:
        self._eq_events: list[Event] = []
        self._eq_dirty = False
        self._curve: list[tuple[float, Decimal]] = []
        self._peak = self.initial_tmn

    def _reset_conversions(self) -> None:
        self._conv_events: list[Event] = []
        self._conv_dirty = False
        self._conv_usdt = ZERO
        self._conv_tmn = ZERO

    def sync(self) -> None:
        """Apply any fills / events recorded in the store since the last sync (in store order)."""
        in_tx = _in_tx(self._store)
        if self._ledger_dirty and not in_tx:
            self._reset_ledger()
        fills = self._store.list_fills()
        n = len(self._applied)
        if len(fills) < n or (n and fills[n - 1] != self._applied[-1]):
            self._reset_ledger()
            n = 0
        for f in fills[n:]:
            self._apply(f)
            self._applied.append(f)
            self._applied_keys.add(f.fill_key)
            self._ledger_dirty = self._ledger_dirty or in_tx
        self._sync_events(EQUITY_EVENT, "_eq_events", "_eq_dirty", self._reset_equity, self._on_equity)
        self._sync_events(CONVERSION_EVENT, "_conv_events", "_conv_dirty", self._reset_conversions,
                          self._on_conversion)

    def _sync_events(self, kind: str, attr: str, dirty_attr: str, reset: Callable[[], None],
                     on_event: Callable[[Event], None]) -> None:
        in_tx = _in_tx(self._store)
        events: list[Event] = getattr(self, attr)
        if (getattr(self, dirty_attr) and not in_tx) or (
                events and self._store.get_event(events[-1].id) != events[-1]):
            reset()
            events = getattr(self, attr)
        for ev in self._store.iter_events(kind, events[-1].id if events else 0):
            on_event(ev)
            events.append(ev)
            if in_tx:
                setattr(self, dirty_attr, True)

    def _on_equity(self, ev: Event) -> None:
        v = Decimal(ev.payload["equity_tmn_net"])
        self._curve.append((ev.ts, v))
        self._peak = max(self._peak, v)

    def _on_conversion(self, ev: Event) -> None:
        self._conv_usdt += Decimal(ev.payload["usdt_received"])
        self._conv_tmn += Decimal(ev.payload["tmn_spent"])

    # ------------------------------------------------------------------ ledger
    def apply_fill(self, fill: FillRecord) -> bool:
        """Record ``fill`` in the store (idempotent) and apply it. True if newly applied here."""
        before = fill.fill_key in self._applied_keys
        self._store.add_fill(fill)
        self.sync()
        return not before and fill.fill_key in self._applied_keys

    def _apply(self, f: FillRecord) -> None:
        pair = self._pairs.get(f.symbol)
        if pair is None:
            raise InvariantError(f"fill {f.fill_key} on unknown symbol {f.symbol}")
        base, quote = pair
        if f.side is Side.BUY:
            self._add(quote, -f.quote_sum)
            self._add(base, f.quantity)
        else:
            self._add(base, -f.quantity)
            self._add(quote, f.quote_sum)
        self._add(f.fee_asset, -f.fee)        # whatever asset the exchange charged
        self._fees[f.fee_asset] = self._fees.get(f.fee_asset, ZERO) + f.fee
        if f.symbol == self.btc_symbol:
            self._position_fill(f)
        elif f.symbol == self.usdt_symbol and f.side is Side.BUY:
            self._fill_conv_usdt += f.quantity - (f.fee if f.fee_asset == base else ZERO)
            self._fill_conv_tmn += f.quote_sum + (f.fee if f.fee_asset == quote else ZERO)
        for asset in sorted({base, quote, f.fee_asset}):
            if self._bal[asset] < 0:
                self._anomalies.append(f"negative {asset} balance {self._bal[asset]} after fill {f.fill_key}")

    def _add(self, asset: str, amount: Decimal) -> None:
        self._bal[asset] = self._bal.get(asset, ZERO) + amount

    def _position_fill(self, f: FillRecord) -> None:
        fee_base = f.fee if f.fee_asset == self.base else ZERO
        fee_quote = f.fee if f.fee_asset == self.quote else ZERO
        if f.fee_asset not in (self.base, self.quote):
            self._untracked_fee_assets.add(f.fee_asset)
        if f.side is Side.BUY:
            if not self._trip_open:
                self._trip_open, self._entry_ts, self._trip_pnl = True, f.ts, ZERO
            self._pos_qty += f.quantity - fee_base          # base fee reduces what we receive
            self._pos_cost += f.quote_sum + fee_quote
            return
        consumed = f.quantity + fee_base                     # base fee on a sell is consumed too
        proceeds = f.quote_sum - fee_quote                   # quote fee reduces proceeds
        held = self._pos_qty
        covered = min(consumed, max(held, ZERO))
        if consumed > held:
            self._anomalies.append(f"sell {consumed} {self.base} exceeds position {held} (fill {f.fill_key}); "
                                   "excess excluded from realized P&L")
        if covered <= 0:
            return
        cost_out = self._pos_cost if covered == held else self._pos_cost * covered / held
        pnl = (proceeds if covered == consumed else proceeds * covered / consumed) - cost_out
        self._pos_qty -= covered
        self._pos_cost -= cost_out
        self._realized += pnl
        if self._trip_open:
            self._trip_pnl += pnl
            if self._pos_qty <= 0 or self._pos_qty * f.price < self.dust_notional:
                self._round_trips += 1
                if self._trip_pnl > 0:
                    self._wins += 1
                else:
                    self._losses += 1
                self._trip_open, self._entry_ts, self._trip_pnl = False, None, ZERO

    def expected_balances(self) -> dict[str, Decimal]:
        self.sync()
        return dict(self._bal)

    def fees_paid(self) -> dict[str, Decimal]:
        self.sync()
        return dict(self._fees)

    def anomalies(self) -> list[str]:
        self.sync()
        return list(self._anomalies)

    def fills(self) -> list[FillRecord]:
        self.sync()
        return list(self._applied)

    # ------------------------------------------------------------------ position / P&L
    def record_conversion(self, tmn_spent: Decimal, usdt_received: Decimal, *, ts: float | None = None,
                          note: str = "") -> int:
        """Declare the TMN cost basis of USDT (metadata only: balances still move only via fills).
        When any conversion is recorded it overrides the basis derived from USDTTMN buy fills."""
        tmn, usdt = dec(tmn_spent), dec(usdt_received)
        if tmn <= 0 or usdt <= 0:
            raise ValueError("conversion amounts must be > 0")
        eid = self._store.append(CONVERSION_EVENT, {"tmn_spent": str(tmn), "usdt_received": str(usdt),
                                                    "note": note}, ts=ts)
        self.sync()
        return eid

    def usdt_cost_basis_tmn(self) -> Decimal | None:
        """TMN paid per USDT (fee-inclusive), or None if unknown."""
        self.sync()
        if self._conv_usdt > 0:
            return self._conv_tmn / self._conv_usdt
        if self._fill_conv_usdt > 0:
            return self._fill_conv_tmn / self._fill_conv_usdt
        return None

    def btc_position(self) -> Position:
        self.sync()
        basis = self.usdt_cost_basis_tmn()
        return Position(
            qty=self._pos_qty,
            avg_cost_usdt=self._pos_cost / self._pos_qty if self._pos_qty > 0 else ZERO,
            entry_ts=self._entry_ts, realized_pnl_usdt=self._realized, round_trips=self._round_trips,
            wins=self._wins, losses=self._losses, cost_usdt=self._pos_cost,
            realized_pnl_tmn=None if basis is None else self._realized * basis,
            open_trip_pnl_usdt=self._trip_pnl,
            untracked_fee_assets=tuple(sorted(self._untracked_fee_assets)))

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, actual: Mapping[str, Balance], tolerance: Mapping[str, Decimal], *,
                  locked_semantics: str = "unknown", strict_excess: bool = False) -> ReconcileReport:
        """Compare the fill ledger with real balances. Tolerance is absolute, per asset, default 0.

        Whether Wallex ``value`` includes ``locked`` is undocumented: with ``locked_semantics=
        'unknown'`` an asset matches if EITHER reading (value, value+locked) is within tolerance.
        Every missed fill makes some asset SHORT (you always pay something), so a shortfall beyond
        tolerance is never OK; excess (e.g. funds outside the bot's budget) is reported only.
        """
        if locked_semantics not in LOCKED_SEMANTICS:
            raise ValueError(f"locked_semantics must be one of {LOCKED_SEMANTICS}")
        exp = self.expected_balances()
        drifts: dict[str, AssetDrift] = {}
        reasons: list[str] = []
        excess: list[str] = []
        for asset in sorted(set(exp) | set(actual)):
            e = exp.get(asset, ZERO)
            b = actual.get(asset)
            value = b.value if b else ZERO
            locked = b.locked if b else ZERO
            tol = dec(tolerance.get(asset, ZERO))
            if tol < 0:
                raise ValueError(f"negative tolerance for {asset}")
            options = {"value": value - e, "value+locked": value + locked - e}
            if locked_semantics == "value_includes_locked":
                interp = "value"
            elif locked_semantics == "value_excludes_locked":
                interp = "value+locked"
            else:
                interp = min(options, key=lambda k: abs(options[k]))
            drift = options[interp]
            ok = True
            if e < 0:
                ok = False
                reasons.append(f"{asset}: ledger balance is negative ({e})")
            if drift < -tol:
                direction, ok = "short", False
                reasons.append(f"{asset}: actual {value + (locked if interp != 'value' else ZERO)} < "
                               f"expected {e} by {-drift} (tolerance {tol})")
            elif drift > tol:
                direction = "excess"
                excess.append(f"{asset}: actual exceeds expected {e} by {drift} (tolerance {tol})")
                if strict_excess:
                    ok = False
                    reasons.append(excess[-1])
            else:
                direction = "match"
            drifts[asset] = AssetDrift(asset, e, value, locked, drift, tol, direction, ok, interp)
        return ReconcileReport(ok=not reasons, drifts=drifts, reasons=reasons, excess=excess,
                               anomalies=self.anomalies())

    # ------------------------------------------------------------------ valuation
    def liquidation_value(self, books: Mapping[str, OrderBook], fees: Mapping[str, Any] | None,
                          rules: Mapping[str, MarketRules] | None = None) -> EquityReport:
        """Value the ledger in TMN: BTC by walking BTCUSDT bids net of the taker fee, then ALL USDT
        (held + proceeds) by walking USDTTMN bids net of the taker fee, TMN as is. With ``rules``,
        quantities are rounded down to the step and unsellable dust is valued at 0. A missing fee
        is never assumed: the report is flagged ``unknown_fee:<SYMBOL>`` with ``equity_tmn_net=None``.
        """
        rules = rules if rules is not None else self._rules
        bal = self.expected_balances()
        flags: list[str] = []
        net_known = mid_known = True
        btc, usdt, tmn = bal.get(self.base, ZERO), bal.get(self.quote, ZERO), bal.get(self.fiat, ZERO)
        for asset, v in sorted(bal.items()):
            if v < 0:
                flags.append(f"negative_balance:{asset}")
                net_known = mid_known = False
            elif v > 0 and asset not in (self.base, self.quote, self.fiat):
                flags.append(f"unvalued_asset:{asset}")
        bbook, ubook = books.get(self.btc_symbol), books.get(self.usdt_symbol)
        bd: dict[str, Any] = {"btc_qty": btc, "usdt_qty": usdt, "tmn_qty": tmn}

        def leg(symbol: str, book: OrderBook | None, qty: Decimal, asset: str) -> Decimal:
            nonlocal net_known
            if qty <= 0:
                return ZERO
            rate = _taker(fees, symbol)
            if rate is None:
                flags.append(f"unknown_fee:{symbol}")
                net_known = False
            if book is None:
                flags.append(f"missing_book:{symbol}")
                net_known = False
            if rate is None or book is None:
                return ZERO
            r = rules.get(symbol) if rules else None
            sell_qty = r.round_qty(qty) if r else qty
            walk = book.walk_qty(Side.SELL, sell_qty)
            if not walk.complete:
                flags.append(f"insufficient_depth:{symbol}")
            gross = walk.quote_sum
            if r and (sell_qty < r.min_qty or sell_qty <= 0 or gross < r.min_notional):
                flags.append(f"dust:{asset}")
                gross = ZERO
            net = gross * (ONE - rate)
            key = asset.lower()
            bd.update({f"{key}_sell_qty": sell_qty, f"{key}_gross": gross, f"{key}_fee_rate": rate,
                       f"{key}_net": net, f"{key}_avg_bid": walk.avg_price})
            return net

        btc_net = leg(self.btc_symbol, bbook, btc, self.base)
        usdt_after = max(usdt, ZERO) + btc_net
        bd["usdt_after_btc_liquidation"] = usdt_after
        tmn_net = leg(self.usdt_symbol, ubook, usdt_after, self.quote)
        equity_net = tmn + tmn_net if net_known else None

        if (btc > 0 and bbook is None) or ((btc > 0 or usdt > 0) and ubook is None):
            mid_known = False
        equity_mid = None
        if mid_known:
            btc_mid = bbook.mid if bbook else ZERO
            usdt_mid = ubook.mid if ubook else ZERO
            bd.update({"btc_mid": btc_mid, "usdt_mid": usdt_mid})
            equity_mid = btc * btc_mid * usdt_mid + usdt * usdt_mid + tmn
        return EquityReport(equity_net, equity_mid, ok=not flags, flags=tuple(flags), breakdown=bd)

    # ------------------------------------------------------------------ equity curve
    def record_equity(self, ts: float, equity_tmn_net: Decimal) -> int:
        if equity_tmn_net is None or isinstance(equity_tmn_net, (bool, float)):
            raise TypeError("equity_tmn_net must be a Decimal")
        eid = self._store.append(EQUITY_EVENT, {"equity_tmn_net": str(dec(equity_tmn_net))}, ts=float(ts))
        self.sync()
        return eid

    def equity_curve(self) -> list[tuple[float, Decimal]]:
        self.sync()
        return list(self._curve)

    def peak_equity(self) -> Decimal:
        """Max of ``initial_tmn`` and every recorded equity point (conservative baseline)."""
        self.sync()
        return self._peak

    def drawdown(self) -> Decimal:
        """Fraction below peak of the LAST recorded equity point (0 if none recorded)."""
        self.sync()
        if not self._curve:
            return ZERO
        return max(ZERO, (self._peak - self._curve[-1][1]) / self._peak)
