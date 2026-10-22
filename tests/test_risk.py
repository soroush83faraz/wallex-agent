from dataclasses import replace
from decimal import Decimal as D

import pytest

from wallex_agent import risk as R
from wallex_agent.config import Config
from wallex_agent.portfolio import Position
from wallex_agent.risk import RiskContext, RiskGate, forced_exit
from wallex_agent.types import Balance, BotState as S, OrderBook, Side, TradeIntent

from ._sim_support import BTC, BTC_ASKS, BTC_BIDS, FEES, RULES, USD, USD_ASKS, USD_BIDS

NOW = 1_700_000_000.0
CFG = Config()


class NoClock:
    def now(self):
        raise AssertionError("gate must use ctx.now")


GATE = RiskGate(CFG, NoClock())


def books(ts=NOW):
    return {BTC: OrderBook.build(BTC, BTC_BIDS, BTC_ASKS, ts), USD: OrderBook.build(USD, USD_BIDS, USD_ASKS, ts)}


def bal(usdt="100", btc="0", tmn="0"):
    return {"USDT": Balance("USDT", D(usdt)), "BTC": Balance("BTC", D(btc)), "TMN": Balance("TMN", D(tmn))}


def ctx(**kw):
    base = dict(state=S.READY, balances=bal(), fees=FEES, rules=RULES, books=books(), now=NOW,
                ledger={"USDT": D("100"), "BTC": D(0), "TMN": D(0)}, equity_tmn=D("5000000"),
                peak_equity_tmn=D("5000000"), expected_move_bps=D("200"), confidence=0.9)
    base.update(kw)
    return RiskContext(**base)


def entry(qty="0.0002", **kw):
    a = dict(purpose="entry", side=Side.BUY, symbol=BTC, quantity=D(qty), reference_price=D("81010"),
             max_slippage_bps=D("20"))
    a.update(kw)
    return TradeIntent(**a)


def exit_(qty="0.0002", **kw):
    a = dict(purpose="exit", side=Side.SELL, symbol=BTC, quantity=D(qty), reference_price=D("81000"),
             max_slippage_bps=D("20"))
    a.update(kw)
    return TradeIntent(**a)


def held_ctx(btc="0.0002", **kw):
    base = dict(state=S.BTC_HELD, balances=bal("10", btc), ledger={"USDT": D("10"), "BTC": D(btc), "TMN": D(0)})
    base.update(kw)
    return ctx(**base)


def test_baseline_entry_and_exit_are_approved():
    v = GATE.evaluate(entry(), ctx())
    assert v.approved and v.reasons == [] and v.adjusted == entry()
    assert D(v.metrics["net_edge_bps"]) > CFG.min_net_edge_bps
    v = GATE.evaluate(exit_(), held_ctx())
    assert v.approved and v.adjusted.quantity == D("0.0002")


def test_pure_and_deterministic():
    c = ctx()
    assert GATE.evaluate(entry(), c) == GATE.evaluate(entry(), c)


def crossed():
    from wallex_agent.types import BookLevel
    b = OrderBook(BTC, (BookLevel(D("81100"), D("1")),), (BookLevel(D("81010"), D("1")),), NOW)
    return {**books(), BTC: b}


def thin():
    return {**books(), BTC: OrderBook.build(BTC, [(D("81000"), D("0.0001"))], [(D("81010"), D("0.0001"))], NOW)}


def wide():
    return {**books(), BTC: OrderBook.build(BTC, [(D("80000"), D("1"))], [(D("81010"), D("1"))], NOW)}


ENTRY_CASES = {
    R.R_KILL: (entry(), dict(kill=True)),
    R.R_STOPPED: (entry(), dict(state=S.STOPPED)),
    R.R_DEADLINE: (entry(), dict(deadline_ts=NOW - 1)),
    R.R_UNRESOLVED: (entry(), dict(unresolved_orders=True)),
    R.R_QUARANTINE: (entry(), dict(quarantined=frozenset({(BTC, Side.BUY)}))),
    R.R_STATE: (entry(), dict(state=S.BTC_HELD)),
    R.R_RULES_UNKNOWN: (entry(), dict(rules={USD: RULES[USD]})),
    R.R_FEE_UNKNOWN: (entry(), dict(fees={USD: FEES[USD]})),
    R.R_BOOK_MISSING: (entry(), dict(books={USD: books()[USD]})),
    R.R_BOOK_STALE: (entry(), dict(books=books(NOW - CFG.max_book_age_s - 1))),
    R.R_BOOK_CROSSED: (entry(), dict(books=crossed())),
    R.R_SLIPPAGE_CAP: (entry(max_slippage_bps=CFG.max_slippage_bps + 1), {}),
    R.R_SPREAD: (entry(), dict(books=wide(), expected_move_bps=D("100000"))),
    R.R_DEPTH: (entry(qty="0.0001"), dict(books=thin())),
    R.R_RULES: (entry(qty="0.000000009"), {}),
    R.R_ROUND_TRIPS: (entry(), dict(round_trips_done=CFG.max_round_trips)),
    R.R_COOLDOWN: (entry(), dict(last_loss_ts=NOW - CFG.cooldown_after_loss_s + 1)),
    R.R_INTERVAL: (entry(), dict(last_trade_ts=NOW - CFG.min_trade_interval_s + 1)),
    R.R_EQUITY_UNKNOWN: (entry(), dict(equity_tmn=None)),
    R.R_DRAWDOWN: (entry(), dict(equity_tmn=D("5000000") * (1 - CFG.max_drawdown_pct))),
    R.R_EDGE_UNKNOWN: (entry(), dict(expected_move_bps=None)),
    R.R_EDGE: (entry(), dict(expected_move_bps=D("60"))),
    R.R_CONFIDENCE: (entry(), dict(confidence=CFG.min_confidence - 0.01)),
    R.R_INVALID: (entry(side=Side.SELL), {}),
    R.R_SYMBOL: (entry(symbol="ETHUSDT"), {}),
}


@pytest.mark.parametrize("code", sorted(ENTRY_CASES))
def test_each_gate_rejects_with_its_own_reason(code):
    intent, over = ENTRY_CASES[code]
    v = GATE.evaluate(intent, ctx(**over))
    assert not v.approved and v.adjusted is None
    assert code in v.codes, v.reasons
    isolated = code not in (R.R_DEPTH, R.R_INVALID, R.R_SYMBOL)   # these imply secondary reasons
    if isolated:
        assert v.codes == [code], v.reasons


def test_reason_codes_are_distinct():
    codes = [getattr(R, n) for n in dir(R) if n.startswith("R_")]
    assert len(codes) == len(set(codes)) >= 25


def test_insufficient_balance_shrinks_then_rejects():
    cfg = replace(CFG, max_exposure_fraction=D("1"))
    g = RiskGate(cfg, NoClock())
    v = g.evaluate(entry(qty="0.0002"), ctx(balances=bal("10"), ledger={"USDT": D("10")}))
    assert v.approved and v.adjusted.quantity < D("0.0002")
    fee = FEES[BTC].taker + cfg.fee_safety_margin
    cap = RULES[BTC].round_price(D("81010") * (1 + D("20") / 10000), Side.BUY)
    assert v.adjusted.quantity * cap * (1 + fee) <= D("10")
    v = g.evaluate(entry(), ctx(balances=bal("0.5"), ledger={"USDT": D("0.5")}))
    assert v.codes == [R.R_BALANCE]


def test_spendable_is_min_of_ledger_and_exchange():
    v = GATE.evaluate(entry(), ctx(ledger={"USDT": D("0.5")}))
    assert R.R_BALANCE in v.codes or R.R_EXPOSURE in v.codes
    locked = {"USDT": Balance("USDT", D("100"), D("99.5"))}
    assert not GATE.evaluate(entry(), ctx(balances=locked)).approved


def test_exposure_cap_shrinks_and_rejects_when_no_room():
    v = GATE.evaluate(entry(qty="0.01"), ctx())       # 810 USDT wanted; 25% of 100 allowed
    assert v.approved and v.adjusted.quantity * D("81010") <= D("25.1")
    assert any("exposure" in n for n in v.notes)
    v = GATE.evaluate(entry(), ctx(balances=bal("100", "0.001"), ledger={"USDT": D("100"), "BTC": D("0.001")}))
    assert v.codes == [R.R_EXPOSURE]


def test_sell_never_exceeds_held():
    v = GATE.evaluate(exit_(qty="1"), held_ctx())
    assert v.approved and v.adjusted.quantity == D("0.0002")
    v = GATE.evaluate(exit_(), held_ctx(btc="0"))
    assert v.codes == [R.R_SELL_HELD]
    # ledger says less than the exchange: the ledger wins
    c = held_ctx(btc="0.0002", ledger={"BTC": D("0.0001"), "USDT": D(10)})
    assert GATE.evaluate(exit_(), c).adjusted.quantity == D("0.0001")


def test_exits_bypass_entry_only_gates_but_not_safety_gates():
    c = held_ctx(deadline_ts=NOW - 1, equity_tmn=D("1"), round_trips_done=99, last_loss_ts=NOW,
                 last_trade_ts=NOW, expected_move_bps=None, confidence=0.0, books={**books(), BTC: wide()[BTC]})
    assert GATE.evaluate(exit_(), c).approved
    for over, code in [(dict(kill=True), R.R_KILL), (dict(unresolved_orders=True), R.R_UNRESOLVED),
                       (dict(fees={}), R.R_FEE_UNKNOWN), (dict(books=books(NOW - 100)), R.R_BOOK_STALE),
                       (dict(state=S.READY), R.R_STATE)]:
        v = GATE.evaluate(exit_(), held_ctx(**over))
        assert code in v.codes


def test_conversion_intent_gates():
    conv = TradeIntent("convert", Side.BUY, USD, D("18"), D("272000"), D("30"))
    c = ctx(state=S.UNFUNDED, balances=bal("0", "0", "5000000"), ledger={"TMN": D("5000000")})
    assert GATE.evaluate(conv, c).approved
    assert R.R_SLIPPAGE_CAP in GATE.evaluate(replace(conv, max_slippage_bps=D("31")), c).codes
    assert R.R_STATE in GATE.evaluate(conv, replace(c, state=S.BTC_HELD)).codes
    v = GATE.evaluate(replace(conv, quantity=D("0.1")), c)
    assert R.R_RULES in v.codes      # 27,200 TMN < 50,000 minNotional


# --------------------------------------------------------------------------- forced exits
def pos(qty="0.0002", cost="16.2"):
    return Position(qty=D(qty), avg_cost_usdt=D(cost) / D(qty), entry_ts=NOW - 100, realized_pnl_usdt=D(0),
                    round_trips=0, wins=0, losses=0, cost_usdt=D(cost))


def test_forced_exit_none_when_flat_or_fine():
    assert forced_exit(ctx(), CFG) is None
    assert forced_exit(held_ctx(position=pos(cost="16.2")), CFG) is None   # mark 16.2*(1-0.0035)


def test_hard_stop_loss_is_net_of_exit_fee():
    # bid 81000 * 0.0002 = 16.2 gross. cost 16.42 => gross loss 1.34% < 1.5%, net loss 1.68% >= 1.5%
    c = held_ctx(position=pos(cost="16.42"))
    fe = forced_exit(c, CFG)
    assert fe is not None and fe.side is Side.SELL and fe.quantity == D("0.0002") and "hard_stop_loss" in fe.note
    assert forced_exit(replace(c, fees={**FEES, BTC: replace(FEES[BTC], taker=D(0))}), CFG) is None
    assert GATE.evaluate(fe, c).approved
    assert GATE.forced_exit(c) == fe


def test_drawdown_stop_forces_exit_and_blocks_entries():
    c = held_ctx(position=pos(), equity_tmn=D("4500000"))
    fe = forced_exit(c, CFG)
    assert fe is not None and "drawdown_stop" in fe.note and GATE.evaluate(fe, c).approved


@pytest.mark.parametrize("policy,expect", [("report_only", None), ("flatten_to_usdt", "flatten"),
                                           ("flatten_to_tmn", "flatten")])
def test_deadline_only_flattens_if_policy_says_so(policy, expect):
    cfg = replace(CFG, final_policy=policy)
    c = held_ctx(position=pos(), deadline_ts=NOW)
    fe = forced_exit(c, cfg)
    assert (fe.purpose if fe else None) == expect
    if fe:
        assert fe.symbol == BTC and RiskGate(cfg, NoClock()).evaluate(fe, c).approved


def test_deadline_flatten_to_tmn_sells_usdt_when_flat():
    cfg = replace(CFG, final_policy="flatten_to_tmn")
    c = ctx(deadline_ts=NOW, balances=bal("18.2"), ledger={"USDT": D("18.2")})
    fe = forced_exit(c, cfg)
    assert fe is not None and fe.symbol == USD and fe.side is Side.SELL and fe.quantity == D("18.2")
    assert RiskGate(cfg, NoClock()).evaluate(fe, c).approved
    assert forced_exit(c, replace(cfg, final_policy="flatten_to_usdt")) is None


def test_forced_exit_skips_while_orders_unresolved_or_stopped():
    c = held_ctx(position=pos(cost="30"))
    assert forced_exit(c, CFG) is not None
    assert forced_exit(replace(c, unresolved_orders=True), CFG) is None
    assert forced_exit(replace(c, state=S.STOPPED), CFG) is None
    assert forced_exit(replace(c, state=S.SELL_PENDING), CFG) is None
