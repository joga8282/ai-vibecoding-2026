"""Low entry zones use synthetic completed bars and mock brokers only."""
from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.models import Candle, Currency, Side
from app.swing_signals import KST, swing_signal, trend_context, _completed_weekly
from app.swing_recommendations import build_swing_recommendations
from tests import test_swing_trader


def four_hour_bars(now, values):
    return [Candle((now - timedelta(days=len(values) - index)).replace(hour=13),
                   D(value), D(value), D(value), D(value), D(1000), Currency.KRW)
            for index, value in enumerate(values)]


def weekly_bars(now):
    return [Candle(now - timedelta(weeks=52-index), D(160+index), D(300+index),
                   D(80), D(160+index), D(1000), Currency.KRW)
            for index in range(52)]


class LowEntrySignalTest(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 10, tzinfo=KST)
        self.daily = test_swing_trader.history(self.now)
        self.weekly = weekly_bars(self.now)

    def signal(self, price, bars, daily=None):
        return swing_signal(self.daily if daily is None else daily, D(price), self.now, bars, self.weekly)

    def test_narrow_band_upper_price_was_inside_old_three_percent_allowance(self):
        bars = four_hour_bars(self.now, ['155', '155.1', '154.9', '155.2', '155', '155.1'])
        levels = self.signal('155', bars)
        upper = D(levels['bollinger_upper'])
        signal = self.signal(upper, bars)
        self.assertTrue(signal['context_eligible'])
        self.assertTrue(signal['daily_bottom_zone'])
        self.assertTrue(signal['bollinger_touched'])  # Old 3% gate would buy here.
        self.assertFalse(signal['four_hour_lower_zone'])
        self.assertFalse(signal['eligible'])
        self.assertEqual(signal['conditions_passed'], 2)

    def test_band_lower_quarter_boundary_and_middle_do_not_overlap(self):
        bars = four_hour_bars(self.now, ['155', '155.1', '154.9', '155.2', '155', '155.1'])
        levels = self.signal('155', bars)
        ceiling = D(levels['four_hour_entry_ceiling'])
        self.assertTrue(self.signal(ceiling, bars)['eligible'])
        self.assertFalse(self.signal(ceiling + D('.0001'), bars)['eligible'])
        self.assertFalse(self.signal(levels['four_hour_middle'], bars)['eligible'])
        self.assertLess(ceiling, D(levels['four_hour_middle']))

    def test_short_band_dip_near_daily_high_is_rejected(self):
        bars = four_hour_bars(self.now, ['168', '167', '166', '166', '168', '165'])
        signal = self.signal('164.5', bars)
        self.assertTrue(signal['context_eligible'])
        self.assertTrue(signal['bollinger_touched'])
        self.assertTrue(signal['four_hour_lower_zone'])
        self.assertFalse(signal['daily_bottom_zone'])
        self.assertFalse(signal['eligible'])
        self.assertGreater(D(signal['daily_range_position_percent']), D(40))

    def test_recent_daily_low_range_boundary(self):
        bars = four_hour_bars(self.now, ['162', '165', '160', '163', '164', '157'])
        levels = self.signal('155', bars)
        ceiling = D(levels['recent_daily_low']) + (
            D(levels['recent_daily_high']) - D(levels['recent_daily_low'])) * D('.40')
        self.assertLess(ceiling, D(levels['four_hour_entry_ceiling']))
        self.assertEqual(D(levels['entry_ceiling']), ceiling)
        self.assertTrue(self.signal(ceiling, bars)['eligible'])
        self.assertFalse(self.signal(ceiling + D('.0001'), bars)['eligible'])

    def test_low_zone_passes_and_three_percent_cap_still_applies(self):
        bars = four_hour_bars(self.now, ['140', '150', '160', '140', '150', '160'])
        signal = self.signal('120', bars)
        self.assertTrue(signal['eligible'])
        self.assertEqual(signal['conditions_passed'], 3)
        self.assertEqual(D(signal['four_hour_entry_ceiling']), D(signal['bollinger_lower']) * D('1.03'))
        above = D(signal['four_hour_entry_ceiling']) + D('.0001')
        self.assertFalse(self.signal(above, bars)['eligible'])

    def test_active_four_hour_and_today_daily_bars_do_not_change_signal(self):
        bars = four_hour_bars(self.now, ['162', '165', '160', '163', '164', '157'])
        baseline = self.signal('155', bars)
        active = Candle(self.now.replace(hour=9), D(1000), D(2000), D(1), D(1000), D(1000), Currency.KRW)
        today = Candle(self.now, D(1000), D(2000), D(1), D(1000), D(1000), Currency.KRW)
        self.assertEqual(self.signal('155', bars + [active], self.daily + [today]), baseline)

    def test_short_stale_invalid_and_flat_four_hour_history_blocks_entry(self):
        bars = four_hour_bars(self.now, ['155', '156', '157', '158', '157', '155'])
        self.assertFalse(self.signal('150', bars[:5])['eligible'])
        for bar in bars:
            bar.timestamp -= timedelta(days=10)
        self.assertFalse(self.signal('150', bars)['eligible'])
        for value in ('0', 'NaN', 'Infinity'):
            invalid = four_hour_bars(self.now, ['155'] * 6)
            invalid[-1].close_price = D(value)
            self.assertFalse(self.signal('150', invalid)['eligible'])
        self.assertFalse(self.signal('150', four_hour_bars(self.now, ['155'] * 6))['eligible'])

    def test_invalid_daily_range_does_not_create_low_zone(self):
        bars = four_hour_bars(self.now, ['162', '165', '160', '163', '164', '157'])
        self.daily[-1].low_price = D('NaN')
        signal = self.signal('155', bars)
        self.assertFalse(signal['daily_bottom_checked'])
        self.assertFalse(signal['eligible'])
        self.assertEqual(signal['entry_ceiling'], '0')

    def test_weekly_high_zone_rejects_a_valid_daily_and_four_hour_pullback(self):
        for candle in self.daily:
            for field in ('open_price', 'high_price', 'low_price', 'close_price'):
                setattr(candle, field, getattr(candle, field) + D(165))
        bars = four_hour_bars(self.now, [327, 330, 325, 328, 329, 322])
        signal = self.signal('320', bars)
        self.assertTrue(signal['weekly_uptrend'])
        self.assertTrue(signal['daily_uptrend'])
        self.assertFalse(signal['weekly_peak_excluded'])  # More than 5% below the old resistance gate.
        self.assertTrue(signal['four_hour_lower_zone'])
        self.assertTrue(signal['daily_bottom_zone'])
        self.assertFalse(signal['weekly_bottom_zone'])
        self.assertFalse(signal['eligible'])
        self.assertGreater(D(signal['weekly_range_position_percent']), D(40))

    def test_weekly_interest_upper_and_support_floor_boundaries(self):
        context = trend_context(self.daily, D(155), self.now, self.weekly)
        floor, ceiling = D(context['weekly_support_floor']), D(context['weekly_entry_ceiling'])
        for price in (floor, ceiling):
            self.assertTrue(trend_context(self.daily, price, self.now, self.weekly)['weekly_bottom_zone'])
        self.assertFalse(trend_context(self.daily, ceiling + D('.0001'), self.now, self.weekly)['weekly_bottom_zone'])
        broken = trend_context(self.daily, floor - D('.0001'), self.now, self.weekly)
        self.assertTrue(broken['weekly_support_broken'])
        self.assertFalse(broken['context_eligible'])

    def test_current_week_and_future_week_do_not_change_entry_range(self):
        bars = four_hour_bars(self.now, [162, 165, 160, 163, 164, 157])
        baseline = self.signal('155', bars)
        for days in (0, 7):
            self.weekly.append(Candle(self.now + timedelta(days=days), D(999), D(2000),
                                      D(1), D(999), D(1000), Currency.KRW))
        self.assertEqual(self.signal('155', bars), baseline)

    def test_invalid_short_or_stale_weekly_data_blocks_entry(self):
        bars = four_hour_bars(self.now, [162, 165, 160, 163, 164, 157])
        self.assertFalse(swing_signal(self.daily, D(155), self.now, bars, self.weekly[:51])['eligible'])
        invalid = weekly_bars(self.now)
        invalid[-1].high_price = D('NaN')
        self.assertFalse(swing_signal(self.daily, D(155), self.now, bars, invalid)['eligible'])
        for candle in self.weekly:
            candle.timestamp -= timedelta(days=14)
        stale = self.signal('155', bars)
        self.assertFalse(stale['eligible'])
        self.assertFalse(stale['weekly_entry_checked'])

    def test_daily_to_weekly_aggregation_preserves_intraweek_extremes(self):
        candles = [Candle(self.now - timedelta(days=8), D(120), D(200), D(80), D(110), D(10), Currency.KRW),
                   Candle(self.now - timedelta(days=7), D(110), D(120), D(100), D(115), D(20), Currency.KRW)]
        weekly = _completed_weekly(candles, self.now)
        self.assertEqual(len(weekly), 1)
        self.assertEqual((weekly[0].open_price, weekly[0].high_price, weekly[0].low_price,
                          weekly[0].close_price, weekly[0].volume), (D(120), D(200), D(80), D(115), D(30)))


class LowEntryFlowTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_swing_trader.SwingTest.asyncSetUp
    asyncTearDown = test_swing_trader.SwingTest.asyncTearDown
    setup_swing = test_swing_trader.SwingTest.setup_swing

    async def test_daily_high_is_hidden_from_both_lists_and_retained_in_diagnostics(self):
        await self.setup_swing()
        self.price = D(160)
        with patch('app.swing_recommendations.load_universe', return_value=(D('1e12'), {'005930': ['mock']})):
            payload = await build_swing_recommendations(self.engine)
        self.assertFalse(payload['candidates'])
        self.assertFalse(payload['watchlist'])
        self.assertEqual(len(payload['diagnostics']['symbols']), 1)
        self.assertFalse(payload['diagnostics']['symbols'][0]['eligible'])
        self.assertTrue(any('20개 완료 일봉' in reason for reason in payload['diagnostics']['symbols'][0]['reasons']))

    async def test_only_candidates_near_entry_ceiling_are_visible_as_waiting(self):
        await self.setup_swing()
        for price, waiting in ((D(149), True), (D(156), False), (D(145), False)):
            self.price = price
            with patch('app.swing_recommendations.load_universe', return_value=(D('1e12'), {'005930': ['mock']})):
                payload = await build_swing_recommendations(self.engine)
            self.assertEqual(bool(payload['watchlist']), waiting, price)
            self.assertEqual(bool(payload['candidates']), price == D(145))

    async def test_weekly_high_is_hidden_and_rechecked_before_mock_orders(self):
        await self.setup_swing()
        self.price = D(320)
        for candle in self.daily:
            for field in ('open_price', 'high_price', 'low_price', 'close_price'):
                setattr(candle, field, getattr(candle, field) + D(165))
        bars = four_hour_bars(self.now, [327, 330, 325, 328, 329, 322])
        self.engine.candles = AsyncMock(side_effect=lambda symbol, interval, count:
                                       weekly_bars(self.now) if interval == '1w' else bars)
        with patch('app.swing_recommendations.load_universe', return_value=(D('1e12'), {'005930': ['mock']})):
            payload = await build_swing_recommendations(self.engine)
        self.assertFalse(payload['candidates'])
        self.assertFalse(payload['watchlist'])
        self.assertFalse(payload['diagnostics']['symbols'][0]['weekly_bottom_zone'])
        self.assertTrue(all(call.args[1] == '1w' for call in self.engine.candles.await_args_list))
        await self.auto.tick()  # Even a stale, mocked eligible recommendation cannot submit.
        with self.assertRaisesRegex(RuntimeError, '현재 자동매수 조건'):
            await self.auto.buy_qualified('005930')
        self.assertFalse(self.broker.orders)

    async def test_qualified_high_zone_is_rechecked_before_automatic_order(self):
        await self.setup_swing()
        self.price = D('164.5')
        bars = four_hour_bars(self.now, ['168', '167', '166', '166', '168', '165'])
        self.engine.candles = AsyncMock(side_effect=lambda symbol, interval, count:
                                       weekly_bars(self.now) if interval == '1w' else bars)
        signal = await self.auto.signal('005930', await self.auto.quote('005930'))
        self.assertTrue(signal['context_eligible'])
        self.assertFalse(signal['daily_bottom_zone'])
        await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.assertFalse(self.auto.session['targets'])

    async def test_held_position_stop_loss_is_independent_of_new_entry_gates(self):
        await self.setup_swing()
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY])
        self.price = D(140)
        self.now += timedelta(minutes=5)
        self.engine.candles = AsyncMock(return_value=[])
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])
        self.assertFalse(self.broker.positions)
