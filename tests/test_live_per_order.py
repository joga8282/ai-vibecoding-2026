"""15% per buy / 75% aggregate policy; account and order transports are mocked."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.models import Currency, Order, OrderStatus, Position, Quote, Side
from app.risk import LiveRiskManager
from tests import test_live_allocation, test_live_dashboard, test_live_limits


def per_order_settings(settings, **changes):
    return replace(settings, live_auto_allocation_mode='per_order', live_max_buy_ratio=D('.15'),
                   recommended_trade_ratio=D('.15'), manual_trade_ratio=D('.15'),
                   live_max_total_exposure_ratio=D('.75'), live_auto_max_total_exposure_ratio=D('.75'),
                   live_max_order_amount_krw=D(0), live_max_total_exposure_krw=D(0), **changes)


class PerOrderConfigTest(TestCase):
    settings = test_live_limits.LiveLimitConfigTest.settings

    def test_explicit_mode_separates_per_buy_and_aggregate_limits(self):
        settings = self.settings(LIVE_AUTO_ALLOCATION_MODE='per_order', LIVE_MAX_BUY_RATIO='.15',
                                 LIVE_MAX_TOTAL_EXPOSURE_RATIO='.75', LIVE_AUTO_MAX_TOTAL_EXPOSURE_RATIO='.75')
        self.assertEqual(settings.live_auto_allocation_mode, 'per_order')
        self.assertEqual(settings.live_auto_ratio_limit, D('.15'))
        self.assertEqual(settings.live_manual_ratio_limit, D('.15'))
        self.assertEqual(settings.live_max_total_exposure_ratio, D('.75'))

    def test_absent_mode_preserves_legacy_equal_slots(self):
        self.assertEqual(self.settings().live_auto_allocation_mode, 'equal_slots')

    def test_invalid_mode_and_per_buy_limits_fail_startup(self):
        for changes in ({'LIVE_AUTO_ALLOCATION_MODE': 'all_cash'},
                        *({'LIVE_MAX_BUY_RATIO': value} for value in ('NaN', 'Infinity', '0', '1.01', 'abc'))):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.settings(**changes)


class PerOrderRiskTest(TestCase):
    def setUp(self):
        legacy = test_live_limits.FullEquityRiskTest()
        legacy.setUp()
        self.settings = per_order_settings(legacy.settings)
        self.risk = LiveRiskManager(self.settings)
        self.risk.armed = self.risk.reconciled = True
        self.risk.set_recommended_symbols(['082740'])
        self.args = {**legacy.args, 'quantity': D(7), 'price': D(100000)}

    def test_fees_count_toward_fifteen_percent_for_raw_orders(self):
        self.assertTrue(self.risk.validate(**self.args).allowed)
        self.assertEqual(self.risk.validate(**{**self.args, 'quantity': D(8)}).rule, 'max-buy-ratio')
        # Even a notional exactly at 15% exceeds the limit after fees.
        self.assertEqual(self.risk.validate(**{**self.args, 'quantity': D(1), 'price': D(750000)}).rule,
                         'max-buy-ratio')
        self.risk.settings = replace(self.settings, fee_rate=D(0))
        self.assertTrue(self.risk.validate(**{**self.args, 'quantity': D(1), 'price': D(750000)}).allowed)

    def test_aggregate_limit_counts_fees_and_existing_pending_exposure(self):
        self.assertEqual(self.risk.validate(**{**self.args, 'total_exposure': D(3050000)}).rule,
                         'max-equity-exposure')
        self.assertTrue(self.risk.validate(**{**self.args, 'total_exposure': D(3000000)}).allowed)

    def test_sell_is_not_restricted_by_per_buy_or_aggregate_limits(self):
        self.risk.clear_recommended_symbols()
        self.assertTrue(self.risk.validate(**{**self.args, 'side': Side.SELL, 'quantity': D(50),
                                             'total_exposure': D(5000000)}).allowed)

    def test_malformed_buy_limits_fail_closed(self):
        for changes in ({'live_max_buy_ratio': D('NaN')}, {'live_max_buy_ratio': D(0)},
                        {'fee_rate': D('NaN')}, {'fee_rate': D('-1')}):
            with self.subTest(changes=changes):
                self.risk.settings = replace(self.settings, **changes)
                self.assertEqual(self.risk.validate(**self.args).rule, 'invalid-limits')

    def test_fresh_recommendation_and_arming_are_still_required(self):
        self.risk.armed = False
        self.assertEqual(self.risk.validate(**self.args).rule, 'live-lock')
        self.risk.armed = True
        self.risk.clear_recommended_symbols()
        self.assertEqual(self.risk.validate(**self.args).rule, 'recommendation')


class PerOrderAllocationTest(IsolatedAsyncioTestCase):
    asyncTearDown = test_live_allocation.LiveAllocationTest.asyncTearDown

    async def asyncSetUp(self):
        await test_live_allocation.LiveAllocationTest.asyncSetUp(self)
        self.settings = per_order_settings(self.settings)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings

    def holding(self, value, symbol='005930'):
        self.broker.positions[symbol] = Position(symbol, D(1), D(value), Currency.KRW)
        self.broker.position_market_values[symbol] = D(value)

    async def test_existing_thirty_percent_holdings_allow_a_fifteen_percent_new_buy(self):
        self.broker.cash[Currency.KRW] = D(3500000)
        self.holding(1500000)
        self.assertEqual(self.auto.capital(), (D(3750000), D(1500000), D(2249999)))
        self.assertEqual(self.auto.buy_budget('082740'), D(750000))
        self.assertEqual(self.auto.manual_buy_budget('082740'), D(750000))
        self.assertEqual(self.auto.buy_budget('005930'), D(0))
        self.client.create_order.assert_not_awaited()

    async def test_auto_selected_ratio_can_lower_but_not_exceed_per_buy_limit(self):
        self.engine.settings = replace(self.settings, recommended_trade_ratio=D('.10'))
        self.assertEqual(self.auto.buy_budget('082740'), D(500000))
        self.assertEqual(self.auto.capital()[0], D(3750000))
        self.engine.settings = replace(self.settings, recommended_trade_ratio=D(1))
        self.assertEqual(self.auto.buy_budget('082740'), D(750000))

    async def test_partial_pending_buys_reserve_remaining_notional_fees_and_slots(self):
        self.broker.cash[Currency.KRW] = D(2000000)
        self.holding(3000000)
        self.broker.open_orders = [{'symbol': '012450', 'side': 'BUY', 'quantity': '10',
                                   'price': '100000', 'execution': {'filledQuantity': '8'}}]
        self.assertEqual(self.auto.buy_budget('082740'), D(549969))
        self.assertEqual(self.auto.manual_buy_budget('082740'), D(549969))
        self.assertEqual(self.auto.buy_budget('012450'), D(0))
        for symbol in ('006400', '357780', '000660'):
            self.broker.open_orders.append({'symbol': symbol, 'side': 'BUY', 'quantity': '1', 'price': '1'})
        self.assertEqual(self.auto.buy_budget('082740'), D(0))
        self.assertIn('보유·미체결 매수 합계 최대 5종목 도달', self.auto.status()['buy_block_reasons'])

    async def test_unknown_pending_amount_blocks_buys_with_visible_reason(self):
        self.broker.open_orders = [{'symbol': '012450', 'side': 'BUY', 'quantity': '1'}]
        self.assertEqual(self.auto.buy_budget('082740'), D(0))
        self.assertIn('미체결 매수 금액 확인 필요', self.auto.status()['buy_block_reasons'])

    async def test_above_seventy_five_percent_only_blocks_new_buys(self):
        self.broker.cash[Currency.KRW] = D(1000000)
        self.holding(4000000)
        self.assertEqual(self.auto.buy_budget('082740'), D(0))
        self.assertEqual(self.auto.manual_buy_budget('082740'), D(0))
        self.assertIn('보유 평가액·미체결 매수가 합산 투자 한도에 도달', self.auto.status()['buy_block_reasons'])
        self.assertEqual(self.broker.positions['005930'].quantity, D(1))
        self.client.create_order.assert_not_awaited()

    async def test_direct_slider_ratio_does_not_inherit_seventy_five_percent_limit(self):
        for ratio in (D('.16'), D('.75'), D('NaN')):
            with self.subTest(ratio=ratio), self.assertRaisesRegex(RuntimeError, '직접 매수 비율'):
                await self.auto.buy_qualified('082740', ratio=ratio)
        self.broker.reconcile.assert_not_awaited()
        self.assertEqual(self.auto.manual_buy_budget('082740', D('.05')), D(250000))

    async def test_expensive_stock_is_skipped_and_next_stock_buys_seven_shares(self):
        self.engine.running = True
        self.recommend.return_value = {'candidates': [{'symbol': '012450', 'eligible': True},
                                                     {'symbol': '082740', 'eligible': True}]}
        async def quotes(symbols):
            price = D(2000000) if symbols[0] == '012450' else D(100000)
            return [Quote(symbols[0], price, Currency.KRW, datetime.now(timezone.utc), 'toss', price - 100, price)]
        self.broker._fresh_risk_quotes.side_effect = quotes
        self.broker.place_market_order = AsyncMock(return_value=Order(
            'mock-order', 'mock-client', '082740', Side.BUY, D(7), D(100000), D(100000),
            Currency.KRW, OrderStatus.FILLED, D(105), None, datetime.now(timezone.utc)))
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        args = self.broker.place_market_order.await_args.kwargs
        self.assertEqual(args['quote'].symbol, '082740')
        self.assertEqual(args['quantity'], D(7))
        self.assertEqual(args['order_budget'], D(750000))
        self.assertNotIn('012450', self.auto.session['targets'])

    async def test_five_fills_keep_each_buy_below_fifteen_and_total_below_seventy_five(self):
        self.engine.running = True
        symbols = ('005930', '082740', '012450', '357780', '006400', '000660')
        self.recommend.return_value = {'candidates': [{'symbol': s, 'eligible': True} for s in symbols]}
        costs = []
        async def fill(**kwargs):
            equity = D(self.broker.account({})['total_equity']['KRW'])
            quote, qty = kwargs['quote'], kwargs['quantity']
            amount = qty * quote.ask_price
            fee = amount * self.settings.fee_rate
            costs.append(amount + fee)
            self.assertLessEqual(amount + fee, equity * D('.15'))
            self.assertLessEqual(amount + fee, kwargs['order_budget'])
            self.broker.cash[Currency.KRW] -= amount + fee
            self.broker.positions[quote.symbol] = Position(quote.symbol, qty, quote.ask_price, Currency.KRW)
            self.broker.position_market_values[quote.symbol] = amount
            return Order('mock-' + quote.symbol, kwargs['client_order_id'], quote.symbol, Side.BUY,
                         qty, quote.ask_price, quote.ask_price, Currency.KRW,
                         OrderStatus.FILLED, fee, None, datetime.now(timezone.utc))
        self.broker.place_market_order = AsyncMock(side_effect=fill)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.assertEqual(len(costs), 5)
        self.assertLess(sum(costs), D(5000000) * D('.75'))
        self.assertNotIn('000660', self.broker.positions)
        self.assertGreaterEqual(self.broker.cash[Currency.KRW], D(0))

    async def test_final_transport_rejects_oversized_raw_buy_without_budget(self):
        with self.assertRaisesRegex(RuntimeError, 'max-buy-ratio'):
            await self.broker.place_order({'clientOrderId': 'mock', 'symbol': '082740', 'side': 'BUY',
                                           'quantity': '8', 'orderType': 'MARKET', 'quote': self.book})
        self.client.create_order.assert_not_awaited()
        self.assertFalse(self.broker.order_journal)

    async def test_final_transport_counts_pending_symbol_slots(self):
        self.holding(1)
        self.broker.open_orders = [{'symbol': symbol, 'side': 'BUY', 'quantity': '1', 'price': '1'}
                                   for symbol in ('006400', '357780', '000660', '012450')]
        with self.assertRaisesRegex(RuntimeError, 'max-positions'):
            await self.broker.place_order({'clientOrderId': 'mock', 'symbol': '082740', 'side': 'BUY',
                                           'quantity': '1', 'orderType': 'MARKET', 'quote': self.book})
        self.client.create_order.assert_not_awaited()

    async def test_final_transport_rejects_additional_buy_in_held_symbol(self):
        self.holding(100000, '082740')
        with self.assertRaisesRegex(RuntimeError, '추가 매수'):
            await self.broker.place_order({'clientOrderId': 'mock', 'symbol': '082740', 'side': 'BUY',
                                           'quantity': '1', 'orderType': 'MARKET', 'quote': self.book})
        self.client.create_order.assert_not_awaited()

    async def test_status_distinguishes_engine_arm_and_funding_blocks(self):
        self.engine.running = False
        self.broker.risk.armed = False
        reasons = self.auto.status()['buy_block_reasons']
        self.assertIn('자동매매 엔진 중지', reasons)
        self.assertIn('LIVE 무장 해제', reasons)
        self.assertNotIn('보유 평가액·미체결 매수가 합산 투자 한도에 도달', reasons)


class PerOrderDashboardTest(IsolatedAsyncioTestCase):
    asyncTearDown = test_live_dashboard.LiveDashboardTest.asyncTearDown

    async def asyncSetUp(self):
        await test_live_dashboard.LiveDashboardTest.asyncSetUp(self)
        self.settings = per_order_settings(self.settings)
        self.app.state.settings = self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings

    async def test_ratio_endpoints_cap_both_sliders_at_fifteen(self):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            for path in ('/api/v1/settings/investment-ratio', '/api/v1/settings/manual-investment-ratio'):
                self.assertEqual((await client.put(path, json={'ratio_percent': 75},
                    headers={'X-API-Token': 'mock-token'})).status_code, 422)
                self.assertEqual((await client.put(path, json={'ratio_percent': 15},
                    headers={'X-API-Token': 'mock-token'})).status_code, 200)
            result = (await client.get('/api/v1/risk/status')).json()
        self.assertEqual(result['live_limits']['allocation_mode'], 'per_order')
        self.assertEqual(result['live_limits']['max_buy_ratio'], '0.15')
        self.assertEqual(result['live_limits']['max_total_exposure_ratio'], '0.75')
        self.client.create_order.assert_not_called()

    async def test_restart_ignores_old_full_ratios_and_preserves_direct_five_percent(self):
        await self.repository.save('trading_preferences', {'recommended_trade_ratio': '1', 'manual_trade_ratio': '.05'})
        client = Mock(holdings=AsyncMock(return_value={'items': []}), account_orders=AsyncMock(return_value=[]),
                      buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}))
        settings = replace(self.settings, recommended_trade_ratio=D(1), toss_client_id='mock',
                           toss_client_secret='mock', toss_account_seq='mock')
        with patch('app.main.TossMarketClient', return_value=client):
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                self.assertEqual(app.state.settings.recommended_trade_ratio, D('.15'))
                self.assertEqual(app.state.settings.manual_trade_ratio, D('.05'))
                self.assertEqual(app.state.engine.automation.capital()[0], D(750000))
                self.assertEqual(app.state.engine.automation.buy_budget('005930'), D(150000))
                self.assertFalse(app.state.live_broker.risk.armed)
                self.assertFalse(app.state.engine.running)
        client.create_order.assert_not_called()
