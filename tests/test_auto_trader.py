import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from app.auto_trader import PaperAutoTrader
from app.models import Currency, Quote, Side, utc_now
from tests import test_paper_trader
from tests.test_recommendation_filters import trend_candles


class AutoTraderTest(IsolatedAsyncioTestCase):
    asyncSetUp = test_paper_trader.PaperTraderTest.asyncSetUp
    asyncTearDown = test_paper_trader.PaperTraderTest.asyncTearDown

    async def setup_auto(self, count=5):
        candles = trend_candles()
        self.price = candles[-1].close_price
        self.engine.toss_client = Mock(
            candles=AsyncMock(return_value=candles),
            prices=AsyncMock(side_effect=lambda symbols: [Quote(s, self.price, Currency.KRW, utc_now(), 'toss') for s in symbols]),
        )
        self.recommend = AsyncMock(return_value={'candidates': [
            {'symbol': f'{i:06}', 'name': f'기업{i}', 'eligible': True, 'currency': 'KRW'} for i in range(count)
        ]})
        self.auto = PaperAutoTrader(self.engine, self.recommend)
        self.engine.automation = self.auto
        await self.auto.prepare()
        self.engine.running = True

    async def test_fixed_total_budget_includes_fees_and_slippage(self):
        settings = replace(self.engine.settings, fee_rate=D('.001'), slippage_bps=D(5))
        self.engine.settings = self.broker.settings = settings
        await self.setup_auto(6)
        await self.engine.tick()
        self.assertEqual(len(self.broker.positions), 5)
        self.assertLessEqual(self.auto.spent(), D(self.auto.session['budget']))
        self.assertEqual(self.auto.spent(), D('1000000') - self.broker.cash[Currency.KRW])
        for order in self.auto.orders():
            self.assertLessEqual(order.filled_price * order.quantity + order.fee, D('100000'))

    async def test_duplicate_ticks_do_not_duplicate_buys(self):
        await self.setup_auto()
        await asyncio.gather(self.engine.tick(), self.engine.tick())
        self.assertEqual(len(self.broker.orders), 5)
        self.recommend.assert_awaited_once()

    async def test_stop_and_kill_switch_block_trading(self):
        await self.setup_auto()
        self.engine.running = False
        await self.engine.tick()
        self.engine.running = True
        self.engine.kill_switch = True
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 0)

    async def test_resume_uses_same_budget_and_order_ledger(self):
        await self.setup_auto(1)
        await self.engine.tick()
        budget = self.auto.session['budget']
        spent = self.auto.spent()
        resumed = PaperAutoTrader(self.engine, self.recommend)
        await resumed.restore()
        await resumed.prepare()
        self.engine.automation = resumed
        await self.engine.tick()
        self.assertEqual(resumed.session['budget'], budget)
        self.assertEqual(resumed.spent(), spent)
        self.assertEqual(len(self.broker.orders), 1)

    async def test_take_profit_does_not_rebuy_or_recycle_budget(self):
        await self.setup_auto(1)
        await self.engine.tick()
        spent = self.auto.spent()
        self.price = D(125)
        self.auto.next_scan = datetime.min.replace(tzinfo=timezone.utc)
        await self.engine.tick()
        self.assertEqual([o.side for o in self.broker.orders], [Side.BUY, Side.SELL])
        self.assertEqual(self.auto.spent(), spent)
        self.assertFalse(self.broker.positions)

    async def test_stop_loss(self):
        await self.setup_auto(1)
        await self.engine.tick()
        self.price *= D('.969')
        await self.engine.tick()
        self.assertEqual(self.broker.orders[-1].side, Side.SELL)
        self.assertFalse(self.broker.positions)

    async def test_no_candidates_or_missing_quotes_do_not_buy(self):
        await self.setup_auto(0)
        await self.engine.tick()
        self.assertFalse(self.broker.orders)
        self.recommend.return_value['candidates'] = [{'symbol': '005930', 'eligible': True, 'currency': 'KRW'}]
        self.engine.toss_client.prices.side_effect = lambda symbols: []
        self.auto.next_scan = datetime.min.replace(tzinfo=timezone.utc)
        await self.engine.tick()
        self.assertFalse(self.broker.orders)

    async def test_existing_positions_are_not_adopted(self):
        await self.setup_auto(1)
        await self.broker.place_market_order(client_order_id='legacy', quote=Quote('000000', self.price, Currency.KRW, utc_now()), side=Side.BUY, quantity=D(1))
        await self.engine.tick()
        self.price = D(125)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertIn('000000', self.broker.positions)

    async def test_real_mode_rejected(self):
        await self.setup_auto()
        self.engine.settings = replace(self.engine.settings, mode='live')
        with self.assertRaises(RuntimeError):
            await self.auto.prepare()

    async def test_order_limit_is_respected(self):
        self.engine.settings = self.broker.settings = replace(self.engine.settings, max_order_amount_krw=D('1000'))
        await self.setup_auto(1)
        await self.engine.tick()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertLessEqual(self.broker.orders[0].filled_price * self.broker.orders[0].quantity, D(1000))

    async def test_stale_quotes_are_not_used(self):
        await self.setup_auto(1)
        self.engine.toss_client.prices.side_effect = lambda symbols: [Quote(s, self.price, Currency.KRW, utc_now() - timedelta(minutes=5), 'toss') for s in symbols]
        await self.engine.tick()
        self.assertFalse(self.broker.orders)

    async def test_stop_during_scan_prevents_order(self):
        await self.setup_auto(1)
        async def stop_in_scan():
            self.engine.running = False
            return self.recommend.return_value
        self.auto.recommend = stop_in_scan
        await self.engine.tick()
        self.assertFalse(self.broker.orders)

    async def test_app_start_stop_and_restart_are_paper_only(self):
        from app.main import create_app
        from starlette.requests import Request
        settings = replace(self.engine.settings, auto_start=False)
        app = create_app(settings)
        request = Request({'type': 'http', 'app': app})
        routes = {r.path: r.endpoint for r in app.routes if hasattr(r, 'endpoint')}
        async with app.router.lifespan_context(app):
            engine = app.state.engine
            self.assertIsNotNone(engine.automation)
            self.assertFalse(engine.running)
            engine.toss_client = Mock()
            engine._run = AsyncMock()
            result = await routes['/api/v1/engine/start'](request)
            self.assertEqual(result['mode'], 'paper')
            self.assertEqual(result['automation']['strategy'], 'swing-v1')
            self.assertEqual(D(result['automation']['budget']), D('500000'))
            result = await routes['/api/v1/engine/stop'](request)
            self.assertFalse(result['running'])
        async with app.router.lifespan_context(app):
            self.assertFalse(app.state.engine.running)
            self.assertEqual(D(app.state.engine.automation.session['budget']), D('500000'))

    async def test_shared_recommendation_service_provides_allocated_quantities(self):
        from app.recommendation_service import build_recommendations
        await self.setup_auto(1)
        self.engine.toss_client.rankings = AsyncMock(return_value={'rankings': [
            {'symbol': '005930', 'currency': 'KRW', 'price': {'lastPrice': str(self.price), 'changeRate': '.01'}}
        ]})
        self.engine.toss_client.stocks_info = AsyncMock(return_value=[
            {'symbol': '005930', 'name': '삼성전자', 'securityType': 'STOCK', 'isCommonShare': True, 'sharesOutstanding': '10000000000'}
        ])
        payload = await build_recommendations(self.engine)
        self.assertEqual(len(payload['candidates']), 1)
        self.assertLessEqual(payload['candidates'][0]['quantity'] * self.price, D('100000'))
