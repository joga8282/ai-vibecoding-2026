from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.models import Currency, OrderStatus, Side
from app.paper import PaperBroker
from app.recommendations import analyze_candidate


class PaperAutoTrader:
    """A fixed-budget paper session; all executions stay in the local broker."""

    def __init__(self, engine, recommend):
        self.engine = engine
        self.recommend = recommend
        self.session = None
        self.message = "가상 자동매매 시작 대기"
        self.next_scan = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = asyncio.Lock()

    async def restore(self):
        self.session = await self.engine.repository.load("auto_session") or None

    async def save(self):
        await self.engine.repository.save("auto_session", self.session or {})

    async def reset(self):
        self.session = None
        await self.save()

    async def prepare(self):
        engine = self.engine
        if engine.settings.mode != "paper" or type(engine.broker) is not PaperBroker:
            raise RuntimeError("가상계좌 PAPER 주문만 지원합니다.")
        if not engine.toss_client:
            raise RuntimeError("자동매매에는 토스 시세 연결이 필요합니다.")
        if not self.session:
            account = engine.broker.account(engine.quotes)
            budget = min(Decimal(account["cash"]["KRW"]), Decimal(account["total_equity"]["KRW"]) * engine.settings.recommended_trade_ratio)
            if budget <= 0:
                raise RuntimeError("사용 가능한 가상 매매 예산이 없습니다.")
            self.session = {"id": uuid4().hex, "budget": str(budget), "targets": {}}
            await self.save()
        self.next_scan = datetime.min.replace(tzinfo=timezone.utc)
        self.message = "가상 자동매매 실행 준비"

    def orders(self):
        if not self.session:
            return []
        prefix = f"auto-{self.session['id']}-"
        return [order for order in self.engine.broker.orders if order.client_order_id.startswith(prefix) and order.status is OrderStatus.FILLED]

    def spent(self):
        return sum((order.filled_price * order.quantity + order.fee for order in self.orders() if order.side is Side.BUY), Decimal("0"))

    def status(self):
        budget = Decimal(self.session["budget"]) if self.session else Decimal("0")
        return {"budget": str(budget), "spent": str(self.spent()), "remaining": str(max(Decimal("0"), budget - self.spent())), "message": self.message, "targets": list(self.session["targets"].values()) if self.session else []}

    def active(self):
        live_ready = (self.engine.settings.mode == "live"
                      and self.engine.settings.live_trading_enabled
                      and getattr(self.engine.broker, 'risk', None)
                      and self.engine.broker.risk.armed
                      and self.engine.broker.reconciled)
        return self.engine.running and not self.engine.kill_switch and (self.engine.settings.mode == "paper" or live_ready)

    async def quote(self, symbol):
        quotes = await asyncio.wait_for(self.engine.toss_client.prices([symbol]), timeout=10)
        quote = next((q for q in quotes if q.symbol == symbol and q.currency is Currency.KRW and q.source == "toss" and q.price > 0), None)
        if not quote or not timedelta(0) <= datetime.now(timezone.utc) - quote.timestamp <= timedelta(seconds=60):
            raise RuntimeError(f"{symbol}: 유효한 현재가가 없어 주문 보류")
        self.engine.set_quote(quote)
        return quote

    async def tick(self):
        async with self._lock:
            if not self.active() or not self.session:
                return
            # Exits are evaluated before scanning; failed analysis cannot block exits.
            buys = {order.symbol: order for order in self.orders() if order.side is Side.BUY}
            for symbol, buy in buys.items():
                position = self.engine.broker.positions.get(symbol)
                if not position:
                    continue
                try:
                    quote = await self.quote(symbol)
                    target = self.session["targets"][symbol]
                    if quote.price >= Decimal(target["take_profit"]) or quote.price <= buy.filled_price * Decimal("0.97"):
                        if self.active():
                            await self.engine.broker.place_market_order(client_order_id=f"auto-{self.session['id']}-{symbol}-sell", quote=quote, side=Side.SELL, quantity=min(position.quantity, buy.quantity))
                            self.message = f"{target['name']} 가상 매도 처리"
                except Exception as exc:
                    self.message = f"매도 확인 보류: {exc}"
            now = datetime.now(timezone.utc)
            if now < self.next_scan or not self.active():
                return
            self.next_scan = now + timedelta(seconds=60)
            remaining = Decimal(self.session["budget"]) - self.spent()
            if remaining <= 0 or len(buys) >= 5:
                self.message = "신규 매수 완료 · 자동 매도 감시 중"
                return
            try:
                payload = await asyncio.wait_for(self.recommend(), timeout=45)
                candidates = payload["candidates"]
                self.message = "조건을 통과한 추천 종목 대기" if not candidates else "추천 후보 매수 조건 확인 중"
                for candidate in candidates:
                    if not self.active() or len(buys) >= 5:
                        break
                    symbol = candidate["symbol"]
                    if candidate.get("currency") != "KRW" or not candidate.get("eligible") or symbol in buys or symbol in self.engine.broker.positions:
                        continue
                    quote = await self.quote(symbol)
                    if not await self.engine._buy_allowed(quote):
                        continue
                    # Recheck the full entry signal at the refreshed price.
                    candles = self.engine._buy_candles[symbol][1]
                    previous = [bar for bar in candles if bar.timestamp.astimezone(timezone(timedelta(hours=9))).date() < quote.timestamp.astimezone(timezone(timedelta(hours=9))).date()]
                    analysis = analyze_candidate(candles, quote.price, quote.price / previous[-1].close_price - 1)
                    if not analysis or not analysis["eligible"]:
                        continue
                    settings = self.engine.settings
                    fill = quote.price * (1 + settings.slippage_bps / Decimal("10000"))
                    unit_cost = fill * (1 + settings.fee_rate)
                    remaining = Decimal(self.session["budget"]) - self.spent()
                    allocation = Decimal(self.session["budget"]) / 5
                    if candidate.get("weekly_direction") == "defensive":
                        allocation /= 2
                    allowance = min(remaining, allocation, self.engine.broker.cash[Currency.KRW])
                    quantity = min(int(allowance // unit_cost), int(settings.max_order_amount_krw // fill))
                    take_profit = min(Decimal(analysis["daily_resistance"]), Decimal(analysis["weekly_resistance"]))
                    if quantity < 1 or take_profit <= unit_cost:
                        continue
                    self.session["targets"][symbol] = {"symbol": symbol, "name": candidate.get("name") or symbol, "take_profit": str(take_profit)}
                    # Persist exit rules before the order; the broker's order ledger
                    # supplies actual spend after stop, restart or an interrupted save.
                    await self.save()
                    if not self.active():
                        break
                    order = await self.engine.broker.place_market_order(client_order_id=f"auto-{self.session['id']}-{symbol}-buy", quote=quote, side=Side.BUY, quantity=Decimal(quantity))
                    if order.status is OrderStatus.FILLED:
                        buys[symbol] = order
                        self.message = f"{candidate.get('name') or symbol} {quantity}주 가상 매수"
            except Exception as exc:
                self.message = f"추천 매수 보류: {exc}"
