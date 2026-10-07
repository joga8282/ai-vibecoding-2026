"""Profit gates and recovery use mock quotes, candles and storage only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from app.config import Settings
from app.models import Candle, Currency, Position, Quote
from app.swing_profit import estimate_exit_profit
from app.swing_signals import KST, four_hour_exit_signal
from app.swing_trader import SwingTrader


class ProfitEstimateTest(TestCase):
    def setUp(self):
        self.position = Position('005930', D(2), D(100), Currency.KRW)
        self.settings = Settings(mode='live', fee_rate=D('.00015'), slippage_bps=D(5))

    def test_gross_three_percent_does_not_pass_net_three_percent(self):
        result = estimate_exit_profit(self.position, D(103), self.settings)
        self.assertFalse(result['meets_minimum'])
        self.assertLess(result['net_return_percent'], D(3))
        self.assertGreater(result['estimated_sell_tax'], D(0))

    def test_buy_fee_sell_fee_tax_and_slippage_are_deducted(self):
        result = estimate_exit_profit(self.position, D(104), self.settings)
        cost = D(200) * D('1.00015')
        proceeds = D(208) * D('.9995') * D('.99785')
        self.assertEqual(result['purchase_cost'], cost)
        self.assertEqual(result['net_proceeds'], proceeds)
        self.assertEqual(result['net_return_percent'], (proceeds - cost) / cost * 100)
        self.assertTrue(result['meets_minimum'])

    def test_exact_three_percent_boundary_uses_unrounded_return(self):
        settings = replace(self.settings, fee_rate=D(0), slippage_bps=D(0), live_sell_tax_rate=D(0))
        self.assertTrue(estimate_exit_profit(self.position, D(103), settings)['meets_minimum'])
        result = estimate_exit_profit(self.position, D('102.9999'), settings)
        self.assertEqual(result['net_return_percent'].quantize(D('.01')), D('3.00'))
        self.assertFalse(result['meets_minimum'])

    def test_paper_matches_paper_fee_model_without_live_tax(self):
        settings = replace(self.settings, mode='paper')
        result = estimate_exit_profit(self.position, D(104), settings)
        self.assertEqual(result['estimated_sell_tax'], D(0))
        self.assertEqual(result['net_proceeds'], D(208) * D('.9995') * D('.99985'))

    def test_invalid_inputs_block_take_profit(self):
        for price in (None, D(0), D('-1'), D('NaN'), D('Infinity')):
            with self.subTest(price=price):
                self.assertFalse(estimate_exit_profit(self.position, price, self.settings)['ready'])
        for changes in ({'fee_rate': D('NaN')}, {'fee_rate': D(1)}, {'live_sell_tax_rate': D(1)},
                        {'slippage_bps': D(-1)}, {'slippage_bps': D(10000)},
                        {'swing_min_net_profit_percent': D('NaN')}):
            with self.subTest(changes=changes):
                self.assertFalse(estimate_exit_profit(self.position, D(104), replace(self.settings, **changes))['ready'])


class ProfitExitTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 10, 7, 10, 56, tzinfo=KST)
        self.settings = Settings(mode='live', fee_rate=D(0), slippage_bps=D(0), live_sell_tax_rate=D(0))
        self.saved = {}
        async def save(name, payload):
            self.saved[name] = deepcopy(payload)
        repository = Mock(save=AsyncMock(side_effect=save),
                          load=AsyncMock(side_effect=lambda name: deepcopy(self.saved.get(name))),
                          signal_observation_count=AsyncMock(return_value=0))
        self.engine = SimpleNamespace(settings=self.settings, repository=repository,
                                      candles=AsyncMock(return_value=[]),
                                      broker=SimpleNamespace(positions={}, orders=[]))
        self.auto = SwingTrader(self.engine, AsyncMock(), lambda: self.now)
        self.day = {'id': 'mock', 'date': self.now.date().isoformat(), 'targets': {}}
        self.auto.days = {self.day['date']: self.day}
        self.position = Position('005930', D(1), D(100), Currency.KRW)

    async def check(self, price, *, high=None, upper=False, touch=True, last=None, ready=True):
        price = D(price)
        last = price if last is None else D(last)
        quote = Quote('005930', last, Currency.KRW, self.now, 'toss', price, max(price, last))
        baseline = {'ready': ready, 'upper_touched': touch, 'sell_at_upper': upper,
                    'active_high': str(price if high is None else high), 'bollinger_upper': '105'}
        with patch('app.swing_trader.four_hour_exit_signal', return_value=baseline):
            return await self.auto.exit_signal('005930', quote, self.position, self.day)

    async def test_upper_touch_below_three_percent_waits(self):
        signal = await self.check('102.9', upper=True)
        self.assertFalse(signal['take_profit'])
        self.assertFalse(signal['profit_trailing_armed'])
        self.assertFalse(signal['sell'])
        self.assertIsNone(signal['exit_reason'])

    async def test_three_percent_requires_upper_condition(self):
        signal = await self.check('103', upper=False, touch=False)
        self.assertFalse(signal['sell'])
        signal = await self.check('103', upper=True)
        self.assertTrue(signal['take_profit'])
        self.assertTrue(signal['sell'])
        self.assertIn('예상 순수익 3%', signal['exit_reason'])

    async def test_five_percent_peak_does_not_leave_three_percent_after_drawdown(self):
        signal = await self.check('102', high='105')
        self.assertFalse(signal['profit_trailing_armed'])
        self.assertFalse(signal['trailing_exit'])
        self.assertFalse(signal['sell'])

    async def test_samsung_old_low_profit_exit_is_not_armed(self):
        self.engine.settings = replace(self.settings, fee_rate=D('.00015'), slippage_bps=D(5), live_sell_tax_rate=D('.002'))
        self.position.average_price = D(271500)
        self.day['targets']['005930'] = {'upper_band_touched': True, 'exit_peak': '279500', 'adopted': True}
        signal = await self.check('272500', high='279500', touch=False)
        self.assertFalse(signal['sell'])
        self.assertFalse(signal['profit_trailing_armed'])
        self.assertEqual(self.day['targets']['005930']['exit_peak'], '279500')

    async def test_costs_can_prevent_activation_despite_gross_three_percent_at_trigger(self):
        self.engine.settings = replace(self.settings, fee_rate=D('.00015'), slippage_bps=D(5), live_sell_tax_rate=D('.002'))
        signal = await self.check('104', high='105.2')
        self.assertFalse(signal['profit_trailing_armed'])
        self.assertLess(D(signal['estimated_trigger_net_return_percent']), D(3))
        signal = await self.check('104', high='106')
        self.assertTrue(signal['profit_trailing_armed'])

    async def test_armed_trailing_exit_protects_below_three_percent(self):
        signal = await self.check('104', high='106')
        self.assertTrue(signal['profit_trailing_armed'])
        self.assertFalse(signal['sell'])
        self.now += timedelta(minutes=5)
        signal = await self.check('102', touch=False)
        self.assertTrue(signal['trailing_exit'])
        self.assertTrue(signal['sell'])
        self.assertLess(D(signal['estimated_net_return_percent']), D(3))
        self.assertIn('보호 매도', signal['exit_reason'])

    async def test_restart_and_date_change_keep_armed_state(self):
        await self.check('104', high='106')
        self.now += timedelta(days=1)
        self.auto = SwingTrader(self.engine, AsyncMock(), lambda: self.now)
        await self.auto.restore()
        self.day = next(iter(self.auto.days.values()))
        signal = await self.check('102', touch=False)
        self.assertTrue(signal['trailing_exit'])
        self.assertEqual(self.day['targets']['005930']['profit_trailing_min_percent'], '3')
        self.assertTrue(self.day['targets']['005930']['profit_trailing_armed_at'].startswith('2026-10-07'))

    async def test_live_uses_bid_instead_of_higher_last_price(self):
        signal = await self.check('102.9', last='104', upper=True)
        self.assertFalse(signal['take_profit'])
        self.assertFalse(signal['sell'])
        self.assertEqual(self.day['targets']['005930']['last_exit_check']['sell_reference_price'], '102.9')

    async def test_spread_is_reserved_at_future_trailing_trigger(self):
        signal = await self.check('104', last='105', high='106')
        self.assertEqual(signal['trailing_trigger_price'], '102.88')
        self.assertFalse(signal['profit_trailing_armed'])

    async def test_stop_loss_ignores_profit_gate_and_candle_failure(self):
        self.engine.candles.side_effect = ConnectionError('mock candle outage')
        quote = Quote('005930', D(97), Currency.KRW, self.now, 'toss', D(97), D(98))
        signal = await self.auto.exit_signal('005930', quote, self.position, self.day)
        self.assertTrue(signal['stop_loss'])
        self.assertTrue(signal['sell'])
        self.assertIn('-3% 손절', signal['exit_reason'])

    async def test_armed_protection_survives_candle_failure(self):
        await self.check('104', high='106')
        self.engine.candles.side_effect = ConnectionError('mock candle outage')
        quote = Quote('005930', D(102), Currency.KRW, self.now, 'toss', D(102), D(103))
        signal = await self.auto.exit_signal('005930', quote, self.position, self.day)
        self.assertTrue(signal['trailing_exit'])
        self.assertTrue(signal['sell'])

    async def test_unarmed_position_does_not_use_candle_failure_to_start_trailing(self):
        self.engine.candles.side_effect = ConnectionError('mock candle outage')
        quote = Quote('005930', D(104), Currency.KRW, self.now, 'toss', D(104), D(105))
        with self.assertRaises(ConnectionError):
            await self.auto.exit_signal('005930', quote, self.position, self.day)


class ExitCandleValidityTest(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 10, 56, tzinfo=KST)
        self.bars = [Candle((self.now - timedelta(days=3-i//2)).replace(hour=9 if i%2 == 0 else 13),
                           D(100+i), D(100+i), D(100+i), D(100+i), D(10), Currency.KRW)
                     for i in range(6)]

    def test_future_candle_cannot_arm_trailing(self):
        baseline = four_hour_exit_signal(self.bars, D(100), self.now)
        future = Candle(self.now + timedelta(hours=3), D(1000), D(1000), D(1000), D(1000), D(10), Currency.KRW)
        self.assertEqual(four_hour_exit_signal(self.bars + [future], D(100), self.now), baseline)
        self.assertFalse(baseline['upper_touched'])

    def test_stale_and_invalid_ohlc_block_new_profit_signals(self):
        self.assertFalse(four_hour_exit_signal(self.bars, D(200), self.now + timedelta(days=8))['ready'])
        self.bars[-1].high_price = D(1)
        self.assertFalse(four_hour_exit_signal(self.bars, D(200), self.now)['ready'])
