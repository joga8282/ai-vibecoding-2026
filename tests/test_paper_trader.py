from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.engine import TradingEngine
from app.models import Candle, Currency, OrderStatus, Quote, Side, ThresholdStrategy, utc_now
from app.paper import PaperBroker
from app.repository import SnapshotRepository
from app.recommendations import analyze_candidate
from app.weekly_report import WeeklyReportService
from app.toss import TossMarketClient


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
        performance = restored.performance({"005930": sell_quote})
        self.assertEqual(performance["by_currency"]["KRW"]["profit_loss"], "10000")
        self.assertEqual(performance["orders"]["filled"], 2)

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

    async def test_manual_quote_history_is_available_as_candles(self) -> None:
        self.engine.set_quote(
            Quote("005930", Decimal("69000"), Currency.KRW, utc_now())
        )
        self.engine.set_quote(
            Quote("005930", Decimal("70000"), Currency.KRW, utc_now())
        )
        candles = await self.engine.candles("005930", "1m", 100)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1].close_price, Decimal("70000"))

    async def test_supported_candle_intervals_are_aggregated(self) -> None:
        start = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
        source = [
            Candle(
                timestamp=start + timedelta(minutes=index),
                open_price=Decimal(index),
                high_price=Decimal(index + 2),
                low_price=Decimal(index - 1),
                close_price=Decimal(index + 1),
                volume=Decimal("10"),
                currency=Currency.KRW,
            )
            for index in range(120)
        ]
        expected_counts = {"5m": 24, "15m": 8, "50m": 4, "60m": 2}
        for interval, expected in expected_counts.items():
            with self.subTest(interval=interval):
                aggregated = self.engine._aggregate_candles(source, interval)
                self.assertEqual(len(aggregated), expected)
        first = self.engine._aggregate_candles(source, "5m")[0]
        self.assertEqual(first.open_price, Decimal("0"))
        self.assertEqual(first.close_price, Decimal("5"))
        self.assertEqual(first.high_price, Decimal("6"))
        self.assertEqual(first.low_price, Decimal("-1"))
        self.assertEqual(first.volume, Decimal("50"))

    async def test_weekly_and_monthly_candles_are_aggregated(self) -> None:
        dates = [
            datetime(2026, 8, 31, tzinfo=timezone.utc),
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 8, tzinfo=timezone.utc),
            datetime(2026, 10, 1, tzinfo=timezone.utc),
        ]
        source = [
            Candle(
                timestamp=value,
                open_price=Decimal(index + 1),
                high_price=Decimal(index + 2),
                low_price=Decimal(index),
                close_price=Decimal(index + 1),
                volume=Decimal("1"),
                currency=Currency.KRW,
            )
            for index, value in enumerate(dates)
        ]
        self.assertEqual(len(self.engine._aggregate_candles(source, "1w")), 3)
        self.assertEqual(len(self.engine._aggregate_candles(source, "1M")), 3)

    async def test_korean_stock_name_search_prefers_exact_match(self) -> None:
        client = TossMarketClient("test", "test")
        client._korean_stocks = [
            {"symbol": "005930", "name": "삼성전자", "market": "KOSPI"},
            {"symbol": "028300", "name": "HLB", "market": "KOSDAQ"},
            {"symbol": "001230", "name": "동국홀딩스", "market": "KOSPI"},
            {"symbol": "014530", "name": "극동유화", "market": "KOSPI"},
        ]
        results = await client.search_stocks("삼성전자")
        self.assertEqual(results[0]["symbol"], "005930")
        english_name_results = await client.search_stocks("HLB")
        self.assertEqual(english_name_results[0]["symbol"], "028300")

    async def test_recommendation_analysis_calculates_indicators(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        candles = [
            Candle(
                timestamp=start + timedelta(days=index),
                open_price=Decimal(100 + index),
                high_price=Decimal(102 + index),
                low_price=Decimal(99 + index),
                close_price=Decimal(101 + index),
                volume=Decimal("200") if index == 119 else Decimal("100"),
                currency=Currency.KRW,
            )
            for index in range(120)
        ]
        result = analyze_candidate(candles, Decimal("220"), Decimal("0.02"))
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["score"], 70)
        self.assertEqual(result["volume_ratio"], "2.00")

    async def test_recommendation_detects_support_touch(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        candles = []
        for index in range(120):
            close = Decimal("102") + Decimal(index % 5)
            low = Decimal("100") if index % 10 == 8 or index == 119 else close - Decimal("1")
            candles.append(
                Candle(
                    timestamp=start + timedelta(days=index),
                    open_price=close - Decimal("0.5"),
                    high_price=close + Decimal("2"),
                    low_price=low,
                    close_price=close,
                    volume=Decimal("100"),
                    currency=Currency.KRW,
                )
            )
        result = analyze_candidate(candles, Decimal("102"), Decimal("0.01"))
        self.assertIsNotNone(result)
        self.assertTrue(result["support_touched"])
        self.assertEqual(result["support"], "101.00")
        self.assertEqual(result["support_distance_percent"], "0.99")

    async def test_weekly_report_sets_defensive_direction(self) -> None:
        issue = {
            "title": "증시 급락과 경기 침체 우려, 금리 인상",
            "url": "https://example.com/news",
            "source": "test",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        report = WeeklyReportService._analyze([issue], [issue], datetime.now(timezone.utc))
        self.assertEqual(report["direction"], "defensive")
        self.assertLess(report["sentiment_score"], 0)

    async def test_market_report_limits_and_deduplicates_issues(self) -> None:
        now = datetime.now(timezone.utc)
        duplicate = {"title": "같은 시장 이슈 - 매체", "url": "https://example.com/1", "source": "test", "published_at": now.isoformat()}
        global_news = [duplicate] + [
            {"title": f"해외 이슈 {index}", "url": f"https://example.com/g{index}", "source": "test", "published_at": now.isoformat()}
            for index in range(4)
        ]
        domestic = [duplicate] + [
            {"title": f"국내 이슈 {index}", "url": f"https://example.com/d{index}", "source": "test", "published_at": now.isoformat()}
            for index in range(3)
        ]
        report = WeeklyReportService._analyze(domestic, global_news, now, "daily", now + timedelta(days=1))
        self.assertEqual(len(report["global"]), 3)
        self.assertEqual(len(report["domestic"]), 3)
        self.assertNotIn(duplicate["url"], [item["url"] for item in report["domestic"]])
