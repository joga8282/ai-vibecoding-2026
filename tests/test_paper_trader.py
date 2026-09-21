from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.engine import TradingEngine
from app.models import Currency, OrderStatus, Quote, Side, ThresholdStrategy, utc_now
from app.paper import PaperBroker
from app.repository import SnapshotRepository


class PaperTraderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_path = Path("tests") / f".paper-trader-{uuid4().hex}.db"
        settings = Settings(
            database_path=self.database_path,
            initial_cash_krw=Decimal("1000000"),
            initial_cash_usd=Decimal("1000"),
            max_order_amount_krw=Decimal("1000000"),
            max_order_amount_usd=Decimal("1000"),
            fee_rate=Decimal("0"),
            slippage_bps=Decimal("0"),
        )
        self.repository = SnapshotRepository(settings.database_path)
        await self.repository.initialize()
        self.broker = PaperBroker(settings, self.repository)
        await self.broker.restore()
        self.engine = TradingEngine(settings, self.broker, self.repository)

    async def asyncTearDown(self) -> None:
        await self.engine.stop()
        self.database_path.unlink(missing_ok=True)

    async def test_paper_buy_sell_and_restore(self) -> None:
        buy_quote = Quote("005930", Decimal("70000"), Currency.KRW, utc_now())
        buy = await self.broker.place_market_order(
            client_order_id="buy-1",
            quote=buy_quote,
            side=Side.BUY,
            quantity=Decimal("2"),
        )
        self.assertEqual(buy.status, OrderStatus.FILLED)
        self.assertEqual(self.broker.cash[Currency.KRW], Decimal("860000"))

        duplicate = await self.broker.place_market_order(
            client_order_id="buy-1",
            quote=buy_quote,
            side=Side.BUY,
            quantity=Decimal("2"),
        )
        self.assertEqual(duplicate.order_id, buy.order_id)
        self.assertEqual(len(self.broker.orders), 1)

        sell_quote = Quote("005930", Decimal("75000"), Currency.KRW, utc_now())
        sell = await self.broker.place_market_order(
            client_order_id="sell-1",
            quote=sell_quote,
            side=Side.SELL,
            quantity=Decimal("2"),
        )
        self.assertEqual(sell.status, OrderStatus.FILLED)
        self.assertEqual(self.broker.cash[Currency.KRW], Decimal("1010000"))

        restored = PaperBroker(self.engine.settings, self.repository)
        await restored.restore()
        self.assertEqual(restored.cash[Currency.KRW], Decimal("1010000"))
        self.assertEqual(len(restored.orders), 2)

    async def test_threshold_strategy_trades_automatically(self) -> None:
        strategy = ThresholdStrategy(
            strategy_id="samsung-threshold",
            symbol="005930",
            currency=Currency.KRW,
            quantity=Decimal("1"),
            buy_below=Decimal("70000"),
            sell_above=Decimal("75000"),
        )
        await self.engine.upsert_strategy(strategy)
        self.engine.set_quote(
            Quote("005930", Decimal("69000"), Currency.KRW, utc_now())
        )
        await self.engine.tick()
        self.assertIn("005930", self.broker.positions)

        self.engine.set_quote(
            Quote("005930", Decimal("76000"), Currency.KRW, utc_now())
        )
        await self.engine.tick()
        self.assertNotIn("005930", self.broker.positions)
        self.assertEqual([order.status for order in self.broker.orders], [
            OrderStatus.FILLED,
            OrderStatus.FILLED,
        ])

    async def test_kill_switch_blocks_engine_start(self) -> None:
        await self.engine.activate_kill_switch()
        with self.assertRaises(RuntimeError):
            await self.engine.start()
