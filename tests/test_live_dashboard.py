"""LIVE dashboard contract tests. All account, quote and order calls are mocked."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.brokers.toss_real import TossRealBroker
from app.config import Settings
from app.engine import TradingEngine
from app.main import create_app
from app.models import Currency, Order, OrderStatus, Position, Quote, Side
from app.repository import SnapshotRepository
from app.swing_trader import SwingTrader


class LiveDashboardTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path('tests') / f'.live-dashboard-{uuid4().hex}.db'
        self.settings = Settings(
            mode='live', live_trading_enabled=True, api_access_token='mock-token',
            database_path=self.path, recommended_trade_ratio=D('.15'), fee_rate=D(0),
            live_allowed_symbols=('005930',), live_max_order_amount_krw=D(100000),
            live_max_total_exposure_krw=D(500000),
        )
        self.repository = SnapshotRepository(self.path)
        await self.repository.initialize()
        self.client = Mock(
            stock_info=AsyncMock(return_value={'symbol': '005930', 'name': 'mock stock'}),
            candles=AsyncMock(return_value=[]), create_order=AsyncMock(),
        )
        self.broker = TossRealBroker(self.client, 'mock-account', self.repository, self.settings)
        self.broker.cash[Currency.KRW] = D(1000000)
        self.broker.reconciled = self.broker.risk.reconciled = self.broker.risk.armed = True
        self.broker.last_sync_at = datetime.now(timezone.utc)
        self.broker.reconcile = AsyncMock(return_value={'reconciled': True})
        self.book = Quote('005930', D(69950), Currency.KRW, datetime.now(timezone.utc),
                          'toss', D(69900), D(70000))
        self.broker._fresh_risk_quotes = AsyncMock(return_value=[self.book])
        self.engine = TradingEngine(self.settings, self.broker, self.repository, self.client)
        self.engine.candles = AsyncMock(return_value=[])
        self.recommend = AsyncMock(return_value={
            'candidates': [{'symbol': '005930', 'currency': 'KRW', 'eligible': True}],
        })
        # Trading schedule is deterministic; broker quote freshness uses real UTC.
        self.auto = SwingTrader(self.engine, self.recommend,
                                lambda: datetime(2026, 10, 6, 13, 0, tzinfo=timezone(timedelta(hours=9))))
        self.engine.automation = self.auto
        self.app = create_app(self.settings)
        self.app.state.settings = self.settings
        self.app.state.engine = self.engine
        self.app.state.live_broker = self.broker
        self.app.state.repository = self.repository

    async def asyncTearDown(self):
        await self.engine.stop()
        self.path.unlink(missing_ok=True)

    def order(self, side=Side.BUY, status=OrderStatus.FILLED, quantity=D(1)):
        return Order('mock-order', 'mock-client', '005930', side, quantity, D(70000),
                     D(70000) if quantity else None, Currency.KRW, status, D(0), None,
                     datetime.now(timezone.utc))

    async def test_live_trade_routes_require_auth_and_explicit_confirmation(self):
        self.auto.buy_qualified = AsyncMock(return_value=self.order())
        self.auto.manual_close = AsyncMock(return_value=[self.order(Side.SELL)])
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            for path in ('/api/v1/live/qualified-buy/005930', '/api/v1/live/positions/005930/close'):
                response = await client.post(path, json={'confirm_real_order': True})
                self.assertEqual(response.status_code, 401)
                headers = {'X-API-Token': 'mock-token'}
                for body in ({}, {'confirm_real_order': False}):
                    response = await client.post(path, json=body, headers=headers)
                    self.assertEqual(response.status_code, 422)
                response = await client.post(path, json={'confirm_real_order': True}, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertIn('LIVE', response.json()['message'])
        self.auto.buy_qualified.assert_awaited_once_with('005930')
        self.auto.manual_close.assert_awaited_once_with('005930')

    async def test_paper_order_routes_cannot_submit_live_orders(self):
        self.auto.buy_qualified = AsyncMock()
        self.auto.manual_close = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            for path in ('/api/v1/paper/qualified-buy/005930', '/api/v1/paper/positions/005930/close',
                         '/api/v1/paper/test-buy/005930'):
                response = await client.post(path)
                self.assertEqual(response.status_code, 409)
        self.auto.buy_qualified.assert_not_awaited()
        self.auto.manual_close.assert_not_awaited()
        self.client.create_order.assert_not_awaited()

    async def test_live_ratio_requires_auth_and_cannot_exceed_fifteen_percent(self):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            path = '/api/v1/settings/investment-ratio'
            self.assertEqual((await client.put(path, json={'ratio_percent': 10})).status_code, 401)
            headers = {'X-API-Token': 'mock-token'}
            self.assertEqual((await client.put(path, json={'ratio_percent': 16}, headers=headers)).status_code, 422)
            self.assertEqual((await client.put(path, json={'ratio_percent': 10}, headers=headers)).status_code, 200)
        self.assertEqual(self.engine.settings.recommended_trade_ratio, D('.10'))
        self.assertIs(self.broker.risk.settings, self.engine.settings)
        self.assertEqual((await self.repository.load('trading_preferences'))['recommended_trade_ratio'], '0.1')

    async def test_direct_ratio_setting_is_authenticated_and_independent(self):
        await self.repository.save('trading_preferences', {'recommended_trade_ratio': '0.15', 'other': 'keep'})
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            path, headers = '/api/v1/settings/manual-investment-ratio', {'X-API-Token': 'mock-token'}
            self.assertEqual((await client.put(path, json={'ratio_percent': 5})).status_code, 401)
            self.assertEqual((await client.put(path, json={'ratio_percent': 16}, headers=headers)).status_code, 422)
            self.assertEqual((await client.put(path, json={'ratio_percent': 5}, headers=headers)).status_code, 200)
            self.assertEqual((await client.put('/api/v1/settings/investment-ratio',
                json={'ratio_percent': 10}, headers=headers)).status_code, 200)
        self.assertEqual(self.engine.settings.manual_trade_ratio, D('.05'))
        self.assertEqual(self.engine.settings.recommended_trade_ratio, D('.10'))
        self.assertEqual(await self.repository.load('trading_preferences'), {
            'recommended_trade_ratio': '0.1', 'manual_trade_ratio': '0.05', 'other': 'keep'})

    async def test_selected_order_ratio_reaches_trader_without_saving_automatic_ratio(self):
        self.auto.buy_qualified = AsyncMock(return_value=self.order())
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            response = await client.post('/api/v1/live/qualified-buy/005930',
                json={'confirm_real_order': True, 'ratio_percent': 5}, headers={'X-API-Token': 'mock-token'})
            self.assertEqual(response.status_code, 200)
        self.auto.buy_qualified.assert_awaited_once_with('005930', ratio=D('.05'))
        self.assertEqual(self.engine.settings.recommended_trade_ratio, D('.15'))

    async def test_selected_order_ratio_cannot_exceed_aggregate_limit(self):
        self.auto.buy_qualified = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            response = await client.post('/api/v1/live/qualified-buy/005930',
                json={'confirm_real_order': True, 'ratio_percent': 16}, headers={'X-API-Token': 'mock-token'})
            self.assertEqual(response.status_code, 422)
        self.auto.buy_qualified.assert_not_awaited()

    async def test_concurrent_ratio_updates_preserve_both_preferences(self):
        import asyncio
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            headers = {'X-API-Token': 'mock-token'}
            responses = await asyncio.gather(
                client.put('/api/v1/settings/investment-ratio', json={'ratio_percent': 10}, headers=headers),
                client.put('/api/v1/settings/manual-investment-ratio', json={'ratio_percent': 5}, headers=headers))
            self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(await self.repository.load('trading_preferences'), {
            'recommended_trade_ratio': '0.1', 'manual_trade_ratio': '0.05'})
        self.assertEqual(self.engine.settings.recommended_trade_ratio, D('.10'))
        self.assertEqual(self.engine.settings.manual_trade_ratio, D('.05'))

    async def test_automatic_ratio_cannot_inherit_full_manual_exposure_limit(self):
        settings = replace(self.settings, live_max_total_exposure_ratio=D(1))
        self.app.state.settings = self.engine.settings = self.broker.settings = self.broker.risk.settings = settings
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            headers = {'X-API-Token': 'mock-token'}
            self.assertEqual((await client.put('/api/v1/settings/investment-ratio',
                json={'ratio_percent': 100}, headers=headers)).status_code, 422)
            self.assertEqual((await client.put('/api/v1/settings/manual-investment-ratio',
                json={'ratio_percent': 100}, headers=headers)).status_code, 200)
        self.assertEqual(self.engine.settings.recommended_trade_ratio, D('.15'))

    async def test_capital_uses_market_value_pending_buys_and_strict_boundary(self):
        self.broker.positions['005930'] = Position('005930', D(1), D(100), Currency.KRW)
        self.broker.position_market_values['005930'] = D(150000)
        self.broker.open_orders = [{'side': 'BUY', 'quantity': '1', 'price': '10000'}]
        budget, invested, remaining = self.auto.capital()
        self.assertEqual(budget, D(172500))
        self.assertEqual(invested, D(150000))
        self.assertEqual(remaining, D(12499))
        self.broker.open_orders[0]['price'] = '22500'
        self.assertEqual(self.auto.capital()[2], D(0))
        self.broker.open_orders[0].pop('price')
        self.assertEqual(self.auto.capital()[2], D(0))

    async def test_selected_live_buy_rechecks_recommendation_and_final_quote(self):
        self.broker.cash[Currency.KRW] = D(3000000)
        result = self.order()
        self.broker.place_market_order = AsyncMock(return_value=result)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            self.assertIs(await self.auto.buy_qualified('005930'), result)
        args = self.broker.place_market_order.await_args.kwargs
        self.assertEqual(args['quantity'], D(1))
        self.assertEqual(args['order_budget'], D(449999))
        self.assertIs(args['quote'], self.book)
        self.assertEqual(self.broker.reconcile.await_count, 2)
        self.recommend.assert_awaited_once()
        self.assertIn('005930', self.broker.risk.recommended_symbols)
        self.assertIn('LIVE', self.auto.message)

    async def test_unqualified_disarmed_or_over_budget_live_buy_never_submits(self):
        self.broker.place_market_order = AsyncMock()
        self.broker.risk.armed = False
        with self.assertRaisesRegex(RuntimeError, '무장'):
            await self.auto.buy_qualified('005930')
        self.broker.risk.armed = True
        with self.assertRaisesRegex(RuntimeError, '허용 종목'):
            await self.auto.buy_qualified('082740')
        self.recommend.return_value = {'candidates': [], 'watchlist': [{'symbol': '005930'}]}
        with self.assertRaisesRegex(RuntimeError, '최신 추천'):
            await self.auto.buy_qualified('005930')
        self.recommend.return_value = {'candidates': [{'symbol': '005930', 'eligible': True}]}
        self.broker.positions['005930'] = Position('005930', D(1), D(100), Currency.KRW)
        self.broker.position_market_values['005930'] = D(200000)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            with self.assertRaisesRegex(RuntimeError, '예산이 부족'):
                await self.auto.buy_qualified('005930')
        self.broker.place_market_order.assert_not_awaited()

    async def test_live_partial_buy_is_reported_as_partial_without_retry(self):
        self.broker.cash[Currency.KRW] = D(3000000)
        self.broker.place_market_order = AsyncMock(return_value=self.order(status=OrderStatus.PARTIALLY_FILLED))
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            order = await self.auto.buy_qualified('005930')
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertIn('PARTIALLY_FILLED', self.auto.message)
        self.assertEqual(self.auto.session['diagnostics']['buy_orders_filled'], 1)
        self.broker.place_market_order.assert_awaited_once()

    async def test_live_manual_sell_tracks_partial_without_resubmitting_remainder(self):
        self.broker.positions['005930'] = Position('005930', D(2), D(65000), Currency.KRW)
        self.broker.position_market_values['005930'] = D(140000)
        result = self.order(Side.SELL, OrderStatus.PARTIALLY_FILLED)
        self.broker.place_market_order = AsyncMock(return_value=result)
        orders = await self.auto.manual_close('005930')
        self.assertEqual(orders, [result])
        self.assertEqual(self.broker.place_market_order.await_args.kwargs['quantity'], D(2))
        self.broker.place_market_order.assert_awaited_once()
        self.assertTrue(self.auto.session['targets']['005930']['manual_exit'])
        self.assertIn('PARTIALLY_FILLED', self.auto.message)

    async def test_live_quote_uses_orderbook_and_rejects_stale_snapshot(self):
        self.assertIs(await self.auto.quote('005930'), self.book)
        self.client.candles.assert_not_awaited()
        self.book.timestamp -= timedelta(seconds=11)
        with self.assertRaisesRegex(RuntimeError, '오래'):
            await self.auto.quote('005930')

    async def test_transport_enforces_selected_budget_before_any_order_submission(self):
        self.broker.risk.set_recommended_symbols(['005930'])
        with self.assertRaisesRegex(RuntimeError, 'investment budget'):
            await self.broker.place_market_order(client_order_id='mock-budget', quote=self.book,
                                                  side=Side.BUY, quantity=D(1), order_budget=D(69999))
        self.client.create_order.assert_not_awaited()

    async def test_live_automatic_buy_sizes_at_ask_and_live_order_limit(self):
        self.broker.cash[Currency.KRW] = D(4000000)
        self.engine.running = True
        self.engine.settings = replace(self.settings, max_order_amount_krw=D(1000000))
        self.broker.place_market_order = AsyncMock(return_value=self.order())
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        args = self.broker.place_market_order.await_args.kwargs
        self.assertEqual(args['quantity'], D(1))
        self.assertIs(args['quote'], self.book)
        self.assertEqual(args['order_budget'], D(100000))

    async def test_live_automatic_buy_skips_symbols_outside_allowlist(self):
        self.engine.running = True
        self.recommend.return_value = {'candidates': [{'symbol': '082740', 'eligible': True}]}
        self.broker.place_market_order = AsyncMock()
        await self.auto.tick()
        self.broker.place_market_order.assert_not_awaited()
        self.client.stock_info.assert_not_awaited()

    async def test_live_exit_rechecks_final_bid_against_net_profit_gate(self):
        self.broker.positions['005930'] = Position('005930', D(1), D(69900), Currency.KRW)
        self.broker.position_market_values['005930'] = D(71500)
        await self.auto.restore()
        self.engine.running = True
        first = Quote('005930', D(72500), Currency.KRW, datetime.now(timezone.utc), 'toss', D(72500), D(72550))
        final = Quote('005930', D(71500), Currency.KRW, datetime.now(timezone.utc), 'toss', D(71500), D(71550))
        self.auto.quote = AsyncMock(side_effect=[first, final])
        self.broker.place_market_order = AsyncMock()
        self.recommend.return_value = {'candidates': []}
        baseline = {'ready': True, 'upper_touched': True, 'sell_at_upper': True,
                    'bollinger_upper': '71000', 'active_high': '72500'}
        with patch('app.swing_trader.four_hour_exit_signal', side_effect=lambda *args: dict(baseline)):
            await self.auto.tick()
        self.assertEqual(self.auto.quote.await_count, 2)
        self.broker.place_market_order.assert_not_awaited()
        target = next(day['targets']['005930'] for day in self.auto.days.values() if '005930' in day['targets'])
        self.assertEqual(target['last_exit_check']['sell_reference_price'], '71500')
        self.assertFalse(target['last_exit_check']['take_profit'])
        self.assertFalse(target['last_exit_check']['profit_trailing_armed'])

    async def test_risk_endpoint_reports_profit_policy_without_changing_allocation(self):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            result = (await client.get('/api/v1/risk/status')).json()
        self.assertEqual(result['swing_exit_policy']['min_net_profit_percent'], '3')
        self.assertEqual(result['swing_exit_policy']['estimated_sell_tax_rate'], '0.002')
        self.assertTrue(result['swing_exit_policy']['protective_exit_below_minimum'])
        self.assertEqual(result['recommended_trade_ratio'], '0.15')
        self.assertEqual(result['live_limits']['max_total_exposure_ratio'], '0.15')

    def enable_full_equity(self):
        settings = replace(self.settings, recommended_trade_ratio=D(1), live_max_total_exposure_ratio=D(1),
                           live_auto_max_total_exposure_ratio=D(1), manual_trade_ratio=D('.20'),
                           live_max_order_amount_krw=D(0), live_max_total_exposure_krw=D(0),
                           live_symbol_policy='recommended', fee_rate=D('.00015'))
        self.engine.settings = self.broker.settings = self.broker.risk.settings = settings
        self.app.state.settings = settings

    async def test_full_equity_budget_reserves_pending_buys_and_fees(self):
        self.enable_full_equity()
        self.broker.positions['005930'] = Position('005930', D(2), D(50000), Currency.KRW)
        self.broker.position_market_values['005930'] = D(200000)
        self.broker.open_orders = [{'side': 'BUY', 'quantity': '1', 'price': '50000'}]
        self.assertEqual(self.auto.capital(), (D(1200000), D(200000), D(949999)))
        self.book.symbol = '082740'
        self.recommend.return_value = {'candidates': [{'symbol': '082740', 'eligible': True}]}
        self.broker.place_market_order = AsyncMock(return_value=self.order())
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.buy_qualified('082740')
        args = self.broker.place_market_order.await_args.kwargs
        self.assertEqual(args['quantity'], D(3))
        self.assertEqual(args['order_budget'], D(240000))
        self.assertLessEqual(args['quantity'] * self.book.ask_price * D('1.00015'), args['order_budget'])

    async def test_full_equity_manual_buy_can_select_a_new_qualified_symbol(self):
        self.enable_full_equity()
        self.book.symbol = '082740'
        self.recommend.return_value = {'candidates': [{'symbol': '082740', 'eligible': True, 'currency': 'KRW'}]}
        self.broker.place_market_order = AsyncMock(return_value=self.order())
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.buy_qualified('082740')
        self.assertEqual(self.broker.place_market_order.await_args.kwargs['quantity'], D(2))
        self.assertIn('082740', self.broker.risk.recommended_symbols)

    async def test_full_equity_automatic_buy_uses_new_recommendation_and_cash_budget(self):
        self.enable_full_equity()
        self.engine.running = True
        self.book.symbol = '082740'
        self.recommend.return_value = {'candidates': [{'symbol': '082740', 'eligible': True, 'currency': 'KRW'}]}
        self.broker.place_market_order = AsyncMock(return_value=self.order())
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.assertEqual(self.broker.place_market_order.await_args.kwargs['quantity'], D(2))
        self.assertIn('082740', self.broker.risk.recommended_symbols)

    async def test_full_equity_still_checks_broker_cash_before_transport(self):
        self.enable_full_equity()
        self.broker.risk.set_recommended_symbols(['005930'])
        self.client.buying_power = AsyncMock(return_value={'cashBuyingPower': '65000'})
        with self.assertRaisesRegex(RuntimeError, 'cash buying power'):
            await self.broker.place_market_order(client_order_id='mock-no-credit', quote=self.book,
                                                  side=Side.BUY, quantity=D(1), order_budget=D(100000))
        self.client.create_order.assert_not_awaited()

    async def test_full_equity_api_accepts_one_hundred_but_rejects_higher_ratios(self):
        self.enable_full_equity()
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://mock') as client:
            path = '/api/v1/settings/investment-ratio'
            headers = {'X-API-Token': 'mock-token'}
            self.assertEqual((await client.put(path, json={'ratio_percent': 100}, headers=headers)).status_code, 200)
            self.assertEqual((await client.put(path, json={'ratio_percent': 101}, headers=headers)).status_code, 422)
        self.assertEqual((await self.repository.load('trading_preferences'))['recommended_trade_ratio'], '1')

    async def test_live_positions_show_synced_market_profit_loss(self):
        self.broker.positions['005930'] = Position('005930', D(2), D(65000), Currency.KRW)
        self.broker.position_market_values['005930'] = D(140000)
        row = self.broker.account({})['positions'][0]
        self.assertEqual(row['market_price'], '70000')
        self.assertEqual(row['unrealized_profit_loss'], '10000')
        self.assertIsNotNone(row['quote_timestamp'])

    async def test_live_startup_ignores_saved_paper_ratio_above_live_limit(self):
        await self.repository.save('trading_preferences', {'recommended_trade_ratio': '0.5'})
        client = Mock(holdings=AsyncMock(return_value={'items': []}),
                      account_orders=AsyncMock(return_value=[]),
                      buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}))
        settings = replace(self.settings, toss_client_id='mock', toss_client_secret='mock', toss_account_seq='mock')
        with patch('app.main.TossMarketClient', return_value=client):
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                self.assertEqual(app.state.settings.recommended_trade_ratio, D('.15'))
                self.assertFalse(app.state.live_broker.risk.armed)

    async def test_restart_clamps_old_full_allocation_and_restores_direct_ratio(self):
        client = Mock(holdings=AsyncMock(return_value={'items': []}),
                      account_orders=AsyncMock(return_value=[]),
                      buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}))
        settings = replace(self.settings, recommended_trade_ratio=D(1),
                           toss_client_id='mock', toss_client_secret='mock', toss_account_seq='mock')
        for saved_manual, expected in (('0.05', D('.05')), ('1', D('.15'))):
            await self.repository.save('trading_preferences', {
                'recommended_trade_ratio': '1', 'manual_trade_ratio': saved_manual})
            with patch('app.main.TossMarketClient', return_value=client):
                app = create_app(settings)
                async with app.router.lifespan_context(app):
                    self.assertEqual(app.state.settings.recommended_trade_ratio, D('.15'))
                    self.assertEqual(app.state.settings.manual_trade_ratio, expected)
                    self.assertEqual(app.state.engine.automation.buy_budget('005930'), D(30000))
                    self.assertFalse(app.state.live_broker.risk.armed)
            client.create_order.assert_not_called()

