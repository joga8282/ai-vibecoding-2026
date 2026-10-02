from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock

from app.models import Candle, Currency, Quote, Side, ThresholdStrategy, utc_now
from app.recommendations import analyze_candidate
from tests import test_paper_trader


def trend_candles():
    start = datetime.now(timezone.utc) - timedelta(days=119)
    result = []
    for index in range(120):
        price = D('100') + D(index) * D('.1') + D([0, 1, 2, 1, 0, -1, -2, -1][index % 8])
        result.append(Candle(start + timedelta(days=index), price, price + 8, price - 3, price, D(100), Currency.KRW))
    return result


class RecommendationFiltersTest(TestCase):
    def test_uptrend_lower_band_recovery_qualifies(self):
        candles = trend_candles()
        result = analyze_candidate(candles, D('110.6'), D('.01'))
        self.assertTrue(result['uptrend'])
        self.assertTrue(result['bollinger_touched'])
        self.assertTrue(result['eligible'])
        self.assertEqual(result['bollinger_lower'], '108.09')

    def test_lower_band_can_qualify_without_daily_support_touch(self):
        candles = trend_candles()
        # Lift prior local lows so the remaining support lies farther below price.
        for candle in candles[-60:-1]:
            candle.low_price = D('104')
        candles[-1].low_price = D('108')
        result = analyze_candidate(candles, D('110.6'), D('.01'))
        self.assertFalse(result['support_touched'])
        self.assertTrue(result['bollinger_touched'])
        self.assertTrue(result['eligible'])

    def test_lower_band_breakdown_is_not_a_recovery(self):
        candles = trend_candles()
        result = analyze_candidate(candles, D('107'), D('-.03'))
        self.assertFalse(result['bollinger_touched'])
        self.assertFalse(result['eligible'])

    def test_downtrend_is_excluded(self):
        candles = trend_candles()
        for index, candle in enumerate(candles):
            candle.close_price = D(200) - index
            candle.low_price = candle.close_price - 3
            candle.high_price = candle.close_price + 8
        result = analyze_candidate(candles, candles[-1].close_price, D('0'))
        self.assertFalse(result['uptrend'])
        self.assertFalse(result['eligible'])

    def test_daily_overheating_boundary(self):
        candles = trend_candles()
        for rate, eligible in [(D('.0699'), True), (D('.07'), False)]:
            with self.subTest(rate=rate):
                result = analyze_candidate(candles, D('110.6'), rate)
                self.assertEqual(result['eligible'], eligible)

    def test_flat_market_is_not_overbought_or_a_band_touch(self):
        candles = trend_candles()
        for candle in candles:
            candle.close_price = candle.low_price = candle.high_price = D(100)
        result = analyze_candidate(candles, D(100), D(0))
        self.assertEqual(result['rsi'], '50.0')
        self.assertFalse(result['bollinger_touched'])
        self.assertFalse(result['eligible'])

    def test_insufficient_history(self):
        self.assertIsNone(analyze_candidate(trend_candles()[:64], D(100), D(0)))

    def test_excessive_rally_is_excluded(self):
        candles = trend_candles()
        result = analyze_candidate(candles, D(140), D('.01'))
        self.assertTrue(result['overheated'])
        self.assertFalse(result['eligible'])
        self.assertIn('5거래일 10% 이상 상승', result['overheating_reasons'])
        self.assertIn('20거래일 20% 이상 상승', result['overheating_reasons'])
        self.assertIn('20일선 대비 8% 이상 상승', result['overheating_reasons'])


class BuyFilterTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_paper_trader.PaperTraderTest.asyncSetUp
    asyncTearDown = test_paper_trader.PaperTraderTest.asyncTearDown

    async def test_buy_filter_passes_and_reuses_candles(self):
        candles = trend_candles()
        self.engine.toss_client = Mock(candles=AsyncMock(return_value=candles))
        quote = Quote('005930', D('110.6'), Currency.KRW, utc_now())
        self.assertTrue(await self.engine._buy_allowed(quote))
        self.assertTrue(await self.engine._buy_allowed(quote))
        self.engine.toss_client.candles.assert_awaited_once()
        quote.price = D(130)
        self.assertFalse(await self.engine._buy_allowed(quote))

    async def test_missing_or_failed_data_blocks_buys(self):
        quote = Quote('005930', D(110), Currency.KRW, utc_now())
        self.assertFalse(await self.engine._buy_allowed(quote))
        self.engine.toss_client = Mock(candles=AsyncMock(side_effect=RuntimeError('offline')))
        self.assertFalse(await self.engine._buy_allowed(quote))
        self.engine.toss_client.candles = AsyncMock(return_value=trend_candles()[:20])
        self.assertFalse(await self.engine._buy_allowed(quote))

    async def test_blocked_buy_retries_and_sell_bypasses_filter(self):
        strategy = ThresholdStrategy('filter-test', '005930', Currency.KRW, D(1), D(115), D(120))
        self.engine.set_quote(Quote('005930', D(110), Currency.KRW, utc_now()))
        self.engine._buy_allowed = AsyncMock(return_value=False)
        await self.engine._evaluate(strategy)
        self.assertEqual(len(self.broker.orders), 0)
        self.engine._buy_allowed.return_value = True
        await self.engine._evaluate(strategy)
        self.assertEqual(self.broker.orders[-1].side, Side.BUY)
        self.engine._buy_allowed = AsyncMock(side_effect=AssertionError('Sell must bypass buy filter'))
        self.engine.set_quote(Quote('005930', D(125), Currency.KRW, utc_now()))
        await self.engine._evaluate(strategy)
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)

    async def test_failed_quote_refresh_blocks_buy(self):
        strategy = ThresholdStrategy('refresh-test', '005930', Currency.KRW, D(1), D(115), D(120))
        await self.engine.upsert_strategy(strategy)
        self.engine.set_quote(Quote('005930', D(110), Currency.KRW, utc_now()))
        self.engine.toss_client = Mock(prices=AsyncMock(side_effect=RuntimeError('offline')))
        self.engine._buy_allowed = AsyncMock(return_value=True)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 0)
        self.engine._buy_allowed.assert_not_awaited()
