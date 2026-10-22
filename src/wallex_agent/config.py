"""Configuration. Conservative defaults; nothing here can enable live trading by itself."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .types import Mode, dec

FINAL_POLICIES = ("report_only", "flatten_to_usdt", "flatten_to_tmn")

# `competition` is a deliberate, explicit opt-in to more risk. It does NOT promise returns.
PROFILES: dict[str, dict[str, Any]] = {
    "conservative": {},
    "competition": {
        "max_exposure_fraction": Decimal("0.90"),
        "stop_loss_pct": Decimal("0.03"),
        "max_drawdown_pct": Decimal("0.35"),
        "max_round_trips": 6,
        "min_net_edge_bps": Decimal("20"),
        "min_confidence": 0.50,
        "cooldown_after_loss_s": 600.0,
    },
}


@dataclass(frozen=True)
class Config:
    # ---- run identity
    mode: Mode = Mode.PAPER
    profile: str = "conservative"
    data_dir: str = "state"
    api_base: str = "https://api.wallex.ir"
    api_key: str = field(default="", repr=False)   # secret: never logged / reported
    enable_live: bool = False
    use_env_proxy: bool = False                     # honour HTTP(S)_PROXY (changes source IP!)

    # ---- competition
    initial_tmn: Decimal = Decimal("5000000")
    competition_hours: float = 24.0
    target_multiple: Decimal = Decimal("2")        # scoring target only; never forces trades
    final_policy: str = "report_only"

    # ---- symbols
    usdt_symbol: str = "USDTTMN"
    btc_symbol: str = "BTCUSDT"

    # ---- risk (hard, deterministic gates)
    max_exposure_fraction: Decimal = Decimal("0.25")   # of USDT-equivalent equity into BTC
    stop_loss_pct: Decimal = Decimal("0.015")          # forced exit when mark-to-bid net loss >= this
    max_drawdown_pct: Decimal = Decimal("0.08")        # equity drawdown from peak => STOPPED
    max_round_trips: int = 4
    min_net_edge_bps: Decimal = Decimal("30")          # required edge AFTER fees+spread+slippage
    max_spread_bps: Decimal = Decimal("40")
    max_slippage_bps: Decimal = Decimal("25")
    min_confidence: float = 0.55
    cooldown_after_loss_s: float = 900.0
    min_trade_interval_s: float = 120.0
    max_book_age_s: float = 15.0
    min_book_depth_quote_usdt: Decimal = Decimal("50")  # visible depth within slippage cap
    conversion_slippage_bps: Decimal = Decimal("30")
    fee_safety_margin: Decimal = Decimal("0.002")       # extra headroom when sizing buys

    # ---- execution
    loop_interval_s: float = 30.0
    order_timeout_s: float = 20.0
    unknown_resolve_window_s: float = 45.0
    max_orders_per_intent: int = 3
    max_requests_per_s: float = 5.0
    order_requests_per_s: float = 2.0
    http_timeout_s: float = 10.0

    # ---- paper/backtest only
    paper_fee_rate: Decimal = Decimal("0.0035")   # ASSUMED; live uses the account's real fee

    # ---- misc
    seed: int = 42

    # ------------------------------------------------------------------ helpers
    @property
    def state_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def kill_file(self) -> Path:
        return self.state_path / "KILL"

    def validate(self) -> "Config":
        def need(cond: bool, msg: str) -> None:
            if not cond:
                raise ConfigError(msg)

        need(self.profile in PROFILES, f"unknown profile {self.profile!r}")
        need(self.final_policy in FINAL_POLICIES, f"final_policy must be one of {FINAL_POLICIES}")
        need(self.initial_tmn > 0, "initial_tmn must be > 0")
        need(0 < self.competition_hours <= 168, "competition_hours out of range")
        need(Decimal("0") < self.max_exposure_fraction <= Decimal("1"), "max_exposure_fraction in (0,1]")
        need(Decimal("0") < self.stop_loss_pct < Decimal("1"), "stop_loss_pct in (0,1)")
        need(Decimal("0") < self.max_drawdown_pct < Decimal("1"), "max_drawdown_pct in (0,1)")
        need(self.max_round_trips >= 1, "max_round_trips >= 1")
        need(self.min_net_edge_bps >= 0, "min_net_edge_bps >= 0")
        need(0.0 <= self.min_confidence <= 1.0, "min_confidence in [0,1]")
        need(self.loop_interval_s >= 1, "loop_interval_s >= 1")
        need(self.order_timeout_s >= 1, "order_timeout_s >= 1")
        need(0 <= self.paper_fee_rate < Decimal("0.05"), "paper_fee_rate in [0,0.05)")
        need(self.max_requests_per_s > 0 and self.order_requests_per_s > 0, "rate limits > 0")
        if self.mode is Mode.LIVE:
            need(bool(self.api_key), "live mode needs WALLEX_API_KEY in the environment/.env")
        return self

    def redacted(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            d[f.name] = ("***set***" if v else "") if f.name == "api_key" else (
                str(v) if isinstance(v, (Decimal, Path)) else (v.value if isinstance(v, Mode) else v))
        return d


# env/.env name -> (field, parser)
def _b(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


_ENV: dict[str, tuple[str, Any]] = {
    "WALLEX_API_KEY": ("api_key", str),
    "TRADING_MODE": ("mode", lambda v: Mode(v.strip().lower())),
    "PROFILE": ("profile", lambda v: v.strip().lower()),
    "INITIAL_TMN": ("initial_tmn", dec),
    "COMPETITION_HOURS": ("competition_hours", float),
    "MAX_EXPOSURE_FRACTION": ("max_exposure_fraction", dec),
    "ENABLE_LIVE": ("enable_live", _b),
    "FINAL_POLICY": ("final_policy", lambda v: v.strip().lower()),
    "DATA_DIR": ("data_dir", str),
    "API_BASE": ("api_base", str),
    "WALLEX_USE_ENV_PROXY": ("use_env_proxy", _b),
    "PAPER_FEE_RATE": ("paper_fee_rate", dec),
    "STOP_LOSS_PCT": ("stop_loss_pct", dec),
    "MAX_DRAWDOWN_PCT": ("max_drawdown_pct", dec),
    "MAX_ROUND_TRIPS": ("max_round_trips", int),
    "MIN_NET_EDGE_BPS": ("min_net_edge_bps", dec),
    "MAX_SPREAD_BPS": ("max_spread_bps", dec),
    "MAX_SLIPPAGE_BPS": ("max_slippage_bps", dec),
    "MIN_CONFIDENCE": ("min_confidence", float),
    "LOOP_INTERVAL_S": ("loop_interval_s", float),
    "ORDER_TIMEOUT_S": ("order_timeout_s", float),
    "SEED": ("seed", int),
}


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def load_config(env: Mapping[str, str] | None = None, dotenv_path: str | Path | None = ".env",
                overrides: Mapping[str, Any] | None = None) -> Config:
    """Precedence: overrides > real environment > .env file > profile > defaults."""
    merged: dict[str, str] = {}
    if dotenv_path and Path(dotenv_path).is_file():
        merged.update(parse_dotenv(Path(dotenv_path).read_text(encoding="utf-8")))
    merged.update(dict(os.environ if env is None else env))

    parsed: dict[str, Any] = {}
    for name, (fld, parser) in _ENV.items():
        raw = merged.get(name)
        if raw is None or (raw.strip() == "" and fld != "api_key"):
            continue
        try:
            parsed[fld] = parser(raw)
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"bad value for {name}: {exc}") from exc

    profile = parsed.get("profile", "conservative")
    if profile not in PROFILES:
        raise ConfigError(f"unknown profile {profile!r}")
    base = replace(Config(), **PROFILES[profile])       # profile defaults ...
    cfg = replace(base, **parsed)                       # ... then explicit env values win
    if overrides:
        cfg = replace(cfg, **dict(overrides))
    return cfg.validate()
