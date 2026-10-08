"""Cash reserve and remaining-slot allocation; all broker calls are mocked."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.models import Currency, Order, OrderStatus, Position, Side
from tests import test_live_limits, test_live_per_order, test_swing_averaging


def cash_settings(settings, **changes):
    options = dict(live_min_cash_ratio=D('.30'), live_max_buy_ratio=D(1),
                   live_max_total_exposure_ratio=D('.70'), live_auto_max_total_exposure_ratio=D('.70'),
                   live_auto_allocation_mode='per_order', live_auto_budget_split='remaining_slots',
                   recommended_trade_ratio=D('.70'), live_daily_loss_limit_enabled=False)
    options.update(changes)
    return replace(settings, **options)


class CashReserveConfigTest(TestCase):
    settings = test_live_limits.LiveLimitConfigTest.settings

    def test_default_preserves_legacy_funding(self):
        configured = self.settings()
        self.assertEqual(configured.live_min_cash_ratio, D(0))
        self.assertEqual(configured.live_auto_budget_split, 'per_buy')
        self.assertEqual(configured.live_exposure_ratio_limit, configured.live_max_total_exposure_ratio)

    def test_cash_reserve_caps_both_ratio_sliders_and_aggregate_budgets(self):
        configured = self.settings(LIVE_MIN_CASH_RATIO='.30', LIVE_MAX_BUY_RATIO='1',
            LIVE_MAX_TOTAL_EXPOSURE_RATIO='1', LIVE_AUTO_MAX_TOTAL_EXPOSURE_RATIO='1',
            LIVE_AUTO_ALLOCATION_MODE='per_order', LIVE_AUTO_BUDGET_SPLIT='remaining_slots')
        self.assertEqual(configured.live_manual_ratio_limit, D('.70'))
        self.assertEqual(configured.live_auto_ratio_limit, D('.70'))
        self.assertEqual(configured.live_auto_exposure_ratio_limit, D('.70'))

    def test_invalid_cash_ratio_or_allocation_fails_startup(self):
        for value in ('NaN', 'Infinity', '-.3', '1', '1.01', 'bad'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings(LIVE_MIN_CASH_RATIO=value)
        with self.assertRaisesRegex(RuntimeError, 'per_order'):
            self.settings(LIVE_AUTO_BUDGET_SPLIT='remaining_slots')
        with self.assertRaisesRegex(RuntimeError, 'LIVE_AUTO_BUDGET_SPLIT'):
            self.settings(LIVE_AUTO_BUDGET_SPLIT='all_cash')


class CashReserveRiskTest(TestCase):
    def setUp(self):
        test_live_limits.FullEquityRiskTest.setUp(self)
        self.settings = cash_settings(self.settings, live_max_total_exposure_ratio=D(1))
        self.risk.settings = self.settings
        self.args.update(price=D(1000000), quantity=D(1), total_exposure=D(1000000))

    def test_buy_can_exceed_old_fifteen_percent_without_using_cash_reserve(self):
        self.assertGreater(self.args['price'], self.args['current_equity'] * D('.15'))
        self.assertTrue(self.risk.validate(**self.args).allowed)

    def test_fees_and_pending_exposure_cannot_spend_reserved_cash(self):
        result = self.risk.validate(**{**self.args, 'price': D(2500000)})
        self.assertEqual(result.rule, 'cash-reserve')
        result = self.risk.validate(**{**self.args, 'total_exposure': D(2500000)})
        self.assertEqual(result.rule, 'cash-reserve')
        self.assertTrue(self.risk.validate(**{**self.args, 'price': D(2499000)}).allowed)

    def test_invalid_reserve_fails_closed(self):
        for value in (D('NaN'), D('Infinity'), D(-1), D(1)):
            self.risk.settings = replace(self.settings, live_min_cash_ratio=value)
            self.assertEqual(self.risk.validate(**self.args).rule, 'invalid-limits')

    def test_above_reserve_threshold_does_not_block_sell(self):
        self.assertTrue(self.risk.validate(**{**self.args, 'total_exposure': D(4500000), 'side': Side.SELL}).allowed)


class CashReserveAllocationTest(IsolatedAsyncioTestCase):
    asyncTearDown = test_live_per_order.PerOrderAllocationTest.asyncTearDown
    holding = test_live_per_order.PerOrderAllocationTest.holding

    async def asyncSetUp(self):
        await test_live_per_order.PerOrderAllocationTest.asyncSetUp(self)
        self.settings = cash_settings(self.settings)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings

    async def test_empty_portfolio_splits_seventy_percent_across_five_remaining_places(self):
        budget, invested, remaining = self.auto.capital()
        self.assertEqual(budget, D(3500000))
        self.assertEqual(invested, D(0))
        self.assertEqual(remaining, D(3499999))
        self.assertEqual(self.auto.buy_budget('082740'), remaining / 5)
        self.assertEqual(self.auto.status()['remaining_allocation_slots'], 5)

    async def test_remaining_one_place_may_exceed_fifteen_percent(self):
        self.broker.cash[Currency.KRW] = D(4000000)
        for symbol in ('005930', '012450', '357780', '006400'):
            self.holding(250000, symbol)
        self.assertEqual(self.auto.buy_budget('082740'), D(2499999))
        self.assertEqual(self.auto.status()['remaining_allocation_slots'], 1)
        self.assertEqual(self.auto.buy_budget('005930'), D(0))

    async def test_pending_symbols_amounts_and_fees_reserve_funding_and_places(self):
        self.broker.cash[Currency.KRW] = D(4000000)
        self.holding(1000000)
        self.broker.open_orders = [{'symbol': '012450', 'side': 'BUY', 'quantity': '5', 'price': '100000',
                                   'execution': {'filledQuantity': '2'}}]
        self.assertEqual(self.auto.capital()[2], D('2199954'))
        self.assertEqual(self.auto.buy_budget('082740'), D('2199954') / 3)
        self.assertEqual(self.auto.status()['remaining_allocation_slots'], 3)
        self.assertEqual(self.auto.buy_budget('012450'), D(0))

    async def test_manual_buy_uses_selected_ratio_and_preserves_thirty_percent_cash(self):
        self.broker.cash[Currency.KRW] = D(4000000)
        self.holding(1000000)
        self.assertEqual(self.auto.manual_buy_budget('082740', D('.5')), D(2499999))
        self.assertEqual(self.auto.manual_buy_budget('082740', D('.1')), D(500000))
        with self.assertRaises(RuntimeError):
            self.auto.manual_buy_budget('082740', D('.71'))

    async def test_existing_over_seventy_percent_blocks_new_buy_without_selling(self):
        self.broker.cash[Currency.KRW] = D(1000000)
        self.holding(4000000)
        self.assertEqual(self.auto.buy_budget('082740'), D(0))
        self.assertEqual(self.auto.manual_buy_budget('082740', D('.7')), D(0))
        self.assertIn('총자산 대비 현금 유지 기준으로 신규 매수 예산 없음', self.auto.status()['buy_block_reasons'])
        self.assertEqual(self.broker.positions['005930'].quantity, D(1))
        self.client.create_order.assert_not_awaited()

    async def test_five_symbols_fill_without_spending_cash_reserve(self):
        self.engine.running = True
        symbols = ('005930', '082740', '012450', '357780', '006400', '000660')
        self.recommend.return_value = {'candidates': [{'symbol': symbol, 'eligible': True} for symbol in symbols]}
        async def fill(**args):
            quote, quantity = args['quote'], args['quantity']
            equity = D(self.broker.account({})['total_equity']['KRW'])
            cost = quantity * quote.ask_price * (1 + self.settings.fee_rate)
            self.assertLessEqual(cost, args['order_budget'])
            self.broker.cash[Currency.KRW] -= cost
            self.broker.positions[quote.symbol] = Position(quote.symbol, quantity, quote.ask_price, Currency.KRW)
            self.broker.position_market_values[quote.symbol] = quantity * quote.ask_price
            self.assertGreaterEqual(self.broker.cash[Currency.KRW], equity * D('.3'))
            return Order('mock-' + quote.symbol, args['client_order_id'], quote.symbol, Side.BUY, quantity,
                         quote.ask_price, quote.ask_price, Currency.KRW, OrderStatus.FILLED,
                         cost - quantity * quote.ask_price, None, datetime.now(timezone.utc))
        self.broker.place_market_order = AsyncMock(side_effect=fill)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.assertEqual(self.broker.place_market_order.await_count, 5)
        self.assertNotIn('000660', self.broker.positions)
        self.assertGreaterEqual(self.broker.cash[Currency.KRW], D(self.broker.account({})['total_equity']['KRW']) * D('.3'))
        self.client.create_order.assert_not_awaited()

    async def test_final_transport_rechecks_cash_reserve_and_does_not_submit(self):
        self.holding(3300000, '005930')
        self.broker.cash[Currency.KRW] = D(1700000)
        self.client.buying_power.return_value = {'cashBuyingPower': '1700000'}
        with self.assertRaisesRegex(RuntimeError, 'cash-reserve'):
            await self.broker.place_order({'clientOrderId': 'mock-over-reserve', 'symbol': '082740',
                'side': 'BUY', 'quantity': '3', 'orderType': 'MARKET', 'quote': self.book})
        self.client.create_order.assert_not_awaited()

    async def test_final_mock_transport_accepts_over_fifteen_percent_with_cash_reserved(self):
        self.client.create_order.return_value = {'orderId': 'mock-cash-reserve'}
        result = await self.broker.place_order({'clientOrderId': 'mock-with-reserve', 'symbol': '082740',
            'side': 'BUY', 'quantity': '10', 'orderType': 'MARKET', 'quote': self.book,
            'orderBudget': '1000150', 'returnAfterAccept': True})
        self.assertEqual(result['status'], 'ACCEPTED')
        self.client.create_order.assert_awaited_once()


class CashReserveAveragingTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_swing_averaging.AveragingTest.asyncSetUp
    asyncTearDown = test_swing_averaging.AveragingTest.asyncTearDown

    async def test_averaging_uses_new_cash_reserve_and_original_cost_target(self):
        self.settings = cash_settings(self.settings)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        self.broker.cash[Currency.KRW] = D(1150)
        self.assertEqual(self.auto.averaging.budget(D('1000.15')), D(549))
        self.broker.cash[Currency.KRW] = D(350)
        self.assertEqual(self.auto.averaging.budget(D('1000.15')), D(0))
        self.broker.cash[Currency.KRW] = D(10000)
        self.assertEqual(self.auto.averaging.budget(D('1000.15')), D('1000.15'))


class CashReserveApiTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_live_per_order.PerOrderDashboardTest.asyncSetUp
    asyncTearDown = test_live_per_order.PerOrderDashboardTest.asyncTearDown

    async def test_api_reports_policy_and_permits_manual_ratio_above_fifteen(self):
        self.settings = cash_settings(self.settings)
        self.app.state.settings = self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            result = (await client.get('/api/v1/risk/status')).json()['live_limits']
            response = await client.put('/api/v1/settings/manual-investment-ratio',
                json={'ratio_percent': 50}, headers={'X-API-Token': 'mock-token'})
            over = await client.put('/api/v1/settings/manual-investment-ratio',
                json={'ratio_percent': 71}, headers={'X-API-Token': 'mock-token'})
        self.assertEqual(result['min_cash_ratio'], '0.30')
        self.assertEqual(result['effective_total_exposure_ratio'], '0.70')
        self.assertEqual(result['budget_split'], 'remaining_slots')
        self.assertEqual(result['max_buy_ratio'], '1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(over.status_code, 422)
        self.client.create_order.assert_not_called()
