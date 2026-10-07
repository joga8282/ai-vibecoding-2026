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
    database_path: Path = Path("data/auto_trader.db")
    initial_cash_krw: Decimal = Decimal("10000000")
    initial_cash_usd: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.00015")
    slippage_bps: Decimal = Decimal("5")
    swing_min_net_profit_percent: Decimal = Decimal("3")
    live_sell_tax_rate: Decimal = Decimal("0.002")
    max_order_amount_krw: Decimal = Decimal("1000000")
    max_order_amount_usd: Decimal = Decimal("1000")
    recommended_trade_ratio: Decimal = Decimal("0.10")
    manual_trade_ratio: Decimal = Decimal("0.15")
    engine_interval_seconds: float = 2.0
    auto_start: bool = False
    toss_market_data_enabled: bool = True
    toss_client_id: str | None = None
    toss_client_secret: str | None = None
    toss_account_seq: str | None = None
    live_trading_enabled: bool = False
    live_require_manual_arm: bool = True
    live_max_order_amount_krw: Decimal = Decimal("100000")
    live_max_total_exposure_krw: Decimal = Decimal("500000")
    live_max_total_exposure_ratio: Decimal = Decimal("0.15")
    live_auto_max_total_exposure_ratio: Decimal = Decimal("0.15")
    live_max_daily_loss_krw: Decimal = Decimal("50000")
    live_max_position_loss_percent: Decimal = Decimal("3")
    live_allowed_symbols: tuple[str, ...] = ()
    live_symbol_policy: str = 'allowlist'
    api_access_token: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mode = os.getenv("TRADER_MODE", "paper").strip().lower()
        if mode not in {"paper", "live"}:
            raise RuntimeError("TRADER_MODE는 paper 또는 live여야 합니다.")
        try:
            min_profit = Decimal(os.getenv('SWING_MIN_NET_PROFIT_PERCENT', '3'))
            sell_tax = Decimal(os.getenv('LIVE_SELL_TAX_RATE', '0.002'))
        except ArithmeticError as exc:
            raise RuntimeError('Swing profit and sell-tax settings must be numbers.') from exc
        if not min_profit.is_finite() or not Decimal(0) <= min_profit <= Decimal(100):
            raise RuntimeError('SWING_MIN_NET_PROFIT_PERCENT must be between 0 and 100.')
        if not sell_tax.is_finite() or not Decimal(0) <= sell_tax < Decimal(1):
            raise RuntimeError('LIVE_SELL_TAX_RATE must be between 0 and 1 (exclusive).')
        recommended_trade_ratio = Decimal(
            os.getenv("RECOMMENDED_TRADE_RATIO", "0.10")
        )
        if not recommended_trade_ratio.is_finite() or not Decimal("0") <= recommended_trade_ratio <= Decimal("1"):
            raise RuntimeError("RECOMMENDED_TRADE_RATIO는 0부터 1 사이여야 합니다.")
        manual_trade_ratio = Decimal(os.getenv("MANUAL_TRADE_RATIO", "0.15"))
        auto_max_ratio = Decimal(os.getenv("LIVE_AUTO_MAX_TOTAL_EXPOSURE_RATIO", "0.15"))
        for name, value in (("MANUAL_TRADE_RATIO", manual_trade_ratio),
                            ("LIVE_AUTO_MAX_TOTAL_EXPOSURE_RATIO", auto_max_ratio)):
            if not value.is_finite() or not Decimal('.01') <= value <= Decimal(1):
                raise RuntimeError(f"{name} must be between 0.01 and 1.")
        database_path = Path(os.getenv(
            "LIVE_DATABASE_PATH" if mode == "live" else "DATABASE_PATH",
            "data/live_trader.db" if mode == "live" else "data/auto_trader.db",
        ))
        account_seq = os.getenv("TOSS_ACCOUNT_SEQ") or None
        live_enabled = _as_bool(os.getenv("LIVE_TRADING_ENABLED"))
        api_access_token = os.getenv("API_ACCESS_TOKEN") or None
        live_limits = {
            "LIVE_MAX_ORDER_AMOUNT_KRW": Decimal(os.getenv("LIVE_MAX_ORDER_AMOUNT_KRW", "100000")),
            "LIVE_MAX_TOTAL_EXPOSURE_KRW": Decimal(os.getenv("LIVE_MAX_TOTAL_EXPOSURE_KRW", "500000")),
            "LIVE_MAX_TOTAL_EXPOSURE_RATIO": Decimal(os.getenv("LIVE_MAX_TOTAL_EXPOSURE_RATIO", "0.15")),
            "LIVE_MAX_DAILY_LOSS_KRW": Decimal(os.getenv("LIVE_MAX_DAILY_LOSS_KRW", "50000")),
            "LIVE_MAX_POSITION_LOSS_PERCENT": Decimal(os.getenv("LIVE_MAX_POSITION_LOSS_PERCENT", "3")),
        }
        optional_caps = {'LIVE_MAX_ORDER_AMOUNT_KRW', 'LIVE_MAX_TOTAL_EXPOSURE_KRW'}
        if any(not value.is_finite() or value < 0 or (value == 0 and key not in optional_caps)
               for key, value in live_limits.items()):
            raise RuntimeError("LIVE risk limits must be finite positive numbers; optional KRW caps may be zero.")
        if live_limits["LIVE_MAX_POSITION_LOSS_PERCENT"] >= Decimal("100"):
            raise RuntimeError("LIVE_MAX_POSITION_LOSS_PERCENT must be below 100.")
        if live_limits["LIVE_MAX_TOTAL_EXPOSURE_RATIO"] > Decimal("1"):
            raise RuntimeError("LIVE_MAX_TOTAL_EXPOSURE_RATIO cannot exceed 1 (100%).")
        symbol_policy = os.getenv('LIVE_SYMBOL_POLICY', 'allowlist').strip().lower()
        if symbol_policy not in {'allowlist', 'recommended'}:
            raise RuntimeError('LIVE_SYMBOL_POLICY must be allowlist or recommended.')
        if mode == 'live' and not account_seq:
            raise RuntimeError("LIVE 읽기 전용 모드에는 TOSS_ACCOUNT_SEQ가 필요합니다.")
        if mode == 'live' and database_path == Path(os.getenv("DATABASE_PATH", "data/auto_trader.db")):
            raise RuntimeError("LIVE_DATABASE_PATH는 PAPER DB와 달라야 합니다.")
        if live_enabled and mode != 'live':
            raise RuntimeError("LIVE_TRADING_ENABLED=true requires TRADER_MODE=live.")
        if live_enabled and not api_access_token:
            raise RuntimeError("LIVE trading requires API_ACCESS_TOKEN to protect arming and order controls.")
        if live_enabled and symbol_policy == 'allowlist' and not os.getenv('LIVE_ALLOWED_SYMBOLS', '').strip():
            raise RuntimeError("LIVE trading requires an explicit LIVE_ALLOWED_SYMBOLS allowlist.")
        return cls(
            mode=mode,
            database_path=database_path,
            initial_cash_krw=Decimal(os.getenv("INITIAL_CASH_KRW", "10000000")),
            initial_cash_usd=Decimal(os.getenv("INITIAL_CASH_USD", "10000")),
            fee_rate=Decimal(os.getenv("FEE_RATE", "0.00015")),
            slippage_bps=Decimal(os.getenv("SLIPPAGE_BPS", "5")),
            swing_min_net_profit_percent=min_profit,
            live_sell_tax_rate=sell_tax,
            max_order_amount_krw=Decimal(os.getenv("MAX_ORDER_AMOUNT_KRW", "1000000")),
            max_order_amount_usd=Decimal(os.getenv("MAX_ORDER_AMOUNT_USD", "1000")),
            recommended_trade_ratio=recommended_trade_ratio,
            manual_trade_ratio=manual_trade_ratio,
            engine_interval_seconds=float(os.getenv("ENGINE_INTERVAL_SECONDS", "2")),
            auto_start=_as_bool(os.getenv("AUTO_START")),
            toss_market_data_enabled=_as_bool(
                os.getenv("TOSS_MARKET_DATA_ENABLED"), default=True
            ),
            toss_client_id=os.getenv("TOSS_CLIENT_ID") or None,
            toss_client_secret=os.getenv("TOSS_CLIENT_SECRET") or None,
            toss_account_seq=account_seq,
            live_trading_enabled=live_enabled,
            live_require_manual_arm=_as_bool(os.getenv("LIVE_REQUIRE_MANUAL_ARM"), default=True),
            live_max_order_amount_krw=live_limits["LIVE_MAX_ORDER_AMOUNT_KRW"],
            live_max_total_exposure_krw=live_limits["LIVE_MAX_TOTAL_EXPOSURE_KRW"],
            live_max_total_exposure_ratio=live_limits["LIVE_MAX_TOTAL_EXPOSURE_RATIO"],
            live_auto_max_total_exposure_ratio=auto_max_ratio,
            live_max_daily_loss_krw=live_limits["LIVE_MAX_DAILY_LOSS_KRW"],
            live_max_position_loss_percent=live_limits["LIVE_MAX_POSITION_LOSS_PERCENT"],
            live_allowed_symbols=tuple(filter(None, (s.strip().upper() for s in os.getenv("LIVE_ALLOWED_SYMBOLS", "").split(',')))),
            live_symbol_policy=symbol_policy,
            api_access_token=api_access_token,
        )
