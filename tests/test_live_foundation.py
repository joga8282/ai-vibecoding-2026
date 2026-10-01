from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from app.brokers.toss_real import TossRealBroker
from app.brokers.models import LiveOrderState, LiveOrderStatus
from app.config import Settings
from app.models import Side
from app.repository import SnapshotRepository
from app.risk import LiveRiskManager


class LiveBrokerTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path('tests') / f'.live-{uuid4().hex}.db'
        self.repository = SnapshotRepository(self.path)
        await self.repository.initialize()

    async def asyncTearDown(self):
        self.path.unlink(missing_ok=True)

    async def test_read_only_reconciliation_and_order_lock(self):
        client = Mock(
            holdings=AsyncMock(return_value=[{'symbol': '005930', 'name': '삼성전자', 'quantity': '1'}]),
            account_orders=AsyncMock(return_value=[{'orderId': 'broker-1', 'status': 'FILLED'}]),
        )
        broker = TossRealBroker(client, '7', self.repository)
        result = await broker.reconcile()
        self.assertTrue(result['reconciled'])
        self.assertTrue(broker.reconciled)
        with self.assertRaisesRegex(RuntimeError, '잠겨'):
            await broker.place_order({'symbol': '005930'})

        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM live_positions').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM account_snapshots').fetchone()[0], 1)


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
                    total_exposure=D(0), position_count=0, quote_at=datetime.now(timezone.utc))
        self.assertEqual(manager.validate(**args).rule, 'live-lock')
        manager.armed = manager.reconciled = True
        self.assertTrue(manager.validate(**args).allowed)
        self.assertEqual(manager.validate(**{**args, 'quantity': D(2)}).rule, 'max-order')
        self.assertEqual(manager.validate(**{**args, 'symbol': '000660'}).rule, 'allowlist')

    def test_live_order_state_machine_rejects_invalid_transition(self):
        order = LiveOrderState(LiveOrderStatus.CREATED, D(10))
        order.transition(LiveOrderStatus.SUBMITTING)
        order.transition(LiveOrderStatus.ACCEPTED)
        order.transition(LiveOrderStatus.PARTIALLY_FILLED, D(4))
        order.transition(LiveOrderStatus.FILLED, D(10))
        self.assertEqual(order.filled_quantity, D(10))
        with self.assertRaises(ValueError):
            order.transition(LiveOrderStatus.ACCEPTED)
