from datetime import datetime, timedelta, timezone
import asyncio
from decimal import Decimal as D
import json
from pathlib import Path
import urllib.error
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.brokers.toss_real import TossRealBroker
from app.brokers.models import LiveOrderState, LiveOrderStatus
from app.config import Settings
from app.models import Currency, Order, OrderStatus, Quote, Side
from app.repository import SnapshotRepository
from app.risk import LiveRiskManager
from app.toss import TossMarketClient


class TossOrderTransportTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TossMarketClient('test-client', 'test-secret')
        self.client._access_token = AsyncMock(return_value='test-token')
        self.requests = []

        def capture(request):
            self.requests.append(request)
            return {'result': {'orderId': 'broker-123'}}

        self.client._request_json_sync = Mock(side_effect=capture)
        self.thread_patch = patch(
            'app.toss.asyncio.to_thread',
            new=AsyncMock(side_effect=lambda fn, *args: fn(*args)),
        )
        self.thread_patch.start()

    async def asyncTearDown(self):
        self.thread_patch.stop()

    async def test_create_order_sends_account_header_and_json_payload(self):
        payload = {
            'symbol': '005930', 'side': 'BUY', 'orderType': 'LIMIT',
            'quantity': 1, 'price': 70000, 'clientOrderId': 'test-order-1',
        }

        result = await self.client.create_order('7', payload)

        request = self.requests[-1]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(request.full_url, 'https://openapi.tossinvest.com/api/v1/orders')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(headers['x-tossinvest-account'], '7')
        self.assertEqual(headers['authorization'], 'Bearer test-token')
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(result, {'orderId': 'broker-123'})

    async def test_order_path_identifiers_are_escaped(self):
        result = await self.client.cancel_account_order('7', 'order/with/slash')

        self.assertEqual(
            self.requests[-1].full_url,
            'https://openapi.tossinvest.com/api/v1/orders/order%2Fwith%2Fslash/cancel',
        )
        self.assertEqual(result, {'orderId': 'broker-123'})

    async def test_live_quote_uses_timestamped_two_sided_orderbook(self):
        now = datetime.now(timezone.utc).isoformat()
        self.client._request_json_sync = Mock(return_value={'result': {
            'timestamp': now, 'currency': 'KRW',
            'bids': [{'price': '69900', 'volume': '10'}],
            'asks': [{'price': '70100', 'volume': '12'}],
        }})

        quote = await self.client.live_quote('013580')

        self.assertEqual(quote.symbol, '013580')
        self.assertEqual(quote.price, D('70000'))
        self.assertEqual(quote.bid_price, D('69900'))
        self.assertEqual(quote.ask_price, D('70100'))
        self.assertEqual(quote.source, 'toss')
        request = self.client._request_json_sync.call_args.args[0]
        self.assertIn('/api/v1/orderbook?symbol=013580', request.full_url)

    async def test_orderbook_retries_transient_http_errors(self):
        self.client._request_json_sync = Mock(side_effect=[
            urllib.error.HTTPError('https://example.test/api/v1/orderbook', 503,
                                   'unavailable', {}, None),
            {'result': {'timestamp': datetime.now(timezone.utc).isoformat(),
                        'currency': 'KRW', 'bids': [{'price': '69900', 'volume': '1'}],
                        'asks': [{'price': '70100', 'volume': '1'}]}},
        ])

        quote = await self.client.live_quote('013580')

        self.assertEqual(quote.bid_price, D('69900'))
        self.assertEqual(self.client._request_json_sync.call_count, 2)

    async def test_orderbook_reports_safe_http_status_without_response_body(self):
        self.client._request_json_sync = Mock(side_effect=urllib.error.HTTPError(
            'https://example.test/api/v1/orderbook?symbol=013580', 403,
            'forbidden', {}, None))

        with self.assertRaisesRegex(RuntimeError, 'HTTP 403 for /api/v1/orderbook') as error:
            await self.client.orderbook('013580')

        self.assertNotIn('forbidden', str(error.exception))
        self.assertEqual(self.client._request_json_sync.call_count, 1)

    async def test_live_quote_rejects_invalid_or_one_sided_book(self):
        self.client._request_json_sync = Mock(return_value={'result': {
            'timestamp': datetime.now(timezone.utc).isoformat(), 'currency': 'KRW',
            'bids': [{'price': '70100', 'volume': '10'}],
            'asks': [{'price': '70000', 'volume': '12'}],
        }})

        with self.assertRaisesRegex(RuntimeError, 'invalid two-sided'):
            await self.client.live_quote('013580')

    async def test_live_quotes_uses_timestamped_websocket_orderbook(self):
        class FakeSocket:
            def __init__(self):
                self.frames = [
                    json.dumps({'type': 'subscriptions',
                                'subscribed': ['orderbook:kr:013580'], 'rejected': []}),
                    json.dumps({'type': 'message', 'topic': 'orderbook:kr:013580',
                                'data': {
                                    'timestamp': datetime.now(timezone.utc).isoformat(),
                                    'currency': 'KRW',
                                    'bids': [{'price': '69900', 'volume': '10'}],
                                    'asks': [{'price': '70100', 'volume': '12'}],
                                }}),
                ]
                self.sent = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, value):
                self.sent = json.loads(value)

            async def recv(self):
                return self.frames.pop(0)

        socket = FakeSocket()
        with patch('app.toss.websockets.connect', return_value=socket):
            quotes = await self.client.live_quotes(['013580'])

        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].bid_price, D('69900'))
        self.assertEqual(quotes[0].ask_price, D('70100'))
        self.assertEqual(socket.sent[1], {'type': 'orderbook:kr', 'codes': ['013580']})

    async def test_live_quotes_fails_closed_when_stream_has_no_fresh_update(self):
        class SilentSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, _value):
                return None

            async def recv(self):
                return json.dumps({'type': 'subscriptions',
                                   'subscribed': ['orderbook:kr:013580'], 'rejected': []})

        with patch('app.toss.websockets.connect', return_value=SilentSocket()):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                await self.client.live_quotes(['013580'], timeout=0.01)


class LiveBrokerTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path('tests') / f'.live-{uuid4().hex}.db'
        self.repository = SnapshotRepository(self.path)
        await self.repository.initialize()

    async def asyncTearDown(self):
        self.path.unlink(missing_ok=True)

    def approve_live_buy(self, broker, *symbols):
        broker.risk.set_recommended_symbols(symbols)

    async def test_live_startup_keeps_dashboard_available_when_toss_is_offline(self):
        from app.main import create_app

        settings = Settings(
            mode='live', live_trading_enabled=True, live_allowed_symbols=('005930',),
            toss_client_id='test-client', toss_client_secret='test-secret',
            toss_account_seq='7', api_access_token='test-token', database_path=self.path,
        )
        client = Mock(
            holdings=AsyncMock(side_effect=ConnectionError('network unavailable')),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
        )
        with patch('app.main.TossMarketClient', return_value=client):
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                self.assertIsNotNone(app.state.engine)
                self.assertFalse(app.state.live_broker.reconciled)
                self.assertFalse(app.state.live_broker.risk.armed)
                self.assertIn('account sync unavailable', app.state.live_broker.last_error)

    async def test_live_arm_api_requires_confirmed_mock_reconciliation(self):
        from types import SimpleNamespace
        from starlette.requests import Request
        from app.main import create_app

        settings = Settings(mode='live', live_trading_enabled=True,
                            api_access_token='test-token', live_allowed_symbols=('005930',))
        app = create_app(settings)
        async def successful_reconcile():
            broker.reconciled = True
            return {'reconciled': True}

        broker = SimpleNamespace(
            settings=settings, reconciled=False,
            risk=SimpleNamespace(armed=False),
            reconcile=AsyncMock(side_effect=successful_reconcile),
            arm=Mock(side_effect=lambda: setattr(broker.risk, 'armed', True)),
        )
        app.state.settings = settings
        app.state.live_broker = broker
        request = Request({'type': 'http', 'app': app,
                           'headers': [(b'x-api-token', b'test-token')]})
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/api/v1/live/arm')

        result = await endpoint(request)

        self.assertEqual(result, {'armed': True, 'reconciled': True})
        self.assertTrue(broker.risk.armed)
        broker.reconcile.assert_awaited_once()

    async def test_live_orders_api_defaults_to_open_order_filter(self):
        from types import SimpleNamespace
        from httpx import ASGITransport, AsyncClient
        from app.main import create_app

        settings = Settings(mode='live')
        app = create_app(settings)
        broker = SimpleNamespace(
            settings=settings, list_orders=AsyncMock(return_value=[]),
        )
        app.state.settings = settings
        app.state.live_broker = broker

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/v1/live/orders')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'orders': []})
        broker.list_orders.assert_awaited_once_with('OPEN')

    async def test_manual_live_limit_test_api_requires_explicit_confirmation_and_stopped_engine(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        from app.main import LiveLimitOrderTestInput, create_app
        from types import SimpleNamespace

        settings = Settings(mode='live', live_trading_enabled=True,
                            api_access_token='test-token', live_allowed_symbols=('005930',))
        app = create_app(settings)
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        engine = SimpleNamespace(running=False, kill_switch=False,
                                 lookup_quote=AsyncMock(return_value=quote))
        result_order = Order(
            'broker-test', 'manual-test', '005930', Side.BUY, D(0), D(69000), None,
            Currency.KRW, OrderStatus.CANCELED, D(0), 'CANCELED', datetime.now(timezone.utc))
        broker = SimpleNamespace(
            settings=settings, risk=SimpleNamespace(armed=True), reconciled=True,
            place_limit_order=AsyncMock(return_value=result_order))
        app.state.settings = settings
        app.state.engine = engine
        app.state.live_broker = broker
        request = Request({'type': 'http', 'app': app,
                           'headers': [(b'x-api-token', b'test-token')]})
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/api/v1/live/test-limit-order')
        body = LiveLimitOrderTestInput(
            symbol='005930', side=Side.BUY, quantity=1, limit_price=D(69000),
            confirm_real_order=True)

        response = await endpoint(body, request)

        self.assertEqual(response['order']['status'], 'CANCELED')
        self.assertTrue(response['cancel_confirmed'])
        self.assertFalse(response['fully_filled'])
        broker.place_limit_order.assert_awaited_once()

        engine.running = True
        with self.assertRaises(HTTPException) as context:
            await endpoint(body, request)
        self.assertEqual(context.exception.status_code, 409)
        broker.place_limit_order.assert_awaited_once()

        engine.running = False
        engine.kill_switch = True
        with self.assertRaises(HTTPException) as context:
            await endpoint(body, request)
        self.assertEqual(context.exception.status_code, 409)
        broker.place_limit_order.assert_awaited_once()

    async def test_read_only_reconciliation_and_order_lock(self):
        client = Mock(
            holdings=AsyncMock(return_value={
                'items': [{'symbol': '005930', 'name': '?쇱꽦?꾩옄', 'quantity': '2',
                           'averagePurchasePrice': '70000', 'currency': 'KRW'}],
                'marketValue': {'amount': {'krw': '150000', 'usd': '0'}},
            }),
            account_orders=AsyncMock(return_value=[{'orderId': 'broker-open', 'status': 'PENDING'}]),
            buying_power=AsyncMock(side_effect=lambda account, currency: {
                'currency': currency.value, 'cashBuyingPower': '1000000'
            }),
        )
        broker = TossRealBroker(client, '7', self.repository)
        result = await broker.reconcile()
        self.assertTrue(result['reconciled'])
        self.assertTrue(broker.reconciled)
        self.assertEqual(broker.cash[Currency.KRW], D('1000000'))
        self.assertIn('005930', broker.positions)
        self.assertEqual(broker.positions['005930'].quantity, D(2))
        client.account_orders.assert_awaited_once_with('7', 'OPEN')
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            await broker.place_order({'symbol': '005930'})

        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM live_positions').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM account_snapshots').fetchone()[0], 1)

    async def test_live_account_api_reconciles_and_exposes_buying_power_read_only(self):
        from starlette.requests import Request
        from types import SimpleNamespace
        from app.main import create_app

        settings = Settings(mode='live', live_trading_enabled=False)
        app = create_app(settings)
        account = {
            'mode': 'live', 'cash': {'KRW': '123456', 'USD': '0'},
            'positions': [], 'total_equity': {'KRW': '123456', 'USD': '0'},
            'data_status': 'synced',
        }
        broker = SimpleNamespace(
            settings=settings,
            reconcile=AsyncMock(return_value={'reconciled': True}),
            account=Mock(return_value=account),
            last_sync_at=datetime.now(timezone.utc),
        )
        engine = SimpleNamespace(quotes={}, with_stock_names=AsyncMock(return_value=[]))
        app.state.settings = settings
        app.state.live_broker = broker
        app.state.engine = engine
        request = Request({'type': 'http', 'app': app})
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/api/v1/live/account')

        response = await endpoint(request)

        broker.reconcile.assert_awaited_once_with(recover_unfinished_orders=False)
        self.assertEqual(response['buying_power'], account['cash'])
        self.assertEqual(response['data_status'], 'synced')
        self.assertIsNotNone(response['last_sync_at'])
        self.assertEqual(response['positions'], [])

    async def test_read_only_account_reconcile_does_not_cancel_unfinished_orders(self):
        settings = Settings(mode='live', live_trading_enabled=False)
        key = 'bot-' + 'b' * 32
        await self.repository.save('live_broker_state', {
            'client_order_ids': {key: 'unresolved-readonly'},
            'order_journal': {key: {
                'clientOrderId': key, 'originalClientOrderId': 'unresolved-readonly',
                'symbol': '005930', 'side': 'BUY', 'quantity': '1', 'price': '70000',
                'currency': 'KRW', 'status': 'ACCEPTED', 'orderId': 'broker-open',
                'createdAt': datetime.now(timezone.utc).isoformat(),
            }}, 'orders': [],
        })
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[{
                'orderId': 'broker-open', 'symbol': '005930', 'side': 'BUY', 'status': 'PENDING',
            }]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '100000'}),
            account_order=AsyncMock(), cancel_account_order=AsyncMock(),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        await broker.restore()

        result = await broker.reconcile(recover_unfinished_orders=False)

        self.assertTrue(result['reconciled'])
        client.account_order.assert_not_awaited()
        client.cancel_account_order.assert_not_awaited()
        state = await self.repository.load('live_broker_state')
        self.assertEqual(state['order_journal'][key]['status'], 'ACCEPTED')

    async def test_live_reconcile_accepts_fresh_orderbook_when_last_trade_quote_is_old(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('013580',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('013580', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss',
                      bid_price=D('69900'), ask_price=D('70100'))
        client = Mock(
            holdings=AsyncMock(return_value={'items': [{
                'symbol': '013580', 'quantity': '1', 'averagePurchasePrice': '70000',
                'currency': 'KRW',
            }]}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            live_quote=AsyncMock(return_value=quote),
            prices=AsyncMock(side_effect=AssertionError('stale last-price endpoint used')),
        )
        client.requires_orderbook_snapshot = True
        broker = TossRealBroker(client, '7', self.repository, settings)

        result = await broker.reconcile()

        self.assertTrue(result['reconciled'])
        self.assertEqual(broker.daily_equity_start, D('1069900'))
        client.live_quote.assert_awaited_once_with('013580')
        client.prices.assert_not_awaited()

    async def test_live_reconcile_uses_broker_position_market_value_after_hours(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        client = Mock(
            holdings=AsyncMock(return_value={'items': [{
                'symbol': '013580', 'quantity': '23', 'averagePurchasePrice': '21700',
                'marketValue': {'amount': '494500'}, 'currency': 'KRW',
            }]}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            live_quote=AsyncMock(side_effect=AssertionError('market quote should not be needed')),
        )
        client.requires_orderbook_snapshot = True
        broker = TossRealBroker(client, '7', self.repository, settings)

        result = await broker.reconcile()

        self.assertTrue(result['reconciled'])
        self.assertEqual(broker.position_market_values, {'013580': D('494500')})
        self.assertEqual(broker.daily_equity_start, D('1494500'))
        client.live_quote.assert_not_awaited()

    async def test_timed_out_live_orderbook_stream_falls_back_to_rest(self):
        client = TossMarketClient('test-client', 'test-secret')
        quote = Quote('005930', D('271750'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss',
                      bid_price=D('271500'), ask_price=D('272000'))
        client.live_quotes = AsyncMock(side_effect=asyncio.TimeoutError())
        client.live_quote = AsyncMock(return_value=quote)
        client.requires_orderbook_snapshot = True
        broker = TossRealBroker(client, '7', self.repository)

        result = await broker._fresh_risk_quotes(['005930'])

        self.assertEqual(result, [quote])
        client.live_quote.assert_awaited_once_with('005930')

    async def test_live_reconcile_measures_quote_age_when_valuation_quote_returns(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('013580',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))

        async def slow_holdings(_account_seq):
            await asyncio.sleep(2.2)
            return {'items': [{'symbol': '013580', 'quantity': '1',
                               'averagePurchasePrice': '70000', 'currency': 'KRW'}]}

        async def fresh_quote(_symbol):
            return Quote('013580', D('70000'), Currency.KRW,
                         datetime.now(timezone.utc), source='toss',
                         bid_price=D('69900'), ask_price=D('70100'))

        client = Mock(
            holdings=AsyncMock(side_effect=slow_holdings),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            live_quote=AsyncMock(side_effect=fresh_quote),
        )
        client.requires_orderbook_snapshot = True
        broker = TossRealBroker(client, '7', self.repository, settings)

        result = await broker.reconcile()

        self.assertTrue(result['reconciled'])

    async def test_live_limit_cross_check_uses_fresh_best_ask(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss',
                      bid_price=D('69900'), ask_price=D('70100'))
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            live_quote=AsyncMock(return_value=quote),
            create_order=AsyncMock(return_value={'orderId': 'book-limit'}),
        )
        client.requires_orderbook_snapshot = True
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()

        with self.assertRaisesRegex(ValueError, 'marketable'):
            await broker.submit_limit_order(
                client_order_id='book-crossing', quote=quote, side=Side.BUY,
                quantity=D(1), limit_price=D('70100'))
        client.create_order.assert_not_awaited()

        result = await broker.submit_limit_order(
            client_order_id='book-passive', quote=quote, side=Side.BUY,
            quantity=D(1), limit_price=D('70050'))

        self.assertEqual(result['status'], 'ACCEPTED')
        client.create_order.assert_awaited_once()

    async def test_live_limit_accepts_same_orderbook_with_newer_timestamp(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        observed_at = datetime.now(timezone.utc)
        initial_quote = Quote('005930', D('70000'), Currency.KRW,
                              observed_at, source='toss',
                              bid_price=D('69900'), ask_price=D('70100'))
        refreshed_quote = Quote('005930', D('70000'), Currency.KRW,
                                observed_at + timedelta(seconds=1), source='toss',
                                bid_price=D('69900'), ask_price=D('70100'))
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            live_quote=AsyncMock(return_value=refreshed_quote),
            create_order=AsyncMock(return_value={'orderId': 'refreshed-book'}),
        )
        client.requires_orderbook_snapshot = True
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()

        result = await broker.submit_limit_order(
            client_order_id='same-book-new-timestamp', quote=initial_quote,
            side=Side.BUY, quantity=D(1), limit_price=D('70050'))

        self.assertEqual(result['status'], 'ACCEPTED')
        client.create_order.assert_awaited_once()

    async def test_armed_live_order_uses_mock_transport_and_reconciles(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        async def accept_and_check_journal(account, payload):
            state = await self.repository.load('live_broker_state')
            self.assertEqual(next(iter(state['order_journal'].values()))['status'], 'SUBMITTING')
            return {'orderId': 'broker-1'}

        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(side_effect=accept_and_check_journal),
            account_order=AsyncMock(return_value={
                'status': 'FILLED',
                'execution': {'filledQuantity': '1', 'averageFilledPrice': '70000'},
            }),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()

        order = await broker.place_market_order(
            client_order_id='swing-test-1', quote=quote, side=Side.BUY, quantity=D(1))

        self.assertEqual(order.status.value, 'FILLED')
        payload = client.create_order.await_args.args[1]
        self.assertEqual(payload['symbol'], '005930')
        self.assertNotEqual(payload['clientOrderId'], 'swing-test-1')
        self.assertLessEqual(len(payload['clientOrderId']), 36)
        self.assertTrue(broker.reconciled)

    async def test_partial_fill_is_cancel_confirmed_and_persisted(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('200000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(return_value={'orderId': 'broker-partial'}),
            account_order=AsyncMock(side_effect=[
                {'status': 'PARTIAL_FILLED', 'execution': {'filledQuantity': '1', 'averageFilledPrice': '70000'}},
                {'status': 'CANCELED', 'execution': {'filledQuantity': '1', 'averageFilledPrice': '70000'}},
            ]),
            cancel_account_order=AsyncMock(return_value={'orderId': 'broker-partial'}),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()
        with patch('app.brokers.toss_real.asyncio.sleep', new=AsyncMock()):
            order = await broker.place_market_order(
                client_order_id='partial-1', quote=quote, side=Side.BUY, quantity=D(2))

        self.assertEqual(order.status.value, 'PARTIALLY_FILLED')
        self.assertEqual(order.quantity, D(1))
        client.cancel_account_order.assert_awaited_once_with('7', 'broker-partial')
        state = await self.repository.load('live_broker_state')
        journal = next(iter(state['order_journal'].values()))
        self.assertEqual(journal['status'], 'PARTIALLY_FILLED')
        self.assertEqual(journal['filledQuantity'], '1')

    async def test_manual_cancel_persists_confirmed_partial_fill_and_reconciles(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('200000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        key = 'bot-' + 'c' * 32
        client = Mock(
            holdings=AsyncMock(return_value={'items': [{
                'symbol': '005930', 'quantity': '1', 'averagePurchasePrice': '70000',
                'currency': 'KRW',
            }]}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '930000'}),
            prices=AsyncMock(return_value=[quote]),
            account_order=AsyncMock(return_value={
                'status': 'CANCELED',
                'execution': {'filledQuantity': '1', 'averageFilledPrice': '70000',
                              'commission': '10'},
            }),
            cancel_account_order=AsyncMock(return_value={'orderId': 'broker-cancel'}),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        broker.order_journal[key] = {
            'originalClientOrderId': 'manual-order-1', 'orderId': 'broker-cancel',
            'symbol': '005930', 'side': 'BUY', 'quantity': '2', 'price': '70000',
            'currency': 'KRW', 'status': 'ACCEPTED',
        }

        order = await broker.cancel_and_resolve_order('broker-cancel')

        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(order.quantity, D('1'))
        self.assertEqual(order.fee, D('10'))
        client.cancel_account_order.assert_awaited_once_with('7', 'broker-cancel')
        client.account_order.assert_awaited_once_with('7', 'broker-cancel')
        self.assertFalse(broker.risk.armed)
        self.assertTrue(broker.reconciled)
        state = await self.repository.load('live_broker_state')
        self.assertEqual(state['order_journal'][key]['status'], 'PARTIALLY_FILLED')
        self.assertEqual(state['order_journal'][key]['filledQuantity'], '1')

    async def test_manual_cancel_refuses_unmanaged_order(self):
        settings = Settings(mode='live', live_trading_enabled=True)
        client = Mock(cancel_account_order=AsyncMock())
        broker = TossRealBroker(client, '7', self.repository, settings)

        with self.assertRaisesRegex(LookupError, 'not managed'):
            await broker.cancel_and_resolve_order('manual-user-order')

        client.cancel_account_order.assert_not_awaited()

    async def test_manual_limit_submit_returns_acceptance_without_auto_cancel(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('200000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(return_value={'orderId': 'broker-open'}),
            account_order=AsyncMock(),
            cancel_account_order=AsyncMock(),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()

        result = await broker.submit_limit_order(
            client_order_id='manual-pending-1', quote=quote, side=Side.BUY,
            quantity=D(2), limit_price=D(69000))

        self.assertEqual(result['status'], 'ACCEPTED')
        self.assertEqual(result['orderId'], 'broker-open')
        self.assertFalse(broker.risk.armed)
        client.create_order.assert_awaited_once()
        client.account_order.assert_not_awaited()
        client.cancel_account_order.assert_not_awaited()
        state = await self.repository.load('live_broker_state')
        journal = next(iter(state['order_journal'].values()))
        self.assertEqual(journal['status'], 'ACCEPTED')
        self.assertEqual(journal['orderId'], 'broker-open')

    async def test_manual_cancel_api_requires_stopped_engine_and_returns_partial_fill(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        from types import SimpleNamespace
        from app.main import create_app

        settings = Settings(mode='live', live_trading_enabled=True,
                            api_access_token='test-token')
        app = create_app(settings)
        order = Order(
            'broker-cancel', 'manual-order-1', '005930', Side.BUY, D('1'), D('70000'),
            D('70000'), Currency.KRW, OrderStatus.PARTIALLY_FILLED, D('10'), None,
            datetime.now(timezone.utc))
        broker = SimpleNamespace(cancel_and_resolve_order=AsyncMock(return_value=order))
        engine = SimpleNamespace(running=False)
        app.state.settings = settings
        app.state.live_broker = broker
        app.state.engine = engine
        request = Request({'type': 'http', 'app': app,
                           'headers': [(b'x-api-token', b'test-token')]})
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/api/v1/live/orders/{order_id}/cancel')

        response = await endpoint('broker-cancel', request)

        self.assertTrue(response['cancel_confirmed'])
        self.assertFalse(response['fully_filled'])
        self.assertEqual(response['order']['status'], 'PARTIALLY_FILLED')
        broker.cancel_and_resolve_order.assert_awaited_once_with('broker-cancel')

        engine.running = True
        with self.assertRaises(HTTPException) as context:
            await endpoint('broker-cancel', request)
        self.assertEqual(context.exception.status_code, 409)
        broker.cancel_and_resolve_order.assert_awaited_once_with('broker-cancel')

    async def test_manual_limit_submit_api_requires_confirmation_and_live_readiness(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        from types import SimpleNamespace
        from app.main import LiveLimitOrderTestInput, create_app

        settings = Settings(mode='live', live_trading_enabled=True,
                            api_access_token='test-token', live_allowed_symbols=('005930',))
        app = create_app(settings)
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        engine = SimpleNamespace(running=False, kill_switch=False,
                                 lookup_quote=AsyncMock(return_value=quote))
        broker = SimpleNamespace(
            settings=settings, risk=SimpleNamespace(armed=True), reconciled=True,
            submit_limit_order=AsyncMock(return_value={
                'orderId': 'broker-open', 'status': 'ACCEPTED', 'symbol': '005930',
            }),
        )
        app.state.settings = settings
        app.state.engine = engine
        app.state.live_broker = broker
        request = Request({'type': 'http', 'app': app,
                           'headers': [(b'x-api-token', b'test-token')]})
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/api/v1/live/test-limit-order/submit')
        payload = LiveLimitOrderTestInput(
            symbol='005930', side=Side.BUY, quantity=1,
            limit_price=D('69000'), confirm_real_order=True)

        response = await endpoint(payload, request)

        self.assertTrue(response['accepted'])
        self.assertEqual(response['cancel_path'], '/api/v1/live/orders/broker-open/cancel')
        broker.submit_limit_order.assert_awaited_once()

        engine.running = True
        with self.assertRaises(HTTPException) as context:
            await endpoint(payload, request)
        self.assertEqual(context.exception.status_code, 409)
        broker.submit_limit_order.assert_awaited_once()

    async def test_unfilled_order_is_canceled_after_poll_timeout(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        pending = {'status': 'PENDING', 'execution': {'filledQuantity': '0'}}
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}), account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(return_value={'orderId': 'broker-pending'}),
            account_order=AsyncMock(side_effect=[pending] * 6 + [
                {'status': 'CANCELED', 'execution': {'filledQuantity': '0'}}]),
            cancel_account_order=AsyncMock(return_value={'orderId': 'broker-pending'}),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()
        with patch('app.brokers.toss_real.asyncio.sleep', new=AsyncMock()):
            order = await broker.place_market_order(
                client_order_id='pending-1', quote=quote, side=Side.BUY, quantity=D(1))

        self.assertEqual(order.status.value, 'CANCELED')
        self.assertEqual(order.quantity, D(0))
        client.cancel_account_order.assert_awaited_once_with('7', 'broker-pending')
        self.assertEqual(next(iter((await self.repository.load('live_broker_state'))['order_journal'].values()))['status'], 'CANCELED')

    async def test_limit_order_is_submitted_pending_checked_then_cancel_confirmed(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        pending = {'status': 'PENDING', 'execution': {'filledQuantity': '0'}}
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(return_value={'orderId': 'broker-limit'}),
            account_order=AsyncMock(side_effect=[pending] * 6 + [
                {'status': 'CANCELED', 'execution': {'filledQuantity': '0'}},
            ]),
            cancel_account_order=AsyncMock(return_value={'orderId': 'broker-limit'}),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()
        with patch('app.brokers.toss_real.asyncio.sleep', new=AsyncMock()):
            order = await broker.place_limit_order(
                client_order_id='limit-pending-1', quote=quote, side=Side.BUY,
                quantity=D(1), limit_price=D(69000))

        self.assertEqual(order.status.value, 'CANCELED')
        self.assertEqual(order.quantity, D(0))
        payload = client.create_order.await_args.args[1]
        self.assertEqual(payload['orderType'], 'LIMIT')
        self.assertEqual(payload['price'], '69000')
        client.create_order.assert_awaited_once()
        client.cancel_account_order.assert_awaited_once_with('7', 'broker-limit')
        state = await self.repository.load('live_broker_state')
        journal = next(iter(state['order_journal'].values()))
        self.assertEqual(journal['status'], 'CANCELED')
        self.assertEqual(journal['price'], '69000')

    async def test_marketable_limit_is_rejected_before_submit(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()

        with self.assertRaisesRegex(ValueError, 'marketable'):
            await broker.place_limit_order(
                client_order_id='limit-marketable-1', quote=quote, side=Side.BUY,
                quantity=D(1), limit_price=D(70000))

        client.create_order.assert_not_awaited()

    async def test_ambiguous_submit_after_restart_blocks_rearming(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(side_effect=[[], [], []]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(side_effect=TimeoutError('mock timeout after submit')),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()
        with self.assertRaises(TimeoutError):
            await broker.place_market_order(
                client_order_id='ambiguous-submit', quote=quote, side=Side.BUY, quantity=D(1))

        restarted = TossRealBroker(client, '7', self.repository, settings)
        await restarted.restore()
        self.assertTrue((await restarted.reconcile())['reconciled'])
        with self.assertRaisesRegex(RuntimeError, 'Unresolved LIVE orders'):
            restarted.arm()
        client.create_order.assert_awaited_once()

    async def test_restart_recovers_accepted_order_before_rearming(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        key = 'bot-' + 'a' * 32
        await self.repository.save('live_broker_state', {
            'client_order_ids': {key: 'restart-order-1'},
            'order_journal': {key: {
                'clientOrderId': key, 'originalClientOrderId': 'restart-order-1',
                'symbol': '005930', 'side': 'BUY', 'quantity': '1', 'price': '70000',
                'currency': 'KRW', 'status': 'ACCEPTED', 'orderId': 'broker-restart',
                'createdAt': datetime.now(timezone.utc).isoformat(),
            }}, 'orders': [],
        })
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[]),
            account_order=AsyncMock(return_value={
                'status': 'CANCELED', 'execution': {'filledQuantity': '0'},
            }),
            create_order=AsyncMock(),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        await broker.restore()
        self.assertFalse(broker.risk.armed)
        self.assertTrue((await broker.reconcile())['reconciled'])

        self.assertEqual(broker.orders[0].order_id, 'broker-restart')
        self.assertEqual(broker.orders[0].status.value, 'CANCELED')
        self.approve_live_buy(broker, '005930')
        broker.arm()
        client.create_order.assert_not_awaited()

    async def test_daily_loss_limit_uses_reconciled_account_equity(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(side_effect=[
                {'cashBuyingPower': '100000'}, {'cashBuyingPower': '0'},
                {'cashBuyingPower': '40000'}, {'cashBuyingPower': '0'},
            ]),
            prices=AsyncMock(return_value=[]),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()
        self.assertTrue((await broker.reconcile())['reconciled'])

        self.assertEqual(broker.risk.daily_loss, D('60000'))
        broker.risk.set_recommended_symbols(['005930'])
        decision = broker.risk.validate(
            symbol='005930', side=Side.BUY, quantity=D(1), price=D(70000),
            total_exposure=D(0), current_equity=D('1000000'),
            position_count=0, quote_at=datetime.now(timezone.utc))
        self.assertEqual(decision.rule, 'daily-loss')

    async def test_live_order_fails_closed_when_symbol_has_open_order(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_daily_loss_krw=D('50000'))
        quote = Quote('005930', D('70000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[{'orderId': 'existing', 'symbol': '005930',
                                                     'side': 'BUY', 'status': 'PENDING'}]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()

        with self.assertRaisesRegex(RuntimeError, 'opposite-open-order'):
            await broker.place_market_order(
                client_order_id='swing-test-2', quote=quote, side=Side.BUY, quantity=D(1))
        client.create_order.assert_not_awaited()

    async def test_pending_buy_and_existing_holding_count_toward_equity_ratio(self):
        settings = Settings(mode='live', live_trading_enabled=True,
                            live_allowed_symbols=('005930',),
                            live_max_order_amount_krw=D('100000'),
                            live_max_total_exposure_krw=D('500000'),
                            live_max_total_exposure_ratio=D('0.15'))
        quote = Quote('005930', D('10000'), Currency.KRW,
                      datetime.now(timezone.utc), source='toss')
        client = Mock(
            holdings=AsyncMock(return_value={'items': [{
                'symbol': '013580', 'quantity': '1', 'averagePurchasePrice': '100000',
                'marketValue': {'amount': '100000'}, 'currency': 'KRW',
            }]}),
            account_orders=AsyncMock(return_value=[{
                'orderId': 'other-symbol-pending', 'symbol': '000660', 'side': 'BUY',
                'status': 'PENDING', 'quantity': '1', 'price': '40000',
            }]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '900000'}),
            prices=AsyncMock(return_value=[quote]),
            create_order=AsyncMock(),
        )
        broker = TossRealBroker(client, '7', self.repository, settings)
        self.assertTrue((await broker.reconcile())['reconciled'])
        self.approve_live_buy(broker, '005930')
        broker.arm()

        with self.assertRaisesRegex(RuntimeError, 'max-equity-exposure'):
            await broker.place_market_order(
                client_order_id='ratio-pending-check', quote=quote,
                side=Side.BUY, quantity=D(1))
        client.create_order.assert_not_awaited()

    async def test_invalid_snapshot_fails_closed_and_keeps_last_good_positions(self):
        client = Mock(
            holdings=AsyncMock(side_effect=[
                {'items': [{'symbol': '005930', 'quantity': '1', 'averagePurchasePrice': '70000'}]},
                {'items': [{'symbol': '005930', 'quantity': 'NaN', 'averagePurchasePrice': '70000'}]},
            ]),
            account_orders=AsyncMock(return_value=[]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
        )
        broker = TossRealBroker(client, '7', self.repository)
        self.assertTrue((await broker.reconcile())['reconciled'])

        result = await broker.reconcile()

        self.assertFalse(result['reconciled'])
        self.assertTrue(result['mismatches'])
        self.assertFalse(broker.reconciled)
        self.assertEqual(broker.positions['005930'].quantity, D(1))
        self.assertEqual(broker.account({})['data_status'], 'stale')
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.path)) as connection:
            saved = connection.execute(
                'SELECT symbol, quantity FROM live_positions WHERE account_ref=?', ('7',)
            ).fetchall()
            status = connection.execute(
                'SELECT status FROM broker_accounts WHERE account_ref=?', ('7',)
            ).fetchone()[0]
            event = connection.execute(
                'SELECT reconciled FROM reconciliation_events WHERE account_ref=? ORDER BY id DESC LIMIT 1',
                ('7',),
            ).fetchone()[0]
        self.assertEqual(saved, [('005930', '1')])
        self.assertEqual(status, 'MISMATCH')
        self.assertEqual(event, 0)

    async def test_open_order_without_status_makes_snapshot_unreconciled(self):
        client = Mock(
            holdings=AsyncMock(return_value={'items': []}),
            account_orders=AsyncMock(return_value=[{'orderId': 'broker-open'}]),
            buying_power=AsyncMock(return_value={'cashBuyingPower': '1000000'}),
        )
        broker = TossRealBroker(client, '7', self.repository)

        result = await broker.reconcile()

        self.assertFalse(result['reconciled'])
        self.assertIn('status', result['mismatches'][0])


class LiveRiskTest(TestCase):
    def settings(self, **changes):
        values = dict(mode='live', live_trading_enabled=True, live_allowed_symbols=('005930',),
                      live_max_order_amount_krw=D('100000'), live_max_total_exposure_krw=D('500000'),
                      live_max_daily_loss_krw=D('50000'))
        values.update(changes)
        return Settings(**values)

    def test_fail_closed_then_allows_small_reconciled_order(self):
        manager = LiveRiskManager(self.settings())
        args = dict(symbol='005930', side=Side.BUY, quantity=D(1), price=D(70000),
                    total_exposure=D(0), current_equity=D('1000000'),
                    position_count=0, quote_at=datetime.now(timezone.utc))
        self.assertEqual(manager.validate(**args).rule, 'live-lock')
        manager.armed = manager.reconciled = True
        manager.set_recommended_symbols(['005930'])
        self.assertTrue(manager.validate(**args).allowed)
        self.assertEqual(manager.validate(**{**args, 'quantity': D(2)}).rule, 'max-order')
        self.assertEqual(manager.validate(**{**args, 'symbol': '000660'}).rule, 'allowlist')

    def test_invalid_or_future_order_inputs_fail_closed(self):
        manager = LiveRiskManager(self.settings())
        manager.armed = manager.reconciled = True
        args = dict(symbol='005930', side=Side.BUY, quantity=D(1), price=D(70000),
                    total_exposure=D(0), current_equity=D('1000000'), position_count=0,
                    quote_at=datetime.now(timezone.utc))
        manager.set_recommended_symbols(['005930'])
        self.assertEqual(manager.validate(**{**args, 'quantity': D(0)}).rule, 'invalid-order')
        self.assertEqual(manager.validate(**{**args, 'price': D('NaN')}).rule, 'invalid-order')
        self.assertEqual(manager.validate(**{**args, 'total_exposure': D(-1)}).rule, 'invalid-order')
        self.assertEqual(manager.validate(**{**args, 'quantity': '1'}).rule, 'invalid-order')
        self.assertEqual(manager.validate(**{**args, 'quote_at': datetime.now()}).rule, 'invalid-quote-time')
        self.assertEqual(manager.validate(**{
            **args, 'quote_at': datetime.now(timezone.utc).replace(year=2030)
        }).rule, 'stale-quote')

    def test_empty_live_allowlist_fails_closed(self):
        manager = LiveRiskManager(self.settings(live_allowed_symbols=()))
        manager.armed = manager.reconciled = True
        args = dict(symbol='005930', side=Side.BUY, quantity=D(1), price=D(70000),
                    total_exposure=D(0), current_equity=D('1000000'), position_count=0,
                    quote_at=datetime.now(timezone.utc))
        self.assertEqual(manager.validate(**args).rule, 'allowlist-not-configured')

    def test_recommendation_gate_and_strict_fifteen_percent_exposure(self):
        manager = LiveRiskManager(self.settings())
        manager.armed = manager.reconciled = True
        args = dict(symbol='005930', side=Side.BUY, quantity=D(1), price=D('50000'),
                    total_exposure=D('100000'), current_equity=D('1000000'),
                    position_count=1, quote_at=datetime.now(timezone.utc))
        self.assertEqual(manager.validate(**args).rule, 'recommendation')
        manager.set_recommended_symbols(['005930'])
        self.assertEqual(manager.validate(**args).rule, 'max-equity-exposure')
        self.assertTrue(manager.validate(**{**args, 'total_exposure': D('99999')}).allowed)

    def test_live_order_state_machine_rejects_invalid_transition(self):
        order = LiveOrderState(LiveOrderStatus.CREATED, D(10))
        order.transition(LiveOrderStatus.SUBMITTING)
        order.transition(LiveOrderStatus.ACCEPTED)
        order.transition(LiveOrderStatus.PARTIALLY_FILLED, D(4))
        order.transition(LiveOrderStatus.FILLED, D(10))
        self.assertEqual(order.filled_quantity, D(10))
        with self.assertRaises(ValueError):
            order.transition(LiveOrderStatus.ACCEPTED)
