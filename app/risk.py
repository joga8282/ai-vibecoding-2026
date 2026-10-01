from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

from app.models import Side


class TradingControl(StrEnum):
    ACTIVE = 'ACTIVE'
    BUY_PAUSED = 'BUY_PAUSED'
    ALL_NEW_ORDERS_PAUSED = 'ALL_NEW_ORDERS_PAUSED'
    EMERGENCY_EXIT = 'EMERGENCY_EXIT'


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    rule: str
    detail: str


class LiveRiskManager:
    """Fail-closed validation applied before any future LIVE order transport."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.control = TradingControl.ACTIVE
        self.armed = False
        self.reconciled = False
        self.daily_loss = Decimal(0)
        self.last_exit: dict[str, datetime] = {}

    def validate(self, *, symbol: str, side: Side, quantity: Decimal, price: Decimal,
                 total_exposure: Decimal, position_count: int, quote_at: datetime,
                 has_opposite_open_order: bool = False, warning: bool = False) -> RiskDecision:
        if not self.armed or not self.settings.live_trading_enabled:
            return RiskDecision(False, 'live-lock', '실거래 잠금이 해제되지 않았습니다.')
        if not self.reconciled:
            return RiskDecision(False, 'reconciliation', '실계좌와 DB가 동기화되지 않았습니다.')
        if self.control in {TradingControl.ALL_NEW_ORDERS_PAUSED, TradingControl.EMERGENCY_EXIT}:
            return RiskDecision(False, 'trading-control', self.control.value)
        if side is Side.BUY and self.control is TradingControl.BUY_PAUSED:
            return RiskDecision(False, 'buy-paused', self.control.value)
        if datetime.now(timezone.utc) - quote_at.astimezone(timezone.utc) > timedelta(seconds=10):
            return RiskDecision(False, 'stale-quote', '시세가 10초보다 오래되었습니다.')
        if warning:
            return RiskDecision(False, 'stock-warning', '투자경고·위험 또는 거래 제한 종목입니다.')
        if has_opposite_open_order:
            return RiskDecision(False, 'opposite-open-order', '반대 방향 미체결 주문이 있습니다.')
        if self.settings.live_allowed_symbols and symbol not in self.settings.live_allowed_symbols:
            return RiskDecision(False, 'allowlist', '실거래 허용 종목이 아닙니다.')
        amount = quantity * price
        if amount > self.settings.live_max_order_amount_krw:
            return RiskDecision(False, 'max-order', f'{amount} > {self.settings.live_max_order_amount_krw}')
        if side is Side.BUY and total_exposure + amount > self.settings.live_max_total_exposure_krw:
            return RiskDecision(False, 'max-exposure', '전체 투자금 한도를 초과합니다.')
        if side is Side.BUY and position_count >= 5:
            return RiskDecision(False, 'max-positions', '최대 보유 종목은 5개입니다.')
        if self.daily_loss <= -self.settings.live_max_daily_loss_krw:
            return RiskDecision(False, 'daily-loss', '일일 손실 한도에 도달했습니다.')
        return RiskDecision(True, 'allowed', '모든 LIVE 리스크 검사를 통과했습니다.')
