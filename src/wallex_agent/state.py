"""Persisted bot state machine + pure state inference for restart reconciliation."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .clock import Clock, RealClock
from .errors import InvariantError
from .types import STATE_TRANSITIONS, ZERO, Balance, BotState, MarketRules, OrderBook, Side

STATE_KEY = "bot_state"
EVENT_KIND = "state_transition"


class StateMachine:
    """``state`` lives in kv; every transition also appends a ``state_transition`` event in the same
    transaction, so state and history can never disagree after a crash."""

    def __init__(self, store: Any, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or RealClock()
        last = None
        for ev in store.iter_events(EVENT_KIND):
            last = ev
        cur = self.state
        if last is not None and last.payload.get("to") != cur.value:
            raise InvariantError(
                f"persisted state {cur.value} disagrees with last transition event -> {last.payload.get('to')}")

    @property
    def state(self) -> BotState:
        raw = self._store.kv_get(STATE_KEY)
        if raw is None:
            return BotState.UNFUNDED
        try:
            return BotState(raw["state"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvariantError(f"corrupt persisted bot state: {raw!r}") from exc

    def can_transition(self, to: BotState) -> bool:
        return BotState(to) in STATE_TRANSITIONS[self.state]

    def transition(self, to: BotState | str, reason: str, evidence: dict[str, Any] | None = None, *,
                   force: bool = False) -> BotState:
        """Validate against ``STATE_TRANSITIONS`` (``force`` bypasses it: operator resume only)."""
        to = BotState(to)
        if not reason:
            raise ValueError("a transition needs a reason")
        with self._store.transaction():
            cur = self.state
            if not force and to not in STATE_TRANSITIONS[cur]:
                raise InvariantError(f"illegal state transition {cur.value} -> {to.value} ({reason})")
            ts = float(self._clock.now())
            event_id = self._store.append(EVENT_KIND, {
                "from": cur.value, "to": to.value, "reason": reason, "evidence": evidence or {},
                "forced": bool(force)}, ts=ts)
            self._store.kv_set(STATE_KEY, {"state": to.value, "since": ts, "event_id": event_id})
        return to

    def history(self) -> list[dict[str, Any]]:
        return [{"id": ev.id, "ts": ev.ts, **ev.payload} for ev in self._store.iter_events(EVENT_KIND)]


def _side(o: Any) -> Side:
    return Side(o.side)


def derive_state_explained(balances: Mapping[str, Balance], open_orders: Iterable[Any],
                           rules: Mapping[str, MarketRules], books: Mapping[str, OrderBook], *,
                           btc_symbol: str = "BTCUSDT", usdt_symbol: str = "USDTTMN"
                           ) -> tuple[BotState, str]:
    """Infer the state implied by exchange facts. Pure. Ambiguity => STOPPED (fail closed).

    ``open_orders``: anything with ``.symbol`` and ``.side`` (OrderInfo / OrderRecord); orders on
    other symbols are ignored. Holdings use ``value`` (assume we hold it), spending power uses
    ``spendable`` (assume we cannot spend what is locked).
    """
    missing = [s for s in (btc_symbol, usdt_symbol) if s not in rules]
    if missing:
        return BotState.STOPPED, f"market rules unknown for {missing}"
    br, ur = rules[btc_symbol], rules[usdt_symbol]

    ours = [o for o in open_orders if o.symbol in (btc_symbol, usdt_symbol)]
    btc_buys = [o for o in ours if o.symbol == btc_symbol and _side(o) is Side.BUY]
    btc_sells = [o for o in ours if o.symbol == btc_symbol and _side(o) is Side.SELL]
    conv = [o for o in ours if o.symbol == usdt_symbol]
    if (btc_buys and btc_sells) or (conv and (btc_buys or btc_sells)):
        return BotState.STOPPED, (f"ambiguous open orders: {len(btc_buys)} buy, {len(btc_sells)} sell, "
                                  f"{len(conv)} conversion")
    if btc_sells:
        return BotState.SELL_PENDING, f"{len(btc_sells)} open {btc_symbol} sell order(s)"
    if btc_buys:
        return BotState.BUY_PENDING, f"{len(btc_buys)} open {btc_symbol} buy order(s)"
    if conv:
        return BotState.CONVERTING, f"{len(conv)} open {usdt_symbol} order(s)"

    def bal(asset: str) -> Balance:
        return balances.get(asset) or Balance(asset, ZERO)

    btc = bal(br.base).value
    sell_qty = br.round_qty(btc) if btc > 0 else ZERO
    if sell_qty > 0 and sell_qty >= br.min_qty:
        book = books.get(btc_symbol)
        if book is None:
            return BotState.BTC_HELD, f"{br.base} {sell_qty} held; no book to value it (assume sellable)"
        if sell_qty * book.best_bid >= br.min_notional:
            return BotState.BTC_HELD, f"{br.base} {sell_qty} >= min sell at bid {book.best_bid}"

    usdt = bal(br.quote).spendable
    if usdt >= br.min_notional:
        return BotState.READY, f"{br.quote} {usdt} spendable >= {btc_symbol} minNotional {br.min_notional}"

    tmn = bal(ur.quote).spendable
    if tmn >= ur.min_notional:
        return BotState.UNFUNDED, f"{ur.quote} {tmn} convertible; {br.quote} {usdt} insufficient"
    return BotState.UNFUNDED, (f"nothing tradable: {ur.quote} {tmn} < {ur.min_notional}, "
                               f"{br.quote} {usdt} < {br.min_notional}")


def derive_state(balances: Mapping[str, Balance], open_orders: Iterable[Any],
                 rules: Mapping[str, MarketRules], books: Mapping[str, OrderBook], **kw: Any) -> BotState:
    return derive_state_explained(balances, open_orders, rules, books, **kw)[0]
