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
        self.recommended_symbols: frozenset[str] = frozenset()
        self.recommendations_at: datetime | None = None

    @property
    def daily_loss_limit_reached(self) -> bool:
        return bool(self.settings.live_daily_loss_limit_enabled
                    and self.daily_loss >= self.settings.live_max_daily_loss_krw)

    def set_recommended_symbols(self, symbols) -> None:
        self.recommended_symbols = frozenset(
            str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
        )
        self.recommendations_at = datetime.now(timezone.utc)

    def clear_recommended_symbols(self) -> None:
        self.recommended_symbols = frozenset()
        self.recommendations_at = None

    def validate(self, *, symbol: str, side: Side, quantity: Decimal, price: Decimal,
                 total_exposure: Decimal, position_count: int, quote_at: datetime,
                 current_equity: Decimal | None = None,
                 has_opposite_open_order: bool = False, warning: bool = False,
                 verified_position_addition: bool = False) -> RiskDecision:
        # Reject malformed inputs before comparing limits. This method sits at
        # the future LIVE transport boundary, so invalid data must fail closed.
        numeric_values = (quantity, price, total_exposure) + ((current_equity,) if current_equity is not None else ())
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in numeric_values):
            return RiskDecision(False, 'invalid-order', 'Invalid numeric order data.')
        if quantity <= 0 or price <= 0 or total_exposure < 0 or position_count < 0:
            return RiskDecision(False, 'invalid-order', 'Quantity and price must be positive; exposure and position count cannot be negative.')
        if side not in (Side.BUY, Side.SELL) or not isinstance(symbol, str) or not symbol.strip():
            return RiskDecision(False, 'invalid-order', 'Order side or symbol is invalid.')
        symbol = symbol.strip().upper()
        if verified_position_addition and (side is not Side.BUY or not self.settings.swing_averaging_enabled):
            return RiskDecision(False, 'averaging-disabled', '추가 매수 정책이 활성화되지 않았습니다.')
        if not isinstance(quote_at, datetime) or quote_at.tzinfo is None or quote_at.utcoffset() is None:
            return RiskDecision(False, 'invalid-quote-time', 'Quote timestamp must include a timezone.')
        if not self.armed or not self.settings.live_trading_enabled:
            return RiskDecision(False, 'live-lock', '실거래 잠금이 해제되지 않았습니다.')
        if not self.reconciled:
            return RiskDecision(False, 'reconciliation', '실계좌와 DB가 동기화되지 않았습니다.')
        if self.control in {TradingControl.ALL_NEW_ORDERS_PAUSED, TradingControl.EMERGENCY_EXIT}:
            return RiskDecision(False, 'trading-control', self.control.value)
        if side is Side.BUY and self.control is TradingControl.BUY_PAUSED:
            return RiskDecision(False, 'buy-paused', self.control.value)
        quote_age = datetime.now(timezone.utc) - quote_at.astimezone(timezone.utc)
        if quote_age > timedelta(seconds=10) or quote_age < timedelta(seconds=-2):
            return RiskDecision(False, 'stale-quote', '시세가 10초보다 오래되었습니다.')
        if warning:
            return RiskDecision(False, 'stock-warning', '투자경고·위험 또는 거래 제한 종목입니다.')
        if has_opposite_open_order:
            return RiskDecision(False, 'opposite-open-order', '반대 방향 미체결 주문이 있습니다.')
        if self.settings.live_symbol_policy == 'allowlist':
            if not self.settings.live_allowed_symbols:
                return RiskDecision(False, 'allowlist-not-configured', 'LIVE allowed-symbol list is empty.')
            if symbol not in self.settings.live_allowed_symbols:
                return RiskDecision(False, 'allowlist', '실거래 허용 종목이 아닙니다.')
        elif self.settings.live_symbol_policy != 'recommended':
            return RiskDecision(False, 'symbol-policy', 'Invalid LIVE symbol policy.')
        if side is Side.BUY and not verified_position_addition:
            rec_age = (datetime.now(timezone.utc) - self.recommendations_at
                       if self.recommendations_at is not None else None)
            if (rec_age is None or rec_age > timedelta(minutes=5)
                    or symbol not in self.recommended_symbols):
                return RiskDecision(False, 'recommendation', 'A fresh qualified recommendation is required for LIVE buys.')
        amount = quantity * price
        per_order_mode = self.settings.live_auto_allocation_mode == 'per_order'
        if side is Side.BUY and per_order_mode:
            buy_ratio, fee = self.settings.live_max_buy_ratio, self.settings.fee_rate
            if (not isinstance(buy_ratio, Decimal) or not buy_ratio.is_finite() or not 0 < buy_ratio <= 1
                    or not isinstance(fee, Decimal) or not fee.is_finite() or not 0 <= fee < 1):
                return RiskDecision(False, 'invalid-limits', 'Invalid LIVE per-buy limits.')
        buy_cost = amount * (1 + self.settings.fee_rate) if side is Side.BUY and per_order_mode else amount
        order_cap = self.settings.live_max_order_amount_krw
        exposure_cap = self.settings.live_max_total_exposure_krw
        ratio = self.settings.live_max_total_exposure_ratio
        cash_ratio = self.settings.live_min_cash_ratio
        if (any(not isinstance(value, Decimal) or not value.is_finite() or value < 0
                for value in (order_cap, exposure_cap, ratio, cash_ratio))
                or not 0 < ratio <= 1 or not 0 <= cash_ratio < 1):
            return RiskDecision(False, 'invalid-limits', 'Invalid LIVE investment limits.')
        if order_cap > 0 and amount > order_cap:
            return RiskDecision(False, 'max-order', f'{amount} > {self.settings.live_max_order_amount_krw}')
        if side is Side.BUY and exposure_cap > 0 and total_exposure + buy_cost > exposure_cap:
            return RiskDecision(False, 'max-exposure', '전체 투자금 한도를 초과합니다.')
        if side is Side.BUY:
            if current_equity is None or current_equity <= 0:
                return RiskDecision(False, 'equity-unavailable', 'Current account equity is required for the exposure ratio check.')
            if per_order_mode:
                if buy_cost > current_equity * buy_ratio:
                    return RiskDecision(False, 'max-buy-ratio', '수수료 포함 1회 매수 금액이 총자산 비율 한도를 초과합니다.')
            projected = total_exposure + buy_cost
            if cash_ratio > 0 and projected > current_equity * (1 - cash_ratio):
                return RiskDecision(False, 'cash-reserve', '주문 후 총자산 대비 최소 현금 비율을 충족하지 않습니다.')
            limit = current_equity * ratio
            if projected > limit or (ratio < 1 and projected == limit):
                return RiskDecision(False, 'max-equity-exposure', 'Projected LIVE exposure exceeds the configured account equity budget.')
        if side is Side.BUY and (position_count > 5 or (position_count == 5 and not verified_position_addition)):
            return RiskDecision(False, 'max-positions', '최대 보유 종목은 5개입니다.')
        if side is Side.BUY and self.daily_loss_limit_reached:
            return RiskDecision(False, 'daily-loss', '일일 손실 한도에 도달했습니다.')
        return RiskDecision(True, 'allowed', '모든 LIVE 리스크 검사를 통과했습니다.')
