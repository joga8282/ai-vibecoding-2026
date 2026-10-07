from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from app.models import Candle, Currency, Side, Quote
from app.engine import TradingEngine
from app.swing_signals import four_hour_exit_signal, swing_signal, KST
from app.swing_trader import SwingTrader
from app.swing_recommendations import build_swing_recommendations
from app.swing_universe import membership
from tests import test_paper_trader


def history(now):
    return [Candle(now - timedelta(days=70-i), D(100+i), D(101+i), D(99+i), D(100+i), D(1000), Currency.KRW) for i in range(70)]


def weekly_source(now):
    return [Candle(now - timedelta(days=420-i), D(100+i*D('.1')), D(170+i*D('.1')),
                   D(99+i*D('.1')), D(100+i*D('.1')), D(1000), Currency.KRW)
            for i in range(420)]


class SignalTest(TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 10, tzinfo=KST)
        self.daily = history(self.now)

    def test_exact_boundaries_and_no_rebound_entry(self):
        signal = swing_signal(self.daily, D(160), self.now)
        lower, upper = D(signal['bollinger_lower']), D(signal['bollinger_upper'])
        self.assertTrue(swing_signal(self.daily, lower, self.now)['eligible'])
        self.assertFalse(swing_signal(self.daily, lower + D('.01'), self.now)['eligible'])
        self.assertTrue(swing_signal(self.daily, upper, self.now)['sell'])
        self.assertFalse(swing_signal(self.daily, upper - D('.01'), self.now)['sell'])

    def test_today_bar_does_not_move_daily_bands(self):
        baseline = swing_signal(self.daily, D(145), self.now)
        today = Candle(self.now, D(1000), D(1000), D(1), D(1000), D(10), Currency.KRW)
        self.assertEqual(baseline, swing_signal(self.daily + [today], D(145), self.now))

    def test_multi_timeframe_entry_uses_four_hour_approach_zone(self):
        values = [D(150 + i * 2) for i in range(10)] + [D(150), D(148)]
        four_hour = [Candle((self.now - timedelta(days=6-i//2)).replace(hour=9 if i % 2 == 0 else 13),
                            value, value, value, value, D(1000), Currency.KRW)
                     for i, value in enumerate(values)]
        weekly = TradingEngine._aggregate_candles(weekly_source(self.now), '1w')
        signal = swing_signal(self.daily, D(145), self.now, four_hour, weekly)
        self.assertTrue(signal['weekly_uptrend'])
        self.assertTrue(signal['daily_uptrend'])
        self.assertTrue(signal['eligible'])
        self.assertEqual(signal['conditions_passed'], 3)
        self.assertFalse(swing_signal(self.daily, D(160), self.now, four_hour, weekly)['eligible'])

    def test_four_hour_upper_touch_is_an_exit_signal(self):
        values = [D(100), D(101), D(102), D(103), D(104), D(105)]
        bars = [Candle((self.now - timedelta(days=3-i//2)).replace(hour=9 if i % 2 == 0 else 13),
                       value, value, value, value, D(1000), Currency.KRW)
                for i, value in enumerate(values)]
        baseline = four_hour_exit_signal(bars, D(100), self.now)
        upper = D(baseline['bollinger_upper'])
        self.assertTrue(four_hour_exit_signal(bars, upper, self.now)['sell_at_upper'])

    def test_rising_twenty_day_average_can_enter_when_sixty_day_average_falls(self):
        # Old high prices roll out of MA60 while recent prices recover above it.
        prices = [D(300)] * 5 + [D(100)] * 35 + [D(110 + i) for i in range(25)]
        bars = [Candle(self.now - timedelta(days=65-i), p, p+1, p-1, p, D(1000), Currency.KRW)
                for i, p in enumerate(prices)]
        ma60 = sum(prices[-60:]) / 60
        prev60 = sum(prices[-65:-5]) / 60
        self.assertLess(ma60, prev60)
        result = swing_signal(bars, D(100), self.now)
        self.assertTrue(result['uptrend'])
        self.assertTrue(result['eligible'])
        self.assertTrue(swing_signal(bars[-60:], D(100), self.now)['eligible'])
        self.assertFalse(swing_signal(bars[-59:], D(100), self.now)['eligible'])

    def test_downtrend_stale_flat_and_short_history(self):
        descending = history(self.now)
        for index, c in enumerate(descending):
            c.close_price = D(200-index)
        self.assertFalse(swing_signal(descending, D(1), self.now)['eligible'])
        self.assertFalse(swing_signal(self.daily, D(145), self.now + timedelta(days=10))['eligible'])
        self.assertFalse(swing_signal(self.daily[-19:], D(145), self.now)['sell'])
        self.assertFalse(swing_signal(self.daily[-20:], D(145), self.now)['eligible'])
        self.assertTrue(swing_signal(self.daily[-20:], D(1000), self.now)['sell'])
        for c in descending:
            c.close_price = D(100)
        self.assertFalse(swing_signal(descending, D(100), self.now)['eligible'])
        self.assertFalse(swing_signal(descending, D(100), self.now)['sell'])

    def test_theme_market_cap_and_common_share_required(self):
        stock = {'symbol': '005930', 'securityType': 'STOCK', 'isCommonShare': True, 'sharesOutstanding': '100000000000'}
        self.assertTrue(membership(stock, D(100), D('1e13'), {'005930': ['테마']}))
        self.assertFalse(membership(stock, D(99), D('1e13'), {'005930': ['테마']}))
        self.assertFalse(membership(stock, D(100), D('1e13'), {}))
        stock['isCommonShare'] = False
        self.assertFalse(membership(stock, D(100), D('1e13'), {'005930': ['테마']}))


class SwingTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_paper_trader.PaperTraderTest.asyncSetUp
    asyncTearDown = test_paper_trader.PaperTraderTest.asyncTearDown

    async def setup_swing(self):
        self.now = datetime(2026, 9, 28, 9, 30, tzinfo=KST)
        self.price = D(145)
        self.daily = history(self.now)
        async def candles(symbol, interval, count):
            if interval == '1d':
                if count >= 400:
                    return weekly_source(self.now)
                return self.daily
            if count <= 3:
                return [Candle(self.now, self.price, self.price, self.price, self.price, D(1000), Currency.KRW)]
            values = [D(150 + i * 2) for i in range(10)] + [D(150), D(148)]
            return [Candle((self.now - timedelta(days=6-i//2)).replace(hour=9 if i % 2 == 0 else 13),
                           value, value, value, value, D(1000), Currency.KRW)
                    for i, value in enumerate(values)]
        self.stock = {'symbol': '005930', 'name': '테스트 대형주', 'securityType': 'STOCK',
                      'isCommonShare': True, 'sharesOutstanding': '100000000000'}
        self.engine.toss_client = Mock(candles=AsyncMock(side_effect=candles), stock_info=AsyncMock(return_value=self.stock),
                                      stocks_info=AsyncMock(return_value=[self.stock]),
                                      prices=AsyncMock(side_effect=lambda symbols: [Quote('005930', self.price, Currency.KRW, self.now, 'toss')]))
        self.recommend = AsyncMock(return_value={'candidates': [{'symbol': '005930', 'name': '테스트 대형주', 'eligible': True}], 'diagnostics': {}})
        self.auto = SwingTrader(self.engine, self.recommend, lambda: self.now)
        self.engine.automation = self.auto
        await self.auto.restore()
        await self.auto.prepare()
        self.engine.running = True

    async def test_after_market_hours_allow_only_supported_session(self):
        await self.setup_swing()
        for hour, minute, expected in [
            (15, 29, True), (15, 30, False), (15, 59, False),
            (16, 0, True), (19, 59, True), (20, 0, False),
        ]:
            self.now = self.now.replace(hour=hour, minute=minute)
            self.assertEqual(self.auto.market_open(), expected, (hour, minute))
            self.assertEqual(self.auto.buy_window_open(), hour >= 16 and hour < 20)
        self.now = self.now.replace(month=10, day=3, hour=17)  # Saturday
        self.assertFalse(self.auto.market_open())
        self.assertFalse(self.auto.buy_window_open())

    async def test_three_percent_stop_loss(self):
        await self.setup_swing()
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY])
        self.price = D(140)
        self.now += timedelta(minutes=5)
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])
        self.assertFalse(self.broker.positions)
        self.assertEqual(self.auto.report()['days'][0]['status'], '청산 완료')
        traded_day = next(day for day in self.auto.days.values() if '005930' in day['targets'])
        self.assertIn('-3% 손절', traded_day['outcome'])

    async def test_restart_does_not_duplicate_buy_and_report_keeps_long_hold(self):
        await self.setup_swing()
        await self.auto.tick()
        self.now += timedelta(days=14)
        self.daily = history(self.now)
        resumed = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        await resumed.prepare()
        self.engine.automation = resumed
        await resumed.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(resumed.report()['days'][0]['status'], '보유 중')

    async def test_migration_preserves_existing_positions_and_exit_survives_scan_failure(self):
        await self.setup_swing()
        await self.auto.tick()
        await self.repository.save('morning_sessions', {'days': self.auto.days})
        original_load = self.repository.load
        async def legacy_load(name):
            return None if name == 'swing_sessions' else await original_load(name)
        resumed = SwingTrader(self.engine, self.recommend, lambda: self.now)
        with patch.object(self.repository, 'load', side_effect=legacy_load):
            await resumed.restore()
        self.now += timedelta(days=1)
        self.price = D(180)
        self.recommend.side_effect = RuntimeError('scan offline')
        await resumed.tick()
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        self.assertEqual(len(resumed.report()['days']), 1)

    async def test_rechecks_price_theme_and_market_cap_before_order(self):
        await self.setup_swing()
        self.price = D(160)
        await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.now += timedelta(minutes=5)
        self.price = D(145)
        self.stock['sharesOutstanding'] = '1'
        await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.now += timedelta(minutes=5)
        self.stock['sharesOutstanding'] = '100000000000'
        with patch('app.swing_trader.load_universe', return_value=(D('1e13'), {})):
            await self.auto.tick()
        self.assertFalse(self.broker.orders)

    async def test_market_hours_stale_quotes_and_kill_switch_block_orders(self):
        await self.setup_swing()
        for value in [self.now.replace(hour=8), self.now.replace(hour=15, minute=45), self.now + timedelta(days=5)]:
            self.now = value
            await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.now = datetime(2026, 9, 28, 16, tzinfo=KST)
        self.assertTrue(self.auto.market_open())
        self.assertTrue(self.auto.buy_window_open())
        self.engine.kill_switch = True
        await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.engine.kill_switch = False
        fresh_quote = self.auto.quote
        self.auto.quote = AsyncMock(side_effect=RuntimeError('stale quote'))
        await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.auto.quote = fresh_quote
        self.auto.next_scan = datetime.min.replace(tzinfo=KST)
        await self.auto.tick()
        self.assertEqual([order.side for order in self.broker.orders], [Side.BUY])

    async def test_recommendations_use_theme_universe_and_daily_signal(self):
        await self.setup_swing()
        with patch('app.swing_recommendations.load_universe', return_value=(D('1e13'), {'005930': ['테스트 테마']})):
            payload = await build_swing_recommendations(self.engine)
        self.assertEqual(payload['candidates'][0]['themes'], ['테스트 테마'])
        self.assertEqual(payload['candidates'][0]['strategy'], 'swing-v2-mtf-4h')
        self.assertEqual(payload['funnel']['universe'], 1)
        self.engine.toss_client.rankings.assert_not_called()

    async def test_recommendations_return_near_misses_when_no_exact_match(self):
        await self.setup_swing()
        self.price = D(149)
        with patch('app.swing_recommendations.load_universe', return_value=(D('1e12'), {'005930': ['테스트 테마']})):
            payload = await build_swing_recommendations(self.engine)
        self.assertFalse(payload['candidates'])
        self.assertEqual(payload['watchlist'][0]['symbol'], '005930')
        self.assertFalse(payload['watchlist'][0]['eligible'])
        self.assertEqual(payload['watchlist'][0]['conditions_total'], 3)
        self.assertIn('band_distance_percent', payload['watchlist'][0])

    async def test_signal_observations_are_persisted_for_data_collection(self):
        await self.setup_swing()
        self.price = D(160)
        with patch('app.swing_recommendations.load_universe', return_value=(D('1e12'), {'005930': ['테스트 테마']})):
            payload = await build_swing_recommendations(self.engine)
        inserted = await self.auto.store_observations(payload, 'manual')
        self.assertEqual(inserted, 1)
        self.assertEqual(await self.repository.signal_observation_count(), 1)
        row = (await self.repository.recent_signal_observations(1))[0]
        self.assertEqual(row['symbol'], '005930')
        self.assertEqual(row['source'], 'manual')
        self.assertEqual(row['conditions_passed'], 2)
        self.assertFalse(row['eligible'])

    async def test_one_share_paper_test_buy_uses_near_signal_and_is_tagged(self):
        await self.setup_swing()
        self.price = D(160)
        order = await self.auto.test_buy('005930')
        self.assertEqual(order.quantity, D(1))
        self.assertEqual(order.side, Side.BUY)
        self.assertIn('paper-test-buy', order.client_order_id)
        self.assertTrue(self.auto.session['targets']['005930']['test_entry'])
        self.assertEqual(self.auto.session['diagnostics']['test_buy_orders_filled'], 1)
        with self.assertRaisesRegex(RuntimeError, '이미 보유'):
            await self.auto.test_buy('005930')

    async def test_paper_test_buy_rejects_weak_signal_and_closed_market(self):
        await self.setup_swing()
        descending = history(self.now)
        for index, candle in enumerate(descending):
            candle.close_price = D(200-index)
        self.daily = descending
        with self.assertRaisesRegex(RuntimeError, '2개 이상'):
            await self.auto.test_buy('005930')
        self.daily = history(self.now)
        self.now = self.now.replace(hour=15, minute=45)
        with self.assertRaisesRegex(RuntimeError, '16:00'):
            await self.auto.test_buy('005930')

    async def test_pending_intent_is_not_reordered_after_restart(self):
        await self.setup_swing()
        self.auto.session['targets']['005930'] = {'symbol': '005930'}
        await self.auto.save()
        resumed = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        await resumed.tick()
        self.assertFalse(self.broker.orders)

    async def test_one_candle_timeout_keeps_other_qualified_candidates(self):
        await self.setup_swing()
        self.engine.toss_client.stocks_info.return_value = [self.stock, {**self.stock, 'symbol': '000660'}]
        self.engine.toss_client.prices.side_effect = None
        self.engine.toss_client.prices.return_value = [Quote(s, self.price, Currency.KRW, self.now, 'toss') for s in ['005930', '000660']]
        async def candles(symbol, interval, count):
            if symbol == '000660':
                raise TimeoutError('slow')
            if interval == '1d' and count >= 400:
                return weekly_source(self.now)
            if interval == '1m':
                values = [D(150 + i * 2) for i in range(10)] + [D(150), D(148)]
                return [Candle((self.now - timedelta(days=6-i//2)).replace(hour=9 if i % 2 == 0 else 13),
                               value, value, value, value, D(1000), Currency.KRW)
                        for i, value in enumerate(values)]
            return self.daily
        self.engine.toss_client.candles.side_effect = candles
        with patch('app.swing_recommendations.load_universe', return_value=(D('1e12'), {'005930': ['테마'], '000660': ['테마']})):
            result = await build_swing_recommendations(self.engine)
        self.assertEqual([c['symbol'] for c in result['candidates']], ['005930'])
        self.assertEqual(result['diagnostics']['rejection_counts']['시세 분석 조회 실패'], 1)

    async def test_budget_fees_limit_and_reuse_after_sale(self):
        from dataclasses import replace
        await self.setup_swing()
        self.engine.settings = self.broker.settings = replace(self.engine.settings, fee_rate=D('.001'), slippage_bps=D(5), max_order_amount_krw=D(1000))
        await self.auto.tick()
        buy = self.broker.orders[0]
        self.assertLessEqual(buy.filled_price * buy.quantity, D(1000))
        budget, invested, remaining = self.auto.capital()
        self.assertEqual(invested, buy.filled_price * buy.quantity + buy.fee)
        self.assertLessEqual(remaining + invested, budget)
        self.now += timedelta(days=1)
        self.price = D(180)
        await self.auto.tick()
        self.assertEqual(self.auto.capital()[1], 0)
        self.now += timedelta(days=1)
        self.price = D(145)
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL, Side.BUY])

    async def test_five_positions_and_stop_during_scan_block_buy(self):
        from app.models import Position
        await self.setup_swing()
        self.broker.positions = {str(i): Position(str(i), D(1), D(1), Currency.KRW) for i in range(5)}
        await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.broker.positions.clear()
        self.now += timedelta(minutes=5)
        async def stop_scan():
            self.engine.running = False
            return {'candidates': [{'symbol': '005930', 'eligible': True}], 'diagnostics': {}}
        self.recommend.side_effect = stop_scan
        await self.auto.tick()
        self.assertFalse(self.broker.orders)

    async def test_reset_does_not_restore_legacy_sessions(self):
        await self.setup_swing()
        await self.auto.tick()
        await self.repository.save('morning_sessions', {'days': self.auto.days})
        await self.broker.reset(D(1000000), D(1000))
        await self.auto.reset()
        resumed = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.assertFalse(resumed.days)

    async def test_manual_close_below_target_while_stopped_and_no_rebuy_after_restart(self):
        await self.setup_swing()
        await self.auto.tick()
        self.engine.running = False
        self.price = D(140)
        self.now += timedelta(days=1)
        orders = await self.auto.manual_close('005930')
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, Side.SELL)
        self.assertFalse(self.broker.positions)
        self.assertEqual(self.auto.report()['days'][0]['status'], '청산 완료')
        resumed = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.engine.running = True
        await resumed.tick()
        self.assertEqual(len(self.broker.orders), 2)
        with self.assertRaisesRegex(RuntimeError, '보유하지 않은'):
            await resumed.manual_close('005930')

    async def test_concurrent_manual_requests_create_only_one_sell(self):
        import asyncio
        await self.setup_swing()
        await self.auto.tick()
        results = await asyncio.gather(self.auto.manual_close('005930'), self.auto.manual_close('005930'), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, RuntimeError) for r in results), 1)
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])

    async def test_manual_close_guards_and_quote_failure_preserve_holdings(self):
        await self.setup_swing()
        await self.auto.tick()
        self.engine.kill_switch = True
        with self.assertRaisesRegex(RuntimeError, '킬 스위치'):
            await self.auto.manual_close('005930')
        self.engine.kill_switch = False
        self.now = self.now.replace(hour=16)
        self.auto.next_scan = datetime.min.replace(tzinfo=KST)
        await self.auto.tick()
        self.assertIn('005930', self.broker.positions)
        self.auto.quote = AsyncMock(side_effect=RuntimeError('stale after-hours quote'))
        with self.assertRaisesRegex(RuntimeError, 'stale after-hours quote'):
            await self.auto.manual_close('005930')
        self.auto.quote = AsyncMock(side_effect=RuntimeError('시세 없음'))
        with self.assertRaisesRegex(RuntimeError, '시세 없음'):
            await self.auto.manual_close('005930')
        self.assertEqual(len(self.broker.orders), 1)
        self.assertIn('005930', self.broker.positions)

    async def test_manual_close_adopts_untracked_holding(self):
        await self.setup_swing()
        await self.broker.place_market_order(client_order_id='external-buy',
            quote=Quote('005930', D(140), Currency.KRW, self.now, 'toss'), side=Side.BUY, quantity=D(10))
        await self.auto.manual_close('005930')
        self.assertFalse(self.broker.positions)
        self.assertEqual(self.auto.report()['days'][0]['profit'], '50')

    async def test_manual_close_api_serializes_orders_and_reports_conflict(self):
        from app.main import create_app
        from starlette.requests import Request
        from fastapi import HTTPException
        await self.setup_swing()
        await self.auto.tick()
        app = create_app(self.engine.settings)
        app.state.engine = self.engine
        request = Request({'type': 'http', 'app': app})
        endpoint = next(r.endpoint for r in app.routes if getattr(r, 'path', '') == '/api/v1/paper/positions/{symbol}/close')
        result = await endpoint('005930', request)
        self.assertEqual(result['orders'][0]['side'], 'SELL')
        self.assertEqual(result['orders'][0]['status'], 'FILLED')
        with self.assertRaises(HTTPException) as context:
            await endpoint('005930', request)
        self.assertEqual(context.exception.status_code, 409)

    async def test_afternoon_buy_after_no_morning_signal_and_afternoon_sell(self):
        await self.setup_swing()
        self.price = D(160)
        await self.auto.tick()
        self.assertFalse(self.broker.orders)
        self.assertEqual(self.auto.session['outcome'], '매수 조건 대기')
        self.now = self.now.replace(hour=13, minute=59)
        self.price = D(145)
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY])
        self.now = self.now.replace(hour=15, minute=20)
        self.price = D(180)
        await self.auto.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])

    async def test_start_in_afternoon_and_stop_at_market_close(self):
        await self.setup_swing()
        self.now = self.now.replace(hour=16, minute=0)
        await self.auto.prepare()
        self.assertTrue(self.auto.status()['market_open'])
        await self.auto.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.now = self.now.replace(hour=20, minute=0)
        self.price = D(180)
        await self.auto.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertFalse(self.auto.status()['market_open'])
        self.assertIsNone(self.auto.status()['next_scan_at'])
        self.assertIn('장외 대기', self.auto.status()['execution_state'])

    async def test_legacy_no_entry_diagnostics_do_not_block_afternoon(self):
        await self.setup_swing()
        self.auto.session.pop('strategy', None)
        self.auto.session['outcome'] = '진입 없음'
        self.auto.session['attempted'] = True
        self.auto.session['diagnostics'] = {'legacy_untracked': True}
        self.now = self.now.replace(hour=13, minute=59)
        await self.auto.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertFalse(self.auto.diagnostics()['legacy_untracked'])

    async def test_adopts_unmanaged_positions_without_fake_buys_and_sells_once(self):
        await self.setup_swing()
        await self.broker.place_market_order(client_order_id='old-threshold-buy',
            quote=Quote('005930', D(140), Currency.KRW, self.now, 'toss'), side=Side.BUY, quantity=D(10))
        resumed = SwingTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        await resumed.restore()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(len(resumed.report()['days']), 1)
        self.assertEqual(resumed.report()['days'][0]['cost'], '1400')
        self.assertEqual(resumed.report()['days'][0]['status'], '기존 보유 승계')
        self.price = D(180)
        await resumed.tick()
        self.assertEqual(len(self.broker.orders), 2)
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        self.assertEqual(resumed.report()['days'][0]['profit'], '400')
        self.now += timedelta(minutes=5)
        await resumed.tick()
        self.assertEqual(len(self.broker.orders), 2)
