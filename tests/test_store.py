import json
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from wallex_agent.clock import SimClock
from wallex_agent.errors import InvariantError
from wallex_agent.store import (LOCK_FILENAME, FillRecord, InMemoryStore, InstanceLockError, OrderRecord,
                                SqliteStore, StoreError, dumps, pid_alive)
from wallex_agent.types import ORDER_TRANSITIONS, OrderLifecycle as L, Side

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", clock=SimClock()) if request.param == "sqlite" else \
        InMemoryStore(clock=SimClock())
    yield s
    s.close()


def order(cid="c1", **kw):
    base = dict(client_id=cid, symbol="BTCUSDT", side=Side.BUY, price=D("81000.12"),
                quantity=D("0.00012345"), purpose="entry", decision_id="d1")
    base.update(kw)
    return OrderRecord(**base)


def fill(key="f1", cid="c1", qty=D("0.0001"), side=Side.BUY, **kw):
    base = dict(fill_key=key, client_id=cid, symbol="BTCUSDT", side=side, price=D("81000.12"), quantity=qty,
                quote_sum=qty * D("81000.12"), fee=D("0.00000035"), fee_asset="BTC", fee_source="exchange",
                ts=1.0)
    base.update(kw)
    return FillRecord(**base)


def run_py(code: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=SRC)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, capture_output=True,
                          text=True, timeout=timeout)


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# --------------------------------------------------------------------------- sqlite settings
def test_sqlite_wal_full_fk_integrity(tmp_path):
    with SqliteStore(tmp_path / "db.sqlite") as s:
        assert s.pragma("journal_mode") == "wal"
        assert s.pragma("synchronous") == 2          # FULL
        assert s.pragma("foreign_keys") == 1
        assert s.pragma("integrity_check") == "ok"


def test_corrupt_db_fails_closed_and_releases_lock(tmp_path):
    (tmp_path / "db.sqlite").write_bytes(b"this is not a sqlite database at all" * 100)
    with pytest.raises(StoreError):
        SqliteStore(tmp_path / "db.sqlite")
    assert not (tmp_path / LOCK_FILENAME).exists()


# --------------------------------------------------------------------------- decimals
EXACT = [D("0.00000001"), D("1E-8"), D("10.50"), D("123456789012.123456789012"), D("81000.12"),
         D("0"), D("5000000"), D("0.0035000")]


def test_decimals_round_trip_exactly(store):
    for i, v in enumerate(EXACT):
        price = v if v > 0 else D("1")
        store.insert_order_intent(order(f"c{i}", price=price, quantity=price))
        got = store.get_order(f"c{i}")
        assert got.price == price and str(got.price) == str(price)
        assert str(got.quantity) == str(price)
    eid = store.append("x", {"vals": EXACT, "nested": {"a": [D("1E-8")]}})
    ev = next(store.iter_events(since_id=eid - 1))
    assert [D(x) for x in ev.payload["vals"]] == EXACT
    assert [str(D(x)) for x in ev.payload["vals"]] == [str(x) for x in EXACT]
    store.kv_set("k", {"v": D("0.1")})
    assert store.kv_get("k") == {"v": "0.1"}


def test_floats_are_refused_for_money():
    with pytest.raises(TypeError):
        order(price=81000.12)
    with pytest.raises(TypeError):
        fill(qty=0.0001)


def test_safe_encoder_handles_decimal_enum_dataclass():
    out = json.loads(dumps({"d": D("1.10"), "s": Side.SELL, "lc": L.OPEN, "o": order()}))
    assert out["d"] == "1.10" and out["s"] == "SELL" and out["lc"] == "OPEN"
    assert out["o"]["price"] == "81000.12" and out["o"]["side"] == "BUY"


# --------------------------------------------------------------------------- orders
def test_insert_intent_rules(store):
    rec = store.insert_order_intent(order())
    assert rec.lifecycle is L.INTENT and rec.created_ts is not None and rec.updated_ts is not None
    with pytest.raises(InvariantError):
        store.insert_order_intent(order())                       # duplicate client_id
    with pytest.raises(InvariantError):
        store.insert_order_intent(order("c2", lifecycle=L.SENDING))
    with pytest.raises(InvariantError):
        store.insert_order_intent(order("c3", quantity=D("0")))
    with pytest.raises(InvariantError):
        store.insert_order_intent(order("c4", executed_qty=D("0.0001")))
    assert store.get_order("nope") is None


PATH_TO = {
    L.INTENT: [], L.SENDING: [L.SENDING], L.OPEN: [L.SENDING, L.OPEN], L.PARTIAL: [L.SENDING, L.PARTIAL],
    L.FILLED: [L.SENDING, L.FILLED], L.CANCELED: [L.SENDING, L.CANCELED],
    L.REJECTED: [L.SENDING, L.REJECTED], L.UNKNOWN_OUTCOME: [L.SENDING, L.UNKNOWN_OUTCOME],
    L.ABANDONED: [L.ABANDONED],
}


@pytest.mark.parametrize("src", list(L))
def test_every_lifecycle_transition_is_enforced(src):
    for dst in L:
        if dst is src:
            continue
        s = InMemoryStore(clock=SimClock())
        s.insert_order_intent(order())
        for step in PATH_TO[src]:
            s.update_order("c1", lifecycle=step)
        assert s.get_order("c1").lifecycle is src
        if dst in ORDER_TRANSITIONS[src]:
            assert s.update_order("c1", lifecycle=dst).lifecycle is dst
            assert s.get_order("c1").lifecycle is dst
        else:
            with pytest.raises(InvariantError):
                s.update_order("c1", lifecycle=dst)
            assert s.get_order("c1").lifecycle is src           # unchanged
        s.close()


def test_update_order_field_invariants(store):
    store.insert_order_intent(order())
    store.update_order("c1", lifecycle=L.SENDING)
    with pytest.raises(InvariantError):
        store.update_order("c1", executed_qty=D("0.0001"))      # SENDING cannot carry executions
    with pytest.raises(InvariantError):
        store.update_order("c1", price=D("1"))                  # economic terms immutable
    with pytest.raises(TypeError):
        store.update_order("c1", bogus=1)
    rec = store.update_order("c1", lifecycle=L.PARTIAL, exchange_client_order_id="LIMIT-x",
                             executed_qty=D("0.0001"), executed_sum=D("8.100012"), last_raw={"p": D("1.5")})
    assert rec.last_raw == {"p": "1.5"} and store.get_order("c1").last_raw == {"p": "1.5"}
    with pytest.raises(InvariantError):
        store.update_order("c1", executed_qty=D("0.00005"))     # decreasing
    with pytest.raises(InvariantError):
        store.update_order("c1", executed_qty=D("0.001"))       # > quantity
    with pytest.raises(InvariantError):
        store.update_order("c1", exchange_client_order_id="other")
    store.update_order("c1", lifecycle=L.CANCELED)
    with pytest.raises(InvariantError):
        store.update_order("c1", executed_qty=D("0.00012"))     # terminal executions frozen
    rec = store.update_order("c1", fee_total=D("0.00000035"), fee_asset="BTC", fee_source="exchange")
    assert rec.fee_total == D("0.00000035") and rec.lifecycle is L.CANCELED
    with pytest.raises(InvariantError):
        store.update_order("missing", lifecycle=L.SENDING)


def test_find_by_exchange_id_and_listing(store):
    store.insert_order_intent(order("a"))
    store.insert_order_intent(order("b"))
    store.insert_order_intent(order("c"))
    store.update_order("a", lifecycle=L.SENDING)
    store.update_order("a", lifecycle=L.OPEN, exchange_client_order_id="LIMIT-123")
    store.update_order("b", lifecycle=L.ABANDONED)
    assert store.find_order_by_exchange_id("LIMIT-123").client_id == "a"
    assert store.find_order_by_exchange_id("c").client_id == "c"   # echoed client_id
    assert store.find_order_by_exchange_id("zzz") is None
    store.update_order("c", lifecycle=L.SENDING)
    with pytest.raises(InvariantError):                              # one exchange id <-> one record
        store.update_order("c", lifecycle=L.OPEN, exchange_client_order_id="LIMIT-123")
    assert store.get_order("c").lifecycle is L.SENDING
    assert [o.client_id for o in store.unresolved_orders()] == ["a", "c"]
    assert [o.client_id for o in store.list_orders([L.ABANDONED])] == ["b"]
    assert [o.client_id for o in store.list_orders()] == ["a", "b", "c"]
    assert store.list_orders([]) == []


# --------------------------------------------------------------------------- fills
def test_add_fill_idempotent_and_checked(store):
    store.insert_order_intent(order())
    store.update_order("c1", lifecycle=L.SENDING)
    store.update_order("c1", lifecycle=L.OPEN)
    f = fill()
    assert store.add_fill(f) is True
    assert store.add_fill(f) is False
    assert store.list_fills() == [f] and store.list_fills("c1") == [f] and store.list_fills("x") == []
    with pytest.raises(InvariantError):
        store.add_fill(fill(qty=D("0.00002")))                   # same key, different data
    with pytest.raises(InvariantError):
        store.add_fill(fill("f2", qty=D("0.00003")))              # 0.0001+0.00003 > 0.00012345
    with pytest.raises(InvariantError):
        store.add_fill(fill("f3", side=Side.SELL, qty=D("0.00001")))
    with pytest.raises(InvariantError):
        store.add_fill(fill("f4", cid="ghost"))
    assert store.add_fill(fill("f5", qty=D("0.00002345"))) is True
    assert store.count_fills() == 2


def test_fill_on_never_sent_or_rejected_order_is_refused(store):
    store.insert_order_intent(order("i"))
    store.insert_order_intent(order("r"))
    store.update_order("r", lifecycle=L.SENDING)
    store.update_order("r", lifecycle=L.REJECTED)
    for cid in ("i", "r"):
        with pytest.raises(InvariantError):
            store.add_fill(fill(f"k-{cid}", cid=cid))


def test_fills_and_events_are_append_only_at_sql_level(store):
    store.insert_order_intent(order())
    store.update_order("c1", lifecycle=L.SENDING)
    store.add_fill(fill())
    store.append("x", {})
    for sql in ("UPDATE fills SET fee='0'", "DELETE FROM fills", "UPDATE events SET kind='y'",
                "DELETE FROM events", "DELETE FROM orders"):
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute(sql)


def test_fill_validation():
    with pytest.raises(ValueError):
        fill(fee=D("-1"))
    with pytest.raises(ValueError):
        fill(fee_source="guess")
    with pytest.raises(ValueError):
        fill(qty=D("0"))


# --------------------------------------------------------------------------- transactions / events / kv
def test_transaction_is_atomic_and_nestable(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert_order_intent(order())
            store.update_order("c1", lifecycle=L.SENDING)
            store.append("x", {"a": 1})
            store.kv_set("k", 1)
            with store.transaction():
                assert store.in_transaction
                store.kv_set("k2", 2)
            raise RuntimeError("crash mid-transaction")
    assert store.get_order("c1") is None and list(store.iter_events()) == []
    assert store.kv_get("k") is None and store.kv_get("k2") is None and not store.in_transaction
    with store.transaction():
        store.insert_order_intent(order())
        store.update_order("c1", lifecycle=L.SENDING)
    assert store.get_order("c1").lifecycle is L.SENDING


def test_events_monotonic_filtered_paged(store):
    ids = [store.append("a" if i % 3 else "b", {"i": i}) for i in range(1200)]
    assert ids == sorted(ids) and len(set(ids)) == 1200
    allev = list(store.iter_events())
    assert [e.id for e in allev] == ids and [e.payload["i"] for e in allev] == list(range(1200))
    bs = list(store.iter_events("b"))
    assert len(bs) == 400 and all(e.kind == "b" for e in bs)
    assert [e.id for e in store.iter_events(since_id=ids[1197])] == ids[1198:]
    assert store.get_event(ids[5]).payload == {"i": 5} and store.get_event(10**9) is None
    with pytest.raises(ValueError):
        store.append("", {})
    with pytest.raises(TypeError):
        store.append("x", [1])


def test_event_ts_defaults_to_clock(tmp_path):
    clock = SimClock(1000.0)
    s = InMemoryStore(clock=clock)
    s.append("x", {})
    clock.advance(5)
    s.append("x", {}, ts=1.5)
    s.append("x", {})
    assert [e.ts for e in s.iter_events()] == [1000.0, 1.5, 1005.0]


def test_kv(store):
    assert store.kv_get("missing", default=7) == 7
    store.kv_set("a", {"x": [1, 2]})
    store.kv_set("a", "overwritten")
    assert store.kv_get("a") == "overwritten"


# --------------------------------------------------------------------------- durability & locking
def test_state_survives_close_and_reopen(tmp_path):
    p = tmp_path / "db.sqlite"
    with SqliteStore(p) as s:
        s.insert_order_intent(order())
        s.update_order("c1", lifecycle=L.SENDING)
        s.update_order("c1", lifecycle=L.OPEN, exchange_client_order_id="LIMIT-1")
        s.add_fill(fill())
        s.append("decision", {"edge": D("0.0040")})
        s.kv_set("bot_state", {"state": "BUY_PENDING"})
    assert not (tmp_path / LOCK_FILENAME).exists()
    with SqliteStore(p) as s:
        assert s.get_order("c1").lifecycle is L.OPEN
        assert s.list_fills() == [fill()]
        assert next(s.iter_events("decision")).payload == {"edge": "0.0040"}
        assert s.kv_get("bot_state") == {"state": "BUY_PENDING"}


def test_crash_after_persist_before_send_is_recovered(tmp_path):
    """A real process persists INTENT->SENDING then dies without closing (stale lock, WAL left)."""
    p = tmp_path / "db.sqlite"
    r = run_py(f"""
        import os
        from decimal import Decimal as D
        from wallex_agent.store import SqliteStore, OrderRecord
        from wallex_agent.types import OrderLifecycle as L
        s = SqliteStore(r"{p}")
        with s.transaction():
            s.insert_order_intent(OrderRecord(client_id="wa-1", symbol="BTCUSDT", side="BUY",
                price=D("81000.12"), quantity=D("0.00012345"), purpose="entry"))
            s.update_order("wa-1", lifecycle=L.SENDING)
        s.append("pre_send", {{"cid": "wa-1"}})
        with s.transaction():
            s.append("never_committed", {{}})
            os._exit(3)   # hard crash inside an open transaction
    """)
    assert r.returncode == 3, r.stderr
    lock = json.loads((tmp_path / LOCK_FILENAME).read_text())
    assert not pid_alive(lock["pid"])
    with SqliteStore(p) as s:                           # stale lock reclaimed
        [rec] = s.unresolved_orders()
        assert rec.client_id == "wa-1" and rec.lifecycle is L.SENDING
        assert rec.price == D("81000.12") and rec.quantity == D("0.00012345")
        assert [e.kind for e in s.iter_events()] == ["pre_send"]
        with pytest.raises(InvariantError):              # resolution path enforced after restart too
            s.update_order("wa-1", lifecycle=L.INTENT)
        s.update_order("wa-1", lifecycle=L.UNKNOWN_OUTCOME)


def test_second_instance_in_process_is_refused(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite")
    with pytest.raises(InstanceLockError) as ei:
        SqliteStore(tmp_path / "db.sqlite")
    assert isinstance(ei.value, RuntimeError)
    with pytest.raises(InstanceLockError):
        SqliteStore(tmp_path / "other.sqlite")        # lock is per directory
    s.close()
    SqliteStore(tmp_path / "db.sqlite").close()


def test_second_process_is_refused(tmp_path):
    with SqliteStore(tmp_path / "db.sqlite"):
        r = run_py(f"""
            import sys
            from wallex_agent.store import SqliteStore, InstanceLockError
            try:
                SqliteStore(r"{tmp_path / 'db.sqlite'}")
            except InstanceLockError as e:
                print(e); sys.exit(7)
            sys.exit(0)
        """)
        assert r.returncode == 7, r.stdout + r.stderr
        assert "another instance" in r.stdout or "held open" in r.stdout


def test_stale_lock_with_dead_pid_is_reclaimed(tmp_path):
    (tmp_path / LOCK_FILENAME).write_text(json.dumps(
        {"pid": dead_pid(), "host": socket.gethostname(), "token": "old", "created": 0}))
    with SqliteStore(tmp_path / "db.sqlite") as s:
        info = json.loads(s.lock_path.read_text())
        assert info["pid"] == os.getpid() and info["token"] != "old"
    assert not list(tmp_path.glob(LOCK_FILENAME + "*"))


def test_foreign_or_fresh_unreadable_lock_is_not_stolen(tmp_path):
    lock = tmp_path / LOCK_FILENAME
    lock.write_text(json.dumps({"pid": dead_pid(), "host": "some-other-host", "token": "t"}))
    with pytest.raises(InstanceLockError):
        SqliteStore(tmp_path / "db.sqlite")
    lock.write_text("")                                   # being written right now
    with pytest.raises(InstanceLockError):
        SqliteStore(tmp_path / "db.sqlite")
    old = time.time() - 3600
    os.utime(lock, (old, old))                            # unreadable and old => crashed writer
    SqliteStore(tmp_path / "db.sqlite").close()


def test_pid_alive():
    assert pid_alive(os.getpid())
    assert not pid_alive(dead_pid())
    assert not pid_alive(0) and not pid_alive(-5)
