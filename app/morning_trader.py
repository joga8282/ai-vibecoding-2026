from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone, time
from decimal import Decimal as D
from uuid import uuid4

from app.auto_trader import PaperAutoTrader
from app.models import Currency, Quote, Side, OrderStatus
from app.paper import PaperBroker

KST = timezone(timedelta(hours=9))


def morning_signal(daily, minutes, now):
    """Previous completed daily BB(20,2) plus a live, rising lower-band reclaim."""
    daily = sorted([c for c in daily if c.timestamp.astimezone(KST).date() < now.date()], key=lambda c: c.timestamp)
    bars = sorted([c for c in minutes if c.timestamp.astimezone(KST).date() == now.date() and c.timestamp <= now and c.volume > 0], key=lambda c: c.timestamp)
    if len(daily) < 20:
        return {'eligible': False, 'rejection_reasons': ['일봉 데이터 20개 미만']}
    if len(bars) < 2:
        return {'eligible': False, 'rejection_reasons': ['당일 거래 분봉 2개 미만']}
    if now - bars[-1].timestamp > timedelta(seconds=120):
        return {'eligible': False, 'rejection_reasons': ['최신 거래 분봉 120초 초과']}
    if now - daily[-1].timestamp > timedelta(days=7):
        return {'eligible': False, 'rejection_reasons': ['최근 일봉이 7일보다 오래됨']}
    closes = [c.close_price for c in daily[-20:]]
    if min(closes) <= 0:
        return {'eligible': False, 'rejection_reasons': ['유효하지 않은 일봉 가격']}
    middle = sum(closes) / 20
    deviation = (sum((c - middle) ** 2 for c in closes) / 20).sqrt()
    lower = middle - 2 * deviation
    price = bars[-1].close_price
    touched = deviation > 0 and lower > 0 and min(c.low_price for c in bars[-3:]) <= lower
    recovered = price > lower
    rising = price > bars[-2].close_price
    above_open = price > bars[-1].open_price
    near_lower = lower > 0 and price / lower <= D('1.03')
    not_gap_up = price / daily[-1].close_price < D('1.07')
    checks = [(touched, '볼린저 하단 미접촉'), (recovered, '볼린저 하단 회복 전'),
              (rising, '직전 분봉 대비 상승 아님'), (above_open, '현재 분봉 시가보다 낮음'),
              (near_lower, '볼린저 하단 대비 3% 초과'), (not_gap_up, '전일 종가 대비 7% 이상 상승')]
    reasons = [reason for passed, reason in checks if not passed]
    eligible = not reasons
    return {'eligible': bool(eligible), 'price': str(price), 'bollinger_lower': str(lower.quantize(D('.01'))),
            'bollinger_upper': str((middle + 2 * deviation).quantize(D('.01'))), 'bollinger_touched': bool(touched),
            'ma20': str(middle.quantize(D('.01'))), 'reason': '당일 거래량 확인 · 볼린저 하단 회복 · 직전 분봉 대비 상승',
            'rejection_reasons': reasons, 'score': 100 if eligible else 0, 'signal_at': bars[-1].timestamp.isoformat()}


class MorningTrader(PaperAutoTrader):
    strategy = 'morning-v1'

    def __init__(self, engine, recommend, clock=None):
        super().__init__(engine, recommend)
        self.clock = clock or (lambda: datetime.now(KST))
        self.days = {}

    async def restore(self):
        saved = await self.engine.repository.load('morning_sessions') or {}
        self.days = saved.get('days', {})
        self.session = self.days.get(self.clock().date().isoformat())
        if self.session and not self.session.get('diagnostics', {}).get('tracking_started_at'):
            self.session['diagnostics'] = {**self.session.get('diagnostics', {}),
                                           'tracking_started_at': self.clock().isoformat(),
                                           'legacy_untracked': True}
            if not self.session.get('attempted') and not self.session.get('targets'):
                self.session['outcome'] = '진입 없음'
            await self.save()

    async def save(self):
        await self.engine.repository.save('morning_sessions', {'days': self.days})

    async def reset(self):
        self.days = {}
        self.session = None
        await self.save()

    async def prepare(self):
        if self.engine.settings.mode != 'paper' or type(self.engine.broker) is not PaperBroker:
            raise RuntimeError('가상계좌 PAPER 주문만 지원합니다.')
        if not self.engine.toss_client:
            raise RuntimeError('자동매매에는 토스 시세 연결이 필요합니다.')
        await self.select_day()
        self.next_scan = datetime.min.replace(tzinfo=KST)
        self.message = '08:00~08:05 첫 신호 대기 · 하루 1종목 10분할'

    async def select_day(self):
        key = self.clock().date().isoformat()
        if key not in self.days:
            account = self.engine.broker.account(self.engine.quotes)
            budget = D(account['cash']['KRW'])
            self.days[key] = {'id': uuid4().hex, 'date': key, 'budget': str(budget), 'targets': {}, 'attempted': False, 'outcome': '대기', 'split_count': 10, 'pullback_step': '0.002',
                              'diagnostics': {'tracking_started_at': self.clock().isoformat(), 'legacy_untracked': False}}
            await self.save()
        self.session = self.days[key]
        if not self.session.get('diagnostics', {}).get('tracking_started_at'):
            self.session['diagnostics'] = {**self.session.get('diagnostics', {}),
                                           'tracking_started_at': self.clock().isoformat(),
                                           'legacy_untracked': True}
            if not self.session.get('attempted') and not self.session.get('targets'):
                self.session['outcome'] = '진입 없음'
            await self.save()
        if not self.session['attempted'] and 'split_count' not in self.session:
            self.session.update(budget=str(self.engine.broker.cash[Currency.KRW]), split_count=10, pullback_step='0.002')
            await self.save()

    def daily_orders(self, day):
        prefix = f"auto-{day['id']}-"
        return [o for o in self.engine.broker.orders if o.client_order_id.startswith(prefix) and o.status is OrderStatus.FILLED]

    async def quote(self, symbol):
        bars = await asyncio.wait_for(self.engine.toss_client.candles(symbol, '1m', 3), timeout=10)
        now = self.clock()
        bars = [b for b in bars if b.currency is Currency.KRW and b.volume > 0 and b.close_price > 0 and b.timestamp.astimezone(KST).date() == now.date() and timedelta(0) <= now - b.timestamp <= timedelta(seconds=120)]
        if not bars:
            raise RuntimeError(f'{symbol}: 당일 실제 거래 분봉 확인 실패')
        bar = max(bars, key=lambda b: b.timestamp)
        quote = Quote(symbol, bar.close_price, Currency.KRW, bar.timestamp, 'toss')
        self.engine.set_quote(quote)
        return quote

    def report(self):
        today = self.clock().date()
        rows = []
        for i in range(6, -1, -1):
            key = (today - timedelta(days=i)).isoformat()
            day = self.days.get(key)
            orders = self.daily_orders(day) if day else []
            buys = [o for o in orders if o.side is Side.BUY]
            sells = [o for o in orders if o.side is Side.SELL]
            cost = sum((o.filled_price * o.quantity + o.fee for o in buys), D(0))
            proceeds = sum((o.filled_price * o.quantity - o.fee for o in sells), D(0))
            closed = bool(buys) and sum(o.quantity for o in sells) == sum(o.quantity for o in buys)
            pnl = proceeds - cost if closed else None
            rows.append({'date': key, 'budget': day['budget'] if day else '0', 'cost': str(cost),
                         'profit': str(pnl) if pnl is not None else None,
                         'return_percent': str((pnl / cost * 100).quantize(D('.01'))) if closed and cost else None,
                         'status': '청산 완료' if closed else '미청산' if buys else day['outcome'] if day else '미수집'})
        closed_rows = [r for r in rows if r['profit'] is not None]
        cost = sum((D(r['cost']) for r in closed_rows), D(0))
        pnl = sum((D(r['profit']) for r in closed_rows), D(0))
        return {'days': rows, 'realized_profit': str(pnl), 'return_percent': str((pnl / cost * 100).quantize(D('.01'))) if cost else None,
                'note': '최근 7일 · 청산된 거래의 수수료 포함 손익 / 매수원가 · 미청산·미수집은 수익률 제외'}

    def status(self):
        result = super().status()
        result.update({'strategy': self.strategy, 'weekly': self.report(),
                       'diagnostics': self.diagnostics(),
                       'tranches_filled': len([o for o in self.orders() if o.side is Side.BUY]),
                       'split_count': self.session.get('split_count', 1) if self.session else 10})
        return result

    def diagnostics(self):
        day = self.session or {}
        data = day.get('diagnostics', {})
        return {'date': day.get('date'), 'outcome': day.get('outcome', '대기'),
                'legacy_untracked': data.get('legacy_untracked', False),
                'tracking_started_at': data.get('tracking_started_at'),
                'scan_count': data.get('scan_count', 0),
                'analyzed_count': data.get('analyzed_count', 0),
                'qualified_count': data.get('qualified_count', 0),
                'buy_order_attempts': data.get('buy_order_attempts', 0),
                'buy_orders_filled': data.get('buy_orders_filled', 0),
                'sell_order_attempts': data.get('sell_order_attempts', 0),
                'scan_errors': data.get('scan_errors', 0), 'last_error': data.get('last_error'),
                'last_scan_at': data.get('last_scan_at'), 'last_funnel': data.get('last_funnel', {}),
                'rejection_counts': data.get('rejection_counts', {}),
                'last_symbols': data.get('last_symbols', [])}

    async def record_scan(self, payload=None, error=None):
        data = self.session.setdefault('diagnostics', {})
        data['scan_count'] = data.get('scan_count', 0) + 1
        data['last_scan_at'] = self.clock().isoformat()
        if error:
            data['scan_errors'] = data.get('scan_errors', 0) + 1
            data['last_error'] = str(error)
        else:
            detail = payload.get('diagnostics', {})
            data['last_error'] = None
            data['analyzed_count'] = data.get('analyzed_count', 0) + int(detail.get('analyzed_count', 0))
            data['qualified_count'] = data.get('qualified_count', 0) + int(detail.get('qualified_count', 0))
            data['last_funnel'] = payload.get('funnel', {})
            data['last_symbols'] = detail.get('symbols', [])
            counts = data.setdefault('rejection_counts', {})
            for reason, count in detail.get('rejection_counts', {}).items():
                counts[reason] = counts.get(reason, 0) + int(count)
        await self.save()

    async def record_order_attempt(self, side, filled=None, day=None):
        day = day or self.session
        data = day.setdefault('diagnostics', {})
        key = 'buy_order_attempts' if side is Side.BUY else 'sell_order_attempts'
        data[key] = data.get(key, 0) + 1
        if side is Side.BUY and filled:
            data['buy_orders_filled'] = data.get('buy_orders_filled', 0) + 1
        await self.save()

    def allocation(self, day, index):
        buys = [o for o in self.daily_orders(day) if o.side is Side.BUY]
        spent = sum((o.filled_price * o.quantity + o.fee for o in buys), D(0))
        remaining = max(D(0), D(day['budget']) - spent)
        slots = day.get('split_count', 1)
        return min(remaining, D(day['budget']) / slots if index < slots else remaining,
                   self.engine.broker.cash[Currency.KRW])

    async def buy_tranche(self, day, quote, index):
        allowance = self.allocation(day, index)
        settings = self.engine.settings
        fill = quote.price * (1 + settings.slippage_bps / D(10000))
        quantity = int(allowance // (fill * (1 + settings.fee_rate)))
        if quantity < 1 or not self.active():
            return None
        target = day['targets'][quote.symbol]
        # Write intent first. A crash cannot silently create a second tranche.
        day['pending_tranche'] = index
        target['last_buy_quote_at'] = quote.timestamp.isoformat()
        target['next_pullback_at'] = (self.clock() + timedelta(minutes=20)).isoformat()
        await self.save()
        if not self.active() or self.clock().time() >= time(15, 10):
            return None
        await self.record_order_attempt(Side.BUY)
        order = await self.engine.broker.place_market_order(
            client_order_id=f"auto-{day['id']}-{quote.symbol}-buy-{index:02}",
            quote=quote, side=Side.BUY, quantity=D(quantity), order_budget=allowance)
        target['last_buy_quote_at'] = quote.timestamp.isoformat()
        day['pending_tranche'] = None
        if order.status is not OrderStatus.FILLED:
            day['split_halted'] = True
        else:
            data = day.setdefault('diagnostics', {})
            data['buy_orders_filled'] = data.get('buy_orders_filled', 0) + 1
        await self.save()
        self.message = f'{index}/10차 매수 완료' if order.status is OrderStatus.FILLED else '분할 매수 거절 · 추가 매수 중단'
        return order

    async def add_on_pullback(self, day, buys, quote):
        target = day['targets'][quote.symbol]
        now = self.clock()
        if (day.get('split_count', 1) != 10 or len(buys) >= 10 or day.get('split_halted')
                or day['date'] != now.date().isoformat() or now.time() >= time(15, 10)
                or target.get('trailing_active') or not self.active()):
            return
        if day.get('pending_tranche'):
            if len(buys) >= day['pending_tranche']:
                day['pending_tranche'] = None
                await self.save()
            else:
                self.message = '분할 주문 처리 중단 기록 확인 · 추가 매수 보류'
                return
        index = len(buys) + 1
        threshold = D(target['first_price']) * (1 - D(day['pullback_step']) * (index - 1))
        if not target.get('next_pullback_at'):
            target['next_pullback_at'] = (buys[-1].created_at + timedelta(minutes=20)).isoformat()
            await self.save()
        next_check = datetime.fromisoformat(target['next_pullback_at'])
        if now < next_check:
            self.message = f'{len(buys)}/10차 보유 · 다음 조정 확인 {next_check.astimezone(KST):%H:%M} · 기준 {threshold:,.2f}'
            return
        # A missed or failed signal waits another 20 minutes; no catch-up orders.
        target['next_pullback_at'] = (now + timedelta(minutes=20)).isoformat()
        await self.save()
        last_quote = datetime.fromisoformat(target['last_buy_quote_at'])
        if quote.timestamp <= last_quote or quote.price > threshold:
            self.message = f'{len(buys)}/10차 보유 · 다음 매수 기준 {threshold:,.2f}'
            return
        await self.buy_tranche(day, quote, index)

    async def exit_reason(self, day, buy, quote, now):
        target = day['targets'].setdefault(buy.symbol, {'symbol': buy.symbol})
        before = dict(target)
        entered = datetime.fromisoformat(target.get('entered_at', buy.created_at.isoformat()))
        peak = D(target.get('peak_price', str(buy.filled_price)))
        if quote.timestamp >= entered:
            peak = max(peak, quote.price)
        target['peak_price'] = str(peak)
        # Only prices observed after entry can arm the early-surge trailing stop.
        if (timedelta(0) <= now - entered <= timedelta(minutes=10)
                and quote.timestamp >= entered and quote.price >= buy.filled_price * D('1.02')):
            target['trailing_active'] = True
        trailing = target.get('trailing_active', False)
        target['trailing_stop'] = str(peak * D('.97')) if trailing else None
        if target != before:
            await self.save()
        sell_net = quote.price * (1 - self.engine.settings.slippage_bps / D(10000)) * (1 - self.engine.settings.fee_rate)
        buy_unit = buy.filled_price + buy.fee / buy.quantity
        if day['date'] < now.date().isoformat() or now.time() >= time(15, 10):
            return '시간 청산'
        if quote.price <= buy.filled_price * D('.97'):
            return '손절'
        if trailing:
            if quote.price <= peak * D('.97'):
                return '급등 후 고점 대비 3% 추적 매도'
            self.message = f"급등 추적 중 · 고점 {peak:,.2f} · 매도 기준 {peak * D('.97'):,.2f}"
            return None
        if sell_net >= buy_unit * D('1.025'):
            return '익절'
        return None

    async def tick(self):
        async with self._lock:
            if not self.active():
                return
            await self.select_day()
            now = self.clock()
            pending = False
            for day in self.days.values():
                orders = self.daily_orders(day)
                sold = {o.symbol for o in orders if o.side is Side.SELL}
                symbols = {o.symbol for o in orders if o.side is Side.BUY and o.symbol not in sold}
                for symbol in symbols:
                    buys = [o for o in orders if o.side is Side.BUY and o.symbol == symbol]
                    quantity = sum(o.quantity for o in buys)
                    buy = replace(buys[0], quantity=quantity,
                                  filled_price=sum(o.filled_price * o.quantity for o in buys) / quantity,
                                  fee=sum(o.fee for o in buys))
                    pending = True
                    try:
                        q = await self.quote(buy.symbol)
                        position = self.engine.broker.positions.get(buy.symbol)
                        if not position:
                            self.message = '미청산 주문과 보유 수량 불일치 · 신규 매수 차단'
                            continue
                        reason = await self.exit_reason(day, buy, q, self.clock())
                        if reason and self.active():
                            await self.record_order_attempt(Side.SELL, day=day)
                            order = await self.engine.broker.place_market_order(client_order_id=f"auto-{day['id']}-{buy.symbol}-sell", quote=q, side=Side.SELL, quantity=min(position.quantity, buy.quantity))
                            if order.status is OrderStatus.FILLED:
                                day['outcome'] = reason
                                await self.save()
                                self.message = f'{reason} 완료 · 당일 매매 종료'
                        elif not reason:
                            await self.add_on_pullback(day, buys, q)
                    except Exception as exc:
                        self.message = f'미청산 · 다음 유효 시세에서 청산 재시도: {exc}'
            if pending:
                return
            if self.session['attempted']:
                self.message = '당일 종목 진입 종료 · 재진입 없음'
                return
            if now.weekday() >= 5 or now.time() >= time(8, 5):
                if self.session['outcome'] == '대기':
                    self.session['outcome'] = '진입 없음'
                    await self.save()
                self.message = '오늘 진입 종료 · 다음 평일 08:00 대기'
                return
            if now.time() < time(8) or now < self.next_scan:
                return
            self.next_scan = now + timedelta(seconds=30)
            scan_recorded = False
            try:
                payload = await asyncio.wait_for(self.recommend(), timeout=40)
                await self.record_scan(payload=payload)
                scan_recorded = True
                for candidate in payload['candidates']:
                    symbol = candidate['symbol']
                    if not candidate.get('eligible') or symbol in self.engine.broker.positions:
                        continue
                    daily, bars = await asyncio.gather(self.engine.toss_client.candles(symbol, '1d', 25), self.engine.toss_client.candles(symbol, '1m', 5))
                    signal = morning_signal(daily, bars, self.clock())
                    if not signal or not signal['eligible']:
                        reasons = (signal or {}).get('rejection_reasons') or ['주문 전 재검사 미통과']
                        data = self.session.setdefault('diagnostics', {})
                        counts = data.setdefault('rejection_counts', {})
                        for reason in reasons:
                            label = f'주문 전: {reason}'
                            counts[label] = counts.get(label, 0) + 1
                        await self.save()
                        continue
                    q = await self.quote(symbol)
                    if abs(q.price / D(signal['price']) - 1) > D('.002'):
                        continue
                    settings = self.engine.settings
                    fill = q.price * (1 + settings.slippage_bps / D(10000))
                    quantity = int(self.allocation(self.session, 1) // (fill * (1 + settings.fee_rate)))
                    if quantity < 1 or not self.active() or not time(8) <= self.clock().time() < time(8, 5):
                        continue
                    self.session['attempted'] = True
                    self.session['outcome'] = '매수 시도'
                    self.session['targets'][symbol] = {'symbol': symbol, 'name': candidate.get('name') or symbol,
                                                        'entered_at': self.clock().isoformat(),
                                                        'first_price': str(fill),
                                                        'peak_price': str(fill), 'trailing_active': False}
                    await self.save()  # Durable daily guard, including rejected orders.
                    if not self.active() or not time(8) <= self.clock().time() < time(8, 5):
                        return
                    order = await self.buy_tranche(self.session, q, 1)
                    if order is None:
                        return
                    self.session['outcome'] = '매수 완료' if order.status is OrderStatus.FILLED else '매수 거절'
                    await self.save()
                    self.message = self.session['outcome']
                    return
                self.message = '당일 거래량·볼린저 하단 반등 신호 대기'
            except Exception as exc:
                if not scan_recorded:
                    await self.record_scan(error=exc)
                else:
                    data = self.session.setdefault('diagnostics', {})
                    data['last_error'] = str(exc)
                    await self.save()
                self.message = f'매수 보류: {exc}'
