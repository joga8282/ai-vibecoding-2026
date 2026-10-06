from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from decimal import Decimal

from datetime import datetime

from app.models import Candle, Currency, Quote, utc_now
import websockets


class TossAPIError(RuntimeError):
    """Safe diagnostic for upstream HTTP failures; never includes response data."""

    def __init__(self, status: int, path: str) -> None:
        self.status = status
        self.path = path
        super().__init__(f"Toss API returned HTTP {status} for {path}.")


class TossMarketClient:
    """Read-only Toss market client. This class intentionally has no order methods."""

    base_url = "https://openapi.tossinvest.com"
    websocket_url = "wss://openapi-ws.tossinvest.com/ws/v1"
    requires_orderbook_snapshot = True

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

    async def orderbook(self, symbol: str) -> dict:
        token = await self._access_token()
        query = urllib.parse.urlencode({"symbol": symbol.upper()})
        path = "/api/v1/orderbook"
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        for attempt in range(3):
            try:
                payload = await asyncio.to_thread(self._request_json_sync, request)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    exc.close()
                    await asyncio.sleep(0.25 * (2 ** attempt))
                    continue
                status = exc.code
                exc.close()
                raise TossAPIError(status, path) from None
        result = payload.get('result', payload)
        if not isinstance(result, dict):
            raise RuntimeError('Toss returned an invalid order book.')
        return result

    async def live_quote(self, symbol: str) -> Quote:
        """Build a current indicative price from a fresh two-sided order book."""
        book = await self.orderbook(symbol)
        return self._quote_from_orderbook(symbol, book)

    @staticmethod
    def _quote_from_orderbook(symbol: str, book: dict) -> Quote:
        try:
            timestamp = datetime.fromisoformat(str(book['timestamp']).replace('Z', '+00:00'))
            currency = Currency(book.get('currency', 'KRW'))
            bids, asks = book['bids'], book['asks']
            bid = Decimal(str(bids[0]['price']))
            ask = Decimal(str(asks[0]['price']))
            bid_volume = Decimal(str(bids[0]['volume']))
            ask_volume = Decimal(str(asks[0]['volume']))
        except (ArithmeticError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise RuntimeError('Toss returned an incomplete order book.') from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise RuntimeError('Toss order book timestamp must include a timezone.')
        if (currency is not Currency.KRW or not bid.is_finite() or not ask.is_finite()
                or not bid_volume.is_finite() or not ask_volume.is_finite()
                or bid <= 0 or ask <= 0 or bid >= ask
                or bid_volume <= 0 or ask_volume <= 0):
            raise RuntimeError('Toss returned an invalid two-sided KRW order book.')
        return Quote(
            symbol.upper(), (bid + ask) / Decimal(2), currency, timestamp,
            source='toss', bid_price=bid, ask_price=ask,
        )

    async def live_quotes(self, symbols: list[str], *, timeout: float = 5.0) -> list[Quote]:
        """Collect timestamped, real-time KR order-book updates on one socket."""
        normalized = sorted(set(symbol.upper() for symbol in symbols))
        if not normalized:
            return []
        if any(not (len(symbol) == 6 and symbol.isdigit()) for symbol in normalized):
            raise ValueError('Toss KR order-book stream requires six-digit symbols.')
        token = await self._access_token()
        topics = {f'orderbook:kr:{symbol}': symbol for symbol in normalized}
        request_id = 'risk-orderbook'
        received: dict[str, Quote] = {}
        async with websockets.connect(
            self.websocket_url,
            additional_headers={'Authorization': f'Bearer {token}'},
            ping_interval=60,
            ping_timeout=20,
            close_timeout=5,
        ) as socket:
            await socket.send(json.dumps([
                {'id': request_id},
                {'type': 'orderbook:kr', 'codes': normalized},
            ]))
            deadline = time.monotonic() + timeout
            while len(received) < len(normalized):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = ','.join(sorted(set(normalized) - set(received)))
                    raise RuntimeError(f'Toss real-time order book timed out for {missing}.')
                raw_message = await asyncio.wait_for(socket.recv(), timeout=remaining)
                if not isinstance(raw_message, str):
                    continue
                message = json.loads(raw_message)
                if message.get('type') == 'error':
                    error = message.get('error', {})
                    raise RuntimeError(error.get('message') or error.get('code') or
                                       'Toss WebSocket error')
                if message.get('type') == 'subscriptions':
                    subscribed = set(message.get('subscribed', []))
                    rejected = message.get('rejected', [])
                    expected = set(topics)
                    if expected.issubset(subscribed):
                        continue
                    detail = ', '.join(str(item.get('message') or item.get('code') or item)
                                       for item in rejected)
                    raise RuntimeError('Toss rejected real-time order-book subscription' +
                                       (f': {detail}' if detail else '.'))
                if message.get('type') != 'message':
                    continue
                symbol = topics.get(message.get('topic'))
                data = message.get('data')
                if symbol is None or not isinstance(data, dict):
                    continue
                received[symbol] = self._quote_from_orderbook(symbol, data)
        return [received[symbol] for symbol in normalized]

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

    async def _account_get(self, path: str, account_seq: str | None = None,
                           parameters: dict | None = None):
        token = await self._access_token()
        query = urllib.parse.urlencode({k: v for k, v in (parameters or {}).items() if v is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": f"Bearer {token}"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        payload = await asyncio.to_thread(
            self._request_json_sync, urllib.request.Request(url, headers=headers)
        )
        return payload.get('result', payload)

    async def _account_post(self, path: str, account_seq: str, body: dict):
        token = await self._access_token()
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "X-Tossinvest-Account": str(account_seq),
                     "Content-Type": "application/json"},
            method="POST",
        )
        payload = await asyncio.to_thread(self._request_json_sync, request)
        return payload.get('result', payload)

    async def accounts(self) -> list[dict]:
        result = await self._account_get('/api/v1/accounts')
        return result if isinstance(result, list) else result.get('accounts', [])

    async def holdings(self, account_seq: str) -> dict:
        result = await self._account_get('/api/v1/holdings', account_seq)
        return result if isinstance(result, dict) else {'items': result}

    async def buying_power(self, account_seq: str, currency: Currency = Currency.KRW) -> dict:
        result = await self._account_get('/api/v1/buying-power', account_seq, {'currency': currency.value})
        return result if isinstance(result, dict) else {'items': result}

    async def sellable_quantity(self, account_seq: str, symbol: str) -> dict:
        result = await self._account_get('/api/v1/sellable-quantity', account_seq, {'symbol': symbol})
        return result if isinstance(result, dict) else {'items': result}

    async def account_orders(self, account_seq: str, status: str | None = None) -> list[dict]:
        result = await self._account_get('/api/v1/orders', account_seq, {'status': status})
        if isinstance(result, list):
            return result
        return result.get('orders') or result.get('items') or []

    async def account_order(self, account_seq: str, order_id: str) -> dict:
        result = await self._account_get(
            f'/api/v1/orders/{urllib.parse.quote(order_id, safe="")}', account_seq
        )
        return result if isinstance(result, dict) else {'items': result}

    async def create_order(self, account_seq: str, request: dict) -> dict:
        return await self._account_post('/api/v1/orders', account_seq, request)

    async def cancel_account_order(self, account_seq: str, order_id: str) -> dict:
        return await self._account_post(
            f'/api/v1/orders/{urllib.parse.quote(order_id, safe="")}/cancel', account_seq, {}
        )

    async def commissions(self, account_seq: str) -> dict:
        result = await self._account_get('/api/v1/commissions', account_seq)
        return result if isinstance(result, dict) else {'items': result}

    async def market_calendar(self, country: str = 'KR') -> dict:
        result = await self._account_get(f'/api/v1/market-calendar/{country.upper()}')
        return result if isinstance(result, dict) else {'items': result}

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
                    timestamp=datetime.fromisoformat(item['timestamp']),
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
