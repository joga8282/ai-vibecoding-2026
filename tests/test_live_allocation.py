"""Five LIVE allocations and order rejection/review use only mock transports."""
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.brokers.toss_real import TossRealBroker
from app.config import Settings
from app.engine import TradingEngine
from app.main import create_app
from app.models import Currency, Order, OrderStatus, Position, Quote, Side
from app.repository import SnapshotRepository
from app.swing_trader import SwingTrader
from app.swing_recommendations import build_swing_recommendations
from app.toss import TossMarketClient, TossOrderAPIError
from tests import test_swing_entry, test_swing_trader


class OrderErrorTransportTest(TestCase):
    def test_structured_error_preserves_only_status_and_error_code(self):
        body = {'error': {'code': 'insufficient-buying-power', 'message': 'private-message',
                          'data': {'accountNo': 'private-account'}}}
        error = HTTPError('https://example.test/api/v1/orders', 422, 'failed',
                          {'Authorization': 'private-token'}, io.BytesIO(json.dumps(body).encode()))
        with patch('app.toss.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(TossOrderAPIError) as caught:
                TossMarketClient._request_json_sync(Mock())
        self.assertEqual(caught.exception.error_code, 'insufficient-buying-power')
        self.assertTrue(caught.exception.definitive_rejection)
        for private in ('private-message', 'private-account', 'private-token'):
            self.assertNotIn(private, str(caught.exception))

    def test_missing_error_code_and_server_failure_remain_ambiguous(self):
        for body in (b'bad-json', b'{}'):
            error = HTTPError('https://example.test/orders', 422, 'failed', {}, io.BytesIO(body))
            with patch('app.toss.urllib.request.urlopen', side_effect=error):
                with self.assertRaises(HTTPError) as caught:
                    TossMarketClient._request_json_sync(Mock())
            self.assertNotIsInstance(caught.exception, TossOrderAPIError)
        for status, code in ((500, 'insufficient-buying-power'), (422, 'idempotency-key-conflict'),
                             (409, 'request-in-progress')):
            error = TossOrderAPIError(HTTPError('https://example.test/orders', status, '', {}, None), code)
            self.assertFalse(error.definitive_rejection)


class LiveAllocationTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path('tests') / f'.allocation-{uuid4().hex}.db'
        self.settings = Settings(mode='live', live_trading_enabled=True, api_access_token='mock-token',
                                 database_path=self.path, recommended_trade_ratio=D(1),
                                 live_max_total_exposure_ratio=D(1), live_symbol_policy='recommended',
                                 live_auto_max_total_exposure_ratio=D(1),
                                 live_max_order_amount_krw=D(0), live_max_total_exposure_krw=D(0))
        self.repo = SnapshotRepository(self.path)
        await self.repo.initialize()
        self.client = Mock(stock_info=AsyncMock(return_value={}), candles=AsyncMock(return_value=[]),
                           create_order=AsyncMock(), buying_power=AsyncMock(return_value={'cashBuyingPower': '5000000'}),
                           account_orders=AsyncMock(return_value=[]))
        self.broker = TossRealBroker(self.client, 'mock-account', self.repo, self.settings)
        self.broker.cash[Currency.KRW] = D(5000000)
        self.broker.reconciled = self.broker.risk.reconciled = self.broker.risk.armed = True
        self.broker.last_sync_at = datetime.now(timezone.utc)
        self.broker.reconcile = AsyncMock(return_value={'reconciled': True})
        self.book = Quote('082740', D(100000), Currency.KRW, datetime.now(timezone.utc),
                          'toss', D(99900), D(100000))
        self.broker._fresh_risk_quotes = AsyncMock(side_effect=lambda symbols: [
            Quote(s, self.book.price, Currency.KRW, datetime.now(timezone.utc), 'toss',
                  self.book.bid_price, self.book.ask_price) for s in symbols])
        self.engine = TradingEngine(self.settings, self.broker, self.repo, self.client)
        self.engine.candles = AsyncMock(return_value=[])
        self.recommend = AsyncMock(return_value={'candidates': [{'symbol': '082740', 'eligible': True}]})
        self.now = datetime(2026, 10, 7, 9, 30, tzinfo=timezone(timedelta(hours=9)))
        self.auto = SwingTrader(self.engine, self.recommend, lambda: self.now)
        self.engine.automation = self.auto
        self.broker.risk.set_recommended_symbols(['082740'])

    async def asyncTearDown(self):
        await self.engine.stop()
        self.path.unlink(missing_ok=True)

    async def entry_fixture(self, price, values, ask=None):
        self.book.price = D(price)
        self.book.bid_price = D(price)
        self.book.ask_price = D(ask) if ask is not None else D(price)
        self.client.stock_info.return_value = {'symbol': '082740', 'securityType': 'STOCK',
            'isCommonShare': True, 'sharesOutstanding': '100000000000'}
        self.client.candles.return_value = test_swing_trader.history(self.now)
        bars = test_swing_entry.four_hour_bars(self.now, values)
        self.engine.candles.side_effect = lambda symbol, interval, count: (
            test_swing_entry.weekly_bars(self.now) if interval == '1w' else bars)
        await self.auto.restore()
        await self.auto.prepare()

    async def test_live_automatic_candidate_rechecks_daily_high_zone_without_order(self):
        await self.entry_fixture('164.5', ['168', '167', '166', '166', '168', '165'])
        self.engine.running = True
        with patch('app.swing_trader.load_universe', return_value=(D('1e12'), {'082740': ['mock']})):
            await self.auto.tick()
        self.client.create_order.assert_not_awaited()
        self.assertFalse(self.auto.session['targets'])
        self.assertFalse(self.broker.order_journal)

    async def test_live_manual_candidate_rechecks_daily_high_zone_without_order(self):
        await self.entry_fixture('164.5', ['168', '167', '166', '166', '168', '165'])
        with patch('app.swing_trader.load_universe', return_value=(D('1e12'), {'082740': ['mock']})):
            with self.assertRaisesRegex(RuntimeError, '주문 직전 매수 조건'):
                await self.auto.buy_qualified('082740')
        self.client.create_order.assert_not_awaited()
        self.assertFalse(self.auto.session['targets'])

    async def test_live_ask_above_entry_ceiling_is_blocked_even_with_low_last_price(self):
        await self.entry_fixture('154', ['155', '155.1', '154.9', '155.2', '155', '155.1'], ask='155.2')
        daily = self.client.candles.return_value
        bars = test_swing_entry.four_hour_bars(self.now, ['155', '155.1', '154.9', '155.2', '155', '155.1'])
        self.assertTrue(test_swing_entry.swing_signal(daily, self.book.price, self.now, bars,
                        test_swing_entry.weekly_bars(self.now))['eligible'])
        self.assertFalse((await self.auto.signal('082740', await self.auto.quote('082740')))['eligible'])
        self.engine.running = True
        with patch('app.swing_trader.load_universe', return_value=(D('1e12'), {'082740': ['mock']})):
            await self.auto.tick()
            with self.assertRaisesRegex(RuntimeError, '주문 직전 매수 조건'):
                await self.auto.buy_qualified('082740')
        self.client.create_order.assert_not_awaited()
        self.assertFalse(self.broker.order_journal)

    async def test_live_weekly_high_cannot_pass_with_a_short_term_pullback(self):
        await self.entry_fixture('320', [327, 330, 325, 328, 329, 322])
        for candle in self.client.candles.return_value:
            for field in ('open_price', 'high_price', 'low_price', 'close_price'):
                setattr(candle, field, getattr(candle, field) + D(165))
        self.engine.running = True
        with patch('app.swing_trader.load_universe', return_value=(D('1e12'), {'082740': ['mock']})):
            await self.auto.tick()
            with self.assertRaisesRegex(RuntimeError, '주문 직전 매수 조건'):
                await self.auto.buy_qualified('082740')
        self.client.create_order.assert_not_awaited()
        self.assertFalse(self.broker.order_journal)

    async def test_price_changes_quantity_without_spending_entire_cash(self):
        self.broker.cash[Currency.KRW] = D(12000000)
        self.broker.place_market_order = AsyncMock(return_value=Order(
            'mock-order', 'mock-client', '082740', Side.BUY, D(1), D(1), D(1),
            Currency.KRW, OrderStatus.FILLED, D(0), None, datetime.now(timezone.utc)))
        for price, quantity in ((D(100000), D(23)), (D(2000000), D(1))):
            self.book.price = self.book.ask_price = price
            self.book.bid_price = price - 100
            with patch('app.swing_trader.membership', return_value=True), patch(
                    'app.swing_trader.swing_signal', return_value={'eligible': True}):
                await self.auto.buy_qualified('082740', ratio=D('.20'))
            args = self.broker.place_market_order.await_args.kwargs
            self.assertEqual(args['quantity'], quantity)
            self.assertEqual(args['order_budget'], D(2400000))
            self.assertLessEqual(quantity * price * D('1.00015'), args['order_budget'])

    async def test_expensive_share_is_skipped_and_next_candidate_can_buy(self):
        self.engine.running = True
        self.recommend.return_value = {'candidates': [{'symbol': '012450', 'eligible': True},
                                                     {'symbol': '082740', 'eligible': True}]}
        async def quotes(symbols):
            price = D(2000000) if symbols[0] == '012450' else D(100000)
            return [Quote(symbols[0], price, Currency.KRW, datetime.now(timezone.utc),
                          'toss', price - 100, price)]
        self.broker._fresh_risk_quotes.side_effect = quotes
        self.broker.place_market_order = AsyncMock(return_value=Order(
            'mock-order', 'mock-client', '082740', Side.BUY, D(9), D(100000), D(100000),
            Currency.KRW, OrderStatus.FILLED, D(0), None, datetime.now(timezone.utc)))
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.broker.place_market_order.assert_awaited_once()
        args = self.broker.place_market_order.await_args.kwargs
        self.assertEqual(args['quote'].symbol, '082740')
        self.assertEqual(args['quantity'], D(9))
        self.assertEqual(args['order_budget'], D(1000000))
        self.assertNotIn('012450', self.auto.session['targets'])

    async def test_existing_and_pending_holdings_reserve_slots_and_cash(self):
        self.broker.positions['005930'] = Position('005930', D(1), D(500000), Currency.KRW)
        self.broker.position_market_values['005930'] = D(500000)
        self.broker.cash[Currency.KRW] = D(500000)
        self.broker.open_orders = [{'symbol': '012450', 'side': 'BUY', 'quantity': '1', 'price': '400000'}]
        self.assertEqual(self.auto.buy_budget('005930'), D(0))
        self.assertEqual(self.auto.buy_budget('012450'), D(0))
        self.assertEqual(self.auto.buy_budget('082740'), D(99999))
        for symbol in ('006400', '357780', '000660'):
            self.broker.open_orders.append({'symbol': symbol, 'side': 'BUY', 'quantity': '1', 'price': '1'})
        self.assertEqual(self.auto.buy_budget('082740'), D(0))

    def use_fifteen_percent_budget(self, aggregate=D('.15')):
        settings = replace(self.settings, recommended_trade_ratio=D('.15'),
                           live_auto_max_total_exposure_ratio=D('.15'),
                           live_max_total_exposure_ratio=aggregate, manual_trade_ratio=D('.15'))
        self.engine.settings = self.broker.settings = self.broker.risk.settings = settings

    async def test_auto_ceiling_caps_a_stale_one_hundred_percent_ratio(self):
        self.use_fifteen_percent_budget(aggregate=D(1))
        self.engine.settings = replace(self.engine.settings, recommended_trade_ratio=D(1))
        self.assertEqual(self.auto.capital()[0], D(750000))
        self.assertEqual(self.auto.buy_budget('082740'), D(150000))
        self.assertEqual(self.auto.manual_buy_budget('082740', D('.10')), D(500000))

    async def test_direct_ratio_changes_order_size_without_changing_automatic_budget(self):
        self.use_fifteen_percent_budget()
        self.broker.place_market_order = AsyncMock(return_value=Order(
            'mock-order', 'mock-client', '082740', Side.BUY, D(1), D(100000), D(100000),
            Currency.KRW, OrderStatus.FILLED, D(0), None, datetime.now(timezone.utc)))
        for ratio, quantity, budget in ((D('.04'), D(1), D(200000)), (D('.10'), D(4), D(500000))):
            with patch('app.swing_trader.membership', return_value=True), patch(
                    'app.swing_trader.swing_signal', return_value={'eligible': True}):
                await self.auto.buy_qualified('082740', ratio=ratio)
            args = self.broker.place_market_order.await_args.kwargs
            self.assertEqual(args['quantity'], quantity)
            self.assertEqual(args['order_budget'], budget)
            self.assertLessEqual(quantity * self.book.ask_price * D('1.00015'), budget)
            self.assertEqual(self.auto.buy_budget('082740'), D(150000))

    async def test_direct_budget_deducts_all_existing_and_pending_exposure(self):
        self.use_fifteen_percent_budget()
        self.broker.cash[Currency.KRW] = D(4500000)
        self.broker.positions['005930'] = Position('005930', D(5), D(100000), Currency.KRW)
        self.broker.position_market_values['005930'] = D(500000)
        self.broker.open_orders = [{'symbol': '012450', 'side': 'BUY', 'quantity': '1', 'price': '100000'}]
        self.assertEqual(self.auto.manual_buy_budget('082740', D('.10')), D(149999))
        self.assertEqual(self.auto.manual_buy_budget('005930'), D(0))
        self.assertEqual(self.auto.manual_buy_budget('012450'), D(0))
        self.broker.open_orders[0].pop('price')
        self.assertEqual(self.auto.manual_buy_budget('082740'), D(0))

    async def test_existing_holdings_above_fifteen_percent_block_buys_without_liquidation(self):
        self.use_fifteen_percent_budget()
        self.broker.cash[Currency.KRW] = D(1000000)
        position = Position('005930', D(40), D(100000), Currency.KRW)
        self.broker.positions['005930'] = position
        self.broker.position_market_values['005930'] = D(4000000)
        self.assertEqual(self.auto.buy_budget('082740'), D(0))
        self.assertEqual(self.auto.manual_buy_budget('082740'), D(0))
        self.assertIs(self.broker.positions['005930'], position)
        self.client.create_order.assert_not_awaited()

    async def test_invalid_direct_ratio_is_rejected_before_remote_reads(self):
        self.use_fifteen_percent_budget()
        for ratio in (D('.16'), D(0), D('NaN'), D('Infinity')):
            with self.subTest(ratio=ratio), self.assertRaisesRegex(RuntimeError, '직접 매수 비율'):
                await self.auto.buy_qualified('082740', ratio=ratio)
        self.broker.reconcile.assert_not_awaited()
        self.recommend.assert_not_awaited()
        self.client.create_order.assert_not_awaited()

    async def test_five_buys_share_cash_without_a_sixth_position(self):
        self.engine.running = True
        symbols = ('005930', '082740', '012450', '357780', '006400', '000660')
        self.recommend.return_value = {'candidates': [{'symbol': s, 'eligible': True} for s in symbols]}
        budgets = []
        async def fill(**kwargs):
            quote, quantity = kwargs['quote'], kwargs['quantity']
            cost = quantity * quote.ask_price
            fee = cost * self.settings.fee_rate
            budgets.append(kwargs['order_budget'])
            self.assertLessEqual(cost + fee, kwargs['order_budget'])
            self.broker.cash[Currency.KRW] -= cost + fee
            self.broker.positions[quote.symbol] = Position(quote.symbol, quantity, quote.ask_price, Currency.KRW)
            self.broker.position_market_values[quote.symbol] = cost
            return Order('mock-' + quote.symbol, kwargs['client_order_id'], quote.symbol, Side.BUY,
                         quantity, quote.ask_price, quote.ask_price, Currency.KRW,
                         OrderStatus.FILLED, fee, None, datetime.now(timezone.utc))
        self.broker.place_market_order = AsyncMock(side_effect=fill)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.assertEqual(self.broker.place_market_order.await_count, 5)
        self.assertEqual(len(self.broker.positions), 5)
        self.assertNotIn('000660', self.broker.positions)
        self.assertGreaterEqual(self.broker.cash[Currency.KRW], 0)
        self.assertTrue(all(budget <= D(1000000) for budget in budgets))

    async def test_five_automatic_buys_keep_total_spending_below_fifteen_percent(self):
        self.use_fifteen_percent_budget()
        initial_cash = self.broker.cash[Currency.KRW]
        self.engine.running = True
        symbols = ('005930', '082740', '012450', '357780', '006400', '000660')
        self.recommend.return_value = {'candidates': [{'symbol': s, 'eligible': True} for s in symbols]}
        async def fill(**kwargs):
            quote, quantity = kwargs['quote'], kwargs['quantity']
            cost, fee = quantity * quote.ask_price, quantity * quote.ask_price * self.settings.fee_rate
            self.assertLessEqual(cost + fee, kwargs['order_budget'])
            self.assertLessEqual(kwargs['order_budget'], initial_cash * D('.03'))
            self.broker.cash[Currency.KRW] -= cost + fee
            self.broker.positions[quote.symbol] = Position(quote.symbol, quantity, quote.ask_price, Currency.KRW)
            self.broker.position_market_values[quote.symbol] = cost
            return Order('mock-' + quote.symbol, kwargs['client_order_id'], quote.symbol, Side.BUY,
                         quantity, quote.ask_price, quote.ask_price, Currency.KRW,
                         OrderStatus.FILLED, fee, None, datetime.now(timezone.utc))
        self.broker.place_market_order = AsyncMock(side_effect=fill)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.assertEqual(len(self.broker.positions), 5)
        self.assertNotIn('000660', self.broker.positions)
        self.assertLessEqual(initial_cash - self.broker.cash[Currency.KRW], initial_cash * D('.15'))
        self.assertGreaterEqual(self.broker.cash[Currency.KRW], initial_cash * D('.85'))

    async def test_recommendation_quantity_uses_the_same_fifth_budget(self):
        self.client.stocks_info = AsyncMock(return_value=[{'symbol': '082740', 'sharesOutstanding': '100000000'}])
        self.client.prices = AsyncMock(return_value=[self.book])
        signal = {'eligible': True, 'weekly_peak_checked': True, 'weekly_bottom_zone': True,
                  'weekly_entry_checked': True, 'rejection_reasons': []}
        with patch('app.swing_recommendations.load_universe', return_value=(D(0), {'082740': ['mock']})), patch(
                'app.swing_recommendations.membership', return_value=True), patch(
                'app.swing_recommendations.trend_context', return_value={'context_eligible': False}), patch(
                'app.swing_recommendations.swing_signal', return_value=signal):
            result = await build_swing_recommendations(self.engine)
        self.assertEqual(result['per_symbol_budget'], '1000000')
        self.assertEqual(result['candidates'][0]['quantity'], 9)
        self.assertEqual(result['candidates'][0]['order_budget'], '1000000')

    async def test_known_rejection_is_terminal_and_survives_restart(self):
        self.client.create_order.side_effect = TossOrderAPIError(
            HTTPError('https://example.test/api/v1/orders', 422, '', {}, None), 'insufficient-buying-power')
        result = await self.broker.place_market_order(client_order_id='mock-rejected', quote=self.book,
                                                     side=Side.BUY, quantity=D(1), order_budget=D(1000000))
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertEqual(result.quantity, D(0))
        self.assertTrue(self.broker.risk.armed)
        self.assertFalse(self.broker.unresolved_orders())
        restored = TossRealBroker(self.client, 'mock-account', self.repo, self.settings)
        await restored.restore()
        self.assertEqual(next(iter(restored.order_journal.values()))['status'], 'REJECTED')
        self.assertIn('insufficient-buying-power', restored.last_order_error)
        self.assertFalse(restored.risk.armed)

    async def test_ambiguous_submission_locks_orders_and_stops_engine(self):
        self.client.create_order.side_effect = TimeoutError('mock-timeout')
        with self.assertRaises(TimeoutError):
            await self.broker.place_market_order(client_order_id='mock-unknown', quote=self.book,
                                                side=Side.BUY, quantity=D(1), order_budget=D(1000000))
        self.assertFalse(self.broker.risk.armed)
        self.assertEqual(self.broker.unresolved_orders()[0]['status'], 'UNKNOWN')
        self.engine.running = True
        await self.engine.tick()
        self.assertFalse(self.engine.running)
        self.assertEqual(self.engine.status()['live_readiness']['unresolved_order_count'], 1)

    async def test_review_requires_confirmation_and_checks_closed_orders(self):
        item = {'symbol': '082740', 'side': 'BUY', 'quantity': '1', 'status': 'UNKNOWN',
                'orderId': None, 'originalClientOrderId': 'original-mock',
                'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}
        self.broker.order_journal['mock-key'] = item
        with self.assertRaises(ValueError):
            await self.broker.confirm_unsubmitted('mock-key')
        self.client.account_orders.side_effect = [[], [{'clientOrderId': 'mock-key', 'symbol': '082740'}]]
        with self.assertRaises(RuntimeError):
            await self.broker.confirm_unsubmitted('mock-key', confirmed=True)
        self.assertEqual(item['status'], 'UNKNOWN')
        self.client.account_orders.side_effect = None
        result = await self.broker.confirm_unsubmitted('mock-key', confirmed=True)
        self.assertEqual(result['status'], 'NOT_SUBMITTED')
        self.assertFalse(self.broker.risk.armed)
        self.client.create_order.assert_not_awaited()
        self.assertEqual((await self.repo.load('live_broker_state'))['order_journal']['mock-key']['status'], 'NOT_SUBMITTED')

    async def test_review_failure_preserves_unknown_and_never_submits(self):
        self.broker.order_journal['mock-key'] = {
            'symbol': '082740', 'status': 'UNKNOWN', 'orderId': None,
            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}
        self.client.account_orders.side_effect = TimeoutError('mock-history-failure')
        with self.assertRaises(TimeoutError):
            await self.broker.confirm_unsubmitted('mock-key', confirmed=True)
        self.assertEqual(self.broker.order_journal['mock-key']['status'], 'UNKNOWN')
        self.client.create_order.assert_not_awaited()

    async def test_review_api_requires_auth_confirmation_and_stopped_engine(self):
        app = create_app(self.settings)
        app.state.settings = self.settings
        app.state.engine = self.engine
        app.state.live_broker = self.broker
        self.broker.confirm_unsubmitted = AsyncMock(return_value={'resolved': True})
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://mock') as client:
            path = '/api/v1/live/order-review/confirm-unsubmitted'
            body = {'client_order_id': 'mock-key', 'confirm_no_broker_order': True}
            self.assertEqual((await client.post(path, json=body)).status_code, 401)
            self.engine.running = True
            self.assertEqual((await client.post(path, json=body, headers={'X-API-Token': 'mock-token'})).status_code, 409)
        self.broker.confirm_unsubmitted.assert_not_awaited()
