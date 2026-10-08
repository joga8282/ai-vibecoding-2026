"""Optional buy windows; account, market data and order submission are mocked."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.models import Currency, Order, OrderStatus, Quote, Side
from app.swing_trader import SwingTrader
from tests import test_cash_reserve, test_live_limits, test_live_per_order, test_swing_averaging


class BuyWindowConfigTest(TestCase):
    settings = test_live_limits.LiveLimitConfigTest.settings

    def test_default_preserves_windows_and_explicit_false_disables_them(self):
        self.assertTrue(self.settings().swing_buy_window_limit_enabled)
        for value in ('false', 'FALSE', '0', 'off', 'no'):
            with self.subTest(value=value):
                self.assertFalse(self.settings(SWING_BUY_WINDOW_LIMIT_ENABLED=value).swing_buy_window_limit_enabled)
        self.assertTrue(self.settings(SWING_BUY_WINDOW_LIMIT_ENABLED='true').swing_buy_window_limit_enabled)

    def test_invalid_flag_cannot_silently_remove_windows(self):
        for value in ('flase', '', 'all', 'disabled'):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'explicit boolean'):
                self.settings(SWING_BUY_WINDOW_LIMIT_ENABLED=value)


class UnrestrictedBuyWindowTest(IsolatedAsyncioTestCase):
    asyncTearDown = test_live_per_order.PerOrderAllocationTest.asyncTearDown

    async def asyncSetUp(self):
        await test_live_per_order.PerOrderAllocationTest.asyncSetUp(self)
        self.settings = test_cash_reserve.cash_settings(self.settings, swing_buy_window_limit_enabled=False)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        self.engine.running = True

    async def test_buy_window_matches_supported_sessions_at_exact_boundaries(self):
        for hour, minute, expected in (
            (8, 59, False), (9, 0, True), (9, 59, True), (10, 0, True), (10, 30, True),
            (11, 59, True), (12, 0, True), (13, 59, True), (14, 0, True), (15, 29, True),
            (15, 30, False), (15, 59, False), (16, 0, True), (19, 59, True), (20, 0, False),
        ):
            self.now = self.now.replace(hour=hour, minute=minute)
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(self.auto.buy_window_open(), expected)
                self.assertEqual(self.auto.market_open(), expected)

    async def test_weekend_and_utc_clock_still_use_kst_sessions(self):
        self.now = self.now.replace(hour=10, minute=30).astimezone(timezone.utc)
        self.assertTrue(self.auto.buy_window_open())
        for day in (10, 11):  # October 2026 Saturday and Sunday.
            self.now = datetime(2026, 10, day, 11, tzinfo=timezone(timedelta(hours=9)))
            self.assertFalse(self.auto.buy_window_open())

    async def test_reenabling_restores_previous_hour_restrictions(self):
        self.engine.settings = replace(self.settings, swing_buy_window_limit_enabled=True)
        for hour, expected in ((9, True), (10, False), (11, False), (12, True), (13, True),
                               (14, False), (15, False), (16, True), (19, True), (20, False)):
            self.now = self.now.replace(hour=hour, minute=0)
            self.assertEqual(self.auto.buy_window_open(), expected)

    def mock_buy(self):
        async def fill(**args):
            quote, quantity = args['quote'], args['quantity']
            self.assertLessEqual(quantity * quote.ask_price * (1 + self.settings.fee_rate), args['order_budget'])
            return Order('mock-buy-window', args['client_order_id'], quote.symbol, Side.BUY, quantity,
                         quote.ask_price, quote.ask_price, Currency.KRW, OrderStatus.FILLED,
                         D(0), None, datetime.now(timezone.utc))
        self.broker.place_market_order = AsyncMock(side_effect=fill)

    async def tick(self, eligible=True):
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': eligible}):
            await self.auto.tick()

    async def test_new_buy_at_ten_thirty_uses_existing_signal_and_funding_checks(self):
        self.now = self.now.replace(hour=10, minute=30)
        self.mock_buy()
        await self.tick()
        self.broker.place_market_order.assert_awaited_once()
        self.assertEqual(self.broker.place_market_order.await_args.kwargs['quantity'], D(6))
        self.assertNotIn('자동매수 시간 외', self.auto.status()['buy_block_reasons'])
        self.assertIn('정규장·애프터마켓 전체', self.auto.status()['execution_state'])
        self.client.create_order.assert_not_awaited()

    async def test_new_buy_at_fourteen_thirty_is_allowed(self):
        self.now = self.now.replace(hour=14, minute=30)
        self.mock_buy()
        await self.tick()
        self.broker.place_market_order.assert_awaited_once()

    async def test_failed_entry_signal_still_does_not_buy(self):
        self.now = self.now.replace(hour=11, minute=30)
        self.mock_buy()
        await self.tick(eligible=False)
        self.broker.place_market_order.assert_not_awaited()
        self.assertFalse(self.auto.session['targets'])

    async def test_stale_orderbook_still_blocks_new_buy(self):
        self.now = self.now.replace(hour=10, minute=30)
        self.mock_buy()
        self.broker._fresh_risk_quotes = AsyncMock(return_value=[Quote('082740', D(100000), Currency.KRW,
            datetime.now(timezone.utc) - timedelta(seconds=11), 'toss', D(99900), D(100000))])
        await self.tick()
        self.broker.place_market_order.assert_not_awaited()
        self.assertIn('오래되었', self.auto.message)

    async def test_market_close_weekend_and_kill_switch_do_not_scan_or_order(self):
        self.mock_buy()
        for hour, minute in ((8, 59), (15, 30), (15, 45), (20, 0)):
            self.now = self.now.replace(hour=hour, minute=minute)
            await self.tick()
        self.now = self.now.replace(day=10, hour=10)
        await self.tick()
        self.now = self.now.replace(day=7, hour=10, minute=30)
        self.engine.kill_switch = True
        await self.tick()
        self.recommend.assert_not_awaited()
        self.broker.place_market_order.assert_not_awaited()

    async def test_five_minute_cadence_and_restart_do_not_repeat_same_day_buy(self):
        self.now = self.now.replace(hour=10, minute=30)
        self.mock_buy()
        await self.tick()
        self.now += timedelta(minutes=4)
        await self.tick()
        self.recommend.assert_awaited_once()
        self.broker.place_market_order.assert_awaited_once()
        resumed = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.now += timedelta(minutes=1)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            await resumed.tick()
        self.broker.place_market_order.assert_awaited_once()
        self.assertFalse(resumed.status()['buy_window_limit_enabled'])


class UnrestrictedAveragingWindowTest(IsolatedAsyncioTestCase):
    asyncTearDown = test_swing_averaging.AveragingTest.asyncTearDown
    mock_fill = test_swing_averaging.AveragingTest.mock_fill
    tick = test_swing_averaging.AveragingTest.tick

    async def asyncSetUp(self):
        await test_swing_averaging.AveragingTest.asyncSetUp(self)
        self.settings = replace(self.settings, swing_buy_window_limit_enabled=False)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings

    async def test_averaging_at_ten_thirty_still_only_attempts_once(self):
        self.now = self.now.replace(hour=10, minute=30)
        self.mock_fill()
        await self.tick()
        await self.tick()
        self.broker.place_averaging_order.assert_awaited_once()

    async def test_crossing_market_close_during_lookup_blocks_averaging(self):
        self.now = self.now.replace(hour=15, minute=29)
        self.mock_fill()
        async def closes_during_lookup(symbol):
            self.now = self.now.replace(minute=30)
            return {'symbol': symbol}
        self.client.stock_info.side_effect = closes_during_lookup
        await self.tick()
        self.broker.place_averaging_order.assert_not_awaited()
        self.assertFalse(next(iter(self.auto.averaging.plans.values())).get('attempted_at'))


class BuyWindowApiTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_live_per_order.PerOrderDashboardTest.asyncSetUp
    asyncTearDown = test_live_per_order.PerOrderDashboardTest.asyncTearDown

    async def test_status_exposes_current_full_session_policy_without_account_calls(self):
        self.settings = replace(self.settings, swing_buy_window_limit_enabled=False)
        self.app.state.settings = self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            response = await client.get('/api/v1/system/status')
        auto = response.json()['automation']
        self.assertFalse(auto['buy_window_limit_enabled'])
        self.assertTrue(auto['buy_window_open'])
        self.assertIn('별도 자동매수 시간 제한 해제', auto['trading_hours'])
        self.assertNotIn('09:00~10:00', auto['trading_hours'])
        self.client.create_order.assert_not_called()
