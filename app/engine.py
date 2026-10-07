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
from app.recommendations import analyze_candidate
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
        self._buy_candles: dict[str, tuple[datetime, list[Candle]]] = {}
        self._four_hour_candles: dict[str, tuple[datetime, list[Candle]]] = {}
        self._weekly_candles: dict[str, tuple[datetime, list[Candle]]] = {}
        self._candle_history_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._candle_cache_counts: dict[tuple[str, str], int] = {}
        self.buy_filter_status: dict[str, str] = {}
        self._stock_names: dict[str, tuple[datetime, str | None]] = {}
        self._stock_names_lock = asyncio.Lock()
        self.automation = None

    async def with_stock_names(self, rows: list[dict]) -> list[dict]:
        """Enrich display rows without changing saved orders or positions."""
        symbols = {row["symbol"] for row in rows}
        if self.toss_client and symbols:
            async with self._stock_names_lock:
                now = datetime.now(timezone.utc)
                missing = sorted(symbol for symbol in symbols if (
                    symbol not in self._stock_names or self._stock_names[symbol][0] <= now
                ))
                for offset in range(0, len(missing), 200):
                    batch = missing[offset:offset + 200]
                    # Cache misses/failures briefly to avoid polling the metadata API.
                    for symbol in batch:
                        previous = self._stock_names.get(symbol)
                        self._stock_names[symbol] = (now + timedelta(minutes=1), previous[1] if previous else None)
                    try:
                        stocks = await asyncio.wait_for(self.toss_client.stocks_info(batch), timeout=5)
                        for stock in stocks:
                            name = stock.get("name")
                            if stock.get("symbol") in symbols and isinstance(name, str) and name.strip():
                                self._stock_names[stock["symbol"]] = (now + timedelta(hours=24), name.strip())
                    except Exception as exc:
                        logger.warning("Stock name lookup failed: %s", exc)
        return [{**row, "name": self._stock_names.get(row["symbol"], (None, None))[1]} for row in rows]

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
            if self.settings.mode == "live":
                broker = self.broker
                if not self.settings.live_trading_enabled or not getattr(broker, "risk", None):
                    raise RuntimeError("LIVE order execution is disabled.")
                if not broker.risk.armed or not broker.reconciled:
                    raise RuntimeError("Reconcile and manually arm LIVE before starting automation.")
            elif self.settings.mode != "paper":
                raise RuntimeError("Unknown trader mode.")
            if self.automation:
                await self.automation.prepare()
            if self.kill_switch:
                raise RuntimeError("킬 스위치가 활성화되어 있습니다.")
            self.running = True
            self.last_error = None
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
            if (self.settings.mode == 'live'
                    and getattr(self.toss_client, 'requires_orderbook_snapshot', False) is True):
                quote = await self.toss_client.live_quote(symbol)
            else:
                quotes = await self.toss_client.prices([symbol])
                if not quotes:
                    raise LookupError(symbol)
                quote = quotes[0]
            self.set_quote(quote)
            return quote
        quote = self.quotes.get(symbol)
        if not quote:
            raise LookupError(symbol)
        return quote

    async def candles(self, symbol: str, interval: str, count: int) -> list[Candle]:
        symbol = symbol.upper()
        if interval in {'4h', '1w'}:
            # A chart and an automatic scan must not duplicate the same large
            # history fetch. Recheck the cache after the first caller finishes.
            key = (symbol, interval)
            lock = self._candle_history_locks.setdefault(key, asyncio.Lock())
            async with lock:
                return await self._load_candles(symbol, interval, count)
        return await self._load_candles(symbol, interval, count)

    async def _load_candles(self, symbol: str, interval: str, count: int) -> list[Candle]:
        now = datetime.now(timezone.utc)
        if interval == "4h":
            cached = self._four_hour_candles.get(symbol)
            capacity = self._candle_cache_counts.get((symbol, interval), len(cached[1]) if cached else 0)
            if cached and now - cached[0] < timedelta(minutes=4) and capacity >= count:
                return cached[1][-count:]
        elif interval == "1w":
            cached = self._weekly_candles.get(symbol)
            # Completed-week analysis does not need another multi-page daily
            # history fetch on every five-minute scan. Refresh the history
            # during the session, while still picking up a newly completed week.
            capacity = self._candle_cache_counts.get((symbol, interval), len(cached[1]) if cached else 0)
            if cached and now - cached[0] < timedelta(hours=6) and capacity >= count:
                return cached[1][-count:]
        minute_intervals = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "50m": 50, "60m": 60, "4h": 240}
        base_interval = "1m" if interval in minute_intervals else "1d"
        if interval == base_interval:
            source_count = count
        elif interval == "4h":
            # A Korean trading day produces two four-hour buckets (09:00 and
            # 13:00).  The old 3,000-minute cap only covered about eight
            # sessions, so the chart frequently stopped at 8-9 candles.
            source_count = min(count * minute_intervals[interval], 12000)
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
            if interval == "4h":
                # After-hours prices may drive fresh PAPER exits, but they must
                # not be folded into a regular-session four-hour candle.
                source = [
                    candle for candle in source
                    if candle.currency is not Currency.KRW
                    or time(9) <= candle.timestamp.astimezone(timezone(timedelta(hours=9))).time() < time(15, 30)
                ]
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
        result = self._aggregate_candles(source, interval)[-count:]
        if interval == "4h":
            self._four_hour_candles[symbol] = (datetime.now(timezone.utc), result)
            self._candle_cache_counts[(symbol, interval)] = count
        elif interval == "1w":
            self._weekly_candles[symbol] = (datetime.now(timezone.utc), result)
            self._candle_cache_counts[(symbol, interval)] = count
        return result

    @staticmethod
    def _aggregate_candles(candles: list[Candle], interval: str) -> list[Candle]:
        minute_intervals = {"5m": 5, "15m": 15, "30m": 30, "50m": 50, "60m": 60}

        def bucket(timestamp: datetime) -> datetime:
            if interval == "4h":
                local = timestamp.astimezone(timezone(timedelta(hours=9)))
                hour = 9 if local.time() < time(13) else 13
                return local.replace(hour=hour, minute=0, second=0, microsecond=0)
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
            | set(self.broker.positions)
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
        if self.automation:
            if (self.settings.mode == 'live'
                    and not (getattr(getattr(self.broker, 'risk', None), 'armed', False)
                             and getattr(self.broker, 'reconciled', False))):
                self.running = False
                self.last_error = (getattr(self.broker, 'last_order_error', None)
                                   or 'LIVE orders are locked; reconcile and review unresolved orders before arming.')
                return
            await self.automation.tick()
            if (self.settings.mode == 'live'
                    and not (self.broker.risk.armed and self.broker.reconciled)):
                self.running = False
                self.last_error = (getattr(self.broker, 'last_order_error', None)
                                   or 'LIVE orders are locked; review unresolved orders before arming.')
            self.last_tick_at = datetime.now(timezone.utc)
            return
        enabled = [strategy for strategy in self.strategies.values() if strategy.enabled]
        market_ready = True
        if self.toss_client and enabled:
            try:
                await self.refresh_market_data()
            except Exception as exc:
                market_ready = False
                self.last_error = f"market-data: {type(exc).__name__}: {exc}"
                logger.warning("Toss market data refresh failed: %s", exc)

        if not self.kill_switch:
            for strategy in enabled:
                await self._evaluate(strategy, allow_buy=market_ready)
        self.last_tick_at = datetime.now(timezone.utc)

    async def _buy_allowed(self, quote: Quote) -> bool:
        symbol = quote.symbol
        try:
            if not self.toss_client:
                self.buy_filter_status[symbol] = "일봉 데이터가 없어 신규 매수 보류"
                return False
            now = datetime.now(timezone.utc)
            cached = self._buy_candles.get(symbol)
            if not cached or now - cached[0] >= timedelta(seconds=60):
                candles = await asyncio.wait_for(self.toss_client.candles(symbol, "1d", 120), timeout=10)
                candles = sorted(candles, key=lambda item: item.timestamp)
                self._buy_candles[symbol] = (now, candles)
            else:
                candles = cached[1]
            if len(candles) < 65 or now - candles[-1].timestamp > timedelta(days=7):
                self.buy_filter_status[symbol] = "일봉 데이터 부족 또는 오래된 데이터로 매수 보류"
                return False
            market_timezone = timezone(timedelta(hours=9)) if quote.currency is Currency.KRW else timezone.utc
            quote_date = quote.timestamp.astimezone(market_timezone).date()
            previous_bars = [bar for bar in candles if bar.timestamp.astimezone(market_timezone).date() < quote_date]
            if not previous_bars or previous_bars[-1].close_price <= 0:
                self.buy_filter_status[symbol] = "전일 종가 확인 실패로 매수 보류"
                return False
            change_rate = quote.price / previous_bars[-1].close_price - Decimal("1")
            analysis = analyze_candidate(candles, quote.price, change_rate)
            if not analysis or not analysis["uptrend"]:
                self.buy_filter_status[symbol] = "상승 추세 미충족으로 매수 보류"
                return False
            if analysis["overheated"]:
                self.buy_filter_status[symbol] = "과열 제외: " + ", ".join(analysis["overheating_reasons"])
                return False
            self.buy_filter_status[symbol] = "상승 추세·과열 제외 통과"
            return True
        except Exception as exc:
            self.buy_filter_status[symbol] = "일봉 분석 실패로 매수 보류"
            logger.warning("Buy filter failed for %s: %s", symbol, exc)
            return False

    async def _evaluate(self, strategy: ThresholdStrategy, *, allow_buy: bool = True) -> None:
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
        if side is Side.BUY:
            if not allow_buy:
                self.buy_filter_status[strategy.symbol] = "현재가 갱신 실패로 매수 보류"
                return
            if not await self._buy_allowed(quote):
                return
        if self.kill_switch or not strategy.enabled:
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
        broker = self.broker
        live_readiness = None
        if self.settings.mode == "live":
            live_readiness = {
                "reconciled": bool(getattr(broker, "reconciled", False)),
                "armed": bool(getattr(getattr(broker, "risk", None), "armed", False)),
                "unresolved_order_count": sum(
                    item.get('status') in {'SUBMITTING', 'UNKNOWN', 'ACCEPTED', 'CANCEL_REQUESTED'}
                    for item in getattr(broker, 'order_journal', {}).values()),
                "last_order_error": getattr(broker, 'last_order_error', None),
                "last_sync_at": (broker.last_sync_at.isoformat()
                                 if getattr(broker, "last_sync_at", None) else None),
            }
        return {
            "version": self.settings.version,
            "mode": self.settings.mode,
            "running": self.running,
            "kill_switch": self.kill_switch,
            "market_data": "toss" if self.toss_client else "manual",
            "live_readiness": live_readiness,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_error": self.last_error,
            "strategy_count": len(self.strategies),
            "quote_count": len(self.quotes),
            "buy_filter_status": dict(self.buy_filter_status),
            "automation": self.automation.status() if self.automation else None,
        }

    async def _persist(self) -> None:
        await self.repository.save(
            self.snapshot_name,
            {
                "kill_switch": self.kill_switch,
                "strategies": serialize(list(self.strategies.values())),
            },
        )
