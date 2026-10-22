"""Protocols (ports) that decouple modules. Implementations live elsewhere:

* Exchange   -> exchange/wallex.py (WallexClient), exchange/sim.py (SimExchange)
* BookSource -> market_data.py (LiveBookSource, ReplayBookSource, SyntheticMarket)
* EventStore -> store.py (SqliteStore), tests (InMemoryStore)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Protocol, runtime_checkable

from .types import (Balance, Candle, FeeRates, MarketRules, OrderBook, OrderInfo, OrderRequest,
                    Side, Trade)


@runtime_checkable
class Exchange(Protocol):
    """The 9 required adapter methods (+ candles, which are public data).

    Failure contract (see errors.py):
      * reads: may raise TransportError / ServerError / RateLimitError / AuthError / SchemaError
      * create_order: RejectedError => definitively NOT placed. TransportError / ServerError =>
        UNKNOWN outcome (the caller must reconcile; adapters must NOT retry a POST).
      * get_order: raises OrderNotFound if exchange says 404 / empty.
      * cancel_order: returns None on success; raises on failure. Caller verifies with get_order.
    """

    def get_markets(self) -> dict[str, MarketRules]: ...

    def get_order_book(self, symbol: str) -> OrderBook: ...

    def get_balances(self) -> dict[str, Balance]: ...

    def get_fees(self) -> dict[str, FeeRates]: ...

    def create_order(self, req: OrderRequest) -> OrderInfo: ...

    def get_order(self, client_order_id: str) -> OrderInfo: ...

    def list_open_orders(self, symbol: str | None = None) -> list[OrderInfo]: ...

    def cancel_order(self, client_order_id: str) -> None: ...

    def get_trades(self, symbol: str | None = None, side: Side | None = None) -> list[Trade]: ...

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]: ...


@runtime_checkable
class BookSource(Protocol):
    """Market-data feed for the agent and for SimExchange matching."""

    def get_book(self, symbol: str) -> OrderBook: ...

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]: ...


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    kind: str
    payload: dict[str, Any]


class EventStore(Protocol):
    """Append-only event log (event sourcing for memory/audit/ledger/state)."""

    def append(self, kind: str, payload: dict[str, Any], *, ts: float | None = None) -> int: ...

    def iter_events(self, kind: str | None = None, since_id: int = 0) -> Iterator[Event]: ...
