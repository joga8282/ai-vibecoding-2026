"""Manual holdings use automatic exit rules; all LIVE I/O is mocked."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.models import Currency, Order, OrderStatus, Position, Quote, Side
from app.swing_trader import SwingTrader
from tests import test_live_dashboard, test_swing_trader


def upper_signal(price, *, sell_at_upper=True):
    return {'ready': True, 'upper_touched': True, 'sell_at_upper': sell_at_upper,
            'bollinger_upper': str(price), 'active_high': str(price)}


class PaperManualAutoExitTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_swing_trader.SwingTest.asyncSetUp
    asyncTearDown = test_swing_trader.SwingTest.asyncTearDown
    setup_swing = test_swing_trader.SwingTest.setup_swing

    async def manual_buy(self):
        await self.setup_swing()
        self.engine.running = False
        order = await self.auto.buy_qualified('005930')
        self.recommend.return_value = {'candidates': []}
        self.engine.running = True
        return order

    def tracked(self):
        return sum((max(D(0), self.auto.outstanding(day, '005930'))
                    for day in self.auto.days.values()), D(0))

    async def resume(self):
        self.auto = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await self.auto.restore()
        await self.auto.prepare()
        self.engine.automation = self.auto

    async def test_direct_buy_is_journaled_and_automatically_stopped(self):
        buy = await self.manual_buy()
        self.assertTrue(self.auto.session['targets']['005930']['manual_qualified_buy'])
        self.assertIn(buy, self.auto.daily_orders(self.auto.session))
        self.price = D(140)
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])
        self.assertEqual(self.broker.orders[-1].quantity, buy.quantity)
        self.assertFalse(self.broker.positions)
        self.assertFalse(any(day.get('adopted') for day in self.auto.days.values()))
        self.assertIn('-3% 손절', self.auto.session['outcome'])

    async def test_direct_buy_requires_net_three_percent_for_normal_profit_exit(self):
        await self.manual_buy()
        self.price = D(149)  # Less than 3% above the 145 entry.
        with patch('app.swing_trader.four_hour_exit_signal', return_value=upper_signal(self.price)):
            await self.auto.tick()
        self.assertEqual(len(self.broker.orders), 1)
        check = self.auto.session['targets']['005930']['last_exit_check']
        self.assertFalse(check['take_profit'])
        self.assertFalse(check['profit_trailing_armed'])
        await self.resume()
        self.price = D(152)
        with patch('app.swing_trader.four_hour_exit_signal', return_value=upper_signal(self.price)):
            await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])
        self.assertIn('3% 이상 익절', self.auto.session['outcome'])

    async def test_direct_buy_retains_protective_trailing_exit_after_restart(self):
        await self.manual_buy()
        self.price = D(160)
        with patch('app.swing_trader.four_hour_exit_signal',
                   return_value=upper_signal(self.price, sell_at_upper=False)):
            await self.auto.tick()
        self.assertEqual(len(self.broker.orders), 1)
        target = self.auto.session['targets']['005930']
        self.assertTrue(target['profit_trailing_armed'])
        armed_at = target['profit_trailing_armed_at']
        await self.resume()
        self.price = D(149)  # Protect even after profit drops below 3%.
        self.engine.candles = AsyncMock(side_effect=RuntimeError('mock candles unavailable'))
        await self.auto.tick()
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        target = self.auto.session['targets']['005930']
        self.assertEqual(target['profit_trailing_armed_at'], armed_at)
        self.assertIn('보호 매도', target['exit_order_reason'])
        self.assertFalse(self.broker.positions)

    async def test_external_buy_after_start_is_adopted_once_then_automatically_sold(self):
        await self.setup_swing()
        self.recommend.return_value = {'candidates': []}
        buy = await self.broker.place_market_order(
            client_order_id='external-manual-buy',
            quote=Quote('005930', D(145), Currency.KRW, self.now, 'toss'),
            side=Side.BUY, quantity=D(2))
        self.price = D(149)
        with patch('app.swing_trader.four_hour_exit_signal', return_value={}):
            await self.auto.tick()
            self.now += timedelta(minutes=5)
            await self.auto.tick()
        self.assertEqual(self.tracked(), D(2))
        self.assertEqual(len([day for day in self.auto.days.values() if day.get('adopted')]), 1)
        self.assertEqual(self.broker.orders, [buy])  # No invented buy orders.
        await self.resume()
        self.assertEqual(self.tracked(), D(2))
        self.price = D(140)
        self.recommend.side_effect = RuntimeError('mock recommendation unavailable')
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])
        self.assertEqual(self.broker.orders[-1].quantity, D(2))
        self.assertFalse(self.broker.positions)

    async def test_external_addition_only_adopts_the_untracked_quantity(self):
        buy = await self.manual_buy()
        target = self.auto.session['targets']['005930']
        target.update(profit_trailing_armed=True, exit_peak='160')
        await self.broker.place_market_order(
            client_order_id='external-additional-buy',
            quote=Quote('005930', D(145), Currency.KRW, self.now, 'toss'),
            side=Side.BUY, quantity=D(2))
        self.price = D(160)
        with patch('app.swing_trader.four_hour_exit_signal', return_value={}):
            await self.auto.tick()
            self.now += timedelta(minutes=5)
            await self.auto.tick()
        adopted = [day['adopted']['005930'] for day in self.auto.days.values() if day.get('adopted')]
        self.assertEqual(adopted, [{'quantity': '2', 'cost': '290'}])
        self.assertEqual(self.tracked(), buy.quantity + D(2))
        self.assertTrue(target['profit_trailing_armed'])
        self.assertEqual(target['exit_peak'], '160')
        self.assertEqual(len(self.broker.orders), 2)
        await self.resume()
        self.assertEqual(self.tracked(), buy.quantity + D(2))

    async def test_manual_holding_is_not_sold_while_engine_stopped_or_killed(self):
        await self.manual_buy()
        self.price = D(140)
        self.engine.running = False
        await self.auto.tick()
        self.engine.running = True
        self.engine.kill_switch = True
        await self.auto.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertIn('005930', self.broker.positions)


class LiveManualAutoExitTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_live_dashboard.LiveDashboardTest.asyncSetUp
    asyncTearDown = test_live_dashboard.LiveDashboardTest.asyncTearDown

    def mock_filled_sell(self):
        async def sell(**kwargs):
            self.assertEqual(kwargs['side'], Side.SELL)
            order = Order('mock-sell', kwargs['client_order_id'], '005930', Side.SELL,
                          kwargs['quantity'], kwargs['quote'].bid_price, kwargs['quote'].bid_price,
                          Currency.KRW, OrderStatus.FILLED, D(0), None, datetime.now(timezone.utc))
            self.broker.orders.append(order)
            self.broker.positions.pop('005930')
            self.broker.position_market_values.pop('005930')
            return order
        self.broker.place_market_order = AsyncMock(side_effect=sell)

    async def test_reconciliation_discovers_external_manual_buy_before_exit_scan(self):
        await self.auto.restore()  # Startup has no holdings.
        self.engine.running = True
        self.recommend.side_effect = RuntimeError('mock recommendation unavailable')
        async def reconcile():
            self.broker.positions['005930'] = Position('005930', D(2), D(75000), Currency.KRW)
            self.broker.position_market_values['005930'] = D(139800)
            return {'reconciled': True}
        self.broker.reconcile.side_effect = reconcile
        self.mock_filled_sell()
        await self.auto.tick()
        self.broker.place_market_order.assert_awaited_once()
        self.assertEqual(self.broker.orders[0].quantity, D(2))
        self.assertFalse(self.broker.positions)
        adopted = next(day for day in self.auto.days.values() if day.get('adopted'))
        self.assertIn('-3% 손절', adopted['targets']['005930']['exit_order_reason'])
        self.assertEqual(self.auto.outstanding(adopted, '005930'), D(0))
        self.client.create_order.assert_not_awaited()

    async def test_partial_direct_buy_tracks_only_filled_shares_through_restart(self):
        settings = replace(self.settings, live_max_order_amount_krw=D(200000))
        self.engine.settings = self.broker.settings = self.broker.risk.settings = settings
        self.broker.cash[Currency.KRW] = D(3000000)
        async def partial_buy(**kwargs):
            self.assertEqual(kwargs['side'], Side.BUY)
            self.assertEqual(kwargs['quantity'], D(2))
            order = Order('mock-partial-buy', kwargs['client_order_id'], '005930', Side.BUY,
                          D(1), D(70000), D(70000), Currency.KRW, OrderStatus.PARTIALLY_FILLED,
                          D(0), None, datetime.now(timezone.utc))
            self.broker.orders.append(order)
            self.broker.positions['005930'] = Position('005930', D(1), D(70000), Currency.KRW)
            self.broker.position_market_values['005930'] = D(70000)
            return order
        self.broker.place_market_order = AsyncMock(side_effect=partial_buy)
        with patch('app.swing_trader.membership', return_value=True), patch(
                'app.swing_trader.swing_signal', return_value={'eligible': True}):
            buy = await self.auto.buy_qualified('005930')
        self.auto = SwingTrader(self.engine, self.recommend, self.auto.clock)
        await self.auto.restore()
        self.engine.automation = self.auto
        self.assertIn(buy, self.auto.daily_orders(self.auto.session))
        self.assertEqual(self.auto.outstanding(self.auto.session, '005930'), D(1))
        self.assertFalse(any(day.get('adopted') for day in self.auto.days.values()))
        self.recommend.return_value = {'candidates': []}
        self.book = Quote('005930', D(67050), Currency.KRW, datetime.now(timezone.utc),
                          'toss', D(67000), D(67100))
        self.broker._fresh_risk_quotes.return_value = [self.book]
        self.mock_filled_sell()
        self.engine.running = True
        await self.auto.tick()
        self.broker.place_market_order.assert_awaited_once()
        self.assertEqual(self.broker.orders[-1].quantity, D(1))
        self.assertFalse(self.broker.positions)
        self.client.create_order.assert_not_awaited()

    async def test_failed_reconciliation_does_not_adopt_or_sell_unconfirmed_holding(self):
        self.broker.positions['005930'] = Position('005930', D(1), D(75000), Currency.KRW)
        self.broker.reconcile.return_value = {'reconciled': False}
        self.broker.place_market_order = AsyncMock()
        self.engine.running = True
        await self.auto.tick()
        self.broker.place_market_order.assert_not_awaited()
        self.assertFalse(self.auto.days)

    async def test_external_manual_holding_with_stale_quote_is_tracked_but_not_sold(self):
        self.broker.positions['005930'] = Position('005930', D(1), D(75000), Currency.KRW)
        self.broker.position_market_values['005930'] = D(69900)
        self.book.timestamp -= timedelta(seconds=11)
        self.recommend.return_value = {'candidates': []}
        self.broker.place_market_order = AsyncMock()
        self.engine.running = True
        await self.auto.tick()
        self.broker.place_market_order.assert_not_awaited()
        adopted = next(day for day in self.auto.days.values() if day.get('adopted'))
        self.assertEqual(self.auto.outstanding(adopted, '005930'), D(1))
        self.broker._fresh_risk_quotes.assert_awaited_once_with(['005930'])
        self.assertNotIn('last_exit_check', adopted['targets']['005930'])
        self.client.create_order.assert_not_awaited()
