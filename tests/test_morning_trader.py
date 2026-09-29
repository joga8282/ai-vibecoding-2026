from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, AsyncMock

from app.morning_trader import MorningTrader, morning_signal, KST
from app.models import Candle, Currency, Side
from tests import test_paper_trader


class MorningTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_paper_trader.PaperTraderTest.asyncSetUp
    asyncTearDown = test_paper_trader.PaperTraderTest.asyncTearDown

    async def setup_morning(self):
        self.now = datetime(2026, 9, 28, 8, 2, tzinfo=KST)
        self.price = D(100)
        self.daily = [Candle(self.now - timedelta(days=20-i), D(101), D(103), D(98), D(100+i%2*2), D(1000), Currency.KRW) for i in range(20)]
        def bars():
            return [Candle(self.now-timedelta(minutes=1), D(99), D(100), D(98), D(99), D(200), Currency.KRW),
                    Candle(self.now, D('99.5'), max(D(101), self.price), D('98.5'), self.price, D(500), Currency.KRW)]
        self.bars = bars
        async def candles(symbol, interval, count):
            return self.daily if interval == '1d' else bars()
        self.engine.toss_client = Mock(candles=AsyncMock(side_effect=candles))
        self.recommend = AsyncMock(return_value={'candidates': [{'symbol': '005930', 'name': '삼성전자', 'eligible': True}]})
        self.auto = MorningTrader(self.engine, self.recommend, lambda: self.now)
        self.engine.automation = self.auto
        await self.auto.prepare()
        self.engine.running = True

    async def test_signal_requires_live_volume_and_rebound(self):
        await self.setup_morning()
        self.assertTrue(morning_signal(self.daily, self.bars(), self.now)['eligible'])
        bars = self.bars()
        for b in bars:
            b.volume = D(0)
        self.assertIn('당일 거래 분봉 2개 미만', morning_signal(self.daily, bars, self.now)['rejection_reasons'])
        self.assertIn('최신 거래 분봉 120초 초과', morning_signal(self.daily, self.bars(), self.now + timedelta(minutes=3))['rejection_reasons'])
        self.price = D(98)
        self.assertFalse(morning_signal(self.daily, self.bars(), self.now)['eligible'])

    async def test_once_daily_restart_and_net_profit_report(self):
        await self.setup_morning()
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        resumed = MorningTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.engine.automation = resumed
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.price = D('102.5')
        self.now += timedelta(minutes=11)
        await self.engine.tick()
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        row = resumed.report()['days'][-1]
        self.assertEqual(row['return_percent'], '2.50')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)

    async def test_no_entry_before_after_window_or_weekend(self):
        await self.setup_morning()
        for value in [self.now.replace(hour=7), self.now.replace(minute=5), self.now + timedelta(days=5)]:
            self.now = value
            await self.engine.tick()
        self.assertFalse(self.broker.orders)

    async def test_forced_close_and_pending_report(self):
        await self.setup_morning()
        await self.engine.tick()
        self.assertEqual(self.auto.report()['days'][-1]['status'], '미청산')
        self.now = self.now.replace(hour=15, minute=10)
        await self.engine.tick()
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        self.assertEqual(self.auto.session['outcome'], '시간 청산')

    async def test_failed_close_remains_pending_and_blocks_new_entry(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now = self.now.replace(hour=15, minute=10)
        self.engine.toss_client.candles.side_effect = RuntimeError('offline')
        await self.engine.tick()
        self.assertEqual(self.auto.report()['days'][-1]['status'], '미청산')
        self.now = (self.now+timedelta(days=1)).replace(hour=8, minute=2)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)

    async def test_new_day_gets_one_new_entry_and_keeps_history(self):
        await self.setup_morning()
        await self.engine.tick()
        self.price = D(103)
        self.now += timedelta(minutes=11)
        await self.engine.tick()
        self.now = (self.now + timedelta(days=1)).replace(hour=8, minute=2)
        self.price = D(100)
        await self.engine.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL, Side.BUY])
        self.assertEqual(len(self.auto.days), 2)
        self.assertEqual(self.auto.report()['days'][-2]['return_percent'], '3.00')

    async def test_early_surge_tracks_new_peak_and_sells_at_three_percent(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now += timedelta(minutes=1)
        self.price = D('102')
        await self.engine.tick()
        target = self.auto.session['targets']['005930']
        self.assertTrue(target['trailing_active'])
        self.price = D('110')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(D(target['peak_price']), D(110))
        self.price = D('106.71')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.price = D('106.70')
        await self.engine.tick()
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        self.assertEqual(self.auto.session['outcome'], '급등 후 고점 대비 3% 추적 매도')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)

    async def test_trailing_peak_survives_restart_and_time_window(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now += timedelta(minutes=10)
        self.price = D(110)
        await self.engine.tick()
        resumed = MorningTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.engine.automation = resumed
        self.now += timedelta(minutes=20)
        self.price = D('106.7')
        await self.engine.tick()
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        self.assertEqual(resumed.session['outcome'], '급등 후 고점 대비 3% 추적 매도')

    async def test_late_surge_uses_fixed_profit_and_time_exit_overrides_trailing(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now += timedelta(minutes=10, seconds=1)
        self.price = D(103)
        await self.engine.tick()
        self.assertEqual(self.auto.session['outcome'], '익절')

    async def test_time_exit_overrides_active_trailing(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now += timedelta(minutes=1)
        self.price = D(110)
        await self.engine.tick()
        self.now = self.now.replace(hour=15, minute=10)
        await self.engine.tick()
        self.assertEqual(self.auto.session['outcome'], '시간 청산')

    async def test_ten_tranches_use_cash_and_sell_entire_position(self):
        await self.setup_morning()
        await self.engine.tick()
        self.assertEqual(D(self.auto.session['budget']), D('1000000'))
        self.assertEqual(self.broker.orders[0].quantity, D(1000))
        for index in range(1, 10):
            self.now += timedelta(minutes=20)
            self.price = D(100) * (1 - D('.002') * index)
            await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 10)
        self.assertLess(self.broker.cash[Currency.KRW], self.price)
        self.assertGreaterEqual(self.broker.cash[Currency.KRW], 0)
        total_quantity = sum(o.quantity for o in self.broker.orders)
        self.now = self.now.replace(hour=15, minute=10)
        await self.engine.tick()
        self.assertEqual(self.broker.orders[-1].quantity, total_quantity)
        self.assertEqual(len(self.broker.orders), 11)
        self.assertFalse(self.broker.positions)
        self.assertEqual(self.auto.report()['days'][-1]['status'], '청산 완료')

    async def test_pullback_does_not_repeat_same_bar_or_buy_on_rise(self):
        await self.setup_morning()
        await self.engine.tick()
        self.price = D('99.8')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.now += timedelta(minutes=20)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)
        self.now += timedelta(minutes=20)
        self.price = D('100.1')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)

    async def test_sell_signal_precedes_additional_buy(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now += timedelta(minutes=1)
        self.price = D(96)
        await self.engine.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])

    async def test_split_resume_uses_ledger_without_repeating_tranches(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now += timedelta(minutes=20)
        self.price = D('99.8')
        await self.engine.tick()
        resumed = MorningTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.engine.automation = resumed
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)
        self.now += timedelta(minutes=20)
        self.price = D('99.6')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 3)

    async def test_tranche_budget_includes_costs_even_above_old_order_cap(self):
        from dataclasses import replace
        await self.setup_morning()
        self.engine.settings = self.broker.settings = replace(self.engine.settings, fee_rate=D('.001'), slippage_bps=D(5), max_order_amount_krw=D(1000))
        await self.engine.tick()
        order = self.broker.orders[0]
        self.assertEqual(order.status.value, 'FILLED')
        self.assertLessEqual(order.filled_price * order.quantity + order.fee, D(100000))

    async def test_pending_unsent_tranche_does_not_duplicate_after_restart(self):
        await self.setup_morning()
        await self.engine.tick()
        self.auto.session['pending_tranche'] = 2
        await self.auto.save()
        resumed = MorningTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.engine.automation = resumed
        self.now += timedelta(minutes=20)
        self.price = D('99.8')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)

    async def test_pullback_waits_full_twenty_minutes(self):
        await self.setup_morning()
        await self.engine.tick()
        self.price = D('99.8')
        self.now += timedelta(minutes=19, seconds=59)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        resumed = MorningTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.engine.automation = resumed
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.now += timedelta(seconds=1)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)

    async def test_unmet_pullback_waits_until_next_twenty_minute_check(self):
        await self.setup_morning()
        await self.engine.tick()
        self.now += timedelta(minutes=20)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.now += timedelta(minutes=1)
        self.price = D('99.8')
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.now += timedelta(minutes=19)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 2)

    async def test_scan_diagnostics_persist_rejections_and_attempt_counts(self):
        await self.setup_morning()
        self.recommend.return_value = {
            'candidates': [], 'funnel': {'universe': 100, 'budget_liquidity': 30, 'risk_filtered': 12, 'analyzed': 12},
            'diagnostics': {'analyzed_count': 12, 'qualified_count': 0,
                            'rejection_counts': {'볼린저 하단 미접촉': 8, '직전 분봉 대비 상승 아님': 4},
                            'symbols': [{'symbol': '005930', 'name': '삼성전자', 'eligible': False,
                                         'reasons': ['볼린저 하단 미접촉']}]}}
        await self.engine.tick()
        data = self.auto.diagnostics()
        self.assertEqual(data['scan_count'], 1)
        self.assertEqual(data['analyzed_count'], 12)
        self.assertEqual(data['buy_order_attempts'], 0)
        self.assertEqual(data['rejection_counts']['볼린저 하단 미접촉'], 8)
        resumed = MorningTrader(self.engine, self.recommend, lambda: self.now)
        await resumed.restore()
        self.assertEqual(resumed.diagnostics()['last_symbols'][0]['name'], '삼성전자')

    async def test_scan_error_and_filled_buy_are_counted_separately(self):
        await self.setup_morning()
        self.recommend.side_effect = RuntimeError('API offline')
        await self.engine.tick()
        self.assertEqual(self.auto.diagnostics()['scan_errors'], 1)
        self.recommend.side_effect = None
        self.recommend.return_value = {'candidates': [{'symbol': '005930', 'name': '삼성전자', 'eligible': True}],
                                        'diagnostics': {'analyzed_count': 1, 'qualified_count': 1}, 'funnel': {}}
        self.now += timedelta(seconds=30)
        await self.engine.tick()
        data = self.auto.diagnostics()
        self.assertEqual(data['scan_count'], 2)
        self.assertEqual(data['buy_order_attempts'], 1)
        self.assertEqual(data['buy_orders_filled'], 1)
