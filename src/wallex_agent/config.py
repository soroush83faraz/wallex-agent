"""Configuration. Conservative defaults; nothing here can enable live trading by itself."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .exchange.net import parse_proxy_url
from .types import Mode, dec

FINAL_POLICIES = ("report_only", "flatten_to_usdt", "flatten_to_tmn")

# Upper bounds checked by ``Config.validate`` (values beyond them would effectively switch a gate off).
MAX_SLIPPAGE_BPS = Decimal("200")
MAX_SPREAD_BPS = Decimal("500")
MAX_STOP_LOSS_PCT = Decimal("0.5")
MAX_NET_EDGE_BPS = Decimal("10000")
MAX_ROUND_TRIPS = 100
MAX_BOOK_AGE_S = 120.0
MAX_LOOP_S = 3600.0

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


def absolute_dir(path: str | Path) -> str:
    """``path`` as an absolute, normalised path; a relative path is resolved against the CURRENT
    working directory (so the result is fixed at load time and printed in full everywhere)."""
    return os.path.abspath(os.path.expanduser(str(path)))


@dataclass(frozen=True)
class Config:
    # ---- run identity
    mode: Mode = Mode.PAPER
    profile: str = "conservative"
    data_dir: str = "state"
    # False only when ``load_config`` fell back to the built-in default ``state`` (relative to the
    # working directory): live trading refuses that, because a different working directory would
    # silently mean a different ledger, KILL file and evidence.
    data_dir_explicit: bool = True
    api_base: str = "https://api.wallex.ir"
    api_key: str = field(default="", repr=False)   # secret: never logged / reported
    enable_live: bool = False
    # Network route (both change the source IP the exchange sees!). ``proxy_url`` = WALLEX_PROXY_URL,
    # an explicit http://[user:pass@]host:port CONNECT proxy that wins over the environment (may hold
    # credentials: hidden from repr, masked in ``redacted()``). ``use_env_proxy`` = also honour
    # HTTPS_PROXY / HTTP_PROXY / NO_PROXY of the process environment. The OS proxy settings never apply.
    use_env_proxy: bool = False
    proxy_url: str = field(default="", repr=False)

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
        need(Decimal("0") < self.stop_loss_pct <= MAX_STOP_LOSS_PCT, f"stop_loss_pct in (0,{MAX_STOP_LOSS_PCT}]")
        need(Decimal("0") < self.max_drawdown_pct < Decimal("1"), "max_drawdown_pct in (0,1)")
        need(1 <= self.max_round_trips <= MAX_ROUND_TRIPS, f"max_round_trips in [1,{MAX_ROUND_TRIPS}]")
        need(Decimal("0") <= self.min_net_edge_bps <= MAX_NET_EDGE_BPS, f"min_net_edge_bps in [0,{MAX_NET_EDGE_BPS}]")
        need(0.0 <= self.min_confidence <= 1.0, "min_confidence in [0,1]")
        # Hard upper bounds on the risk knobs an .env typo could otherwise blow open (L4): these caps
        # are what the slippage/spread gates enforce, so an absurd value would disable the gate.
        need(Decimal("0") <= self.max_slippage_bps <= MAX_SLIPPAGE_BPS,
             f"max_slippage_bps in [0,{MAX_SLIPPAGE_BPS}]")
        need(Decimal("0") <= self.conversion_slippage_bps <= MAX_SLIPPAGE_BPS,
             f"conversion_slippage_bps in [0,{MAX_SLIPPAGE_BPS}]")
        need(Decimal("0") < self.max_spread_bps <= MAX_SPREAD_BPS, f"max_spread_bps in (0,{MAX_SPREAD_BPS}]")
        need(0 < self.max_book_age_s <= MAX_BOOK_AGE_S, f"max_book_age_s in (0,{MAX_BOOK_AGE_S:g}]")
        need(self.min_book_depth_quote_usdt >= 0, "min_book_depth_quote_usdt >= 0")
        need(Decimal("0") <= self.fee_safety_margin < Decimal("0.05"), "fee_safety_margin in [0,0.05)")
        need(self.cooldown_after_loss_s >= 0 and self.min_trade_interval_s >= 0, "cooldowns >= 0")
        need(1 <= self.loop_interval_s <= MAX_LOOP_S, f"loop_interval_s in [1,{MAX_LOOP_S:g}]")
        need(1 <= self.order_timeout_s <= MAX_LOOP_S, f"order_timeout_s in [1,{MAX_LOOP_S:g}]")
        need(5 <= self.unknown_resolve_window_s <= 600, "unknown_resolve_window_s in [5,600]")
        need(1 <= self.max_orders_per_intent <= 5, "max_orders_per_intent in [1,5]")
        need(0 < self.http_timeout_s <= 60, "http_timeout_s in (0,60]")
        need(0 <= self.paper_fee_rate < Decimal("0.05"), "paper_fee_rate in [0,0.05)")
        need(0 < self.max_requests_per_s <= 20 and 0 < self.order_requests_per_s <= 5,
             "rate limits: max_requests_per_s in (0,20], order_requests_per_s in (0,5]")
        need(bool(str(self.data_dir).strip()), "data_dir must not be empty")
        if self.proxy_url:
            parse_proxy_url(self.proxy_url, source="WALLEX_PROXY_URL")   # ConfigError, never echoes it
        if self.mode is Mode.LIVE:
            need(bool(self.api_key), "live mode needs WALLEX_API_KEY in the environment/.env")
        return self

    @property
    def proxy_display(self) -> str:
        """``WALLEX_PROXY_URL`` with any credentials masked (``http://***@host:port``), or ''."""
        if not self.proxy_url:
            return ""
        try:
            return parse_proxy_url(self.proxy_url, source="WALLEX_PROXY_URL").display
        except ConfigError:
            return "<invalid>"

    def redacted(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "api_key":
                d[f.name] = "***set***" if v else ""
            elif f.name == "proxy_url":
                d[f.name] = self.proxy_display
            else:
                d[f.name] = str(v) if isinstance(v, (Decimal, Path)) else (v.value if isinstance(v, Mode) else v)
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
    "WALLEX_PROXY_URL": ("proxy_url", str),
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
    """Precedence: overrides > real environment > .env file > profile > defaults.

    ``data_dir`` always comes back ABSOLUTE (a relative value is resolved against the current
    working directory now, once); ``data_dir_explicit`` records whether it was given at all
    (``DATA_DIR`` in the environment/.env or an override such as ``--data-dir``)."""
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
    explicit = "data_dir" in parsed or bool(overrides and str(overrides.get("data_dir") or "").strip())
    cfg = replace(cfg, data_dir=absolute_dir(cfg.data_dir), data_dir_explicit=explicit)
    return cfg.validate()
