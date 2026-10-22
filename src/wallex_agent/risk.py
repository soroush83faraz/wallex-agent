"""Deterministic hard risk gates (pure: no I/O, time comes from the context) and forced exits.

Every rejection reason is ``"<code>: <detail>"`` where ``<code>`` is one of the ``R_*`` constants, so
callers and tests can match on the stable code. Exits (purpose ``exit``/``flatten``) bypass the
entry-only gates (edge, confidence, cooldown, round trips, exposure, drawdown, deadline) but never the
safety gates (kill, unresolved orders, unknown fee/rules/book, selling more than held).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Mapping

from .clock import Clock, RealClock
from .config import Config
from .portfolio import Position
from .types import (BPS, ONE, ZERO, Balance, BotState, FeeRates, MarketRules, OrderBook, Side,
                    TradeIntent)

R_INVALID = "invalid_intent"
R_SYMBOL = "symbol_not_allowed"
R_KILL = "kill_switch"
R_STOPPED = "bot_stopped"
R_DEADLINE = "deadline_passed"
R_UNRESOLVED = "unresolved_orders"
R_QUARANTINE = "quarantined"
R_STATE = "state_mismatch"
R_RULES_UNKNOWN = "rules_unknown"
R_FEE_UNKNOWN = "fee_unknown"
R_BOOK_MISSING = "book_missing"
R_BOOK_CROSSED = "book_crossed"
R_BOOK_STALE = "book_stale"
R_SLIPPAGE_CAP = "slippage_cap_too_high"
R_SPREAD = "spread_too_wide"
R_DEPTH = "insufficient_depth"
R_RULES = "rules_violation"
R_EXPOSURE = "exposure_cap"
R_BALANCE = "insufficient_balance"
R_SELL_HELD = "sell_exceeds_held"
R_ROUND_TRIPS = "round_trip_cap"
R_COOLDOWN = "cooldown_after_loss"
R_INTERVAL = "min_trade_interval"
R_EQUITY_UNKNOWN = "equity_unknown"
R_DRAWDOWN = "drawdown_breach"
R_EDGE_UNKNOWN = "edge_unknown"
R_EDGE = "net_edge_below_hurdle"
R_CONFIDENCE = "confidence_below_min"

PURPOSES = ("convert", "entry", "exit", "flatten")


@dataclass(frozen=True)
class RiskContext:
    """Everything the gate may look at. ``balances`` are exchange balances (``spendable`` used),
    ``ledger`` the fill-driven expected balances; held/spendable amounts use the minimum of both."""

    state: BotState
    balances: Mapping[str, Balance]
    fees: Mapping[str, FeeRates] | None
    rules: Mapping[str, MarketRules] | None
    books: Mapping[str, OrderBook]
    now: float | None = None
    ledger: Mapping[str, Decimal] | None = None
    position: Position | None = None
    equity_tmn: Decimal | None = None
    peak_equity_tmn: Decimal | None = None
    round_trips_done: int = 0
    last_trade_ts: float | None = None
    last_loss_ts: float | None = None
    deadline_ts: float | None = None
    kill: bool = False
    unresolved_orders: bool = False
    quarantined: frozenset[tuple[str, Side]] = frozenset()
    expected_move_bps: Decimal | None = None   # the decision's (calibrated) gross expected move
    confidence: float | None = None


@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    reasons: list[str]
    adjusted: TradeIntent | None          # the intent to execute (possibly shrunk) when approved
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)

    @property
    def codes(self) -> list[str]:
        return [r.split(":", 1)[0] for r in self.reasons]

    def to_dict(self) -> dict[str, object]:
        return {"approved": self.approved, "reasons": list(self.reasons), "notes": list(self.notes),
                "metrics": dict(self.metrics),
                "adjusted_qty": None if self.adjusted is None else str(self.adjusted.quantity)}


def _held(ctx: RiskContext, asset: str) -> Decimal:
    """What we may sell: min(ledger, exchange spendable) — never more than either says."""
    b = ctx.balances.get(asset)
    spendable = b.spendable if b else ZERO
    if ctx.ledger is None:
        return spendable
    return max(ZERO, min(spendable, ctx.ledger.get(asset, ZERO)))


def _drawdown(ctx: RiskContext) -> Decimal | None:
    if ctx.equity_tmn is None or ctx.peak_equity_tmn is None or ctx.peak_equity_tmn <= 0:
        return None
    return max(ZERO, (ctx.peak_equity_tmn - ctx.equity_tmn) / ctx.peak_equity_tmn)


def _book_problem(book: OrderBook | None, now: float, max_age: float) -> str | None:
    if book is None or not book.bids or not book.asks:
        return R_BOOK_MISSING
    if book.best_bid >= book.best_ask:
        return R_BOOK_CROSSED
    if book.age(now) > max_age:
        return R_BOOK_STALE
    return None


class RiskGate:
    def __init__(self, config: Config, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or RealClock()

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, intent: TradeIntent, ctx: RiskContext) -> RiskVerdict:
        cfg = self.config
        now = ctx.now if ctx.now is not None else self.clock.now()
        reasons: list[str] = []
        notes: list[str] = []
        metrics: dict[str, str] = {}

        def rej(code: str, detail: str) -> None:
            reasons.append(f"{code}: {detail}")

        btc, usd = cfg.btc_symbol, cfg.usdt_symbol
        purpose = intent.purpose
        is_exit = purpose in ("exit", "flatten") and intent.side is Side.SELL
        is_entry = purpose == "entry"
        # ---- shape of the intent
        shape_ok = {
            "entry": intent.side is Side.BUY and intent.symbol == btc,
            "exit": intent.side is Side.SELL and intent.symbol == btc,
            "convert": intent.side is Side.BUY and intent.symbol == usd,
            "flatten": intent.side is Side.SELL and intent.symbol in (btc, usd),
        }.get(purpose, False)
        if not shape_ok:
            rej(R_INVALID, f"purpose {purpose!r} with {intent.side.value} {intent.symbol}")
        if intent.symbol not in (btc, usd):
            rej(R_SYMBOL, intent.symbol)
        if intent.quantity <= 0 or intent.reference_price <= 0 or intent.max_slippage_bps < 0:
            rej(R_INVALID, "quantity/reference_price must be > 0 and slippage >= 0")
        # ---- operator / lifecycle gates
        if ctx.kill:
            rej(R_KILL, "kill switch engaged")
        if ctx.state is BotState.STOPPED:
            rej(R_STOPPED, "bot is STOPPED")
        if ctx.deadline_ts is not None and now >= ctx.deadline_ts and not is_exit:
            rej(R_DEADLINE, "deadline passed: only exits allowed")
        if ctx.unresolved_orders:
            rej(R_UNRESOLVED, "an order is unresolved/unknown")
        if (intent.symbol, intent.side) in ctx.quarantined:
            rej(R_QUARANTINE, f"{intent.symbol} {intent.side.value} quarantined after an unknown outcome")
        required = {"entry": (BotState.READY,), "exit": (BotState.BTC_HELD,),
                    "convert": (BotState.UNFUNDED, BotState.CONVERTING, BotState.READY),
                    "flatten": (BotState.BTC_HELD,) if intent.symbol == btc else (BotState.READY,)}
        if purpose in required and ctx.state not in required[purpose] and ctx.state is not BotState.STOPPED:
            rej(R_STATE, f"{purpose} not allowed in state {ctx.state.value}")
        # ---- market knowledge
        rules = (ctx.rules or {}).get(intent.symbol)
        if rules is None:
            rej(R_RULES_UNKNOWN, f"no market rules for {intent.symbol}")
        fee = (ctx.fees or {}).get(intent.symbol)
        if fee is None:
            rej(R_FEE_UNKNOWN, f"no fee for {intent.symbol}")
        book = ctx.books.get(intent.symbol)
        bp = _book_problem(book, now, cfg.max_book_age_s)
        if bp:
            rej(bp, f"{intent.symbol} book" + (f" age {book.age(now):.1f}s" if bp == R_BOOK_STALE else ""))
        slip_cap = cfg.conversion_slippage_bps if intent.symbol == usd else cfg.max_slippage_bps
        if intent.max_slippage_bps > slip_cap:
            rej(R_SLIPPAGE_CAP, f"{intent.max_slippage_bps} > {slip_cap} bps")

        qty = intent.quantity
        if rules is not None and fee is not None and bp is None and qty > 0 and intent.reference_price > 0:
            assert book is not None
            spread = book.spread_bps
            metrics["spread_bps"] = str(spread)
            if not is_exit and spread > cfg.max_spread_bps:
                rej(R_SPREAD, f"{spread:.2f} > {cfg.max_spread_bps} bps")
            slip = intent.max_slippage_bps / BPS
            side = intent.side
            cap_price = intent.reference_price * (ONE + slip if side is Side.BUY else ONE - slip)
            price_r = rules.round_price(cap_price, side)
            if not is_exit:
                depth = book.depth_quote(side, intent.max_slippage_bps)
                need = cfg.min_book_depth_quote_usdt if intent.symbol == btc else qty * intent.reference_price
                metrics["depth_quote"] = str(depth)
                if depth < need:
                    rej(R_DEPTH, f"{depth} {rules.quote} within {intent.max_slippage_bps} bps < {need}")
            orig_violations = rules.violations(price_r, rules.round_qty(qty))
            if orig_violations:
                rej(R_RULES, ",".join(orig_violations))
            shrink_code = None
            if side is Side.BUY:
                spend = _held(ctx, rules.quote)
                afford = spend / (price_r * (ONE + fee.taker + cfg.fee_safety_margin)) if price_r > 0 else ZERO
                if qty > afford:
                    notes.append(f"buy shrunk {qty} -> {afford} to fit spendable {spend} {rules.quote} incl. fees")
                    qty, shrink_code = afford, R_BALANCE
                if is_entry:
                    held_base = _held(ctx, rules.base)
                    equity_q = spend + held_base * book.best_bid
                    room = cfg.max_exposure_fraction * equity_q - held_base * book.best_bid
                    max_q = max(ZERO, room) / price_r if price_r > 0 else ZERO
                    metrics["exposure_room_quote"] = str(room)
                    if qty > max_q:
                        notes.append(f"entry shrunk {qty} -> {max_q} by exposure cap {cfg.max_exposure_fraction}")
                        qty, shrink_code = max_q, R_EXPOSURE
            else:
                held = _held(ctx, rules.base)
                metrics["held_base"] = str(held)
                if qty > held:
                    notes.append(f"sell shrunk {qty} -> {held} (ledger/spendable)")
                    qty, shrink_code = held, R_SELL_HELD
            qty = rules.round_qty(qty)
            if not orig_violations:
                v = rules.violations(price_r, qty)
                if v:
                    rej(shrink_code or R_RULES, ",".join(v) + f" after sizing (qty {qty})")
            # ---- entry economics
            if is_entry:
                if ctx.expected_move_bps is None:
                    rej(R_EDGE_UNKNOWN, "no expected move supplied")
                elif qty > 0:
                    buy = book.walk_qty(Side.BUY, qty)
                    sell = book.walk_qty(Side.SELL, qty)
                    exit_fee = (ctx.fees or {}).get(btc)
                    if not (buy.complete and sell.complete):
                        rej(R_DEPTH, "visible book cannot absorb the round trip")
                    elif exit_fee is None:
                        rej(R_FEE_UNKNOWN, f"no exit fee for {btc}")
                    else:
                        mid = book.mid
                        cost = ((buy.avg_price - sell.avg_price) / mid * BPS
                                + (fee.taker + exit_fee.taker) * BPS)
                        net = ctx.expected_move_bps - cost
                        metrics.update(round_trip_cost_bps=str(cost), net_edge_bps=str(net))
                        if net < cfg.min_net_edge_bps:
                            rej(R_EDGE, f"net {net:.2f} bps < hurdle {cfg.min_net_edge_bps} "
                                        f"(move {ctx.expected_move_bps} - costs {cost:.2f})")
        # ---- entry-only pacing / risk budget
        if is_entry:
            if ctx.round_trips_done >= cfg.max_round_trips:
                rej(R_ROUND_TRIPS, f"{ctx.round_trips_done} >= {cfg.max_round_trips}")
            if ctx.last_loss_ts is not None and now - ctx.last_loss_ts < cfg.cooldown_after_loss_s:
                rej(R_COOLDOWN, f"{now - ctx.last_loss_ts:.0f}s < {cfg.cooldown_after_loss_s}s since loss")
            dd = _drawdown(ctx)
            if dd is None:
                rej(R_EQUITY_UNKNOWN, "equity/peak unknown: cannot check drawdown")
            elif dd >= cfg.max_drawdown_pct:
                rej(R_DRAWDOWN, f"drawdown {dd:.4f} >= {cfg.max_drawdown_pct}: exits only")
            if ctx.confidence is not None and ctx.confidence < cfg.min_confidence:
                rej(R_CONFIDENCE, f"{ctx.confidence} < {cfg.min_confidence}")
        if not is_exit and ctx.last_trade_ts is not None and now - ctx.last_trade_ts < cfg.min_trade_interval_s:
            rej(R_INTERVAL, f"{now - ctx.last_trade_ts:.0f}s < {cfg.min_trade_interval_s}s since last trade")

        approved = not reasons
        adjusted = None
        if approved:
            adjusted = intent if qty == intent.quantity else replace(intent, quantity=qty)
        return RiskVerdict(approved, reasons, adjusted, notes, metrics)

    # ------------------------------------------------------------------ forced exits
    def forced_exit(self, ctx: RiskContext) -> TradeIntent | None:
        return forced_exit(ctx, self.config, self.clock)


def forced_exit(ctx: RiskContext, config: Config, clock: Clock | None = None) -> TradeIntent | None:
    """Hard exits that do not depend on the agent: stop-loss (mark-to-bid net of the exit fee),
    drawdown stop, and deadline flatten ONLY when ``final_policy`` asks for it. Returns ``None`` when
    nothing must happen (or nothing can be sold). The returned intent still goes through ``evaluate``.
    """
    now = ctx.now if ctx.now is not None else (clock or RealClock()).now()
    btc, usd = config.btc_symbol, config.usdt_symbol
    deadline = ctx.deadline_ts is not None and now >= ctx.deadline_ts
    if ctx.state is BotState.STOPPED or ctx.unresolved_orders:
        return None
    rules = (ctx.rules or {}).get(btc)
    book = ctx.books.get(btc)
    if rules is None or book is None or not book.bids:
        return None
    held = rules.round_qty(_held(ctx, rules.base))
    sellable = held > 0 and not rules.violations(rules.round_price(book.best_bid, Side.SELL), held)

    def exit_intent(purpose: str, note: str) -> TradeIntent:
        return TradeIntent(purpose=purpose, side=Side.SELL, symbol=btc, quantity=held,
                           reference_price=book.best_bid, max_slippage_bps=config.max_slippage_bps,
                           style="aggressive_limit", note=note)

    if deadline:
        if config.final_policy not in ("flatten_to_usdt", "flatten_to_tmn"):
            return None
        if sellable and ctx.state is BotState.BTC_HELD:
            return exit_intent("flatten", f"deadline_flatten ({config.final_policy})")
        if config.final_policy == "flatten_to_tmn" and ctx.state is BotState.READY:
            ur, ub = (ctx.rules or {}).get(usd), ctx.books.get(usd)
            if ur is None or ub is None or not ub.bids:
                return None
            uq = ur.round_qty(_held(ctx, ur.base))
            if uq > 0 and not ur.violations(ur.round_price(ub.best_bid, Side.SELL), uq):
                return TradeIntent(purpose="flatten", side=Side.SELL, symbol=usd, quantity=uq,
                                   reference_price=ub.best_bid, max_slippage_bps=config.conversion_slippage_bps,
                                   note="deadline_flatten (flatten_to_tmn)")
        return None
    if not sellable or ctx.state is not BotState.BTC_HELD:
        return None
    dd = _drawdown(ctx)
    if dd is not None and dd >= config.max_drawdown_pct:
        return exit_intent("exit", f"drawdown_stop {dd:.4f} >= {config.max_drawdown_pct}")
    pos = ctx.position
    cost = getattr(pos, "cost_usdt", ZERO) if pos is not None else ZERO
    pos_qty = getattr(pos, "qty", ZERO) if pos is not None else ZERO
    if cost > 0 and pos_qty > 0:
        fee = (ctx.fees or {}).get(btc)
        rate = fee.taker if fee is not None else ZERO   # unknown fee => lower bound on the loss
        mark_qty = min(held, pos_qty)
        walk = book.walk_qty(Side.SELL, mark_qty)
        net = walk.quote_sum * (ONE - rate)              # unfilled remainder valued at 0 (conservative)
        basis = cost * mark_qty / pos_qty
        loss = (basis - net) / basis
        if loss >= config.stop_loss_pct:
            return exit_intent("exit", f"hard_stop_loss {loss:.4f} >= {config.stop_loss_pct}")
    return None
