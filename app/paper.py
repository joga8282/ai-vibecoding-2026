from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from app.config import Settings
from app.models import Currency, Order, OrderStatus, Position, Quote, Side, serialize
from app.repository import SnapshotRepository


class PaperBroker:
    snapshot_name = "paper_account"

    def __init__(self, settings: Settings, repository: SnapshotRepository) -> None:
        self.settings = settings
        self.repository = repository
        self.cash: dict[Currency, Decimal] = {
            Currency.KRW: settings.initial_cash_krw,
            Currency.USD: settings.initial_cash_usd,
        }
        self.initial_cash: dict[Currency, Decimal] = dict(self.cash)
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self.realized_profit_loss: dict[Currency, Decimal] = {
            Currency.KRW: Decimal("0"),
            Currency.USD: Decimal("0"),
        }
        self._lock = asyncio.Lock()

    async def restore(self) -> None:
        snapshot = await self.repository.load(self.snapshot_name)
        if not snapshot:
            await self._persist()
            return
        self.cash = {
            Currency(key): Decimal(value) for key, value in snapshot["cash"].items()
        }
        self.initial_cash = {
            Currency(key): Decimal(value)
            for key, value in snapshot.get("initial_cash", snapshot["cash"]).items()
        }
        self.realized_profit_loss = {
            Currency(key): Decimal(value)
            for key, value in snapshot.get("realized_profit_loss", {}).items()
        }
        for currency in Currency:
            self.realized_profit_loss.setdefault(currency, Decimal("0"))
        self.positions = {
            item["symbol"]: Position(
                symbol=item["symbol"],
                quantity=Decimal(item["quantity"]),
                average_price=Decimal(item["average_price"]),
                currency=Currency(item["currency"]),
            )
            for item in snapshot.get("positions", [])
        }
        self.orders = [
            Order(
                order_id=item["order_id"],
                client_order_id=item["client_order_id"],
                symbol=item["symbol"],
                side=Side(item["side"]),
                quantity=Decimal(item["quantity"]),
                requested_price=Decimal(item["requested_price"]),
                filled_price=(
                    Decimal(item["filled_price"])
                    if item.get("filled_price") is not None
                    else None
                ),
                currency=Currency(item["currency"]),
                status=OrderStatus(item["status"]),
                fee=Decimal(item["fee"]),
                reason=item.get("reason"),
                created_at=datetime.fromisoformat(item["created_at"]),
            )
            for item in snapshot.get("orders", [])
        ]

    async def reset(self, cash_krw: Decimal, cash_usd: Decimal) -> None:
        if cash_krw < 0 or cash_usd < 0:
            raise ValueError("초기자금은 0 이상이어야 합니다.")
        async with self._lock:
            self.cash = {Currency.KRW: cash_krw, Currency.USD: cash_usd}
            self.initial_cash = dict(self.cash)
            self.positions.clear()
            self.orders.clear()
            self.realized_profit_loss = {
                Currency.KRW: Decimal("0"),
                Currency.USD: Decimal("0"),
            }
            await self._persist()

    def _max_order_amount(self, currency: Currency) -> Decimal:
        if currency is Currency.KRW:
            return self.settings.max_order_amount_krw
        return self.settings.max_order_amount_usd

    async def place_market_order(
        self,
        *,
        client_order_id: str,
        quote: Quote,
        side: Side,
        quantity: Decimal,
    ) -> Order:
        if quantity <= 0:
            raise ValueError("주문 수량은 0보다 커야 합니다.")
        async with self._lock:
            existing = next(
                (order for order in self.orders if order.client_order_id == client_order_id),
                None,
            )
            if existing:
                return existing

            order = Order.new(
                client_order_id=client_order_id,
                symbol=quote.symbol,
                side=side,
                quantity=quantity,
                price=quote.price,
                currency=quote.currency,
            )
            slippage = self.settings.slippage_bps / Decimal("10000")
            multiplier = Decimal("1") + slippage if side is Side.BUY else Decimal("1") - slippage
            fill_price = quote.price * multiplier
            gross = fill_price * quantity
            fee = gross * self.settings.fee_rate

            if side is Side.BUY and gross > self._max_order_amount(quote.currency):
                order.reason = "max-order-amount-exceeded"
            elif side is Side.BUY:
                total = gross + fee
                if total > self.cash[quote.currency]:
                    order.reason = "insufficient-cash"
                else:
                    self.cash[quote.currency] -= total
                    current = self.positions.get(quote.symbol)
                    if current:
                        new_quantity = current.quantity + quantity
                        current.average_price = (
                            current.average_price * current.quantity + gross + fee
                        ) / new_quantity
                        current.quantity = new_quantity
                    else:
                        self.positions[quote.symbol] = Position(
                            symbol=quote.symbol,
                            quantity=quantity,
                            average_price=(gross + fee) / quantity,
                            currency=quote.currency,
                        )
                    order.status = OrderStatus.FILLED
            else:
                current = self.positions.get(quote.symbol)
                if not current or current.quantity < quantity:
                    order.reason = "insufficient-position"
                else:
                    self.cash[quote.currency] += gross - fee
                    self.realized_profit_loss[quote.currency] += (
                        fill_price - current.average_price
                    ) * quantity - fee
                    current.quantity -= quantity
                    if current.quantity == 0:
                        del self.positions[quote.symbol]
                    order.status = OrderStatus.FILLED

            if order.status is OrderStatus.FILLED:
                order.filled_price = fill_price
                order.fee = fee
            self.orders.append(order)
            await self._persist()
            return order

    def account(self, quotes: dict[str, Quote]) -> dict:
        position_items = []
        equity = dict(self.cash)
        unrealized = {Currency.KRW: Decimal("0"), Currency.USD: Decimal("0")}
        for position in self.positions.values():
            quote = quotes.get(position.symbol)
            market_price = quote.price if quote else position.average_price
            market_value = market_price * position.quantity
            pnl = (market_price - position.average_price) * position.quantity
            equity[position.currency] += market_value
            unrealized[position.currency] += pnl
            position_items.append(
                {
                    **serialize(position),
                    "market_price": str(market_price),
                    "market_value": str(market_value),
                    "unrealized_profit_loss": str(pnl),
                }
            )
        return {
            "mode": "paper",
            "cash": serialize(self.cash),
            "positions": position_items,
            "realized_profit_loss": serialize(self.realized_profit_loss),
            "unrealized_profit_loss": serialize(unrealized),
            "total_equity": serialize(equity),
        }

    def performance(self, quotes: dict[str, Quote]) -> dict:
        account = self.account(quotes)
        total_equity = {
            Currency(key): Decimal(value)
            for key, value in account["total_equity"].items()
        }
        by_currency = {}
        for currency in Currency:
            initial = self.initial_cash[currency]
            profit_loss = total_equity[currency] - initial
            return_rate = (
                profit_loss / initial * Decimal("100") if initial else Decimal("0")
            )
            by_currency[currency.value] = {
                "initial_equity": str(initial),
                "current_equity": str(total_equity[currency]),
                "profit_loss": str(profit_loss),
                "return_rate_percent": str(return_rate),
                "realized_profit_loss": str(self.realized_profit_loss[currency]),
                "unrealized_profit_loss": account["unrealized_profit_loss"][currency],
            }
        filled = [order for order in self.orders if order.status is OrderStatus.FILLED]
        rejected = [order for order in self.orders if order.status is OrderStatus.REJECTED]
        total_fees = {Currency.KRW: Decimal("0"), Currency.USD: Decimal("0")}
        for order in filled:
            total_fees[order.currency] += order.fee
        return {
            "by_currency": by_currency,
            "orders": {
                "total": len(self.orders),
                "filled": len(filled),
                "rejected": len(rejected),
            },
            "total_fees": serialize(total_fees),
        }

    async def _persist(self) -> None:
        await self.repository.save(
            self.snapshot_name,
            {
                "cash": serialize(self.cash),
                "initial_cash": serialize(self.initial_cash),
                "positions": serialize(list(self.positions.values())),
                "orders": serialize(self.orders[-1000:]),
                "realized_profit_loss": serialize(self.realized_profit_loss),
            },
        )
