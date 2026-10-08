"""Daily buy restriction opt-out; all account and order calls use mocks."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.brokers.toss_real import TossRealBroker
from app.models import Order, OrderStatus, Side
from app.risk import TradingControl
from tests import test_live_limits, test_live_per_order, test_swing_averaging


class DailyLossConfigTest(TestCase):
    settings = test_live_limits.LiveLimitConfigTest.settings

    def test_default_is_enabled_and_disable_requires_explicit_flag(self):
        self.assertTrue(self.settings().live_daily_loss_limit_enabled)
        configured = self.settings(LIVE_DAILY_LOSS_LIMIT_ENABLED='false')
        self.assertFalse(configured.live_daily_loss_limit_enabled)
        self.assertEqual(configured.live_max_daily_loss_krw, D(50000))

    def test_flag_accepts_explicit_boolean_values_and_rejects_typos(self):
        for value in ('false', 'FALSE', '0', 'off', 'no'):
            with self.subTest(value=value):
                self.assertFalse(self.settings(LIVE_DAILY_LOSS_LIMIT_ENABLED=value).live_daily_loss_limit_enabled)
        for value in ('true', '1', 'on', 'yes'):
            with self.subTest(value=value):
                self.assertTrue(self.settings(LIVE_DAILY_LOSS_LIMIT_ENABLED=value).live_daily_loss_limit_enabled)
        for value in ('', 'flase', 'disabled', 'maybe'):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'explicit boolean'):
                self.settings(LIVE_DAILY_LOSS_LIMIT_ENABLED=value)

    def test_disabling_does_not_allow_invalid_saved_loss_threshold(self):
        for value in ('0', '-1', 'NaN', 'Infinity'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings(LIVE_DAILY_LOSS_LIMIT_ENABLED='false', LIVE_MAX_DAILY_LOSS_KRW=value)


class DailyLossRiskTest(TestCase):
    def setUp(self):
        test_live_limits.FullEquityRiskTest.setUp(self)
        self.settings = replace(test_live_per_order.per_order_settings(self.settings),
                                live_daily_loss_limit_enabled=False)
        self.risk.settings = self.settings
        self.risk.daily_loss = D(1000000)
        self.args.update(quantity=D(1), price=D(100000), total_exposure=D(1500000))

    def test_large_daily_loss_alone_does_not_block_buy_when_disabled(self):
        self.assertFalse(self.risk.daily_loss_limit_reached)
        self.assertTrue(self.risk.validate(**self.args).allowed)
        self.assertEqual(self.risk.daily_loss, D(1000000))

    def test_reenabling_immediately_uses_preserved_loss(self):
        self.risk.settings = replace(self.settings, live_daily_loss_limit_enabled=True)
        self.assertTrue(self.risk.daily_loss_limit_reached)
        self.assertEqual(self.risk.validate(**self.args).rule, 'daily-loss')

    def test_per_buy_aggregate_position_quote_and_opposite_order_guards_remain(self):
        cases = [({'quantity': D(8)}, 'max-buy-ratio'),
                 ({'total_exposure': D(3700000)}, 'max-equity-exposure'),
                 ({'position_count': 5}, 'max-positions'),
                 ({'quote_at': datetime.now(timezone.utc) - timedelta(seconds=11)}, 'stale-quote'),
                 ({'has_opposite_open_order': True}, 'opposite-open-order'),
                 ({'warning': True}, 'stock-warning'),
                 ({'current_equity': D(0)}, 'equity-unavailable')]
        for changed, expected in cases:
            with self.subTest(rule=expected):
                self.assertEqual(self.risk.validate(**{**self.args, **changed}).rule, expected)

    def test_arming_reconciliation_recommendation_and_manual_controls_remain(self):
        self.risk.armed = False
        self.assertEqual(self.risk.validate(**self.args).rule, 'live-lock')
        self.risk.armed = True
        self.risk.reconciled = False
        self.assertEqual(self.risk.validate(**self.args).rule, 'reconciliation')
        self.risk.reconciled = True
        self.risk.control = TradingControl.BUY_PAUSED
        self.assertEqual(self.risk.validate(**self.args).rule, 'buy-paused')
        self.risk.control = TradingControl.ALL_NEW_ORDERS_PAUSED
        self.assertEqual(self.risk.validate(**self.args).rule, 'trading-control')
        self.risk.control = TradingControl.ACTIVE
        self.risk.clear_recommended_symbols()
        self.assertEqual(self.risk.validate(**self.args).rule, 'recommendation')

    def test_loss_policy_does_not_change_sell_eligibility(self):
        for enabled in (True, False):
            self.risk.settings = replace(self.settings, live_daily_loss_limit_enabled=enabled)
            self.assertTrue(self.risk.validate(**{**self.args, 'side': Side.SELL}).allowed)


class DailyLossExecutionTest(IsolatedAsyncioTestCase):
    asyncTearDown = test_live_per_order.PerOrderAllocationTest.asyncTearDown

    async def asyncSetUp(self):
        await test_live_per_order.PerOrderAllocationTest.asyncSetUp(self)
        self.settings = replace(self.settings, live_daily_loss_limit_enabled=False)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        self.broker.risk.daily_loss = D(1000000)

    async def test_status_removes_daily_loss_block_but_retains_engine_and_arm_blocks(self):
        self.engine.running = False
        self.broker.risk.armed = False
        reasons = self.auto.status()['buy_block_reasons']
        self.assertNotIn('일일 손실 한도 도달', reasons)
        self.assertIn('자동매매 엔진 중지', reasons)
        self.assertIn('LIVE 무장 해제', reasons)

    async def test_final_mock_transport_accepts_buy_and_keeps_loss_record(self):
        self.client.create_order.return_value = {'orderId': 'mock-daily-loss'}
        result = await self.broker.place_order({
            'clientOrderId': 'mock-daily-buy', 'symbol': '082740', 'side': 'BUY',
            'quantity': '7', 'orderType': 'MARKET', 'quote': self.book,
            'orderBudget': '750000', 'returnAfterAccept': True})
        self.assertEqual(result['status'], 'ACCEPTED')
        self.client.create_order.assert_awaited_once()
        self.assertEqual(self.broker.risk.daily_loss, D(1000000))
        self.assertFalse(self.broker.risk.armed)

    async def test_final_mock_transport_still_rejects_oversized_buy(self):
        with self.assertRaisesRegex(RuntimeError, 'max-buy-ratio'):
            await self.broker.place_order({'clientOrderId': 'mock-oversized', 'symbol': '082740',
                'side': 'BUY', 'quantity': '8', 'orderType': 'MARKET', 'quote': self.book})
        self.client.create_order.assert_not_awaited()

    async def test_automatic_new_buy_passes_mock_risk_boundary_at_fifteen_percent(self):
        self.engine.running = True
        async def fill(**args):
            quote = args['quote']
            decision = self.broker.risk.validate(symbol=quote.symbol, side=Side.BUY,
                quantity=args['quantity'], price=quote.ask_price, total_exposure=D(0),
                current_equity=D(5000000), position_count=0, quote_at=quote.timestamp)
            self.assertTrue(decision.allowed)
            self.assertEqual(args['quantity'], D(7))
            self.assertEqual(args['order_budget'], D(750000))
            return Order('mock-order', args['client_order_id'], quote.symbol, Side.BUY, D(7),
                         quote.ask_price, quote.ask_price, quote.currency, OrderStatus.FILLED,
                         D(105), None, datetime.now(timezone.utc))
        self.broker.place_market_order = AsyncMock(side_effect=fill)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.broker.place_market_order.assert_awaited_once()
        self.client.create_order.assert_not_awaited()

    async def test_automatic_buy_window_still_blocks_order(self):
        self.engine.running = True
        self.now = self.now.replace(hour=11)
        self.broker.place_market_order = AsyncMock()
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await self.auto.tick()
        self.broker.place_market_order.assert_not_awaited()
        self.assertIn('자동매수 시간 외', self.auto.status()['buy_block_reasons'])

    async def test_restart_preserves_baseline_and_calculates_loss_even_when_disabled(self):
        self.broker.daily_equity_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        self.broker.daily_equity_start = D(5000000)
        await self.broker._persist_live_state()
        self.client.holdings = AsyncMock(return_value={'items': []})
        self.client.buying_power.side_effect = [{'cashBuyingPower': '4940000'}, {'cashBuyingPower': '0'}]
        restored = TossRealBroker(self.client, 'mock-account', self.repo, self.settings)
        await restored.restore()
        self.assertFalse(restored.risk.armed)
        self.assertTrue((await restored.reconcile())['reconciled'])
        self.assertEqual(restored.daily_equity_start, D(5000000))
        self.assertEqual(restored.risk.daily_loss, D(60000))
        self.assertFalse(restored.risk.daily_loss_limit_reached)
        self.client.create_order.assert_not_awaited()


class DailyLossAveragingTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_swing_averaging.AveragingTest.asyncSetUp
    asyncTearDown = test_swing_averaging.AveragingTest.asyncTearDown
    mock_fill = test_swing_averaging.AveragingTest.mock_fill
    tick = test_swing_averaging.AveragingTest.tick

    async def test_disabled_daily_loss_allows_only_one_mock_averaging_attempt(self):
        self.settings = replace(self.settings, live_daily_loss_limit_enabled=False)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        self.broker.risk.daily_loss = D(1000000)
        self.mock_fill()
        await self.tick()
        await self.tick()
        self.broker.place_averaging_order.assert_awaited_once()
        self.assertFalse(self.broker.risk.daily_loss_limit_reached)
        self.assertEqual(self.broker.risk.daily_loss, D(1000000))


class DailyLossApiTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_live_per_order.PerOrderDashboardTest.asyncSetUp
    asyncTearDown = test_live_per_order.PerOrderDashboardTest.asyncTearDown

    async def test_risk_status_reports_disabled_policy_and_preserved_loss(self):
        self.settings = replace(self.settings, live_daily_loss_limit_enabled=False)
        self.app.state.settings = self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        self.broker.risk.daily_loss = D(53554)
        self.broker.daily_equity_date = '2026-10-08'
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            result = (await client.get('/api/v1/risk/status')).json()['live_limits']
        self.assertFalse(result['daily_loss_limit_enabled'])
        self.assertFalse(result['daily_loss_limit_reached'])
        self.assertEqual(result['daily_loss_krw'], '53554')
        self.assertEqual(result['max_daily_loss_krw'], '50000')
        self.assertEqual(result['daily_equity_date'], '2026-10-08')
        self.assertEqual(result['max_buy_ratio'], '0.15')
        self.assertEqual(result['max_total_exposure_ratio'], '0.75')
        self.client.create_order.assert_not_called()

    async def test_risk_status_default_still_reports_reached_loss_limit(self):
        self.broker.risk.daily_loss = D(50000)
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            result = (await client.get('/api/v1/risk/status')).json()['live_limits']
        self.assertTrue(result['daily_loss_limit_enabled'])
        self.assertTrue(result['daily_loss_limit_reached'])
