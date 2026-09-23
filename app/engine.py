from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.config import Settings
from app.models import Candle, Currency, Quote, Side, ThresholdStrategy, serialize
from app.paper import PaperBroker
from app.repository import SnapshotRepository
from app.toss import TossMarketClient

logger = logging.getLogger(__name__)


class TradingEngine:
    snapshot_name = "engine"

    def __init__(
        self,
        settings: Settings,
        broker: PaperBroker,
        repository: SnapshotRepository,
        toss_client: TossMarketClient | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.repository = repository
        self.toss_client = toss_client
        self.quotes: dict[str, Quote] = {}
        self.quote_history: dict[str, list[Quote]] = {}
        self.strategies: dict[str, ThresholdStrategy] = {}
        self.running = False
        self.kill_switch = False
        self.last_error: str | None = None
        self.last_tick_at: datetime | None = None
        self._attempted_signal: dict[str, Side] = {}
        self._task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def restore(self) -> None:
        snapshot = await self.repository.load(self.snapshot_name)
        if not snapshot:
            return
        self.kill_switch = bool(snapshot.get("kill_switch", False))
        self.strategies = {
            item["strategy_id"]: ThresholdStrategy(
                strategy_id=item["strategy_id"],
                symbol=item["symbol"],
                currency=Currency(item["currency"]),
                quantity=Decimal(item["quantity"]),
                buy_below=Decimal(item["buy_below"]),
                sell_above=Decimal(item["sell_above"]),
                enabled=bool(item["enabled"]),
            )
            for item in snapshot.get("strategies", [])
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.running:
                return
            if self.kill_switch:
                raise RuntimeError("킬 스위치가 활성화되어 있습니다.")
            self.running = True
            self._task = asyncio.create_task(self._run(), name="paper-trading-engine")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self.running = False
            task = self._task
            self._task = None
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def activate_kill_switch(self) -> None:
        self.kill_switch = True
        await self.stop()
        await self._persist()

    async def clear_kill_switch(self) -> None:
        if self.running:
            raise RuntimeError("엔진 실행 중에는 킬 스위치를 해제할 수 없습니다.")
        self.kill_switch = False
        await self._persist()

    async def upsert_strategy(self, strategy: ThresholdStrategy) -> None:
        self.strategies[strategy.strategy_id] = strategy
        await self._persist()

    async def set_strategy_enabled(self, strategy_id: str, enabled: bool) -> None:
        strategy = self.strategies.get(strategy_id)
        if not strategy:
            raise KeyError(strategy_id)
        strategy.enabled = enabled
        await self._persist()

    def set_quote(self, quote: Quote) -> None:
        self.quotes[quote.symbol] = quote
        history = self.quote_history.setdefault(quote.symbol, [])
        history.append(quote)
        if len(history) > 200:
            del history[:-200]

    async def lookup_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        if self.toss_client:
            quotes = await self.toss_client.prices([symbol])
            if not quotes:
                raise LookupError(symbol)
            self.set_quote(quotes[0])
            return quotes[0]
        quote = self.quotes.get(symbol)
        if not quote:
            raise LookupError(symbol)
        return quote

    async def candles(self, symbol: str, interval: str, count: int) -> list[Candle]:
        symbol = symbol.upper()
        minute_intervals = {"1m": 1, "5m": 5, "15m": 15, "50m": 50, "60m": 60}
        base_interval = "1m" if interval in minute_intervals else "1d"
        if interval == base_interval:
            source_count = count
        elif interval in minute_intervals:
            source_count = min(count * minute_intervals[interval], 3000)
        elif interval == "1w":
            source_count = min(count * 7, 2000)
        elif interval == "1M":
            source_count = min(count * 31, 3000)
        else:
            source_count = 200
        if self.toss_client:
            source = await self.toss_client.candles(symbol, base_interval, source_count)
        else:
            history = self.quote_history.get(symbol, [])[-source_count:]
            source = [
                Candle(
                    timestamp=quote.timestamp,
                    open_price=quote.price,
                    high_price=quote.price,
                    low_price=quote.price,
                    close_price=quote.price,
                    volume=Decimal("0"),
                    currency=quote.currency,
                )
                for quote in history
            ]
        if interval == base_interval:
            return sorted(source, key=lambda item: item.timestamp)[-count:]
        return self._aggregate_candles(source, interval)[-count:]

    @staticmethod
    def _aggregate_candles(candles: list[Candle], interval: str) -> list[Candle]:
        minute_intervals = {"5m": 5, "15m": 15, "50m": 50, "60m": 60}

        def bucket(timestamp: datetime) -> datetime:
            if interval in minute_intervals:
                minutes = minute_intervals[interval]
                start_of_day = datetime.combine(
                    timestamp.date(), time.min, tzinfo=timestamp.tzinfo
                )
                total_minutes = timestamp.hour * 60 + timestamp.minute
                return start_of_day + timedelta(
                    minutes=(total_minutes // minutes) * minutes
                )
            if interval == "1w":
                start_date = timestamp.date() - timedelta(days=timestamp.weekday())
                return datetime.combine(start_date, time.min, tzinfo=timestamp.tzinfo)
            if interval == "1M":
                return datetime(timestamp.year, timestamp.month, 1, tzinfo=timestamp.tzinfo)
            raise ValueError(f"지원하지 않는 캔들 주기입니다: {interval}")

        grouped: dict[datetime, list[Candle]] = {}
        for candle in sorted(candles, key=lambda item: item.timestamp):
            grouped.setdefault(bucket(candle.timestamp), []).append(candle)

        result: list[Candle] = []
        for timestamp, items in grouped.items():
            result.append(
                Candle(
                    timestamp=timestamp,
                    open_price=items[0].open_price,
                    high_price=max(item.high_price for item in items),
                    low_price=min(item.low_price for item in items),
                    close_price=items[-1].close_price,
                    volume=sum((item.volume for item in items), Decimal("0")),
                    currency=items[0].currency,
                )
            )
        return result

    async def refresh_market_data(self) -> list[Quote]:
        if not self.toss_client:
            raise RuntimeError("토스 API 자격 증명이 설정되지 않았습니다.")
        symbols = sorted(
            {strategy.symbol for strategy in self.strategies.values() if strategy.enabled}
        )
        if not symbols:
            raise RuntimeError("활성화된 전략이 없어 조회할 종목이 없습니다.")
        quotes = await self.toss_client.prices(symbols)
        for quote in quotes:
            self.set_quote(quote)
        self.last_error = None
        return quotes

    async def _run(self) -> None:
        try:
            while self.running:
                await self.tick()
                await asyncio.sleep(self.settings.engine_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep API alive, stop trading safely
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.running = False
            logger.exception("Trading engine stopped after an unexpected error")

    async def tick(self) -> None:
        enabled = [strategy for strategy in self.strategies.values() if strategy.enabled]
        if self.toss_client and enabled:
            try:
                await self.refresh_market_data()
            except Exception as exc:
                self.last_error = f"market-data: {type(exc).__name__}: {exc}"
                logger.warning("Toss market data refresh failed: %s", exc)

        if not self.kill_switch:
            for strategy in enabled:
                await self._evaluate(strategy)
        self.last_tick_at = datetime.now(timezone.utc)

    async def _evaluate(self, strategy: ThresholdStrategy) -> None:
        quote = self.quotes.get(strategy.symbol)
        if not quote or quote.currency is not strategy.currency:
            return
        position = self.broker.positions.get(strategy.symbol)
        if quote.price <= strategy.buy_below and not position:
            side = Side.BUY
            quantity = strategy.quantity
        elif quote.price >= strategy.sell_above and position:
            side = Side.SELL
            quantity = min(strategy.quantity, position.quantity)
        else:
            self._attempted_signal.pop(strategy.strategy_id, None)
            return
        if self._attempted_signal.get(strategy.strategy_id) is side:
            return
        self._attempted_signal[strategy.strategy_id] = side
        client_order_id = (
            f"{strategy.strategy_id}-{strategy.symbol}-{side.value.lower()}-{uuid4().hex[:12]}"
        )
        await self.broker.place_market_order(
            client_order_id=client_order_id,
            quote=quote,
            side=side,
            quantity=quantity,
        )

    def status(self) -> dict:
        return {
            "version": self.settings.version,
            "mode": self.settings.mode,
            "running": self.running,
            "kill_switch": self.kill_switch,
            "market_data": "toss" if self.toss_client else "manual",
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_error": self.last_error,
            "strategy_count": len(self.strategies),
            "quote_count": len(self.quotes),
        }

    async def _persist(self) -> None:
        await self.repository.save(
            self.snapshot_name,
            {
                "kill_switch": self.kill_switch,
                "strategies": serialize(list(self.strategies.values())),
            },
        )
