from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class LiveOrderStatus(StrEnum):
    CREATED = 'CREATED'
    SUBMITTING = 'SUBMITTING'
    ACCEPTED = 'ACCEPTED'
    PARTIALLY_FILLED = 'PARTIALLY_FILLED'
    FILLED = 'FILLED'
    CANCEL_REQUESTED = 'CANCEL_REQUESTED'
    CANCELED = 'CANCELED'
    UNKNOWN = 'UNKNOWN'
    REJECTED = 'REJECTED'
    FAILED = 'FAILED'


ALLOWED_TRANSITIONS = {
    LiveOrderStatus.CREATED: {LiveOrderStatus.SUBMITTING, LiveOrderStatus.REJECTED},
    LiveOrderStatus.SUBMITTING: {LiveOrderStatus.ACCEPTED, LiveOrderStatus.UNKNOWN,
                                 LiveOrderStatus.REJECTED, LiveOrderStatus.FAILED},
    LiveOrderStatus.UNKNOWN: {LiveOrderStatus.ACCEPTED, LiveOrderStatus.PARTIALLY_FILLED,
                              LiveOrderStatus.FILLED, LiveOrderStatus.REJECTED, LiveOrderStatus.FAILED},
    LiveOrderStatus.ACCEPTED: {LiveOrderStatus.PARTIALLY_FILLED, LiveOrderStatus.FILLED,
                               LiveOrderStatus.CANCEL_REQUESTED, LiveOrderStatus.REJECTED},
    LiveOrderStatus.PARTIALLY_FILLED: {LiveOrderStatus.PARTIALLY_FILLED, LiveOrderStatus.FILLED,
                                      LiveOrderStatus.CANCEL_REQUESTED},
    LiveOrderStatus.CANCEL_REQUESTED: {LiveOrderStatus.CANCELED, LiveOrderStatus.PARTIALLY_FILLED,
                                       LiveOrderStatus.FILLED, LiveOrderStatus.UNKNOWN},
}


@dataclass
class LiveOrderState:
    status: LiveOrderStatus
    requested_quantity: Decimal
    filled_quantity: Decimal = Decimal(0)

    def transition(self, status: LiveOrderStatus, filled_quantity: Decimal | None = None) -> None:
        if status not in ALLOWED_TRANSITIONS.get(self.status, set()):
            raise ValueError(f'허용되지 않은 주문 상태 변경: {self.status} -> {status}')
        if filled_quantity is not None:
            if filled_quantity < self.filled_quantity or filled_quantity > self.requested_quantity:
                raise ValueError('체결 수량은 감소하거나 주문 수량을 초과할 수 없습니다.')
            self.filled_quantity = filled_quantity
        if status is LiveOrderStatus.FILLED and self.filled_quantity != self.requested_quantity:
            raise ValueError('FILLED 상태에는 전량 체결 수량이 필요합니다.')
        self.status = status
