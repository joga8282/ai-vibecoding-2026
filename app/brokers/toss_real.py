from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import Currency, Order, OrderStatus, Position, Side, serialize
from app.risk import LiveRiskManager


class TossRealBroker:
    """Toss account adapter with fail-closed order submission and recovery."""

    def __init__(self, client, account_seq: str, repository, settings=None) -> None:
        self.client = client
        self.account_seq = account_seq
        self.repository = repository
        self.positions: dict[str, Position] = {}
        self.position_market_values: dict[str, Decimal] = {}
        self.orders: list = []
        self.cash = {Currency.KRW: Decimal(0), Currency.USD: Decimal(0)}
        self.last_sync_at: datetime | None = None
        self.last_error: str | None = None
        self.reconciled = False
        self.holdings_summary: dict = {}
        self.settings = settings
        self.risk = LiveRiskManager(settings) if settings is not None else None
        self.open_orders: list[dict] = []
        self.client_order_ids: dict[str, str] = {}
        self.order_journal: dict[str, dict] = {}
        self.daily_equity_date: str | None = None
        self.daily_equity_start: Decimal | None = None

    async def restore(self) -> None:
        # LIVE state is never restored as truth; the broker account is authoritative.
        self.positions = {}
        self.orders = []
        state = await self.repository.load('live_broker_state') or {}
        self.client_order_ids = state.get('client_order_ids', {})
        self.order_journal = state.get('order_journal', {})
        if not isinstance(self.client_order_ids, dict):
            self.client_order_ids = {}
        if not isinstance(self.order_journal, dict):
            self.order_journal = {}
        self.daily_equity_date = state.get('daily_equity_date')
        try:
            self.daily_equity_start = (Decimal(str(state['daily_equity_start']))
                                       if state.get('daily_equity_start') is not None else None)
        except (ArithmeticError, ValueError, TypeError):
            self.daily_equity_date = None
            self.daily_equity_start = None
        restored = []
        for item in state.get('orders', []):
            try:
                restored.append(Order(
                    order_id=str(item['order_id']), client_order_id=str(item['client_order_id']),
                    symbol=str(item['symbol']), side=Side(item['side']),
                    quantity=Decimal(str(item['quantity'])),
                    requested_price=Decimal(str(item['requested_price'])),
                    filled_price=Decimal(str(item['filled_price'])) if item.get('filled_price') is not None else None,
                    currency=Currency(item['currency']), status=OrderStatus(item['status']),
                    fee=Decimal(str(item['fee'])), reason=item.get('reason'),
                    created_at=datetime.fromisoformat(item['created_at']),
                ))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                # Corrupt local order history must not be treated as account truth.
                continue
        self.orders = restored[-1000:]
        if self.risk:
            self.risk.armed = False
            self.risk.reconciled = False

    async def get_accounts(self) -> list[dict]:
        return await self.client.accounts()

    async def get_positions(self) -> list[dict]:
        result = await self.client.holdings(self.account_seq)
        if isinstance(result, list):
            return result
        return result.get('items') or result.get('holdings') or result.get('positions') or []

    async def get_buying_power(self, currency: Currency = Currency.KRW) -> dict:
        return await self.client.buying_power(self.account_seq, currency)

    async def get_sellable_quantity(self, symbol: str) -> dict:
        return await self.client.sellable_quantity(self.account_seq, symbol)

    async def list_orders(self, status: str | None = None) -> list[dict]:
        return await self.client.account_orders(self.account_seq, status)

    async def get_order(self, order_id: str) -> dict:
        return await self.client.account_order(self.account_seq, order_id)


    async def _fresh_risk_quotes(self, symbols: list[str]):
        normalized = sorted(set(symbols))
        if getattr(self.client, 'requires_orderbook_snapshot', False) is True:
            if callable(getattr(type(self.client), 'live_quotes', None)):
                try:
                    return await self.client.live_quotes(normalized)
                except asyncio.TimeoutError:
                    # The stream sends changes, not a guaranteed initial snapshot.
                    # Fall back to REST, whose exchange timestamp is still checked
                    # by every LIVE caller before it can be used for an order.
                    return await asyncio.gather(*(self.client.live_quote(symbol) for symbol in normalized))
            return await asyncio.gather(*(self.client.live_quote(symbol) for symbol in normalized))
        return await self.client.prices(normalized)

    def _pending_buy_exposure(self) -> Decimal | None:
        exposure = Decimal(0)
        for order in self.open_orders:
            if str(order.get('side', '')).upper() != Side.BUY.value:
                continue
            execution = order.get('execution') if isinstance(order.get('execution'), dict) else {}
            quantity = (order.get('quantity') or order.get('orderQuantity')
                        or order.get('orderedQuantity'))
            price = order.get('price') or order.get('limitPrice') or order.get('orderPrice')
            if quantity is None or price is None:
                return None
            try:
                quantity = Decimal(str(quantity))
                filled = Decimal(str(execution.get('filledQuantity')
                                     or order.get('filledQuantity') or '0'))
                price = Decimal(str(price))
            except (ArithmeticError, TypeError, ValueError):
                return None
            if (not quantity.is_finite() or not filled.is_finite() or not price.is_finite()
                    or quantity < 0 or filled < 0 or price <= 0):
                return None
            exposure += max(Decimal(0), quantity - filled) * price
        return exposure

    async def place_order(self, request: dict) -> dict:
        if not self.settings or not self.settings.live_trading_enabled or not self.risk:
            raise RuntimeError('LIVE order submission is disabled by configuration.')
        if not self.risk.armed or not self.risk.reconciled or not self.reconciled:
            raise RuntimeError('Account reconciliation and manual LIVE arming are required.')
        quote = request.get('quote')
        if quote is None or quote.currency is not Currency.KRW or quote.source != 'toss':
            raise RuntimeError('A fresh Toss KRW quote is required.')
        symbol = str(request.get('symbol', '')).upper()
        side = Side(request.get('side'))
        quantity = Decimal(str(request.get('quantity', '0')))
        if not symbol or quantity <= 0 or quantity != quantity.to_integral_value():
            raise ValueError('A symbol and positive integer quantity are required.')
        order_type = str(request.get('orderType', '')).upper()
        if order_type not in {'MARKET', 'LIMIT'}:
            raise ValueError('Only MARKET and LIMIT orders are supported.')
        limit_price = None
        if order_type == 'LIMIT':
            try:
                limit_price = Decimal(str(request.get('price')))
            except (ArithmeticError, TypeError, ValueError):
                raise ValueError('A positive finite KRW limit price is required.') from None
            if not limit_price.is_finite() or limit_price <= 0 or limit_price != limit_price.to_integral_value():
                raise ValueError('A positive integer KRW limit price is required.')
        if any(position.symbol not in self.position_market_values for position in self.positions.values()):
            raise RuntimeError('Account exposure valuation is unavailable; reconcile holdings first.')
        fresh_quotes = await self._fresh_risk_quotes([symbol])
        price_map = {item.symbol: item for item in fresh_quotes}
        if getattr(self.client, 'requires_orderbook_snapshot', False) is True:
            for quote_snapshot in fresh_quotes:
                age = (datetime.now(timezone.utc) - quote_snapshot.timestamp.astimezone(timezone.utc)).total_seconds()
                if (quote_snapshot.bid_price is None or quote_snapshot.ask_price is None
                        or age > 10 or age < -2):
                    raise RuntimeError('A fresh two-sided KRW order book is required for LIVE risk checks.')
        fresh = price_map.get(symbol)
        if (not fresh or fresh.currency is not quote.currency or fresh.price != quote.price
                or fresh.bid_price != quote.bid_price or fresh.ask_price != quote.ask_price):
            raise RuntimeError('Quote changed before submission.')
        # Providers can refresh an unchanged order book with a new timestamp
        # between the UI lookup and this final risk check. Use the verified,
        # newest snapshot for the risk timestamp and order valuation.
        quote = fresh
        valuation_price = limit_price if limit_price is not None else (
            quote.ask_price if side is Side.BUY and quote.ask_price is not None else
            quote.bid_price if side is Side.SELL and quote.bid_price is not None else quote.price
        )
        if side is Side.BUY and request.get('orderBudget') is not None:
            budget = Decimal(str(request['orderBudget']))
            if (not budget.is_finite() or budget <= 0
                    or quantity * valuation_price * (1 + self.settings.fee_rate) > budget):
                raise RuntimeError('Order exceeds the selected investment budget.')
        if limit_price is not None:
            # A passive limit reduces immediate execution risk, but market movement can still fill it.
            buy_crosses = (limit_price >= quote.ask_price if quote.ask_price is not None
                           else limit_price >= quote.price)
            sell_crosses = (limit_price <= quote.bid_price if quote.bid_price is not None
                            else limit_price <= quote.price)
            if (side is Side.BUY and buy_crosses) or (side is Side.SELL and sell_crosses):
                raise ValueError('Limit price would be marketable against the current quote.')
        opposite = any(str(o.get('symbol', '')).upper() == symbol for o in self.open_orders)
        if side is Side.BUY and opposite:
            raise RuntimeError('LIVE risk check blocked order: opposite-open-order')
        position_exposure = sum(self.position_market_values.values(), Decimal(0))
        pending_buy_exposure = self._pending_buy_exposure()
        if side is Side.BUY and pending_buy_exposure is None:
            raise RuntimeError('LIVE pending-buy exposure cannot be valued safely.')
        exposure = position_exposure + (pending_buy_exposure or Decimal(0))
        current_equity = None
        if side is Side.BUY:
            power = await self.get_buying_power(Currency.KRW)
            cash_buying_power = Decimal(str(power.get('cashBuyingPower', '0')))
            if quantity * valuation_price > cash_buying_power:
                raise RuntimeError('Order exceeds cash buying power.')
            current_equity = cash_buying_power + position_exposure
        else:
            sellable = await self.get_sellable_quantity(symbol)
            if quantity > Decimal(str(sellable.get('sellableQuantity', '0'))):
                raise RuntimeError('Order exceeds sellable quantity.')
        decision = self.risk.validate(
            symbol=symbol, side=side, quantity=quantity, price=valuation_price,
            total_exposure=exposure, position_count=len(self.positions),
            quote_at=quote.timestamp, current_equity=current_equity,
            has_opposite_open_order=opposite,
        )
        await self.repository.append_risk_event(symbol, decision.rule, decision.allowed, decision.detail)
        if not decision.allowed:
            raise RuntimeError('LIVE risk check blocked order: ' + decision.rule)
        if request.get('returnAfterAccept') is True:
            # Lock concurrent test submissions while the accepted order is open.
            self.disarm()
        original_id = str(request['clientOrderId'])
        if not original_id or original_id in self.client_order_ids.values():
            raise RuntimeError('Duplicate client order ID; refusing to submit again.')
        exchange_id = 'bot-' + hashlib.sha256(original_id.encode()).hexdigest()[:32]
        self.client_order_ids[exchange_id] = original_id
        self.order_journal[exchange_id] = {
            'clientOrderId': exchange_id, 'originalClientOrderId': original_id,
            'symbol': symbol, 'side': side.value, 'quantity': str(quantity),
            'price': str(valuation_price), 'currency': quote.currency.value,
            'status': 'SUBMITTING', 'orderId': None,
            'createdAt': datetime.now(timezone.utc).isoformat(),
        }
        await self._persist_live_state()
        try:
            payload = {
                'clientOrderId': exchange_id, 'symbol': symbol, 'side': side.value,
                'orderType': order_type, 'quantity': str(int(quantity)),
            }
            if limit_price is not None:
                # Toss limit order prices are KRW integer won.
                payload['price'] = str(int(limit_price))
            accepted = await self.client.create_order(self.account_seq, payload)
        except Exception:
            self.order_journal[exchange_id]['status'] = 'UNKNOWN'
            await self._persist_live_state()
            self.risk.armed = self.risk.reconciled = self.reconciled = False
            raise
        order_id = accepted.get('orderId')
        if not order_id:
            self.order_journal[exchange_id]['status'] = 'UNKNOWN'
            await self._persist_live_state()
            self.risk.armed = self.risk.reconciled = self.reconciled = False
            raise RuntimeError('Broker response lacked an order ID; LIVE has been disarmed.')
        self.order_journal[exchange_id].update({'status': 'ACCEPTED', 'orderId': str(order_id)})
        await self._persist_live_state()
        if request.get('returnAfterAccept') is True:
            # A still-open accepted order blocks further LIVE orders until an
            # operator cancels it and reconciles the account again.
            self.disarm()
            return {
                'orderId': str(order_id), 'clientOrderId': original_id,
                'symbol': symbol, 'side': side.value, 'quantity': str(quantity),
                'price': str(valuation_price), 'currency': quote.currency.value,
                'status': 'ACCEPTED',
            }
        return await self._resolve_order(str(order_id), original_id, symbol, side,
                                         quantity, valuation_price, quote.currency,
                                         client_order_key=exchange_id)

    async def _resolve_order(self, order_id, client_id, symbol, side, quantity, price, currency,
                             client_order_key=None, post_reconcile=True):
        detail = None
        for _ in range(6):
            try:
                detail = await self.get_order(order_id)
            except Exception:
                await asyncio.sleep(.5)
                continue
            status = str(detail.get('status', '')).upper()
            if status in {'FILLED', 'REJECTED', 'CANCELED', 'PARTIAL_FILLED', 'PARTIALLY_FILLED'}:
                break
            await asyncio.sleep(.5)
        else:
            try:
                await self.cancel_order(order_id)
                detail = await self.get_order(order_id)
            except Exception as exc:
                self.risk.armed = self.risk.reconciled = self.reconciled = False
                raise RuntimeError('Order status unknown; LIVE is locked pending reconciliation.') from exc
        if str((detail or {}).get('status', '')).upper() in {'PARTIAL_FILLED', 'PARTIALLY_FILLED'}:
            if client_order_key in self.order_journal:
                self.order_journal[client_order_key]['status'] = 'CANCEL_REQUESTED'
                await self._persist_live_state()
            try:
                await self.cancel_order(order_id)
                detail = await self.get_order(order_id)
            except Exception as exc:
                self.risk.armed = self.risk.reconciled = self.reconciled = False
                raise RuntimeError('Partial order remainder could not be confirmed canceled; LIVE is locked.') from exc
            if str((detail or {}).get('status', '')).upper() not in {'CANCELED', 'FILLED'}:
                self.risk.armed = self.risk.reconciled = self.reconciled = False
                raise RuntimeError('Partial order remainder is unconfirmed; LIVE is locked.')
        execution = (detail or {}).get('execution') or {}
        try:
            filled = Decimal(str(execution.get('filledQuantity') or '0'))
            average = Decimal(str(execution.get('averageFilledPrice') or price))
            fee = (Decimal(str(execution.get('commission') or '0'))
                   + Decimal(str(execution.get('tax') or '0')))
        except (ArithmeticError, TypeError, ValueError) as exc:
            self.risk.armed = self.risk.reconciled = self.reconciled = False
            raise RuntimeError('Broker returned invalid execution details; LIVE is locked.') from exc
        exchange_status = str((detail or {}).get('status', '')).upper()
        if (not filled.is_finite() or filled < 0 or filled > quantity
                or filled != filled.to_integral_value()
                or not average.is_finite() or average <= 0
                or not fee.is_finite() or fee < 0):
            self.risk.armed = self.risk.reconciled = self.reconciled = False
            raise RuntimeError('Broker reported invalid execution values; LIVE is locked.')
        if exchange_status == 'FILLED' and filled != quantity:
            self.risk.armed = self.risk.reconciled = self.reconciled = False
            raise RuntimeError('Broker FILLED status does not match filled quantity; LIVE is locked.')
        if filled == quantity:
            result_status = OrderStatus.FILLED
        elif filled > 0:
            result_status = OrderStatus.PARTIALLY_FILLED
        elif exchange_status == 'CANCELED':
            result_status = OrderStatus.CANCELED
        elif exchange_status == 'REJECTED':
            result_status = OrderStatus.REJECTED
        else:
            self.risk.armed = self.risk.reconciled = self.reconciled = False
            raise RuntimeError('Unverified terminal order state; LIVE is locked.')
        order = Order(order_id, client_id, symbol, side, filled, price,
                      average if filled else None, currency, result_status, fee,
                      None if filled else exchange_status, datetime.now(timezone.utc))
        self.orders = [o for o in self.orders if o.order_id != order_id] + [order]
        if client_order_key in self.order_journal:
            self.order_journal[client_order_key].update({
                'status': result_status.value, 'orderId': order_id,
                'filledQuantity': str(filled), 'averageFilledPrice': str(average) if filled else None,
                'resolvedAt': datetime.now(timezone.utc).isoformat(),
            })
        await self._persist_live_state()
        if post_reconcile:
            await self.reconcile()
        return order

    async def cancel_order(self, order_id: str) -> dict:
        if not self.settings or not self.settings.live_trading_enabled:
            raise RuntimeError('LIVE order submission is disabled by configuration.')
        return await self.client.cancel_account_order(self.account_seq, order_id)

    async def cancel_and_resolve_order(self, order_id: str) -> Order:
        """Cancel a bot-managed order and persist the broker-confirmed final fill."""
        if not self.settings or not self.settings.live_trading_enabled:
            raise RuntimeError('LIVE order submission is disabled by configuration.')
        journal_key = next((key for key, item in self.order_journal.items()
                            if str(item.get('orderId') or '') == order_id), None)
        if journal_key is None:
            raise LookupError('Order is not managed by this trading app.')
        item = self.order_journal[journal_key]
        if item.get('status') != 'ACCEPTED':
            if item.get('status') == 'CANCEL_REQUESTED':
                raise ValueError('A cancellation is already pending; reconcile before retrying.')
            raise ValueError('Only an open managed order can be canceled.')

        item['status'] = 'CANCEL_REQUESTED'
        await self._persist_live_state()
        cancel_error = None
        try:
            await self.cancel_order(order_id)
        except Exception as exc:
            # A cancel can race with a fill. Query the broker before deciding
            # that the order is unresolved.
            cancel_error = exc

        detail = None
        terminal = {'FILLED', 'CANCELED', 'REJECTED'}
        for attempt in range(6):
            try:
                detail = await self.get_order(order_id)
            except Exception:
                detail = None
            status = str((detail or {}).get('status', '')).upper()
            if status in terminal:
                break
            if attempt < 5:
                await asyncio.sleep(.5)
        else:
            item['status'] = 'UNKNOWN'
            self.last_error = ('Cancel outcome unknown after '
                               f'{type(cancel_error).__name__}.' if cancel_error
                               else 'Cancel request has no confirmed terminal order status.')
            self.disarm()
            self.reconciled = False
            await self._persist_live_state()
            raise RuntimeError('Cancellation is unconfirmed; LIVE is locked pending reconciliation.')

        execution = (detail or {}).get('execution') or {}
        try:
            filled = Decimal(str(execution.get('filledQuantity') or '0'))
            average = Decimal(str(execution.get('averageFilledPrice') or item['price']))
            fee = (Decimal(str(execution.get('commission') or '0'))
                   + Decimal(str(execution.get('tax') or '0')))
            requested = Decimal(str(item['quantity']))
            requested_price = Decimal(str(item['price']))
            currency = Currency(item['currency'])
            side = Side(item['side'])
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            item['status'] = 'UNKNOWN'
            self.disarm()
            self.reconciled = False
            await self._persist_live_state()
            raise RuntimeError('Broker returned invalid execution details; LIVE is locked.') from exc
        if (not filled.is_finite() or filled < 0 or filled > requested
                or filled != filled.to_integral_value()
                or not average.is_finite() or average <= 0
                or not fee.is_finite() or fee < 0):
            item['status'] = 'UNKNOWN'
            self.disarm()
            self.reconciled = False
            await self._persist_live_state()
            raise RuntimeError('Broker returned invalid execution values; LIVE is locked.')

        exchange_status = str((detail or {}).get('status', '')).upper()
        if exchange_status == 'FILLED':
            if filled != requested:
                item['status'] = 'UNKNOWN'
                self.disarm()
                self.reconciled = False
                await self._persist_live_state()
                raise RuntimeError('Broker FILLED status does not match filled quantity; LIVE is locked.')
            result_status = OrderStatus.FILLED
        elif filled == requested:
            result_status = OrderStatus.FILLED
        elif filled:
            result_status = OrderStatus.PARTIALLY_FILLED
        elif exchange_status == 'CANCELED':
            result_status = OrderStatus.CANCELED
        else:
            result_status = OrderStatus.REJECTED

        order = Order(
            order_id, str(item.get('originalClientOrderId') or journal_key),
            str(item['symbol']), side, filled, requested_price,
            average if filled else None, currency, result_status, fee,
            None if filled else exchange_status, datetime.now(timezone.utc),
        )
        self.orders = [existing for existing in self.orders if existing.order_id != order_id] + [order]
        item.update({
            'status': result_status.value, 'filledQuantity': str(filled),
            'averageFilledPrice': str(average) if filled else None,
            'resolvedAt': datetime.now(timezone.utc).isoformat(),
        })
        self.disarm()
        self.reconciled = False
        await self._persist_live_state()
        try:
            result = await self.reconcile(recover_unfinished_orders=False)
        except Exception as exc:
            self.disarm()
            raise RuntimeError('Order is resolved, but account reconciliation failed; LIVE remains locked.') from exc
        if not result.get('reconciled'):
            self.disarm()
            raise RuntimeError('Order is resolved, but account reconciliation failed; LIVE remains locked.')
        self.disarm()
        return order

    async def place_market_order(self, **kwargs):
        if not self.risk or not self.settings or not self.settings.live_trading_enabled or not self.risk.armed:
            raise RuntimeError('LIVE trading is not armed.')
        quote = kwargs['quote']
        return await self.place_order({
            'clientOrderId': kwargs['client_order_id'], 'symbol': quote.symbol,
            'side': kwargs['side'].value, 'orderType': 'MARKET',
            'quantity': str(kwargs['quantity']), 'quote': quote,
            'orderBudget': kwargs.get('order_budget'),
        })

    async def place_limit_order(self, *, client_order_id: str, quote, side: Side,
                                quantity: Decimal, limit_price: Decimal) -> dict:
        """Submit a passive KRW limit and resolve it, canceling and confirming any remainder."""
        if not self.risk or not self.settings or not self.settings.live_trading_enabled or not self.risk.armed:
            raise RuntimeError('LIVE trading is not armed.')
        return await self.place_order({
            'clientOrderId': client_order_id, 'symbol': quote.symbol,
            'side': side.value, 'orderType': 'LIMIT', 'quantity': str(quantity),
            'price': str(limit_price), 'quote': quote,
        })

    async def submit_limit_order(self, *, client_order_id: str, quote, side: Side,
                                 quantity: Decimal, limit_price: Decimal) -> dict:
        """Submit a passive KRW limit and return after broker acceptance for manual cancellation."""
        if not self.risk or not self.settings or not self.settings.live_trading_enabled or not self.risk.armed:
            raise RuntimeError('LIVE trading is not armed.')
        return await self.place_order({
            'clientOrderId': client_order_id, 'symbol': quote.symbol,
            'side': side.value, 'orderType': 'LIMIT', 'quantity': str(quantity),
            'price': str(limit_price), 'quote': quote, 'returnAfterAccept': True,
        })

    def arm(self) -> None:
        if not self.settings or not self.settings.live_trading_enabled or not self.risk:
            raise RuntimeError('LIVE order submission is disabled by configuration.')
        if not self.reconciled or not self.last_sync_at:
            raise RuntimeError('A successful account reconciliation is required before arming.')
        unresolved = {'SUBMITTING', 'UNKNOWN', 'ACCEPTED', 'CANCEL_REQUESTED'}
        if any(item.get('status') in unresolved for item in self.order_journal.values()):
            raise RuntimeError('Unresolved LIVE orders must be recovered before arming.')
        self.risk.reconciled = True
        self.risk.armed = True

    def disarm(self) -> None:
        if self.risk:
            self.risk.armed = False

    async def _persist_live_state(self) -> None:
        await self.repository.save('live_broker_state', {
            'client_order_ids': self.client_order_ids,
            'order_journal': self.order_journal,
            'orders': serialize(self.orders[-1000:]),
            'daily_equity_date': self.daily_equity_date,
            'daily_equity_start': str(self.daily_equity_start) if self.daily_equity_start is not None else None,
        })

    async def _recover_unfinished_orders(self, open_orders: list[dict]) -> None:
        unfinished = {
            key: item for key, item in self.order_journal.items()
            if item.get('status') in {'SUBMITTING', 'UNKNOWN', 'ACCEPTED', 'CANCEL_REQUESTED'}
        }
        if not unfinished:
            return
        all_orders = None
        for client_key, item in unfinished.items():
            order_id = item.get('orderId')
            if not order_id:
                if all_orders is None:
                    all_orders = await self.list_orders()
                match = next((row for row in all_orders if isinstance(row, dict)
                              and row.get('clientOrderId') == client_key), None)
                if not match:
                    item['status'] = 'UNKNOWN'
                    await self._persist_live_state()
                    self.risk.armed = False
                    self.risk.reconciled = False
                    self.last_error = f"Could not resolve submitted order {client_key}; manual review required."
                    continue
                order_id = match.get('orderId') or match.get('id')
                if not order_id:
                    self.risk.armed = False
                    self.risk.reconciled = False
                    raise RuntimeError('Matched broker order omitted its identifier.')
                item['orderId'] = str(order_id)
            self.client_order_ids.setdefault(client_key, item.get('originalClientOrderId', client_key))
            item['status'] = 'ACCEPTED'
            await self._persist_live_state()
            await self._resolve_order(
                str(order_id), item.get('originalClientOrderId', client_key),
                item['symbol'], Side(item['side']), Decimal(str(item['quantity'])),
                Decimal(str(item['price'])), Currency(item['currency']),
                client_order_key=client_key, post_reconcile=False,
            )
        if any(item.get('status') in {'SUBMITTING', 'UNKNOWN', 'ACCEPTED', 'CANCEL_REQUESTED'}
               for item in self.order_journal.values()):
            self.risk.armed = False
            self.risk.reconciled = False

    async def reconcile(self, *, recover_unfinished_orders: bool = True) -> dict:
        started = datetime.now(timezone.utc)
        try:
            raw_holdings = await self.client.holdings(self.account_seq)
            if isinstance(raw_holdings, list):
                holdings = raw_holdings
                summary = {}
            elif isinstance(raw_holdings, dict):
                holdings = next((raw_holdings[key] for key in ('items', 'holdings', 'positions')
                                 if key in raw_holdings), None)
                summary = raw_holdings.get('marketValue', {})
            else:
                holdings = None
                summary = {}
            orders = await self.list_orders('OPEN')
            buying_power = {
                currency: await self.get_buying_power(Currency(currency))
                for currency in ('KRW', 'USD')
            }
            mismatches = []
            if not isinstance(holdings, list):
                mismatches.append('Holdings response did not contain a list.')
                holdings = []
            if not isinstance(orders, list):
                mismatches.append('Open orders response did not contain a list.')
                orders = []
            new_positions = {}
            new_position_market_values = {}
            for index, item in enumerate(holdings):
                if not isinstance(item, dict):
                    mismatches.append(f'Invalid holding row at index {index}.')
                    continue
                stock = item.get('stock') if isinstance(item.get('stock'), dict) else {}
                symbol = str(item.get('symbol') or stock.get('symbol') or '').upper()
                try:
                    quantity = Decimal(str(item.get('quantity')))
                    average_price = Decimal(str(item.get('averagePurchasePrice') or
                                                 item.get('averagePrice') or item.get('average_price') or '0'))
                    currency = Currency(item.get('currency', 'KRW'))
                except (ArithmeticError, ValueError, TypeError):
                    mismatches.append(f'Invalid holding values at index {index}.')
                    continue
                if (not symbol or not quantity.is_finite() or quantity < 0 or
                        not average_price.is_finite() or average_price < 0):
                    mismatches.append(f'Invalid holding values at index {index}.')
                    continue
                if quantity == 0:
                    continue
                if symbol in new_positions:
                    mismatches.append(f'Duplicate holding symbol {symbol}.')
                    continue
                new_positions[symbol] = Position(symbol, quantity, average_price, currency)
                raw_market_value = item.get('marketValue') or item.get('market_value')
                if isinstance(raw_market_value, dict):
                    raw_market_value = (raw_market_value.get('amount')
                                        or raw_market_value.get('marketValue')
                                        or raw_market_value.get('value'))
                if raw_market_value is not None:
                    try:
                        market_value = Decimal(str(raw_market_value))
                    except (ArithmeticError, ValueError, TypeError):
                        mismatches.append(f'Invalid broker market value for {symbol}.')
                    else:
                        if not market_value.is_finite() or market_value < 0:
                            mismatches.append(f'Invalid broker market value for {symbol}.')
                        else:
                            new_position_market_values[symbol] = market_value

            new_cash = {}
            for currency, value in buying_power.items():
                if not isinstance(value, dict) or 'cashBuyingPower' not in value:
                    mismatches.append(f'Missing {currency} buying-power amount.')
                    continue
                try:
                    amount = Decimal(str(value['cashBuyingPower']))
                except (ArithmeticError, ValueError, TypeError):
                    mismatches.append(f'Invalid {currency} buying-power amount.')
                    continue
                if not amount.is_finite() or amount < 0:
                    mismatches.append(f'Invalid {currency} buying-power amount.')
                    continue
                new_cash[Currency(currency)] = amount

            current_equity = None
            if self.risk and self.settings.live_trading_enabled and not mismatches:
                if any(position.currency is not Currency.KRW for position in new_positions.values()):
                    mismatches.append('LIVE risk tracking cannot value non-KRW holdings safely.')
                else:
                    try:
                        quote_symbols = sorted(set(new_positions) - set(new_position_market_values))
                        risk_quotes = await self._fresh_risk_quotes(quote_symbols) if quote_symbols else []
                        valuation_checked_at = datetime.now(timezone.utc)
                        risk_prices = {quote.symbol: quote for quote in risk_quotes}
                        for symbol, position in new_positions.items():
                            if symbol in new_position_market_values:
                                continue
                            quote = risk_prices.get(symbol)
                            age = ((valuation_checked_at - quote.timestamp.astimezone(timezone.utc)).total_seconds()
                                   if quote and quote.timestamp.tzinfo is not None else float('inf'))
                            needs_book = getattr(self.client, 'requires_orderbook_snapshot', False) is True
                            has_book = (not needs_book
                                        or (quote and quote.bid_price is not None and quote.ask_price is not None))
                            if not quote:
                                reason = 'quote missing'
                            elif quote.currency is not Currency.KRW:
                                reason = 'quote currency is not KRW'
                            elif not has_book:
                                reason = 'two-sided order book missing'
                            elif age > 10:
                                reason = f'quote stale ({age:.1f}s old)'
                            elif age < -2:
                                reason = f'quote timestamp is in the future ({-age:.1f}s)'
                            else:
                                reason = None
                            if reason:
                                mismatches.append(f'Fresh KRW valuation is unavailable for {symbol}: {reason}.')
                        if not mismatches:
                            market_value = sum(new_position_market_values.values(), Decimal(0))
                            market_value += sum((
                                position.quantity * (risk_prices[symbol].bid_price or risk_prices[symbol].price)
                                for symbol, position in new_positions.items()
                                if symbol not in new_position_market_values
                            ), Decimal(0))
                            new_position_market_values.update({
                                symbol: position.quantity * (risk_prices[symbol].bid_price or risk_prices[symbol].price)
                                for symbol, position in new_positions.items()
                                if symbol not in new_position_market_values
                            })
                            current_equity = new_cash.get(Currency.KRW, Decimal(0)) + market_value
                    except Exception as exc:
                        detail = str(exc).strip()
                        if not detail or len(detail) > 240:
                            detail = type(exc).__name__
                        mismatches.append(f'LIVE risk equity valuation failed: {detail}')

            for index, order in enumerate(orders):
                if not isinstance(order, dict) or not (order.get('orderId') or order.get('id')):
                    mismatches.append(f'Open order is missing an order identifier at index {index}.')
                elif not order.get('status'):
                    mismatches.append(f'Open order is missing a status at index {index}.')

            if mismatches:
                detail = '; '.join(mismatches)
                await self.repository.save_live_account_snapshot(
                    self.account_seq, holdings, orders, started.isoformat(),
                    reconciled=False, detail=detail,
                )
                self.last_error = detail
                self.reconciled = False
                if self.risk:
                    self.risk.armed = False
                    self.risk.reconciled = False
                return {'reconciled': False, 'positions': holdings, 'orders': orders,
                        'synced_at': started.isoformat(), 'mismatches': mismatches}

            await self.repository.save_live_account_snapshot(
                self.account_seq, holdings, orders, started.isoformat()
            )
            self.positions = new_positions
            self.position_market_values = new_position_market_values
            self.cash = new_cash
            self.holdings_summary = summary
            self.last_sync_at = started
            self.last_error = None
            self.reconciled = True
            self.open_orders = orders
            if self.risk:
                if self.settings.live_trading_enabled:
                    today = started.astimezone(timezone(timedelta(hours=9))).date().isoformat()
                    if self.daily_equity_date != today or self.daily_equity_start is None:
                        self.daily_equity_date = today
                        self.daily_equity_start = current_equity
                    if current_equity is None or self.daily_equity_start is None:
                        raise RuntimeError('LIVE daily equity baseline is unavailable.')
                    self.risk.daily_loss = max(Decimal(0), self.daily_equity_start - current_equity)
                self.risk.reconciled = True
                await self._persist_live_state()
                if recover_unfinished_orders:
                    await self._recover_unfinished_orders(orders)
            return {'reconciled': True, 'positions': holdings, 'orders': orders,
                    'synced_at': started.isoformat(), 'mismatches': []}
        except Exception as exc:
            self.last_error = f'{type(exc).__name__}: {exc}'
            self.reconciled = False
            if self.risk:
                self.risk.armed = False
                self.risk.reconciled = False
            await self.repository.append_reconciliation_event(
                self.account_seq, False, self.last_error, started.isoformat()
            )
            raise

    def account(self, quotes: dict) -> dict:
        position_rows = []
        equity = {Currency.KRW: self.cash[Currency.KRW], Currency.USD: self.cash[Currency.USD]}
        unrealized = {Currency.KRW: Decimal(0), Currency.USD: Decimal(0)}
        for position in self.positions.values():
            quote = quotes.get(position.symbol)
            market_value = self.position_market_values.get(position.symbol)
            price = (market_value / position.quantity if market_value is not None and position.quantity
                     else quote.price if quote and quote.currency is position.currency
                     else position.average_price)
            market_value = position.quantity * price
            equity[position.currency] += market_value
            pnl = market_value - position.quantity * position.average_price
            unrealized[position.currency] += pnl
            position_rows.append({'symbol': position.symbol, 'quantity': str(position.quantity),
                                  'average_price': str(position.average_price), 'currency': position.currency.value,
                                  'market_price': str(price), 'market_value': str(market_value),
                                  'unrealized_profit_loss': str(pnl),
                                  'quote_timestamp': self.last_sync_at.isoformat() if self.reconciled and self.last_sync_at else None})
        return {'mode': 'live', 'cash': {key.value: str(value) for key, value in self.cash.items()},
                'positions': position_rows, 'realized_profit_loss': {'KRW': '0', 'USD': '0'},
                'unrealized_profit_loss': {key.value: str(value) for key, value in unrealized.items()},
                'total_equity': {key.value: str(value) for key, value in equity.items()},
                'data_status': 'synced' if self.reconciled else 'stale'}

    def performance(self, quotes: dict) -> dict:
        return {'by_currency': {}, 'orders': {'total': len(self.orders),
                'filled': sum(o.status is OrderStatus.FILLED for o in self.orders),
                'rejected': sum(o.status is OrderStatus.REJECTED for o in self.orders)},
                'total_fees': {'KRW': '0', 'USD': '0'}}
