from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Currency(StrEnum):
    KRW = "KRW"
    USD = "USD"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class Quote:
    symbol: str
    price: Decimal
    currency: Currency
    timestamp: datetime
    source: str = "manual"


@dataclass(slots=True)
class Candle:
    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: Currency


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: Decimal
    average_price: Decimal
    currency: Currency


@dataclass(slots=True)
class Order:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    requested_price: Decimal
    filled_price: Decimal | None
    currency: Currency
    status: OrderStatus
    fee: Decimal
    reason: str | None
    created_at: datetime

    @classmethod
    def new(
        cls,
        *,
        client_order_id: str,
        symbol: str,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        currency: Currency,
    ) -> "Order":
        return cls(
            order_id=uuid4().hex,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            requested_price=price,
            filled_price=None,
            currency=currency,
            status=OrderStatus.REJECTED,
            fee=Decimal("0"),
            reason=None,
            created_at=utc_now(),
        )


@dataclass(slots=True)
class ThresholdStrategy:
    strategy_id: str
    symbol: str
    currency: Currency
    quantity: Decimal
    buy_below: Decimal
    sell_above: Decimal
    enabled: bool = True


def serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize(item) for item in value]
    return value
