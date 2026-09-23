from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load a small, dependency-free subset of dotenv without overriding OS env."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Paper Trader"
    version: str = "0.2.0"
    mode: str = "paper"
    database_path: Path = Path("data/paper_trader.db")
    initial_cash_krw: Decimal = Decimal("10000000")
    initial_cash_usd: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.00015")
    slippage_bps: Decimal = Decimal("5")
    max_order_amount_krw: Decimal = Decimal("1000000")
    max_order_amount_usd: Decimal = Decimal("1000")
    recommended_trade_ratio: Decimal = Decimal("0.50")
    engine_interval_seconds: float = 2.0
    auto_start: bool = False
    toss_market_data_enabled: bool = True
    toss_client_id: str | None = None
    toss_client_secret: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mode = os.getenv("TRADER_MODE", "paper").strip().lower()
        if mode != "paper":
            raise RuntimeError("현재 버전은 paper 모드만 지원합니다.")
        recommended_trade_ratio = Decimal(
            os.getenv("RECOMMENDED_TRADE_RATIO", "0.50")
        )
        if not Decimal("0") <= recommended_trade_ratio <= Decimal("1"):
            raise RuntimeError("RECOMMENDED_TRADE_RATIO는 0부터 1 사이여야 합니다.")
        return cls(
            mode=mode,
            database_path=Path(os.getenv("DATABASE_PATH", "data/paper_trader.db")),
            initial_cash_krw=Decimal(os.getenv("INITIAL_CASH_KRW", "10000000")),
            initial_cash_usd=Decimal(os.getenv("INITIAL_CASH_USD", "10000")),
            fee_rate=Decimal(os.getenv("FEE_RATE", "0.00015")),
            slippage_bps=Decimal(os.getenv("SLIPPAGE_BPS", "5")),
            max_order_amount_krw=Decimal(os.getenv("MAX_ORDER_AMOUNT_KRW", "1000000")),
            max_order_amount_usd=Decimal(os.getenv("MAX_ORDER_AMOUNT_USD", "1000")),
            recommended_trade_ratio=recommended_trade_ratio,
            engine_interval_seconds=float(os.getenv("ENGINE_INTERVAL_SECONDS", "2")),
            auto_start=_as_bool(os.getenv("AUTO_START")),
            toss_market_data_enabled=_as_bool(
                os.getenv("TOSS_MARKET_DATA_ENABLED"), default=True
            ),
            toss_client_id=os.getenv("TOSS_CLIENT_ID") or None,
            toss_client_secret=os.getenv("TOSS_CLIENT_SECRET") or None,
        )
