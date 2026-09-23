from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from decimal import Decimal

from datetime import datetime

from app.models import Candle, Currency, Quote, utc_now
import websockets


class TossMarketClient:
    """Read-only Toss market client. This class intentionally has no order methods."""

    base_url = "https://openapi.tossinvest.com"
    websocket_url = "wss://openapi-ws.tossinvest.com/ws/v1"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._stock_cache_lock = asyncio.Lock()
        self._korean_stocks: list[dict] | None = None

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - 60:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires_at - 60:
                return self._token
            payload = await asyncio.to_thread(self._issue_token_sync)
            self._token = payload["access_token"]
            self._expires_at = time.monotonic() + int(payload.get("expires_in", 3600))
            return self._token

    def _issue_token_sync(self) -> dict:
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/oauth2/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    async def prices(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        token = await self._access_token()
        return await asyncio.to_thread(self._prices_sync, token, symbols)

    async def stock_info(self, symbol: str) -> dict | None:
        normalized = symbol.upper()
        if self._korean_stocks is not None:
            cached = next(
                (stock for stock in self._korean_stocks if stock.get("symbol") == normalized),
                None,
            )
            if cached:
                return cached
        token = await self._access_token()
        query = urllib.parse.urlencode({"symbols": normalized})
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/stocks?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = await asyncio.to_thread(self._request_json_sync, request)
        stocks = payload.get("result", [])
        return stocks[0] if stocks else None

    async def stocks_info(self, symbols: list[str]) -> list[dict]:
        if not symbols:
            return []
        token = await self._access_token()
        query = urllib.parse.urlencode({"symbols": ",".join(symbols[:200])})
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/stocks?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = await asyncio.to_thread(self._request_json_sync, request)
        return payload.get("result", [])

    async def rankings(self, count: int = 20) -> dict:
        token = await self._access_token()
        query = urllib.parse.urlencode(
            {
                "type": "MARKET_TRADING_AMOUNT",
                "marketCountry": "KR",
                "duration": "realtime",
                "excludeInvestmentCaution": "true",
                "count": min(max(count, 1), 100),
            }
        )
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/rankings?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = await asyncio.to_thread(self._request_json_sync, request)
        return payload.get("result", {})

    @staticmethod
    def _request_json_sync(request: urllib.request.Request) -> dict:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    async def search_stocks(self, query: str, limit: int = 10) -> list[dict]:
        """Search active Korean stocks by Korean name or symbol."""
        if self._korean_stocks is None:
            async with self._stock_cache_lock:
                if self._korean_stocks is None:
                    token = await self._access_token()
                    stocks: list[dict] = []
                    for index, market in enumerate(("KOSPI", "KOSDAQ", "KR_ETC")):
                        if index:
                            await asyncio.sleep(1.05)
                        market_stocks = await asyncio.to_thread(
                            self._stocks_all_sync, token, market
                        )
                        for stock in market_stocks:
                            stock.setdefault("market", market)
                        stocks.extend(market_stocks)
                    self._korean_stocks = stocks
        needle = query.strip().casefold()
        if not needle:
            return []
        matches = [
            stock
            for stock in self._korean_stocks
            if needle in str(stock.get("name", "")).casefold()
            or needle in str(stock.get("symbol", "")).casefold()
        ]
        matches.sort(
            key=lambda stock: (
                str(stock.get("name", "")).casefold() != needle,
                not str(stock.get("name", "")).casefold().startswith(needle),
                str(stock.get("name", "")),
            )
        )
        return matches[:limit]

    def _stocks_all_sync(self, token: str, market: str) -> list[dict]:
        query = urllib.parse.urlencode({"market": market, "status": "ACTIVE"})
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/stocks/all?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        return payload.get("result", [])

    def _prices_sync(self, token: str, symbols: list[str]) -> list[Quote]:
        query = urllib.parse.urlencode({"symbols": ",".join(symbols[:200])})
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/prices?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        quotes: list[Quote] = []
        for item in payload.get("result", []):
            quotes.append(
                Quote(
                    symbol=item["symbol"].upper(),
                    price=Decimal(item["lastPrice"]),
                    currency=Currency(item["currency"]),
                    timestamp=utc_now(),
                    source="toss",
                )
            )
        return quotes

    async def candles(
        self, symbol: str, interval: str = "1m", count: int = 100
    ) -> list[Candle]:
        token = await self._access_token()
        normalized = symbol.upper()
        remaining = max(count, 1)
        before: str | None = None
        candles: list[Candle] = []
        seen_timestamps: set[datetime] = set()
        while remaining > 0:
            page_size = min(remaining, 200)
            page, next_before = await asyncio.to_thread(
                self._candles_page_sync,
                token,
                normalized,
                interval,
                page_size,
                before,
            )
            for candle in page:
                if candle.timestamp not in seen_timestamps:
                    candles.append(candle)
                    seen_timestamps.add(candle.timestamp)
            remaining = count - len(candles)
            if not page or not next_before or next_before == before:
                break
            before = next_before
        return candles[:count]

    def _candles_page_sync(
        self,
        token: str,
        symbol: str,
        interval: str,
        count: int,
        before: str | None = None,
    ) -> tuple[list[Candle], str | None]:
        parameters = {
            "symbol": symbol,
            "interval": interval,
            "count": min(max(count, 1), 200),
            "adjusted": "true",
        }
        if before:
            parameters["before"] = before
        query = urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/candles?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        result = payload.get("result", {})
        candles = [
            Candle(
                timestamp=datetime.fromisoformat(item["timestamp"]),
                open_price=Decimal(item["openPrice"]),
                high_price=Decimal(item["highPrice"]),
                low_price=Decimal(item["lowPrice"]),
                close_price=Decimal(item["closePrice"]),
                volume=Decimal(item["volume"]),
                currency=Currency(item["currency"]),
            )
            for item in result.get("candles", [])
        ]
        return candles, result.get("nextBefore")

    async def trade_stream(
        self, symbol: str, currency: Currency
    ) -> AsyncIterator[dict]:
        """Yield Toss realtime trade ticks for one symbol."""
        token = await self._access_token()
        market = "kr" if currency is Currency.KRW else "us"
        normalized = symbol.upper()
        async with websockets.connect(
            self.websocket_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            ping_interval=60,
            ping_timeout=20,
            close_timeout=5,
        ) as socket:
            await socket.send(
                json.dumps(
                    [
                        {"id": f"dashboard-{normalized}"},
                        {"type": f"trade:{market}", "codes": [normalized]},
                    ]
                )
            )
            async for raw_message in socket:
                if not isinstance(raw_message, str):
                    continue
                message = json.loads(raw_message)
                if message.get("type") == "error":
                    error = message.get("error", {})
                    raise RuntimeError(error.get("message") or error.get("code") or "Toss WebSocket error")
                if message.get("type") == "subscriptions":
                    topic = f"trade:{market}:{normalized}"
                    if topic not in message.get("subscribed", []):
                        rejected = message.get("rejected", [])
                        raise RuntimeError(f"Toss rejected subscription: {rejected}")
                    yield {"_type": "connected"}
                    continue
                if message.get("type") != "message":
                    continue
                if message.get("topic") != f"trade:{market}:{normalized}":
                    continue
                data = message.get("data") or {}
                if {"price", "volume", "timestamp", "currency"} <= data.keys():
                    yield data
