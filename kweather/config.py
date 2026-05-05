"""Config loading. YAML files under config/, env vars, paths."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path))).resolve()


@dataclass
class Station:
    code: str
    kalshi_city: str
    name: str
    lat: float
    lon: float
    timezone: str
    peak_local_hour: int
    sea_breeze: bool
    region: str
    kalshi_high_series: str | None = None
    kalshi_low_series: str | None = None


@dataclass
class Risk:
    notional_cap_usd: float
    position_cap_contracts: int
    tight_threshold_cents: int
    stale_threshold_cents: int
    half_spread_cents: int
    half_spread_sigma_coef: float
    fee_buffer_cents: int
    kelly_fraction: float
    cancel_debounce_ms: int
    per_market_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    correlation_caps: dict[str, float] = field(default_factory=dict)
    daily_loss_stop_usd: float = 200.0
    fees_trading: float = 0.01
    fees_settlement: float = 0.10
    maker_rebate: float = 0.0


@dataclass
class Settings:
    mode: str
    kill_switch: bool
    kalshi_key_id: str
    kalshi_private_key_path: Path
    kalshi_base_url: str
    kalshi_ws_url: str
    db_path: Path
    cache_dir: Path
    http_host: str
    http_port: int
    stations: list[Station]
    correlation_buckets: dict[str, list[str]]
    risk: Risk
    schedule: dict[str, Any]


def _load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


def load_settings() -> Settings:
    load_dotenv()
    stations_doc = _load_yaml("stations.yaml")
    risk_doc = _load_yaml("risk.yaml")
    schedule_doc = _load_yaml("schedule.yaml")

    stations = [Station(**s) for s in stations_doc["stations"]]
    correlation_buckets = stations_doc.get("correlation_buckets", {})

    d = risk_doc["defaults"]
    fees = risk_doc.get("fees", {})
    risk = Risk(
        notional_cap_usd=d["notional_cap_usd"],
        position_cap_contracts=d["position_cap_contracts"],
        tight_threshold_cents=d["tight_threshold_cents"],
        stale_threshold_cents=d["stale_threshold_cents"],
        half_spread_cents=d["half_spread_cents"],
        half_spread_sigma_coef=d["half_spread_sigma_coef"],
        fee_buffer_cents=d["fee_buffer_cents"],
        kelly_fraction=d["kelly_fraction"],
        cancel_debounce_ms=d["cancel_debounce_ms"],
        per_market_overrides=risk_doc.get("per_market_overrides") or {},
        correlation_caps=risk_doc.get("correlation_caps") or {},
        daily_loss_stop_usd=float(
            os.environ.get("KWEATHER_DAILY_LOSS_STOP_USD", risk_doc["daily_stop"]["loss_usd"])
        ),
        fees_trading=fees.get("trading_pct", 0.01),
        fees_settlement=fees.get("settlement_pct", 0.10),
        maker_rebate=fees.get("maker_rebate_pct", 0.0),
    )

    return Settings(
        mode=os.environ.get("KWEATHER_MODE", "paper"),
        kill_switch=os.environ.get("KWEATHER_KILL_SWITCH", "0") == "1",
        kalshi_key_id=os.environ.get("KALSHI_KEY_ID", ""),
        kalshi_private_key_path=_expand(
            os.environ.get("KALSHI_PRIVATE_KEY_PATH", "~/.kweather/kalshi.pem")
        ),
        kalshi_base_url=os.environ.get(
            "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
        ),
        kalshi_ws_url=os.environ.get(
            "KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"
        ),
        db_path=_expand(os.environ.get("KWEATHER_DB_PATH", "~/.kweather/kweather.db")),
        cache_dir=_expand(os.environ.get("KWEATHER_CACHE_DIR", "~/.kweather/cache")),
        http_host=os.environ.get("KWEATHER_HTTP_HOST", "127.0.0.1"),
        http_port=int(os.environ.get("KWEATHER_HTTP_PORT", "8000")),
        stations=stations,
        correlation_buckets=correlation_buckets,
        risk=risk,
        schedule=schedule_doc,
    )
