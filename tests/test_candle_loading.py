"""Candle reads, pacing and chart cache tests without network/account access."""
import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.models import Candle, Currency
from app.toss import TossCandleRateLimitError, TossMarketClient
from tests import test_paper_trader


def bar(index=0):
    return Candle(datetime(2026, 10, 6, 0, tzinfo=timezone.utc) - timedelta(minutes=index),
                  D(100), D(110), D(90), D(105), D(10), Currency.KRW)


def limited(headers=None):
    return HTTPError('https://openapi.tossinvest.com/api/v1/candles?symbol=082740',
                     429, 'Too Many Requests', headers or {}, io.BytesIO(b'private response'))


class CandleRateTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TossMarketClient('mock-client', 'mock-secret')
        self.client._access_token = AsyncMock(return_value='mock-token')
        self.now = 100.0
        self.client._candle_clock = lambda: self.now
        async def sleep(delay):
            self.now += delay
        self.client._candle_sleep = AsyncMock(side_effect=sleep)
        self.thread_patch = patch('app.toss.asyncio.to_thread',
                                  new=AsyncMock(side_effect=lambda fn, *args: fn(*args)))
        self.thread_patch.start()
        self.jitter_patch = patch('app.toss.random.uniform', return_value=0)
        self.jitter_patch.start()

    async def asyncTearDown(self):
        self.thread_patch.stop()
        self.jitter_patch.stop()

    async def test_shared_rate_paces_different_symbols_and_intervals(self):
        calls = []
        def page(*args):
            calls.append((args[1], self.now))
            return [bar()], None
        self.client._candles_page_sync = Mock(side_effect=page)
        await asyncio.gather(self.client.candles('082740', '1m', 1),
                             self.client.candles('005930', '1d', 1))
        self.assertEqual(calls, [('082740', 100), ('005930', 100.25)])

    async def test_429_retries_same_page_after_provider_delay(self):
        error = limited({'Retry-After': '2'})
        self.client._candles_page_sync = Mock(side_effect=[error, ([bar()], None)])
        self.assertEqual(await self.client.candles('082740', '1m', 1), [bar()])
        self.assertEqual(self.now, 102)
        self.assertEqual(self.client._candles_page_sync.call_count, 2)
        self.assertEqual(self.client._candles_page_sync.call_args_list[0],
                         self.client._candles_page_sync.call_args_list[1])
        self.assertTrue(error.fp.closed)

    async def test_repeated_429_is_bounded_and_returns_safe_diagnostic(self):
        self.client._candles_page_sync = Mock(side_effect=lambda *args: (_ for _ in ()).throw(limited()))
        with self.assertRaises(TossCandleRateLimitError) as caught:
            await self.client.candles('082740', '1m', 1)
        self.assertEqual(self.client._candles_page_sync.call_count, 3)
        self.assertEqual(caught.exception.retry_after, 4)
        self.assertEqual(self.now, 103)
        self.assertNotIn('private', str(caught.exception))
        self.assertNotIn('mock-token', str(caught.exception))

    async def test_long_429_cooldown_is_shared_and_never_retried_early(self):
        self.client._candles_page_sync = Mock(side_effect=limited({'Retry-After': '60'}))
        for symbol in ('082740', '005930'):
            with self.assertRaises(TossCandleRateLimitError) as caught:
                await self.client.candles(symbol, '1m', 1)
            self.assertEqual(caught.exception.retry_after, 60)
        self.client._candles_page_sync.assert_called_once()
        self.client._candle_sleep.assert_not_awaited()

    async def test_provider_headers_reduce_speed_and_pause_empty_bucket(self):
        self.client._update_candle_rate({'X-RateLimit-Limit': '2',
                                        'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1'})
        self.client._candles_page_sync = Mock(return_value=([bar()], None))
        await self.client.candles('082740', '1m', 1)
        await self.client.candles('005930', '1d', 1)
        self.assertAlmostEqual(self.now, 101.55)

    async def test_invalid_headers_do_not_disable_pacing_or_retries(self):
        headers = {'X-RateLimit-Limit': 'NaN', 'X-RateLimit-Reset': 'inf', 'Retry-After': 'invalid'}
        self.client._update_candle_rate(headers)
        self.assertEqual(self.client._candle_interval, .25)
        self.assertEqual(self.client._candle_retry_delay(headers, 1), 2)

    async def test_inclusive_page_cursor_does_not_drop_last_requested_candle(self):
        pages = [([bar(i) for i in range(200)], 'older'), ([bar(199), bar(200)], None)]
        self.client._candles_page_sync = Mock(side_effect=pages)
        result = await self.client.candles('082740', '1m', 201)
        self.assertEqual(len(result), 201)
        self.assertEqual(self.client._candles_page_sync.call_args.args[-2:], (2, 'older'))
        self.assertEqual(len({item.timestamp for item in result}), 201)

    async def test_cursor_cycle_and_no_progress_stop_pagination(self):
        self.client._candles_page_sync = Mock(side_effect=[([bar(0)], 'a'), ([bar(1)], 'b'), ([bar(2)], 'a')])
        self.assertEqual(len(await self.client.candles('082740', '1m', 300)), 3)
        self.assertEqual(self.client._candles_page_sync.call_count, 3)
        self.client._candles_page_sync = Mock(side_effect=[([bar()], 'a'), ([bar()], 'b')])
        self.assertEqual(len(await self.client.candles('082740', '1m', 300)), 1)
        self.assertEqual(self.client._candles_page_sync.call_count, 2)

    async def test_non_rate_http_error_is_not_retried(self):
        error = HTTPError('mock', 401, 'Unauthorized', {}, io.BytesIO())
        self.client._candles_page_sync = Mock(side_effect=error)
        with self.assertRaises(HTTPError):
            await self.client.candles('082740', '1m', 1)
        self.client._candles_page_sync.assert_called_once()
        error.close()

    async def test_success_response_headers_are_applied_without_leaking_auth(self):
        response = io.BytesIO(json.dumps({'result': {'candles': [], 'nextBefore': None}}).encode())
        response.headers = {'X-RateLimit-Limit': '1'}
        with patch('app.toss.urllib.request.urlopen', return_value=response):
            self.assertEqual(self.client._candles_page_sync('mock-token', '082740', '1m', 1), ([], None))
        self.assertEqual(self.client._candle_interval, 1.1)

    async def test_announced_larger_quota_avoids_unnecessary_chart_delay(self):
        self.client._update_candle_rate({'X-RateLimit-Limit': '20'})
        self.assertAlmostEqual(self.client._candle_interval, .055)
        self.assertGreater(self.client._candle_interval, 1 / 20)


class CandleHistoryTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_paper_trader.PaperTraderTest.asyncSetUp
    asyncTearDown = test_paper_trader.PaperTraderTest.asyncTearDown

    async def test_partial_history_is_cached_even_when_fewer_than_requested(self):
        self.engine.toss_client = Mock(candles=AsyncMock(return_value=[bar()]))
        first = await self.engine.candles('082740', '4h', 50)
        self.assertEqual(len(first), 1)
        self.assertEqual(await self.engine.candles('082740', '4h', 50), first)
        self.assertEqual(await self.engine.candles('082740', '4h', 10), first)
        self.engine.toss_client.candles.assert_awaited_once_with('082740', '1m', 12000)

    async def test_chart_and_signal_share_one_fetch_when_loading_concurrently(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            entered.set()
            await release.wait()
            return [bar()]
        self.engine.toss_client = Mock(candles=AsyncMock(side_effect=fetch))
        chart = asyncio.create_task(self.engine.candles('082740', '4h', 50))
        await entered.wait()
        signal = asyncio.create_task(self.engine.candles('082740', '4h', 10))
        release.set()
        self.assertEqual(await chart, await signal)
        self.engine.toss_client.candles.assert_awaited_once()

    async def test_expired_history_is_refreshed_without_stale_error_fallback(self):
        self.engine.toss_client = Mock(candles=AsyncMock(return_value=[bar()]))
        await self.engine.candles('082740', '4h', 50)
        stamp, history = self.engine._four_hour_candles['082740']
        self.engine._four_hour_candles['082740'] = (stamp - timedelta(minutes=5), history)
        self.engine.toss_client.candles.side_effect = TossCandleRateLimitError(2)
        with self.assertRaises(TossCandleRateLimitError):
            await self.engine.candles('082740', '4h', 10)
        self.assertEqual(self.engine.toss_client.candles.await_count, 2)

    async def test_larger_request_fetches_again_instead_of_claiming_small_cache_is_complete(self):
        self.engine.toss_client = Mock(candles=AsyncMock(return_value=[bar()]))
        await self.engine.candles('082740', '4h', 10)
        await self.engine.candles('082740', '4h', 50)
        self.assertEqual(self.engine.toss_client.candles.await_count, 2)
        self.assertEqual(self.engine.toss_client.candles.await_args.args, ('082740', '1m', 12000))

    async def test_cancelled_fetch_releases_lock_and_does_not_cache_incomplete_read(self):
        entered = asyncio.Event()
        async def fetch(*args):
            entered.set()
            await asyncio.Event().wait()
        self.engine.toss_client = Mock(candles=AsyncMock(side_effect=fetch))
        task = asyncio.create_task(self.engine.candles('082740', '4h', 50))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn('082740', self.engine._four_hour_candles)
        self.engine.toss_client.candles.side_effect = None
        self.engine.toss_client.candles.return_value = [bar()]
        self.assertEqual(len(await self.engine.candles('082740', '4h', 10)), 1)

    async def test_candle_api_reports_429_and_retry_after_without_private_response(self):
        app = create_app(self.engine.settings)
        app.state.engine = self.engine
        self.engine.candles = AsyncMock(side_effect=TossCandleRateLimitError(2.1))
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://mock') as client:
            response = await client.get('/api/v1/market/candles?symbol=082740&interval=4h&count=50')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '3')
        self.assertIn('3초 후', response.json()['detail'])
        self.assertNotIn('종목과 API 설정', response.json()['detail'])
