from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from decimal import Decimal

from app.models import Currency, Quote, utc_now


class TossMarketClient:
    """Read-only Toss market client. This class intentionally has no order methods."""

    base_url = "https://openapi.tossinvest.com"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

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
