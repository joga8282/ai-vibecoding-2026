from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Paper Trader"
    version: str = "0.1.0"
    mode: str = "paper"
    database_path: Path = Path("data/paper_trader.db")
    initial_cash_krw: Decimal = Decimal("10000000")
    initial_cash_usd: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.00015")
    slippage_bps: Decimal = Decimal("5")
    max_order_amount_krw: Decimal = Decimal("1000000")
    max_order_amount_usd: Decimal = Decimal("1000")
    engine_interval_seconds: float = 2.0
    auto_start: bool = False
    toss_client_id: str | None = None
    toss_client_secret: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("TRADER_MODE", "paper").strip().lower()
        if mode != "paper":
            raise RuntimeError("v0.1은 paper 모드만 지원합니다.")
        return cls(
            mode=mode,
            database_path=Path(os.getenv("DATABASE_PATH", "data/paper_trader.db")),
            initial_cash_krw=Decimal(os.getenv("INITIAL_CASH_KRW", "10000000")),
            initial_cash_usd=Decimal(os.getenv("INITIAL_CASH_USD", "10000")),
            fee_rate=Decimal(os.getenv("FEE_RATE", "0.00015")),
            slippage_bps=Decimal(os.getenv("SLIPPAGE_BPS", "5")),
            max_order_amount_krw=Decimal(os.getenv("MAX_ORDER_AMOUNT_KRW", "1000000")),
            max_order_amount_usd=Decimal(os.getenv("MAX_ORDER_AMOUNT_USD", "1000")),
            engine_interval_seconds=float(os.getenv("ENGINE_INTERVAL_SECONDS", "2")),
            auto_start=_as_bool(os.getenv("AUTO_START")),
            toss_client_id=os.getenv("TOSS_CLIENT_ID") or None,
            toss_client_secret=os.getenv("TOSS_CLIENT_SECRET") or None,
        )
