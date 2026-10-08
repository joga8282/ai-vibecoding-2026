"""Single averaging attempt, persistence and LIVE exceptions use mock transports."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from app.models import Currency, Order, OrderStatus, Position, Quote, Side
from app.swing_averaging import SNAPSHOT
from app.swing_trader import SwingTrader
from tests import test_live_per_order, test_swing_profit, test_live_limits


class AveragingConfigTest(TestCase):
    settings = test_live_limits.LiveLimitConfigTest.settings

    def test_policy_is_opt_in_and_fixed_stop_can_be_disabled(self):
        self.assertFalse(self.settings().swing_averaging_enabled)
        configured = self.settings(SWING_STOP_LOSS_ENABLED='false', SWING_AVERAGING_ENABLED='true',
                                   LIVE_AUTO_ALLOCATION_MODE='per_order', SWING_AVERAGING_TRIGGER_PERCENT='15')
        self.assertFalse(configured.swing_stop_loss_enabled)
        self.assertTrue(configured.swing_averaging_enabled)
        self.assertEqual(configured.swing_averaging_trigger_percent, D(15))

    def test_invalid_thresholds_and_legacy_live_allocation_fail_startup(self):
        for value in ('0', '100', '-15', 'NaN', 'Infinity', 'bad'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings(SWING_AVERAGING_TRIGGER_PERCENT=value)
        with self.assertRaisesRegex(RuntimeError, 'per_order'):
            self.settings(SWING_AVERAGING_ENABLED='true')
        for changes in ({'SWING_STOP_LOSS_ENABLED': 'flase'}, {'SWING_AVERAGING_ENABLED': 'maybe'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.settings(**changes)


class DisabledStopTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_swing_profit.ProfitExitTest.asyncSetUp
    check = test_swing_profit.ProfitExitTest.check

    async def test_no_fixed_stop_at_three_fifteen_or_fifty_percent_loss(self):
        self.engine.settings = replace(self.settings, swing_stop_loss_enabled=False)
        for price in ('97', '85', '50'):
            signal = await self.check(price, touch=False)
            self.assertFalse(signal['stop_loss'])
            self.assertFalse(signal['sell'])
            self.assertFalse(signal['stop_loss_enabled'])

    async def test_take_profit_and_activated_protection_remain(self):
        self.engine.settings = replace(self.settings, swing_stop_loss_enabled=False)
        self.assertTrue((await self.check('103', upper=True))['take_profit'])
        await self.check('104', high='106')
        result = await self.check('96', touch=False)
        self.assertFalse(result['stop_loss'])
        self.assertTrue(result['trailing_exit'])

    async def test_candle_failure_does_not_turn_into_fixed_stop(self):
        self.engine.settings = replace(self.settings, swing_stop_loss_enabled=False)
        self.engine.candles.side_effect = ConnectionError('mock outage')
        quote = Quote('005930', D(80), Currency.KRW, self.now, 'toss', D(80), D(81))
        with self.assertRaises(ConnectionError):
            await self.auto.exit_signal('005930', quote, self.position, self.day)


class AveragingTest(IsolatedAsyncioTestCase):
    asyncTearDown = test_live_per_order.PerOrderAllocationTest.asyncTearDown

    async def asyncSetUp(self):
        await test_live_per_order.PerOrderAllocationTest.asyncSetUp(self)
        self.settings = replace(self.settings, swing_stop_loss_enabled=False, swing_averaging_enabled=True)
        self.engine.settings = self.broker.settings = self.broker.risk.settings = self.settings
        self.broker.cash[Currency.KRW] = D(10000)
        self.broker.positions['082740'] = Position('082740', D(10), D(100), Currency.KRW)
        self.broker.position_market_values['082740'] = D(850)
        self.book.price = self.book.bid_price = self.book.ask_price = D(85)
        self.client.stock_info.return_value = {'symbol': '082740', 'securityType': 'STOCK',
            'isCommonShare': True, 'sharesOutstanding': '100000000000'}
        self.client.buying_power.return_value = {'cashBuyingPower': '10000'}
        self.recommend.return_value = {'candidates': []}
        await self.auto.restore()
        self.engine.running = True
        self.owner = next(day for day in self.auto.days.values() if day.get('adopted'))

    def mock_fill(self, status=OrderStatus.FILLED, filled=None):
        async def fill(**args):
            saved = await self.repo.load(SNAPSHOT)
            self.assertEqual(saved['plans'][args['averaging_plan_id']]['status'], 'RESERVED')
            qty = args['quantity'] if filled is None else D(filled)
            old = self.broker.positions['082740']
            amount = qty * self.book.ask_price
            fee = amount * self.settings.fee_rate
            if qty:
                old.average_price = (old.average_price * old.quantity + amount) / (old.quantity + qty)
                old.quantity += qty
                self.broker.cash[Currency.KRW] -= amount + fee
                self.broker.position_market_values['082740'] = old.quantity * self.book.price
            order = Order('mock-average', args['client_order_id'], '082740', Side.BUY, qty,
                          self.book.ask_price, self.book.ask_price if qty else None, Currency.KRW,
                          status, fee, None, datetime.now(timezone.utc))
            self.broker.orders.append(order)
            return order
        self.broker.place_averaging_order = AsyncMock(side_effect=fill)

    async def tick(self):
        with patch('app.swing_averaging.membership', return_value=True):
            await self.auto.averaging.tick()

    async def reservation(self):
        self.broker.place_averaging_order = AsyncMock(side_effect=TimeoutError('mock ambiguous submit'))
        await self.tick()
        saved = await self.repo.load(SNAPSHOT)
        plan = next(iter(saved['plans'].values()))
        plan['status'] = 'RESERVED'
        plan['attempted_at'] = datetime.now(timezone.utc).isoformat()
        await self.repo.save(SNAPSHOT, saved)
        return {'clientOrderId': plan['client_order_id'], 'symbol': '082740', 'side': 'BUY',
                'orderType': 'LIMIT', 'price': '85', 'quantity': plan['quantity'], 'orderBudget': plan['budget'],
                'quote': self.book, 'averagingPlanId': plan['id']}

    async def test_exact_loss_threshold_buys_once_with_original_cost_as_target(self):
        self.mock_fill()
        await self.tick()
        args = self.broker.place_averaging_order.await_args.kwargs
        self.assertEqual(args['quantity'], D(11))
        self.assertEqual(args['order_budget'], D('1000.15'))
        self.assertEqual(self.broker.positions['082740'].quantity, D(21))
        self.assertLess(self.broker.positions['082740'].average_price, D(100))
        await self.tick()
        self.broker.place_averaging_order.assert_awaited_once()

    async def test_ask_above_threshold_does_not_buy_even_when_last_is_lower(self):
        self.mock_fill()
        self.book.ask_price = D('85.01')
        await self.tick()
        self.broker.place_averaging_order.assert_not_awaited()
        self.assertEqual(self.auto.averaging.checks['082740']['status'], 'WAITING')

    async def test_recovered_ask_is_checked_after_slow_stock_lookup(self):
        self.mock_fill()
        async def info(symbol):
            self.book.ask_price = D(86)
            return {'symbol': symbol}
        self.client.stock_info.side_effect = info
        await self.tick()
        self.broker.place_averaging_order.assert_not_awaited()

    async def test_existing_five_positions_do_not_prevent_adding_to_one(self):
        for symbol in ('005930', '012450', '006400', '000660'):
            self.broker.positions[symbol] = Position(symbol, D(1), D(50), Currency.KRW)
            self.broker.position_market_values[symbol] = D(85)
        await self.auto.adopt_positions()
        self.mock_fill()
        await self.tick()
        self.assertEqual(len(self.broker.positions), 5)
        self.broker.place_averaging_order.assert_awaited_once()

    async def test_partial_fill_consumes_attempt_and_restart_does_not_buy_again(self):
        self.mock_fill(OrderStatus.PARTIALLY_FILLED, filled=2)
        await self.tick()
        restarted = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await restarted.restore()
        self.engine.automation = restarted
        self.book.price = self.book.bid_price = self.book.ask_price = D(70)
        with patch('app.swing_averaging.membership', return_value=True):
            await restarted.averaging.tick()
        self.broker.place_averaging_order.assert_awaited_once()
        self.assertEqual(self.broker.positions['082740'].quantity, D(12))

    async def test_truncated_original_order_history_does_not_start_a_new_averaging_cycle(self):
        self.owner['adopted'] = {}
        self.broker.orders.append(Order('initial', f"auto-{self.owner['id']}-082740-initial", '082740',
            Side.BUY, D(10), D(100), D(100), Currency.KRW, OrderStatus.FILLED, D(0), None,
            datetime.now(timezone.utc)))
        await self.auto.save()
        self.mock_fill()
        await self.tick()
        self.broker.orders.clear()
        restarted = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await restarted.restore()
        self.book.price = self.book.bid_price = self.book.ask_price = D(70)
        with patch('app.swing_averaging.membership', return_value=True):
            await restarted.averaging.tick()
        self.broker.place_averaging_order.assert_awaited_once()

    async def test_confirmed_flat_holding_closes_old_cycle(self):
        self.mock_fill()
        await self.tick()
        self.broker.positions.clear()
        await self.auto.averaging.refresh_results()
        self.assertTrue(next(iter(self.auto.averaging.plans.values())).get('closed_at'))

    async def test_waiting_cycle_closes_without_an_order_and_new_holding_gets_new_basis(self):
        self.mock_fill()
        self.book.ask_price = D(86)
        await self.tick()
        original_plan = next(iter(self.auto.averaging.plans.values()))
        self.assertEqual(original_plan['status'], 'WAITING')
        self.broker.positions.clear()
        await self.auto.averaging.refresh_results()
        self.assertTrue(original_plan.get('closed_at'))
        self.auto.days = {}
        self.broker.positions['082740'] = Position('082740', D(10), D(200), Currency.KRW)
        self.broker.position_market_values['082740'] = D(1800)
        self.book.price = self.book.bid_price = self.book.ask_price = D(180)
        await self.auto.adopt_positions()
        await self.tick()
        new_plan = next(item for item in self.auto.averaging.plans.values() if not item.get('closed_at'))
        self.assertEqual(D(new_plan['original_amount']), D('2000.3'))
        self.assertNotEqual(original_plan['id'], new_plan['id'])
        self.broker.place_averaging_order.assert_not_awaited()

    async def test_rejection_and_zero_fill_cancellation_do_not_retry(self):
        for status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            self.auto.averaging.plans = {}
            self.mock_fill(status, filled=0)
            await self.tick()
            await self.tick()
            self.broker.place_averaging_order.assert_awaited_once()

    async def test_unknown_submission_does_not_retry_after_restart(self):
        async def unknown(**args):
            self.broker.order_journal['mock'] = {'originalClientOrderId': args['client_order_id'], 'status': 'UNKNOWN'}
            self.broker.disarm()
            raise TimeoutError('mock ambiguous submit')
        self.broker.place_averaging_order = AsyncMock(side_effect=unknown)
        await self.tick()
        saved = await self.repo.load(SNAPSHOT)
        self.assertEqual(next(iter(saved['plans'].values()))['status'], 'UNKNOWN')
        restarted = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await restarted.restore()
        self.broker.risk.armed = True
        with patch('app.swing_averaging.membership', return_value=True):
            await restarted.averaging.tick()
        self.broker.place_averaging_order.assert_awaited_once()

    async def test_recovered_fill_resets_old_trailing_state(self):
        self.mock_fill(OrderStatus.PARTIALLY_FILLED, filled=2)
        self.owner['targets']['082740'].update({'profit_trailing_armed': True, 'exit_peak': '120',
                                                'upper_band_touched': True})
        await self.tick()
        self.assertNotIn('profit_trailing_armed', self.owner['targets']['082740'])
        self.assertNotIn('exit_peak', self.owner['targets']['082740'])
        self.owner['targets']['082740']['profit_trailing_armed'] = True
        await self.auto.save()
        restarted = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await restarted.restore()
        # A later protection activation survives restart; the reset runs once.
        self.assertTrue(next(iter(restarted.days.values()))['targets']['082740']['profit_trailing_armed'])

    async def test_funding_caps_and_unknown_pending_amount_block_averaging(self):
        self.mock_fill()
        self.broker.cash[Currency.KRW] = D(200)
        self.assertLessEqual(self.auto.averaging.budget(D(1000)), D('157.5'))
        self.broker.open_orders = [{'symbol': '005930', 'side': 'BUY', 'quantity': '1'}]
        self.assertEqual(self.auto.averaging.budget(D(1000)), D(0))
        await self.tick()
        self.broker.place_averaging_order.assert_not_awaited()

    async def test_budget_is_capped_at_fifteen_percent_and_aggregate_remaining(self):
        self.broker.cash[Currency.KRW] = D(1150)
        self.assertEqual(self.auto.averaging.budget(D('1000.15')), D(300))
        self.broker.cash[Currency.KRW] = D(350)
        self.assertEqual(self.auto.averaging.budget(D('1000.15')), D(49))

    async def test_pending_same_symbol_and_daily_loss_block_without_consuming_attempt(self):
        self.mock_fill()
        self.broker.open_orders = [{'symbol': '082740', 'side': 'BUY', 'quantity': '1', 'price': '85'}]
        await self.tick()
        self.broker.place_averaging_order.assert_not_awaited()
        self.assertFalse(next(iter(self.auto.averaging.plans.values())).get('attempted_at'))
        self.broker.open_orders = []
        self.broker.risk.daily_loss = self.settings.live_max_daily_loss_krw
        await self.tick()
        self.broker.place_averaging_order.assert_not_awaited()
        self.assertIn('일일 손실', self.auto.averaging.checks['082740']['reason'])

    async def test_stale_quote_and_removed_universe_symbol_do_not_buy(self):
        self.mock_fill()
        self.broker._fresh_risk_quotes = AsyncMock(return_value=[
            Quote('082740', D(85), Currency.KRW, datetime.now(timezone.utc) - timedelta(seconds=11), 'toss', D(85), D(85))])
        await self.tick()
        self.broker.place_averaging_order.assert_not_awaited()
        with patch('app.swing_averaging.load_universe', return_value=(D('5e11'), {})):
            await self.auto.averaging.tick()
        self.assertIn('등록 테마', self.auto.averaging.checks['082740']['reason'])

    async def test_fill_is_managed_on_same_original_holding_day(self):
        self.mock_fill()
        await self.tick()
        self.assertEqual(self.auto.outstanding(self.owner, '082740'), D(21))
        self.assertEqual(self.auto.report()['days'][0]['status'], '기존 보유 승계')

    async def test_full_tick_can_average_when_exit_candles_fail(self):
        self.mock_fill()
        self.engine.candles.side_effect = ConnectionError('mock outage')
        with patch('app.swing_averaging.membership', return_value=True):
            await self.auto.tick()
        self.broker.place_averaging_order.assert_awaited_once()
        self.assertEqual(self.broker.place_averaging_order.await_args.kwargs['side'], Side.BUY)

    async def test_final_transport_validates_held_addition_without_fresh_entry_signal(self):
        request = await self.reservation()
        self.broker.risk.clear_recommended_symbols()
        self.client.create_order.return_value = {'orderId': 'accepted-average'}
        self.broker.get_order = AsyncMock(return_value={'status': 'FILLED', 'execution': {
            'filledQuantity': request['quantity'], 'averageFilledPrice': '85', 'commission': '0'}})
        order = await self.broker.place_order(request)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.client.create_order.assert_awaited_once()
        self.assertEqual(self.client.create_order.await_args.args[1]['orderType'], 'LIMIT')
        self.assertEqual(self.client.create_order.await_args.args[1]['price'], '85')
        saved = await self.repo.load(SNAPSHOT)
        self.assertEqual(saved['plans'][request['averagingPlanId']]['status'], 'SUBMITTING')
        with self.assertRaises(RuntimeError):
            await self.broker.place_order(request)
        self.client.create_order.assert_awaited_once()

    async def test_forged_expired_or_mismatched_reservations_never_reach_transport(self):
        request = await self.reservation()
        for changes in ({'averagingPlanId': 'missing'}, {'quantity': '12'}, {'orderBudget': '2000'},
                        {'clientOrderId': 'other'}, {'side': 'SELL'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                await self.broker.place_order({**request, **changes})
        saved = await self.repo.load(SNAPSHOT)
        saved['plans'][request['averagingPlanId']]['attempted_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await self.repo.save(SNAPSHOT, saved)
        with self.assertRaises(RuntimeError):
            await self.broker.place_order(request)
        self.client.create_order.assert_not_awaited()

    async def test_final_transport_rechecks_current_equity_cap_after_reservation(self):
        request = await self.reservation()
        self.client.buying_power.return_value = {'cashBuyingPower': '1000'}
        with self.assertRaisesRegex(RuntimeError, 'max-buy-ratio'):
            await self.broker.place_order(request)
        self.client.create_order.assert_not_awaited()

    async def test_final_transport_rejects_limit_above_latest_ask(self):
        request = await self.reservation()
        with self.assertRaises(RuntimeError):
            await self.broker.place_order({**request, 'price': '86'})
        self.client.create_order.assert_not_awaited()

    async def test_fill_recovered_after_crash_resets_old_peak_and_arming(self):
        self.mock_fill()
        await self.tick()
        saved = await self.repo.load(SNAPSHOT)
        plan = next(iter(saved['plans'].values()))
        plan['status'] = 'UNKNOWN'
        plan.pop('reset_applied_quantity')
        await self.repo.save(SNAPSHOT, saved)
        self.owner['targets']['082740'].update({'profit_trailing_armed': True, 'exit_peak': '120'})
        await self.auto.save()
        restarted = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await restarted.restore()
        restored = next(iter(restarted.days.values()))['targets']['082740']
        self.assertNotIn('profit_trailing_armed', restored)
        self.assertNotIn('exit_peak', restored)

    async def test_raw_buy_still_cannot_add_to_held_position_without_reservation(self):
        with self.assertRaises(RuntimeError):
            await self.broker.place_order({'clientOrderId': 'raw', 'symbol': '082740', 'side': 'BUY',
                'quantity': '1', 'orderType': 'MARKET', 'quote': self.book})
        self.client.create_order.assert_not_awaited()
