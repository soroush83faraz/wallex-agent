import json
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path

import pytest

from wallex_agent.clock import SimClock
from wallex_agent.errors import InvariantError
from wallex_agent.state import STATE_KEY, StateMachine, derive_state, derive_state_explained
from wallex_agent.store import InMemoryStore, SqliteStore
from wallex_agent.types import STATE_TRANSITIONS, Balance, BotState as S, MarketRules, OrderBook, Side

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"), parse_float=D)


def real_rules():
    syms = _load("markets_real_2026-10-22.json")["result"]["symbols"]
    return {s: MarketRules(s, m["baseAsset"], m["quoteAsset"], int(m["stepSize"]), int(m["tickSize"]),
                           D(str(m["minQty"])), D(str(m["minNotional"])))
            for s, m in syms.items() if s in ("BTCUSDT", "USDTTMN")}


def real_book(symbol, fname):
    r = _load(fname)["result"]
    lv = lambda side: [(D(str(x["price"])), D(str(x["quantity"]))) for x in r[side]]
    return OrderBook.build(symbol, lv("bid"), lv("ask"), 0.0)


RULES = real_rules()
BOOKS = {"BTCUSDT": real_book("BTCUSDT", "depth_btcusdt_real.json"),
         "USDTTMN": real_book("USDTTMN", "depth_usdttmn_real.json")}


def test_fixture_rules_are_the_real_ones():
    b, u = RULES["BTCUSDT"], RULES["USDTTMN"]
    assert (b.qty_decimals, b.price_decimals, b.min_notional) == (8, 2, D(1))
    assert (u.qty_decimals, u.price_decimals, u.min_qty, u.min_notional) == (2, 0, D("0.01"), D(50000))
    assert D(270000) < BOOKS["USDTTMN"].best_bid < D(275000)


@dataclass
class O:
    symbol: str
    side: Side


def bals(**kw):
    out = {}
    for asset, v in kw.items():
        value, locked = (v if isinstance(v, tuple) else (v, "0"))
        out[asset] = Balance(asset, D(value), D(locked))
    return out


CASES = [
    # id, balances, open orders, expected
    ("tmn_only_convertible", bals(TMN="5000000"), [], S.UNFUNDED),
    ("tmn_below_conversion_min", bals(TMN="40000"), [], S.UNFUNDED),
    ("nothing", {}, [], S.UNFUNDED),
    ("usdt_sufficient", bals(USDT="18.3"), [], S.READY),
    ("usdt_and_leftover_tmn", bals(USDT="18.3", TMN="15000"), [], S.READY),
    ("usdt_below_min_with_tmn", bals(USDT="0.5", TMN="5000000"), [], S.UNFUNDED),
    ("usdt_all_locked", bals(USDT=("18.3", "18.3")), [], S.UNFUNDED),
    ("btc_held", bals(BTC="0.0002", USDT="0.4"), [], S.BTC_HELD),
    ("btc_held_wins_over_usdt", bals(BTC="0.0002", USDT="18"), [], S.BTC_HELD),
    ("btc_dust_no_usdt", bals(BTC="0.00001", USDT="0.2"), [], S.UNFUNDED),        # 0.81 USDT < 1
    ("btc_dust_with_usdt", bals(BTC="0.00001", USDT="18"), [], S.READY),
    ("btc_just_sellable", bals(BTC="0.00002"), [], S.BTC_HELD),                   # 1.62 USDT >= 1
    ("btc_sub_step", bals(BTC="0.000000009", USDT="0.1"), [], S.UNFUNDED),
    ("open_buy", bals(USDT="1"), [O("BTCUSDT", Side.BUY)], S.BUY_PENDING),
    ("open_sell", bals(BTC="0.0002"), [O("BTCUSDT", Side.SELL)], S.SELL_PENDING),
    ("open_sell_str_side", bals(BTC="0.0002"), [O("BTCUSDT", "SELL")], S.SELL_PENDING),
    ("open_conversion", bals(TMN="5000000"), [O("USDTTMN", Side.BUY)], S.CONVERTING),
    ("open_reconversion", bals(USDT="18"), [O("USDTTMN", Side.SELL)], S.CONVERTING),
    ("buy_and_sell_open", bals(BTC="0.0002"), [O("BTCUSDT", Side.BUY), O("BTCUSDT", Side.SELL)], S.STOPPED),
    ("btc_and_conversion_open", bals(), [O("BTCUSDT", Side.BUY), O("USDTTMN", Side.BUY)], S.STOPPED),
    ("other_symbol_ignored", bals(USDT="18"), [O("ETHUSDT", Side.BUY)], S.READY),
]


@pytest.mark.parametrize("bal,orders,expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_derive_state_table(bal, orders, expected):
    assert derive_state(bal, orders, RULES, BOOKS) is expected
    state, why = derive_state_explained(bal, orders, RULES, BOOKS)
    assert state is expected and why


def test_derive_state_fail_closed_inputs():
    assert derive_state(bals(USDT="18"), [], {"BTCUSDT": RULES["BTCUSDT"]}, BOOKS) is S.STOPPED
    # BTC sellable by quantity but no book to price it: assume held (never buy on top of it)
    assert derive_state(bals(BTC="0.0002", USDT="18"), [], RULES, {}) is S.BTC_HELD
    # without a BTC book, dust-by-notional cannot be proven either -> held
    assert derive_state(bals(BTC="0.00001"), [], RULES, {}) is S.BTC_HELD


def test_derive_state_is_pure():
    b, o = bals(BTC="0.0002"), [O("BTCUSDT", Side.SELL)]
    before = (dict(b), list(o))
    for _ in range(3):
        assert derive_state(b, o, RULES, BOOKS) is S.SELL_PENDING
    assert (dict(b), list(o)) == before


# --------------------------------------------------------------------------- StateMachine
def sm():
    st = InMemoryStore(clock=SimClock())
    return st, StateMachine(st, SimClock(5000.0))


def test_default_state_and_happy_path():
    st, m = sm()
    assert m.state is S.UNFUNDED and m.history() == []
    for to in (S.CONVERTING, S.READY, S.BUY_PENDING, S.BTC_HELD, S.SELL_PENDING, S.READY, S.STOPPED):
        assert m.transition(to, f"go {to.value}", {"px": D("81208.15")}) is to
        assert m.state is to
    h = m.history()
    assert [x["to"] for x in h] == ["CONVERTING", "READY", "BUY_PENDING", "BTC_HELD", "SELL_PENDING",
                                    "READY", "STOPPED"]
    assert h[0]["from"] == "UNFUNDED" and h[0]["evidence"] == {"px": "81208.15"} and h[0]["ts"] == 5000.0
    assert not any(x["forced"] for x in h)
    ids = [x["id"] for x in h]
    assert ids == sorted(ids)


@pytest.mark.parametrize("src", list(S))
def test_every_state_transition_is_enforced(src):
    for dst in S:
        st, m = sm()
        if src is not S.UNFUNDED:
            m.transition(src, "setup", force=True)
        n = len(m.history())
        if dst in STATE_TRANSITIONS[src]:
            m.transition(dst, "legal")
            assert m.state is dst and len(m.history()) == n + 1
        else:
            with pytest.raises(InvariantError):
                m.transition(dst, "illegal")
            assert m.state is src and len(m.history()) == n      # nothing persisted


def test_force_is_recorded_and_reason_required():
    st, m = sm()
    m.transition(S.STOPPED, "kill")
    with pytest.raises(InvariantError):
        m.transition(S.READY, "resume")
    m.transition(S.READY, "operator resume", {"by": "operator"}, force=True)
    assert m.state is S.READY and m.history()[-1]["forced"] is True
    with pytest.raises(ValueError):
        m.transition(S.BUY_PENDING, "")


def test_transition_rolls_back_with_outer_transaction():
    st, m = sm()
    with pytest.raises(RuntimeError):
        with st.transaction():
            m.transition(S.CONVERTING, "convert")
            assert m.state is S.CONVERTING
            raise RuntimeError("crash")
    assert m.state is S.UNFUNDED and m.history() == []


def test_state_persists_across_restart(tmp_path):
    p = tmp_path / "db.sqlite"
    with SqliteStore(p) as st:
        m = StateMachine(st, SimClock())
        m.transition(S.CONVERTING, "convert")
        m.transition(S.READY, "converted")
        m.transition(S.BUY_PENDING, "entry", {"cid": "wa-1"})
    with SqliteStore(p) as st:
        m = StateMachine(st, SimClock())
        assert m.state is S.BUY_PENDING and len(m.history()) == 3
        with pytest.raises(InvariantError):
            m.transition(S.CONVERTING, "illegal after restart")


def test_corrupt_or_inconsistent_persisted_state_fails_closed():
    st, m = sm()
    st.kv_set(STATE_KEY, {"state": "BOGUS"})
    with pytest.raises(InvariantError):
        _ = m.state
    st2, m2 = sm()
    m2.transition(S.CONVERTING, "convert")
    st2.kv_set(STATE_KEY, {"state": "READY"})           # kv edited behind the event log's back
    with pytest.raises(InvariantError):
        StateMachine(st2, SimClock())
