"""Durable state: append-only events, kv, order intents and fills (SQLite, WAL, synchronous=FULL).

The execution engine persists an order intent BEFORE sending it and rebuilds everything from here
after a crash, so this module is deliberately strict: every Decimal is stored as TEXT and
round-trips exactly, order lifecycles follow ``ORDER_TRANSITIONS``, fills are idempotent by
``fill_key`` and immutable, and one process at a time may own a data directory.

``InMemoryStore`` is the same engine on an in-memory SQLite database (no lock, no WAL), so tests
and backtests exercise exactly the same SQL, constraints and transaction semantics.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .clock import Clock, RealClock
from .errors import InvariantError, WallexAgentError
from .interfaces import Event
from .types import (ORDER_TRANSITIONS, TERMINAL_LIFECYCLES, UNRESOLVED_LIFECYCLES, ZERO,
                    OrderLifecycle, Side)

LOCK_FILENAME = "instance.lock"
SCHEMA_VERSION = 1
FEE_SOURCES = ("exchange", "estimated")
_UNPARSABLE_LOCK_GRACE_S = 60.0   # a writer fills the lock file within microseconds of creating it


class StoreError(WallexAgentError):
    """Database cannot be used safely (corrupt, wrong schema, closed)."""


class InstanceLockError(WallexAgentError, RuntimeError):
    """Another live process (or another store in this process) owns the data directory."""


# --------------------------------------------------------------------------- encoding
def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)          # exact (str(Decimal) round-trips bit-for-bit, unlike float)
    if isinstance(o, Enum):
        return o.value
    if is_dataclass(o) and not isinstance(o, type):
        return {f.name: getattr(o, f.name) for f in fields(o)}
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON-serialisable: {type(o).__name__}")


def dumps(obj: Any) -> str:
    """JSON encoder used for every payload: Decimals become exact strings, never floats."""
    return json.dumps(obj, default=_json_default, ensure_ascii=False, separators=(",", ":"))


def _dtext(value: Any, name: str) -> str:
    """Decimal -> TEXT column. Floats are refused: they would already have lost precision."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"{name} must be Decimal (got {type(value).__name__})")
    d = Decimal(value)
    if not d.is_finite():
        raise ValueError(f"{name} must be finite")
    return str(d)


def _otext(value: Any, name: str) -> str | None:
    return None if value is None else _dtext(value, name)


# --------------------------------------------------------------------------- records
@dataclass(frozen=True, kw_only=True)
class OrderRecord:
    """One order intent and its locally persisted lifecycle. Keyword-only so that price/quantity
    can never be swapped positionally. Use ``dataclasses.replace`` / ``update_order`` to change."""

    client_id: str
    exchange_client_order_id: str | None = None
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    purpose: str
    decision_id: str | None = None
    lifecycle: OrderLifecycle = OrderLifecycle.INTENT
    executed_qty: Decimal = ZERO
    executed_sum: Decimal = ZERO
    fee_total: Decimal = ZERO
    fee_asset: str | None = None
    fee_source: str | None = None        # 'exchange' | 'estimated' | None
    created_ts: float | None = None      # filled from the store clock on insert when None
    updated_ts: float | None = None
    last_raw: dict[str, Any] | None = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "lifecycle", OrderLifecycle(self.lifecycle))
        for name in ("price", "quantity", "executed_qty", "executed_sum", "fee_total"):
            _dtext(getattr(self, name), name)
        if self.fee_source is not None and self.fee_source not in FEE_SOURCES:
            raise ValueError(f"fee_source must be one of {FEE_SOURCES} or None")

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle in TERMINAL_LIFECYCLES

    @property
    def remaining_qty(self) -> Decimal:
        return max(ZERO, self.quantity - self.executed_qty)


@dataclass(frozen=True)
class FillRecord:
    """One execution (or part of one) attributed to an order. ``fill_key`` is the idempotency key."""

    fill_key: str
    client_id: str
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    quote_sum: Decimal
    fee: Decimal
    fee_asset: str
    fee_source: str              # 'exchange' | 'estimated'
    ts: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", Side(self.side))
        for name in ("price", "quantity", "quote_sum", "fee"):
            _dtext(getattr(self, name), name)
        if not self.fill_key or not self.client_id:
            raise ValueError("fill_key and client_id are required")
        if self.price <= 0 or self.quantity <= 0 or self.quote_sum <= 0:
            raise ValueError("fill price/quantity/quote_sum must be > 0")
        if self.fee < 0:
            raise ValueError("fill fee must be >= 0")
        if not self.fee_asset:
            raise ValueError("fill fee_asset is required")
        if self.fee_source not in FEE_SOURCES:
            raise ValueError(f"fill fee_source must be one of {FEE_SOURCES}")


_IMMUTABLE_ORDER_FIELDS = frozenset(
    {"client_id", "symbol", "side", "price", "quantity", "purpose", "decision_id", "created_ts"})
_MUTABLE_ORDER_FIELDS = frozenset(
    {"exchange_client_order_id", "executed_qty", "executed_sum", "fee_total", "fee_asset",
     "fee_source", "last_raw", "note"})
_TERMINAL_EDITABLE = frozenset({"fee_total", "fee_asset", "fee_source", "last_raw", "note"})
# never sent / definitively not placed / resolved as never reached: an execution is a contradiction
_NO_EXECUTION_LIFECYCLES = frozenset(
    {OrderLifecycle.INTENT, OrderLifecycle.SENDING, OrderLifecycle.REJECTED, OrderLifecycle.ABANDONED})


# --------------------------------------------------------------------------- schema
_LIFECYCLE_SQL = ",".join(f"'{lc.value}'" for lc in OrderLifecycle)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    payload TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind_id ON events(kind, id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_id                TEXT PRIMARY KEY,
    exchange_client_order_id TEXT,
    symbol                   TEXT NOT NULL,
    side                     TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price                    TEXT NOT NULL,
    quantity                 TEXT NOT NULL,
    purpose                  TEXT NOT NULL,
    decision_id              TEXT,
    lifecycle                TEXT NOT NULL CHECK (lifecycle IN ({_LIFECYCLE_SQL})),
    executed_qty             TEXT NOT NULL,
    executed_sum             TEXT NOT NULL,
    fee_total                TEXT NOT NULL,
    fee_asset                TEXT,
    fee_source               TEXT CHECK (fee_source IS NULL OR fee_source IN ('exchange','estimated')),
    created_ts               REAL NOT NULL,
    updated_ts               REAL NOT NULL,
    last_raw                 TEXT,
    note                     TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_exchange_id
    ON orders(exchange_client_order_id) WHERE exchange_client_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS orders_lifecycle ON orders(lifecycle);
CREATE TRIGGER IF NOT EXISTS orders_no_delete BEFORE DELETE ON orders
    BEGIN SELECT RAISE(ABORT, 'orders are never deleted'); END;

CREATE TABLE IF NOT EXISTS fills (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_key   TEXT NOT NULL UNIQUE,
    client_id  TEXT NOT NULL REFERENCES orders(client_id),
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price      TEXT NOT NULL,
    quantity   TEXT NOT NULL,
    quote_sum  TEXT NOT NULL,
    fee        TEXT NOT NULL,
    fee_asset  TEXT NOT NULL,
    fee_source TEXT NOT NULL CHECK (fee_source IN ('exchange','estimated')),
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_client ON fills(client_id, seq);
CREATE TRIGGER IF NOT EXISTS fills_no_update BEFORE UPDATE ON fills
    BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fills_no_delete BEFORE DELETE ON fills
    BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
"""

_ORDER_COLS = ("client_id", "exchange_client_order_id", "symbol", "side", "price", "quantity",
               "purpose", "decision_id", "lifecycle", "executed_qty", "executed_sum", "fee_total",
               "fee_asset", "fee_source", "created_ts", "updated_ts", "last_raw", "note")
_FILL_COLS = ("fill_key", "client_id", "symbol", "side", "price", "quantity", "quote_sum", "fee",
              "fee_asset", "fee_source", "ts")


# --------------------------------------------------------------------------- instance lock
def pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists. Unknown => True (fail closed: never steal a lock).

    Windows: ``os.kill(pid, 0)`` would TERMINATE the process there, so use OpenProcess instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            err = ctypes.get_last_error()
            if err == 87:      # ERROR_INVALID_PARAMETER: no such process
                return False
            return True        # e.g. ERROR_ACCESS_DENIED: exists but not ours
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True            # EPERM etc.: exists
    return True


class InstanceLock:
    """``<dir>/instance.lock`` holding ``{pid, host, token, created}``.

    Created with O_EXCL (atomic on Windows and POSIX). A lock whose PID is dead on this host is
    stale and reclaimed; a lock from another host or an unreadable fresh lock is never stolen.
    The file descriptor stays open while held (on Windows this also blocks deletion by others).
    """

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / LOCK_FILENAME
        self.token = uuid.uuid4().hex
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
            except FileExistsError:
                self._reclaim_if_stale()
                continue
            body = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "token": self.token,
                               "created": time.time()}).encode("utf-8")
            try:
                os.write(fd, body)
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                self.path.unlink(missing_ok=True)
                raise
            self._fd = fd
            return
        raise InstanceLockError(f"could not acquire {self.path} (contended)")

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None

    def _reclaim_if_stale(self) -> None:
        try:
            info = self._read(self.path)
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return  # released meanwhile: retry create
        if info is None:
            if time.time() - mtime < _UNPARSABLE_LOCK_GRACE_S:
                raise InstanceLockError(f"{self.path} exists and is being written/unreadable")
        else:
            if info.get("host") != socket.gethostname():
                raise InstanceLockError(
                    f"{self.path} held by pid {info['pid']} on host {info.get('host')!r}; "
                    "cannot verify liveness - remove the file manually if that host is down")
            if pid_alive(info["pid"]):
                same = " (this process)" if info["pid"] == os.getpid() else ""
                raise InstanceLockError(
                    f"another instance is running: pid {info['pid']}{same} holds {self.path}")
        # Stale. Move it aside first so two reclaimers cannot both delete a fresh lock.
        aside = self.path.with_name(f"{LOCK_FILENAME}.stale-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return
        except PermissionError as exc:   # Windows: the holder still has it open => alive
            raise InstanceLockError(f"{self.path} is held open by another process") from exc
        try:
            moved = self._read(aside)
        except FileNotFoundError:
            moved = None
        if info is not None and (moved is None or moved.get("token") != info.get("token")):
            raise InstanceLockError(f"{self.path} changed during stale-lock reclaim; retry")
        aside.unlink(missing_ok=True)

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.close(fd)
        finally:
            try:
                info = self._read(self.path)
            except FileNotFoundError:
                info = None
            if info is not None and info.get("token") == self.token:
                self.path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- store
class _SqlStore:
    """Shared engine. Thread-safe via one RLock; a transaction holds the lock until it ends."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None) -> None:
        self._conn = conn
        self._clock = clock or RealClock()
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StoreError("SQLite foreign keys unavailable")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise StoreError(f"unsupported schema version {version}")
        with self.transaction():
            for stmt in _split_sql(_SCHEMA):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ------------------------------------------------------------------ plumbing
    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def _check_open(self) -> None:
        if self._closed:
            raise StoreError("store is closed")

    @contextmanager
    def transaction(self) -> Iterator["_SqlStore"]:
        """BEGIN IMMEDIATE ... COMMIT; ROLLBACK on any exception. Nested calls join the outer one."""
        with self._lock:
            self._check_open()
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                self._conn.execute("COMMIT")

    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._check_open()
            return self._conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def __enter__(self) -> "_SqlStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ events (EventStore)
    def append(self, kind: str, payload: dict[str, Any], *, ts: float | None = None) -> int:
        if not isinstance(kind, str) or not kind:
            raise ValueError("event kind must be a non-empty string")
        if not isinstance(payload, Mapping):
            raise TypeError("event payload must be a mapping")
        text = dumps(dict(payload))
        with self.transaction():
            cur = self._conn.execute("INSERT INTO events(ts, kind, payload) VALUES (?,?,?)",
                                     (float(self._clock.now() if ts is None else ts), kind, text))
            return int(cur.lastrowid)

    def iter_events(self, kind: str | None = None, since_id: int = 0) -> Iterator[Event]:
        """Events with ``id > since_id`` in id order (paged, so appends during iteration are safe)."""
        last = int(since_id)
        while True:
            if kind is None:
                rows = self._q("SELECT id, ts, kind, payload FROM events WHERE id>? ORDER BY id LIMIT 500",
                               (last,))
            else:
                rows = self._q("SELECT id, ts, kind, payload FROM events WHERE kind=? AND id>? "
                               "ORDER BY id LIMIT 500", (kind, last))
            if not rows:
                return
            for r in rows:
                yield Event(int(r[0]), float(r[1]), r[2], json.loads(r[3]))
            last = int(rows[-1][0])

    def get_event(self, event_id: int) -> Event | None:
        rows = self._q("SELECT id, ts, kind, payload FROM events WHERE id=?", (int(event_id),))
        return Event(int(rows[0][0]), float(rows[0][1]), rows[0][2], json.loads(rows[0][3])) if rows else None

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("kv key must be a non-empty string")
        text = dumps(value)
        with self.transaction():
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (key, text, float(self._clock.now())))

    # ------------------------------------------------------------------ orders
    @staticmethod
    def _order_row(rec: OrderRecord) -> tuple[Any, ...]:
        return (rec.client_id, rec.exchange_client_order_id, rec.symbol, rec.side.value,
                _dtext(rec.price, "price"), _dtext(rec.quantity, "quantity"), rec.purpose,
                rec.decision_id, rec.lifecycle.value, _dtext(rec.executed_qty, "executed_qty"),
                _dtext(rec.executed_sum, "executed_sum"), _dtext(rec.fee_total, "fee_total"),
                rec.fee_asset, rec.fee_source, float(rec.created_ts), float(rec.updated_ts),
                None if rec.last_raw is None else dumps(rec.last_raw), rec.note)

    @staticmethod
    def _order_from(row: sqlite3.Row) -> OrderRecord:
        d = dict(zip(_ORDER_COLS, row))
        for k in ("price", "quantity", "executed_qty", "executed_sum", "fee_total"):
            d[k] = Decimal(d[k])
        d["last_raw"] = None if d["last_raw"] is None else json.loads(d["last_raw"])
        return OrderRecord(**d)

    def insert_order_intent(self, rec: OrderRecord) -> OrderRecord:
        """Persist a NEW intent (lifecycle INTENT, nothing executed). Duplicate client_id => error."""
        if rec.lifecycle is not OrderLifecycle.INTENT:
            raise InvariantError(f"new order must be INTENT, got {rec.lifecycle.value}")
        if not rec.client_id or not rec.symbol or not rec.purpose:
            raise InvariantError("client_id, symbol and purpose are required")
        if rec.price <= 0 or rec.quantity <= 0:
            raise InvariantError("order price and quantity must be > 0")
        if rec.executed_qty != 0 or rec.executed_sum != 0 or rec.fee_total != 0:
            raise InvariantError("a new intent cannot carry executions")
        now = float(self._clock.now())
        rec = replace(rec, created_ts=now if rec.created_ts is None else float(rec.created_ts),
                      updated_ts=now)
        with self.transaction():
            try:
                self._conn.execute(f"INSERT INTO orders({','.join(_ORDER_COLS)}) VALUES "
                                   f"({','.join('?' * len(_ORDER_COLS))})", self._order_row(rec))
            except sqlite3.IntegrityError as exc:
                raise InvariantError(f"order {rec.client_id!r} rejected by store: {exc}") from exc
            return self.get_order(rec.client_id)

    def get_order(self, client_id: str) -> OrderRecord | None:
        rows = self._q(f"SELECT {','.join(_ORDER_COLS)} FROM orders WHERE client_id=?", (client_id,))
        return self._order_from(rows[0]) if rows else None

    def find_order_by_exchange_id(self, exchange_client_order_id: str) -> OrderRecord | None:
        """Match the id the exchange returned, falling back to our own client_id (if echoed)."""
        rows = self._q(f"SELECT {','.join(_ORDER_COLS)} FROM orders WHERE exchange_client_order_id=?",
                       (exchange_client_order_id,))
        if not rows:
            rows = self._q(f"SELECT {','.join(_ORDER_COLS)} FROM orders WHERE client_id=?",
                           (exchange_client_order_id,))
        return self._order_from(rows[0]) if rows else None

    def list_orders(self, lifecycles: Iterable[OrderLifecycle | str] | None = None) -> list[OrderRecord]:
        cols = ",".join(_ORDER_COLS)
        if lifecycles is None:
            rows = self._q(f"SELECT {cols} FROM orders ORDER BY created_ts, rowid")
        else:
            wanted = [OrderLifecycle(x).value for x in lifecycles]
            if not wanted:
                return []
            rows = self._q(f"SELECT {cols} FROM orders WHERE lifecycle IN ({','.join('?' * len(wanted))}) "
                           "ORDER BY created_ts, rowid", wanted)
        return [self._order_from(r) for r in rows]

    def unresolved_orders(self) -> list[OrderRecord]:
        return self.list_orders(UNRESOLVED_LIFECYCLES)

    def update_order(self, client_id: str, *, lifecycle: OrderLifecycle | str | None = None,
                     **changes: Any) -> OrderRecord:
        """Atomically move an order along ``ORDER_TRANSITIONS`` and/or update its mutable fields.

        Raises ``InvariantError`` for: unknown order, illegal transition, changing economic terms
        (symbol/side/price/quantity/...), executed_qty decreasing or exceeding quantity, changing an
        already-set exchange id, or touching executions of a terminal order.
        """
        bad = set(changes) & _IMMUTABLE_ORDER_FIELDS
        if bad:
            raise InvariantError(f"immutable order fields cannot change: {sorted(bad)}")
        unknown = set(changes) - _MUTABLE_ORDER_FIELDS
        if unknown:
            raise TypeError(f"unknown order fields: {sorted(unknown)}")
        with self.transaction():
            cur = self.get_order(client_id)
            if cur is None:
                raise InvariantError(f"unknown order {client_id!r}")
            target = cur.lifecycle if lifecycle is None else OrderLifecycle(lifecycle)
            if target is not cur.lifecycle:
                if target not in ORDER_TRANSITIONS[cur.lifecycle]:
                    raise InvariantError(
                        f"order {client_id}: illegal lifecycle {cur.lifecycle.value} -> {target.value}")
            elif cur.is_terminal and set(changes) - _TERMINAL_EDITABLE:
                # same-state update of a terminal order: only fee/raw/note metadata may change
                raise InvariantError(
                    f"order {client_id} is {cur.lifecycle.value}: cannot change "
                    f"{sorted(set(changes) - _TERMINAL_EDITABLE)}")
            new = replace(cur, lifecycle=target, updated_ts=float(self._clock.now()), **changes)
            if new.lifecycle in _NO_EXECUTION_LIFECYCLES and new.executed_qty != 0:
                raise InvariantError(
                    f"order {client_id}: {new.lifecycle.value} cannot carry executed quantity")
            if new.executed_qty < cur.executed_qty or new.executed_sum < cur.executed_sum:
                raise InvariantError(f"order {client_id}: executed amounts cannot decrease")
            if new.executed_qty > new.quantity:
                raise InvariantError(f"order {client_id}: executed_qty {new.executed_qty} > quantity")
            if new.executed_qty < 0 or new.executed_sum < 0 or new.fee_total < 0:
                raise InvariantError(f"order {client_id}: negative amounts")
            if (cur.exchange_client_order_id is not None
                    and new.exchange_client_order_id != cur.exchange_client_order_id):
                raise InvariantError(f"order {client_id}: exchange id already set")
            try:
                self._conn.execute(
                    f"UPDATE orders SET {','.join(c + '=?' for c in _ORDER_COLS[1:])} WHERE client_id=?",
                    self._order_row(new)[1:] + (client_id,))
            except sqlite3.IntegrityError as exc:
                raise InvariantError(f"order {client_id}: {exc}") from exc
            return self.get_order(client_id)  # exactly what was persisted

    # ------------------------------------------------------------------ fills
    @staticmethod
    def _fill_from(row: sqlite3.Row) -> FillRecord:
        d = dict(zip(_FILL_COLS, row))
        for k in ("price", "quantity", "quote_sum", "fee"):
            d[k] = Decimal(d[k])
        return FillRecord(**d)

    def add_fill(self, fill: FillRecord) -> bool:
        """Record a fill once. Returns False if ``fill_key`` is already recorded with identical
        content; a different fill under an existing key, a fill for an unknown order, a symbol/side
        mismatch or fills exceeding the order quantity raise ``InvariantError``."""
        with self.transaction():
            rows = self._q(f"SELECT {','.join(_FILL_COLS)} FROM fills WHERE fill_key=?", (fill.fill_key,))
            if rows:
                if self._fill_from(rows[0]) != fill:
                    raise InvariantError(f"fill {fill.fill_key!r} already recorded with different data")
                return False
            order = self.get_order(fill.client_id)
            if order is None:
                raise InvariantError(f"fill {fill.fill_key!r} for unknown order {fill.client_id!r}")
            if order.symbol != fill.symbol or order.side is not fill.side:
                raise InvariantError(f"fill {fill.fill_key!r} does not match order {fill.client_id!r}")
            if order.lifecycle in (OrderLifecycle.INTENT, OrderLifecycle.REJECTED, OrderLifecycle.ABANDONED):
                raise InvariantError(
                    f"fill {fill.fill_key!r} for {order.lifecycle.value} order {fill.client_id!r} "
                    "(never sent / not placed): contradiction, needs manual reconcile")
            done = sum((f.quantity for f in self.list_fills(fill.client_id)), ZERO)
            if done + fill.quantity > order.quantity:
                raise InvariantError(
                    f"fills for {fill.client_id!r} would exceed order quantity "
                    f"({done} + {fill.quantity} > {order.quantity})")
            self._conn.execute(
                f"INSERT INTO fills({','.join(_FILL_COLS)}) VALUES ({','.join('?' * len(_FILL_COLS))})",
                (fill.fill_key, fill.client_id, fill.symbol, fill.side.value, _dtext(fill.price, "price"),
                 _dtext(fill.quantity, "quantity"), _dtext(fill.quote_sum, "quote_sum"),
                 _dtext(fill.fee, "fee"), fill.fee_asset, fill.fee_source, float(fill.ts)))
            return True

    def list_fills(self, client_id: str | None = None) -> list[FillRecord]:
        """Fills in recording order (the ledger order)."""
        cols = ",".join(_FILL_COLS)
        if client_id is None:
            rows = self._q(f"SELECT {cols} FROM fills ORDER BY seq")
        else:
            rows = self._q(f"SELECT {cols} FROM fills WHERE client_id=? ORDER BY seq", (client_id,))
        return [self._fill_from(r) for r in rows]

    def count_fills(self) -> int:
        return int(self._q("SELECT COUNT(*) FROM fills")[0][0])


def _split_sql(script: str) -> list[str]:
    """Split the schema script into statements (triggers contain inner ';')."""
    out, buf = [], []
    for line in script.strip().splitlines():
        buf.append(line)
        joined = "\n".join(buf).strip()
        if joined.endswith(";") and (not joined.upper().startswith("CREATE TRIGGER")
                                     or joined.upper().endswith("END;")):
            out.append(joined)
            buf = []
    if "\n".join(buf).strip():
        out.append("\n".join(buf).strip())
    return out


class SqliteStore(_SqlStore):
    """File-backed store. Acquires ``<db dir>/instance.lock`` before touching the database."""

    def __init__(self, path: str | Path, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._instance_lock = InstanceLock(self.path.parent)
        self.acquire_instance_lock()
        try:
            conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10.0,
                                   check_same_thread=False)
            try:
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise StoreError(f"could not enable WAL (got {mode!r})")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA busy_timeout=10000")
                check = conn.execute("PRAGMA integrity_check").fetchall()
                if [r[0] for r in check] != ["ok"]:
                    raise StoreError(f"integrity_check failed: {check[:5]}")
                super().__init__(conn, clock)
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise StoreError(f"cannot open {self.path.name}: {exc}") from exc
            except BaseException:
                conn.close()
                raise
        except BaseException:
            self._instance_lock.release()
            raise

    def acquire_instance_lock(self) -> None:
        self._instance_lock.acquire()

    @property
    def lock_path(self) -> Path:
        return self._instance_lock.path

    def pragma(self, name: str) -> Any:
        return self._q(f"PRAGMA {name}")[0][0]

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._instance_lock.release()


class InMemoryStore(_SqlStore):
    """Same API and SQL as ``SqliteStore`` on ``:memory:`` (no file, no instance lock)."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        super().__init__(sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False), clock)
