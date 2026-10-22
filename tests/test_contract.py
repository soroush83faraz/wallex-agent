from decimal import Decimal as D

import pytest

from wallex_agent.config import load_config
from wallex_agent.errors import ConfigError
from wallex_agent.types import (BotState, MarketRules, OrderBook, Side, STATE_TRANSITIONS, dec, plain,
                                quantize_down)


def test_dec_rejects_bad_values():
    for bad in (True, None, float("nan"), "abc", float("inf")):
        with pytest.raises((TypeError, ValueError)):
            dec(bad)
    assert dec(0.1) == D("0.1") and dec("1e-06") == D("0.000001")


def test_plain_and_rounding():
    assert plain(D("1E-8")) == "0.00000001" and plain(D("10.5000")) == "10.5" and plain(D("0E-8")) == "0"
    assert quantize_down(D("1.23999"), 2) == D("1.23")


RULES = MarketRules("BTCUSDT", "BTC", "USDT", 8, 2, D("1e-8"), D("1"))
TMN = MarketRules("USDTTMN", "USDT", "TMN", 2, 0, D("0.01"), D("50000"))


def test_rules_validation():
    assert RULES.violations(D("81000.12"), D("0.0002")) == []
    assert "price precision>2" in RULES.violations(D("81000.123"), D("0.0002"))
    assert any(v.startswith("notional") for v in RULES.violations(D("81000"), D("0.000001")))
    assert any(v.startswith("notional") for v in TMN.violations(D("272000"), D("0.1")))
    assert TMN.violations(D("272000"), D("18.3")) == []
    assert TMN.round_qty(D("18.3649")) == D("18.36")
    assert RULES.round_price(D("100.019"), Side.BUY) == D("100.01")
    assert RULES.round_price(D("100.011"), Side.SELL) == D("100.02")


def book():
    return OrderBook.build("BTCUSDT", [(D(99), D(1)), (D(98), D(2))], [(D(101), D(1)), (D(102), D(2))], 0.0)


def test_book_walk():
    b = book()
    w = b.walk_qty(Side.BUY, D("1.5"))
    assert w.complete and w.quote_sum == D(101) + D("0.5") * 102 and w.worst_price == D(102)
    w = b.walk_qty(Side.SELL, D(5))
    assert not w.complete and w.filled_qty == D(3)
    w = b.walk_quote(Side.BUY, D(151))
    assert w.complete and w.filled_qty == D(1) + D(50) / D(102)
    assert b.depth_quote(Side.BUY, D("100")) == D(101) + D(204)  # 102 is within 100bps of 101


def test_book_rejects_bad():
    with pytest.raises(ValueError):
        OrderBook.build("X", [(D(100), D(1))], [(D(100), D(1))], 0.0)
    with pytest.raises(ValueError):
        OrderBook.build("X", [], [(D(100), D(1))], 0.0)


def test_state_table_is_closed():
    for s, outs in STATE_TRANSITIONS.items():
        assert s in BotState and all(o in BotState for o in outs)


def test_config_defaults_and_profile(tmp_path):
    c = load_config(env={}, dotenv_path=None)
    assert c.mode.value == "paper" and not c.enable_live and c.final_policy == "report_only"
    c2 = load_config(env={"PROFILE": "competition", "MAX_EXPOSURE_FRACTION": "0.5"}, dotenv_path=None)
    assert c2.max_exposure_fraction == D("0.5") and c2.max_round_trips == 6
    with pytest.raises(ConfigError):
        load_config(env={"TRADING_MODE": "live"}, dotenv_path=None)  # no key
    assert "SECRET" not in str(load_config(env={"WALLEX_API_KEY": "SECRET"}, dotenv_path=None))
    assert load_config(env={"WALLEX_API_KEY": "SECRET"}, dotenv_path=None).redacted()["api_key"] == "***set***"
