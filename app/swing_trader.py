from datetime import datetime, timedelta, time, timezone
from decimal import Decimal as D
from uuid import uuid4
import asyncio

from app.morning_trader import MorningTrader, KST
from app.models import Currency, Side, OrderStatus
from app.paper import PaperBroker
from app.swing_signals import four_hour_exit_signal, swing_signal, trend_context
from app.swing_profit import estimate_exit_profit
from app.swing_averaging import SwingAveraging, TRAILING_KEYS
from app.swing_universe import load_universe, market_cap_label, membership


class SwingTrader(MorningTrader):
    """Multi-day holdings with configurable stops and one optional position addition."""
    strategy = 'swing-v2-mtf-4h'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.averaging = SwingAveraging(self)

    async def restore(self):
        if self.message == '가상 자동매매 시작 대기':
            self.message = f'{self.engine.settings.mode.upper()} 스윙 자동매매 시작 대기'
        saved = await self.engine.repository.load('swing_sessions')
        if saved is None:
            legacy = await self.engine.repository.load('morning_sessions') or {}
            self.days = legacy.get('days', {})
            old = await self.engine.repository.load('auto_session')
            if old and old.get('targets'):
                old.setdefault('date', '이전 자동매매')
                self.days['legacy-' + old['id']] = old
            await self.save()
        else:
            self.days = saved.get('days', {})
        await self.averaging.restore()
        await self.adopt_positions()
        await self.averaging.refresh_results()
        profit_policy_changed = False
        for day in self.days.values():
            for symbol, target in day.get('targets', {}).items():
                if self.outstanding(day, symbol) > 0:
                    profit_policy_changed |= self.reconcile_profit_target(target)
        if profit_policy_changed:
            await self.save()
        self.observation_count = await self.engine.repository.signal_observation_count()
        self.session = self.days.get(self.clock().date().isoformat())

    def reconcile_profit_target(self, target):
        """Discard a lower profit target's trailing state, keeping holding history."""
        if not any(key in target for key in TRAILING_KEYS):
            return False
        minimum = self.engine.settings.swing_min_net_profit_percent
        try:
            # Snapshots predating activation metadata used the original 3% target.
            previous = D(target.get('profit_trailing_min_percent', '3'))
            valid = previous.is_finite() and D(0) <= previous <= D(100)
        except (ArithmeticError, ValueError, TypeError):
            valid = False
        if valid and previous >= minimum:
            return False
        for key in TRAILING_KEYS:
            target.pop(key, None)
        target.update(profit_trailing_armed=False, profit_trailing_min_percent=str(minimum))
        return True

    def outstanding(self, day, symbol):
        carried = D(day.get('adopted', {}).get(symbol, {}).get('quantity', '0'))
        return carried + sum((o.quantity if o.side is Side.BUY else -o.quantity
                              for o in self.daily_orders(day) if o.symbol == symbol), D(0))

    async def adopt_positions(self):
        """Track existing domestic holdings without creating fictional buy orders."""
        adopted = {}
        for symbol, position in self.engine.broker.positions.items():
            if position.currency is not Currency.KRW:
                continue
            tracked = sum((max(D(0), self.outstanding(day, symbol)) for day in self.days.values()), D(0))
            quantity = position.quantity - tracked
            if quantity > 0:
                adopted[symbol] = {'quantity': str(quantity), 'cost': str(position.average_price * quantity)}
        if adopted:
            key = 'adopted-' + uuid4().hex
            self.days[key] = {'id': uuid4().hex, 'date': self.clock().date().isoformat(),
                              'budget': '0', 'adopted': adopted, 'outcome': '기존 보유 승계',
                              'targets': {symbol: {'symbol': symbol, 'name': symbol, 'adopted': True} for symbol in adopted}}
            await self.save()

    async def save(self):
        await self.engine.repository.save('swing_sessions', {'days': self.days})

    async def select_day(self):
        key = self.clock().date().isoformat()
        if key not in self.days:
            self.days[key] = {'id': uuid4().hex, 'date': key, 'budget': str(self.capital()[0]),
                              'targets': {}, 'outcome': '스윙 신호 대기',
                              'diagnostics': {'tracking_started_at': self.clock().isoformat()}}
        self.session = self.days[key]
        if self.session.get('strategy') != self.strategy:
            self.session['strategy'] = self.strategy
            self.session['previous_diagnostics'] = self.session.get('diagnostics', {})
            self.session['diagnostics'] = {'tracking_started_at': self.clock().isoformat(), 'legacy_untracked': False}
            self.session['outcome'] = '스윙 조건 검색 대기'
        await self.save()

    def capital(self):
        account = self.engine.broker.account(self.engine.quotes)
        budget = max(D(0), D(account['total_equity']['KRW']) * self.engine.settings.recommended_trade_ratio)
        if self.engine.settings.mode == 'live':
            broker = self.engine.broker
            aggregate_budget = D(account['total_equity']['KRW']) * self.engine.settings.live_auto_exposure_ratio_limit
            budget = (aggregate_budget if self.engine.settings.live_auto_allocation_mode == 'per_order'
                      else min(budget, aggregate_budget))
            if self.engine.settings.live_max_total_exposure_krw > 0:
                budget = min(budget, self.engine.settings.live_max_total_exposure_krw)
            invested = sum(broker.position_market_values.values(), D(0))
            pending = broker._pending_buy_exposure()
            if pending is not None and self.engine.settings.live_auto_allocation_mode == 'per_order':
                pending *= 1 + self.engine.settings.fee_rate
            remaining = max(D(0), min(D(account['cash']['KRW']), budget - invested - (pending or D(0)) - D(1)))
            if pending is None or not broker.reconciled or len(broker.positions) >= 5:
                remaining = D(0)
            return budget, invested, remaining
        invested = sum((p.quantity * p.average_price for p in self.engine.broker.positions.values() if p.currency is Currency.KRW), D(0))
        remaining = max(D(0), min(D(account['cash']['KRW']), budget - invested))
        if len(self.engine.broker.positions) >= 5:
            remaining = D(0)
        return budget, invested, remaining

    def buy_budget(self, symbol=None):
        """Size one LIVE buy, reserving held symbols and pending buy slots."""
        budget, _, remaining = self.capital()
        if self.engine.settings.mode != 'live':
            return remaining
        broker = self.engine.broker
        reserved = set(broker.positions) | {
            str(order.get('symbol', '')).upper() for order in broker.open_orders
            if str(order.get('side', '')).upper() == 'BUY'
        }
        if len(reserved) >= 5 or (symbol and symbol.upper() in reserved):
            return D(0)
        if self.engine.settings.live_auto_allocation_mode == 'per_order':
            account = broker.account(self.engine.quotes)
            per_order = D(account['total_equity']['KRW']) * min(
                self.engine.settings.recommended_trade_ratio, self.engine.settings.live_auto_ratio_limit)
            if self.engine.settings.live_auto_budget_split == 'remaining_slots':
                per_order = min(per_order, remaining / D(5 - len(reserved)))
            return max(D(0), min(remaining, per_order))
        return max(D(0), min(remaining, budget / D(5)))

    def manual_buy_budget(self, symbol=None, ratio=None):
        """Size a direct buy independently, within aggregate exposure and cash."""
        settings, broker = self.engine.settings, self.engine.broker
        ratio = settings.manual_trade_ratio if ratio is None else D(str(ratio))
        maximum = settings.live_manual_ratio_limit if settings.mode == 'live' else D(1)
        if not ratio.is_finite() or not D('.01') <= ratio <= maximum:
            raise RuntimeError(f'직접 매수 비율은 1%부터 {maximum * 100:g}% 사이여야 합니다.')
        account = broker.account(self.engine.quotes)
        equity, cash = D(account['total_equity']['KRW']), D(account['cash']['KRW'])
        budget = max(D(0), equity * ratio)
        if settings.mode != 'live':
            return min(cash, budget)
        reserved = set(broker.positions) | {
            str(order.get('symbol', '')).upper() for order in broker.open_orders
            if str(order.get('side', '')).upper() == 'BUY'
        }
        pending = broker._pending_buy_exposure()
        if (pending is None or not broker.reconciled or len(reserved) >= 5
                or (symbol and symbol.upper() in reserved)):
            return D(0)
        if settings.live_auto_allocation_mode == 'per_order':
            pending *= 1 + settings.fee_rate
        exposure_limit = equity * settings.live_exposure_ratio_limit
        if settings.live_max_total_exposure_krw > 0:
            exposure_limit = min(exposure_limit, settings.live_max_total_exposure_krw)
        invested = sum(broker.position_market_values.values(), D(0))
        return max(D(0), min(budget, cash - pending,
                             exposure_limit - invested - pending - D(1)))

    async def prepare(self):
        paper_ready = self.engine.settings.mode == 'paper' and type(self.engine.broker) is PaperBroker
        live_ready = (self.engine.settings.mode == 'live' and self.engine.settings.live_trading_enabled
                      and getattr(self.engine.broker, 'risk', None) and self.engine.broker.risk.armed
                      and self.engine.broker.reconciled)
        if not (paper_ready or live_ready):
            raise RuntimeError('Trading mode is disabled or not armed.')
        if not self.engine.toss_client:
            raise RuntimeError('스윙 자동매매에는 토스 시세 연결이 필요합니다.')
        await self.select_day()
        self.next_scan = datetime.min.replace(tzinfo=KST)
        self.message = '스윙투자 · 주봉·일봉 추세 확인 / 4시간봉 눌림목 매수 / 일봉 상단 매도'

    def status(self):
        budget, invested, remaining = self.capital()
        manual_maximum = (self.engine.settings.live_manual_ratio_limit
                          if self.engine.settings.mode == 'live' else D(1))
        manual_capacity = self.manual_buy_budget(ratio=manual_maximum) if manual_maximum >= D('.01') else D(0)
        if self.engine.kill_switch:
            execution_state = '킬 스위치 활성 · 자동 주문 중지'
        elif not self.engine.running:
            execution_state = '자동매매 중지 · 시작 버튼을 눌러야 검색합니다'
        elif self.engine.settings.mode == 'live' and not self.active():
            execution_state = 'LIVE 주문 잠금 · 계좌 동기화·무장 및 미확인 주문을 확인하세요'
        elif not self.market_open():
            execution_state = '장외 대기 · 평일 정규장 및 16:00~20:00 애프터마켓에 검색합니다'
        elif not self.engine.settings.swing_buy_window_limit_enabled:
            execution_state = '매수·매도 감시 중 · 정규장·애프터마켓 전체에서 매수 조건 확인'
        else:
            execution_state = '매도 감시 중 · 자동매수 평일 09:00~10:00, 12:00~14:00, 16:00~20:00'
        automatic_hours = ('평일 09:00~10:00, 12:00~14:00, 16:00~20:00'
                           if self.engine.settings.swing_buy_window_limit_enabled
                           else '평일 09:00~15:30, 16:00~20:00 · 별도 자동매수 시간 제한 해제')
        return {'strategy': self.strategy, 'budget': str(budget), 'spent': str(invested),
                'remaining': str(remaining), 'message': self.message,
                'per_symbol_budget': str(self.buy_budget()),
                'manual_buy_capacity': str(manual_capacity),
                'allocation_slots': 5 if self.engine.settings.mode == 'live' else None,
                'allocation_mode': self.engine.settings.live_auto_allocation_mode if self.engine.settings.mode == 'live' else None,
                'budget_split': self.engine.settings.live_auto_budget_split if self.engine.settings.mode == 'live' else None,
                'remaining_allocation_slots': max(0, 5 - len(set(self.engine.broker.positions) | {
                    str(order.get('symbol', '')).upper() for order in getattr(self.engine.broker, 'open_orders', [])
                    if str(order.get('side', '')).upper() == 'BUY'})),
                'buy_block_reasons': self.buy_block_reasons(budget, invested, remaining),
                'averaging': self.averaging.status(),
                'execution_state': execution_state, 'market_open': self.market_open(),
                'buy_window_limit_enabled': self.engine.settings.swing_buy_window_limit_enabled,
                'buy_window_open': self.buy_window_open(),
                'trading_hours': f'자동매수 {automatic_hours} · {self.engine.settings.mode.upper()} 수동매수·매도 평일 09:00~15:30, 16:00~20:00',
                'signal_observation_count': getattr(self, 'observation_count', 0),
                'next_scan_at': max(self.clock(), self.next_scan).isoformat() if self.active() and self.market_open() else None,
                'targets': [target for day in self.days.values() for target in day['targets'].values()
                            if target['symbol'] in self.engine.broker.positions],
                'diagnostics': self.diagnostics(), 'weekly': self.report(),
                'holding_count': len(self.engine.broker.positions), 'max_positions': 5}

    def buy_block_reasons(self, budget, invested, remaining):
        reasons = []
        if not self.engine.running:
            reasons.append('자동매매 엔진 중지')
        if self.engine.kill_switch:
            reasons.append('긴급 중지 활성')
        if not self.market_open():
            reasons.append('거래시간 외')
        elif not self.buy_window_open():
            reasons.append('자동매수 시간 외')
        broker = self.engine.broker
        if self.engine.settings.mode == 'live':
            if not broker.reconciled:
                reasons.append('실계좌 동기화 필요')
            if not broker.risk.armed:
                reasons.append('LIVE 무장 해제')
            if not self.engine.settings.live_trading_enabled:
                reasons.append('LIVE 주문 비활성화')
            pending = broker._pending_buy_exposure()
            if pending is None:
                reasons.append('미체결 매수 금액 확인 필요')
            elif remaining <= 0:
                reserved_cost = pending * (1 + self.engine.settings.fee_rate) if self.engine.settings.live_auto_allocation_mode == 'per_order' else pending
                if invested + reserved_cost + D(1) >= budget:
                    reasons.append('총자산 대비 현금 유지 기준으로 신규 매수 예산 없음'
                                   if self.engine.settings.live_min_cash_ratio > 0
                                   else '보유 평가액·미체결 매수가 합산 투자 한도에 도달')
                elif broker.cash[Currency.KRW] <= 0:
                    reasons.append('가용 현금 부족')
            reserved = set(broker.positions) | {
                str(order.get('symbol', '')).upper() for order in broker.open_orders
                if str(order.get('side', '')).upper() == 'BUY'}
            if len(reserved) >= 5:
                reasons.append('보유·미체결 매수 합계 최대 5종목 도달')
            if broker.risk.daily_loss_limit_reached:
                reasons.append('일일 손실 한도 도달')
        elif remaining <= 0:
            reasons.append('가용 매수 예산 없음')
        return reasons

    def report(self):
        rows = []
        for day in sorted(self.days.values(), key=lambda d: d['date']):
            orders = self.daily_orders(day)
            for symbol in sorted({o.symbol for o in orders if o.side is Side.BUY} | set(day.get('adopted', {}))):
                buys = [o for o in orders if o.symbol == symbol and o.side is Side.BUY]
                sells = [o for o in orders if o.symbol == symbol and o.side is Side.SELL]
                carried = day.get('adopted', {}).get(symbol, {})
                cost = D(carried.get('cost', '0')) + sum((o.filled_price * o.quantity + o.fee for o in buys), D(0))
                closed = self.outstanding(day, symbol) == 0
                pnl = sum((o.filled_price * o.quantity - o.fee for o in sells), D(0)) - cost if closed else None
                name = day['targets'].get(symbol, {}).get('name', symbol)
                rows.append({'date': day['date'] + (' (승계)' if carried else ''), 'symbol': symbol, 'name': name, 'cost': str(cost),
                             'profit': str(pnl) if closed else None,
                             'return_percent': str((pnl / cost * 100).quantize(D('.01'))) if closed and cost else None,
                             'status': '청산 완료' if closed else '기존 보유 승계' if carried else '보유 중'})
        closed = [r for r in rows if r['profit'] is not None]
        cost = sum((D(r['cost']) for r in closed), D(0))
        profit = sum((D(r['profit']) for r in closed), D(0))
        return {'days': rows, 'realized_profit': str(profit),
                'return_percent': str((profit / cost * 100).quantize(D('.01'))) if cost else None,
                'note': '진입일·종목별 누적 성과 · 청산 완료 거래의 수수료 포함 손익 · 보유 중은 수익률 제외'}

    def market_open(self):
        now = self.clock().astimezone(KST)
        current = now.time()
        regular_session = time(9) <= current < time(15, 30)
        after_market = time(16) <= current < time(20)
        return now.weekday() < 5 and (regular_session or after_market)

    def buy_window_open(self):
        if not self.engine.settings.swing_buy_window_limit_enabled:
            return self.market_open()
        now = self.clock().astimezone(KST)
        current = now.time()
        regular_buy_window = time(9) <= current < time(10) or time(12) <= current < time(14)
        after_market_buy_window = time(16) <= current < time(20)
        return now.weekday() < 5 and (regular_buy_window or after_market_buy_window)

    async def buy_qualified(self, symbol, ratio=None):
        """Recheck a selected candidate under the current broker's risk limits."""
        async with self._lock:
            if self.engine.settings.mode == 'live':
                return await self._buy_live_qualified(symbol, ratio)
            if self.engine.settings.mode != 'paper' or type(self.engine.broker) is not PaperBroker:
                raise RuntimeError('PAPER 주문만 지원합니다.')
            if self.engine.kill_switch:
                raise RuntimeError('킬 스위치가 활성화되어 있습니다.')
            if not self.market_open():
                raise RuntimeError('매수는 평일 정규장 09:00~15:30 또는 애프터마켓 16:00~20:00에 가능합니다.')
            if not self.engine.toss_client:
                raise RuntimeError('토스 시세 연결이 필요합니다.')
            already_held = symbol in self.engine.broker.positions
            if len(self.engine.broker.positions) >= 5 and not already_held:
                raise RuntimeError('최대 보유 종목 수에 도달했습니다.')
            await self.select_day()
            minimum, themes = load_universe()
            if symbol not in themes:
                raise RuntimeError('등록 테마에 없는 종목입니다.')
            quote = await self.quote(symbol)
            stock = await asyncio.wait_for(self.engine.toss_client.stock_info(symbol), timeout=10)
            if not membership(stock, quote.price, minimum, themes):
                raise RuntimeError(f'시가총액 {market_cap_label(minimum)} 이상 국내 보통주 조건을 충족하지 않습니다.')
            signal = await self.signal(symbol, quote)
            if not signal.get('eligible'):
                raise RuntimeError('현재 자동매수 조건을 충족하지 않습니다.')
            budget = self.manual_buy_budget(symbol, ratio)
            # A manual click is an explicit repeat entry. Keep the allocation,
            # cash, and per-order limits while allowing an existing symbol to add shares.
            allowance = min(D(self.engine.broker.cash[Currency.KRW]), budget)
            settings = self.engine.settings
            fill = quote.price * (1 + settings.slippage_bps / D(10000))
            quantity = min(int(allowance // (fill * (1 + settings.fee_rate))),
                           int(settings.max_order_amount_krw // fill))
            if quantity < 1:
                raise RuntimeError('자산 비율 예산 또는 가용 현금으로 1주를 매수할 수 없습니다.')
            target = self.session['targets'].setdefault(symbol, {'symbol': symbol})
            target.update({'name': stock.get('name') or symbol, 'themes': themes[symbol],
                           'entered_at': self.clock().isoformat(), 'manual_qualified_buy': True})
            await self.save()
            await self.record_order_attempt(Side.BUY)
            order = await self.engine.broker.place_market_order(
                client_order_id=f"auto-{self.session['id']}-{symbol}-swing-manual-buy-{uuid4().hex}",
                quote=quote, side=Side.BUY, quantity=D(quantity), order_budget=min(budget, allowance))
            if order.status is not OrderStatus.FILLED:
                raise RuntimeError(f'PAPER 매수 거절: {order.reason}')
            data = self.session.setdefault('diagnostics', {})
            data['buy_orders_filled'] = data.get('buy_orders_filled', 0) + 1
            self.session['outcome'] = '스윙 보유 · 상단 매도 대기'
            self.message = f'{stock.get("name") or symbol} {quantity}주 자산 비율 PAPER 매수'
            await self.save()
            return order

    async def _reconcile_live_manual(self):
        broker = self.engine.broker
        if self.engine.kill_switch:
            raise RuntimeError('킬 스위치가 활성화되어 있습니다.')
        if not (self.engine.settings.live_trading_enabled and broker.risk.armed and broker.reconciled):
            raise RuntimeError('LIVE 계좌 동기화와 무장이 필요합니다.')
        if not self.market_open():
            raise RuntimeError('현재는 국내 주식 주문 시간이 아닙니다.')
        snapshot = await broker.reconcile()
        if not snapshot.get('reconciled') or not broker.risk.armed:
            raise RuntimeError('LIVE 계좌 재동기화에 실패하여 주문을 보류했습니다.')

    async def quote(self, symbol):
        if self.engine.settings.mode != 'live':
            return await super().quote(symbol)
        quotes = await self.engine.broker._fresh_risk_quotes([symbol])
        quote = next((q for q in quotes if q.symbol == symbol and q.currency is Currency.KRW
                      and q.source == 'toss' and q.price > 0 and q.bid_price and q.ask_price), None)
        if not quote:
            raise RuntimeError('LIVE 주문에는 최신 매수·매도 호가가 필요합니다.')
        age = (datetime.now(timezone.utc) - quote.timestamp).total_seconds()
        if age > 10 or age < -2 or quote.bid_price > quote.ask_price:
            raise RuntimeError('LIVE 호가가 오래되었거나 유효하지 않습니다.')
        self.engine.set_quote(quote)
        return quote

    async def _buy_live_qualified(self, symbol, ratio=None):
        # Reject invalid ratios before any remote account or analysis reads.
        self.manual_buy_budget(symbol, ratio)
        await self._reconcile_live_manual()
        broker = self.engine.broker
        if (self.engine.settings.live_symbol_policy == 'allowlist'
                and symbol not in self.engine.settings.live_allowed_symbols):
            raise RuntimeError('실거래 허용 종목 목록에 없는 종목입니다.')
        payload = await asyncio.wait_for(self.recommend(), timeout=75)
        candidates = [c for c in payload.get('candidates', [])
                      if c.get('eligible') and c.get('currency', 'KRW') == 'KRW']
        candidate = next((c for c in candidates if c['symbol'] == symbol), None)
        if not candidate:
            raise RuntimeError('최신 추천에서 매수 조건을 통과하지 못한 종목입니다.')
        broker.risk.set_recommended_symbols(c['symbol'] for c in candidates)
        minimum, themes = load_universe()
        stock = await asyncio.wait_for(self.engine.toss_client.stock_info(symbol), timeout=10)
        daily = await asyncio.wait_for(self.engine.toss_client.candles(symbol, '1d', 100), timeout=10)
        weekly = await asyncio.wait_for(self.engine.candles(symbol, '1w', 60), timeout=20)
        four_hour = await asyncio.wait_for(self.engine.candles(symbol, '4h', 10), timeout=40)
        # Slow analysis reads precede the final account and order-book snapshots.
        await self._reconcile_live_manual()
        quote = await self.quote(symbol)
        if not membership(stock, quote.price, minimum, themes) or not swing_signal(
                daily, quote.ask_price, self.clock(), four_hour, weekly).get('eligible'):
            raise RuntimeError('주문 직전 매수 조건을 충족하지 않습니다.')
        allowance = self.manual_buy_budget(symbol, ratio)
        price = quote.ask_price
        unit_cost = price * (1 + self.engine.settings.fee_rate)
        quantity = int(allowance // unit_cost)
        if self.engine.settings.live_max_order_amount_krw > 0:
            quantity = min(quantity, int(self.engine.settings.live_max_order_amount_krw // price))
        if quantity < 1:
            raise RuntimeError('직접 매수 비율·총자산 한도·보유 종목·미체결 매수·가용 현금을 적용하면 매수 예산이 부족합니다.')
        await self.select_day()
        if self.session['targets'].get(symbol, {}).get('manual_exit'):
            raise RuntimeError('오늘 수동 청산한 종목은 재매수하지 않습니다.')
        target = self.session['targets'].setdefault(symbol, {'symbol': symbol})
        target.update({'name': stock.get('name') or symbol, 'themes': themes[symbol],
                       'entered_at': self.clock().isoformat(), 'manual_qualified_buy': True})
        await self.save()
        if self.engine.kill_switch or not self.market_open():
            raise RuntimeError('LIVE 매수 중단: 킬 스위치 또는 거래시간을 확인하세요.')
        await self.record_order_attempt(Side.BUY)
        order = await broker.place_market_order(
            client_order_id=f"auto-{self.session['id']}-{symbol}-swing-manual-buy-{uuid4().hex}",
            quote=quote, side=Side.BUY, quantity=D(quantity), order_budget=allowance)
        self.message = f'{symbol} LIVE 매수 결과 {order.status.value} · 체결 {order.quantity}주'
        self.session['outcome'] = self.message
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            data = self.session.setdefault('diagnostics', {})
            data['buy_orders_filled'] = data.get('buy_orders_filled', 0) + 1
        await self.save()
        return order

    async def signal(self, symbol, quote):
        daily = await asyncio.wait_for(self.engine.toss_client.candles(symbol, '1d', 100), timeout=10)
        weekly = await asyncio.wait_for(self.engine.candles(symbol, '1w', 60), timeout=20)
        entry_price = quote.ask_price if self.engine.settings.mode == 'live' else quote.price
        context = trend_context(daily, entry_price, self.clock(), weekly)
        four_hour = []
        if context.get('context_eligible'):
            four_hour = await asyncio.wait_for(self.engine.candles(symbol, '4h', 10), timeout=40)
        return swing_signal(daily, entry_price, self.clock(), four_hour, weekly)

    async def exit_signal(self, symbol, quote, position, day):
        settings = self.engine.settings
        sell_price = quote.bid_price if settings.mode == 'live' else quote.price
        price_valid = sell_price is not None and sell_price.is_finite() and sell_price > 0
        target = day.setdefault('targets', {}).setdefault(symbol, {'symbol': symbol, 'name': symbol})
        if self.reconcile_profit_target(target):
            # Persist the change before candle I/O, which may fail.
            await self.save()
        target.setdefault('profit_trailing_min_percent', str(settings.swing_min_net_profit_percent))
        stop_percent = settings.live_max_position_loss_percent if settings.mode == 'live' else D('3')
        stop_loss = bool(settings.swing_stop_loss_enabled and price_valid and position.average_price.is_finite() and position.average_price > 0
                         and sell_price <= position.average_price * (D(1) - stop_percent / D(100)))
        try:
            four_hour = await asyncio.wait_for(self.engine.candles(symbol, '4h', 10), timeout=40)
            signal = four_hour_exit_signal(four_hour, sell_price, self.clock()) if price_valid else {}
        except Exception:
            # Fresh executable quotes can still protect an existing holding
            # when the candle service is unavailable.
            if not stop_loss and target.get('profit_trailing_armed') is not True:
                raise
            signal = {'ready': False, 'upper_touched': False, 'sell_at_upper': False}
        try:
            peak = D(target.get('exit_peak', '0'))
            if not peak.is_finite() or peak < 0:
                peak = D(0)
        except (ArithmeticError, ValueError, TypeError):
            peak = D(0)
        if signal.get('upper_touched'):
            target['upper_band_touched'] = True
            peak = max(peak, D(signal.get('active_high', quote.price)), quote.price)
            target['exit_peak'] = str(peak)
        elif target.get('upper_band_touched') and price_valid:
            peak = max(peak, quote.price)
            target['exit_peak'] = str(peak)
        profit = estimate_exit_profit(position, sell_price, settings)
        # The current bid/last spread is also reserved at the projected trigger.
        spread = max(D(0), quote.price - sell_price) if price_valid else D(0)
        trigger = max(D(0), peak * D('.98') - spread)
        trigger_profit = estimate_exit_profit(position, trigger, settings)
        # Observe the price above the trigger before arming; a historical candle
        # high cannot retroactively activate protection after the price has fallen.
        if (signal.get('ready') and price_valid and sell_price > trigger
                and target.get('upper_band_touched') and peak > 0
                and trigger_profit['meets_minimum'] and target.get('profit_trailing_armed') is not True):
            target.update({'profit_trailing_armed': True, 'profit_trailing_armed_at': self.clock().isoformat(),
                           'profit_trailing_min_percent': str(settings.swing_min_net_profit_percent)})
        armed = target.get('profit_trailing_armed') is True
        trailing_exit = bool(price_valid and armed and peak > 0 and sell_price <= trigger)
        take_profit = bool(signal.get('sell_at_upper') and profit['meets_minimum'])
        signal.update({'stop_loss': stop_loss, 'stop_loss_enabled': settings.swing_stop_loss_enabled, 'trailing_exit': trailing_exit,
                       'take_profit': take_profit, 'profit_trailing_armed': armed,
                       'min_net_profit_percent': str(settings.swing_min_net_profit_percent),
                       'estimated_net_return_percent': str(profit['net_return_percent']) if profit['ready'] else None,
                       'minimum_sell_reference_price': str(profit['minimum_reference_price']) if profit['ready'] else None,
                       'trailing_trigger_price': str(trigger),
                       'estimated_trigger_net_return_percent': str(trigger_profit['net_return_percent']) if trigger_profit['ready'] else None,
                       'sell': stop_loss or take_profit or trailing_exit,
                       'exit_reason': (f'평균 매입가 대비 -{stop_percent:g}% 손절' if stop_loss else
                                       f'4시간봉 볼린저 상단 · 예상 순수익 {settings.swing_min_net_profit_percent:g}% 이상 익절' if take_profit else
                                       '수익 기준 활성화 후 최고가 대비 -2% 보호 매도' if trailing_exit else None)})
        target['last_exit_check'] = {'checked_at': self.clock().isoformat(), 'quote_timestamp': quote.timestamp.isoformat(),
                                    'sell_reference_price': str(sell_price) if price_valid else None,
                                    **{key: signal.get(key) for key in ('bollinger_upper', 'stop_loss', 'take_profit',
                                       'profit_trailing_armed', 'estimated_net_return_percent', 'min_net_profit_percent',
                                       'trailing_trigger_price', 'estimated_trigger_net_return_percent', 'exit_reason')}}
        await self.save()
        return signal

    async def store_observations(self, payload, source):
        observed_at = self.clock().isoformat()
        scan_id = uuid4().hex
        rows = [{**item, 'scan_id': scan_id, 'observed_at': observed_at, 'source': source}
                for item in payload.get('diagnostics', {}).get('symbols', [])]
        inserted = await self.engine.repository.append_signal_observations(rows)
        self.observation_count = getattr(self, 'observation_count', 0) + inserted
        return inserted

    async def record_scan(self, payload=None, error=None):
        await super().record_scan(payload=payload, error=error)
        if payload and not error:
            await self.store_observations(payload, 'automatic')

    async def test_buy(self, symbol):
        """Buy one PAPER share from a current 2/3 near-signal for execution testing."""
        async with self._lock:
            if self.engine.settings.mode != 'paper' or type(self.engine.broker) is not PaperBroker:
                raise RuntimeError('PAPER 테스트 주문만 지원합니다.')
            if self.engine.kill_switch:
                raise RuntimeError('킬 스위치를 해제한 뒤 테스트하세요.')
            if not self.market_open():
                raise RuntimeError('테스트 매수는 평일 정규장 09:00~15:30 또는 애프터마켓 16:00~20:00에 가능합니다.')
            if not self.engine.toss_client:
                raise RuntimeError('테스트 매수에는 토스 시세 연결이 필요합니다.')
            if symbol in self.engine.broker.positions:
                raise RuntimeError('이미 보유 중인 종목입니다.')
            if len(self.engine.broker.positions) >= 5:
                raise RuntimeError('최대 보유 종목 수에 도달했습니다.')
            await self.select_day()
            if symbol in self.session['targets']:
                raise RuntimeError('오늘 이미 주문했거나 수동 청산한 종목입니다.')
            minimum, themes = load_universe()
            if symbol not in themes:
                raise RuntimeError('등록 테마에 없는 종목입니다.')
            quote = await self.quote(symbol)
            stock = await asyncio.wait_for(self.engine.toss_client.stock_info(symbol), timeout=10)
            if not membership(stock, quote.price, minimum, themes):
                raise RuntimeError(f'시가총액 {market_cap_label(minimum)} 이상 국내 보통주 조건을 충족하지 않습니다.')
            signal = await self.signal(symbol, quote)
            if signal.get('conditions_passed', 0) < 2:
                raise RuntimeError('핵심 조건 3개 중 2개 이상 통과한 관찰 후보만 테스트할 수 있습니다.')
            settings = self.engine.settings
            estimated = quote.price * (1 + settings.slippage_bps / D(10000)) * (1 + settings.fee_rate)
            if estimated > self.engine.broker.cash[Currency.KRW] or quote.price > settings.max_order_amount_krw:
                raise RuntimeError('1주 테스트 주문에 사용할 가상 현금 또는 주문 한도가 부족합니다.')
            self.session['targets'][symbol] = {
                'symbol': symbol, 'name': stock.get('name') or symbol, 'themes': themes[symbol],
                'entered_at': self.clock().isoformat(), 'test_entry': True,
                'test_conditions_passed': signal['conditions_passed'],
            }
            await self.save()
            await self.record_order_attempt(Side.BUY)
            order = await self.engine.broker.place_market_order(
                client_order_id=f"auto-{self.session['id']}-{symbol}-paper-test-buy",
                quote=quote, side=Side.BUY, quantity=D(1))
            if order.status is not OrderStatus.FILLED:
                raise RuntimeError(f'테스트 매수 거절: {order.reason}')
            data = self.session.setdefault('diagnostics', {})
            data['buy_orders_filled'] = data.get('buy_orders_filled', 0) + 1
            data['test_buy_orders_filled'] = data.get('test_buy_orders_filled', 0) + 1
            self.session['outcome'] = 'PAPER 테스트 1주 보유'
            self.message = f'{stock.get("name") or symbol} 1주 PAPER 테스트 매수 완료'
            await self.save()
            return order

    async def manual_close(self, symbol):
        """Serialize with automatic orders and attribute exits to their entry records."""
        async with self._lock:
            is_live = self.engine.settings.mode == 'live'
            if is_live:
                await self._reconcile_live_manual()
            elif self.engine.settings.mode != 'paper' or type(self.engine.broker) is not PaperBroker:
                raise RuntimeError('가상계좌 PAPER 주문만 지원합니다.')
            if self.engine.kill_switch:
                raise RuntimeError('킬 스위치를 해제한 뒤 매도하세요.')
            if not self.market_open():
                raise RuntimeError('수동 매도는 평일 정규장 09:00~15:30 또는 애프터마켓 16:00~20:00에 가능합니다.')
            position = self.engine.broker.positions.get(symbol)
            if not position:
                raise RuntimeError('이미 청산되었거나 보유하지 않은 종목입니다.')
            if position.currency is not Currency.KRW:
                raise RuntimeError('현재 수동 매도는 국내 원화 종목만 지원합니다.')
            if not self.engine.toss_client:
                raise RuntimeError('매도에는 토스 시세 연결이 필요합니다.')
            quote = await self.quote(symbol)
            await self.adopt_positions()
            entries = [(day, self.outstanding(day, symbol)) for day in self.days.values()
                       if self.outstanding(day, symbol) > 0]
            if sum((quantity for _, quantity in entries), D(0)) != position.quantity:
                raise RuntimeError('보유 수량과 주문 기록이 달라 매도를 보류했습니다.')
            await self.select_day()
            # A manual exit must not be immediately bought back by the scanner,
            # including after a restart on the same day.
            self.session['targets'].setdefault(symbol, {'symbol': symbol, 'name': symbol})['manual_exit'] = True
            await self.save()
            orders = []
            for day, quantity in entries:
                if self.engine.kill_switch or not self.market_open():
                    raise RuntimeError('매도 중단: 킬 스위치 또는 거래시간을 확인하세요.')
                await self.record_order_attempt(Side.SELL, day=day)
                if is_live:
                    quote = await self.quote(symbol)
                order = await self.engine.broker.place_market_order(
                    client_order_id=f"auto-{day['id']}-{symbol}-manual-{uuid4().hex}",
                    quote=quote, side=Side.SELL, quantity=quantity)
                if order.status is not OrderStatus.FILLED and not is_live:
                    raise RuntimeError(f'매도 거절: {order.reason}')
                orders.append(order)
                day['outcome'] = '수동 전량 매도' if order.status is OrderStatus.FILLED else f'LIVE 매도 결과 {order.status.value} · 체결 {order.quantity}주'
                await self.save()
                if order.status is not OrderStatus.FILLED:
                    self.message = day['outcome']
                    return orders
            self.message = f'{symbol} 수동 전량 매도 완료 · 당일 재매수 제외'
            return orders

    async def tick(self):
        async with self._lock:
            if not self.active() or not self.market_open():
                return
            now = self.clock()
            if now < self.next_scan:
                return
            self.next_scan = now + timedelta(minutes=5)
            if self.engine.settings.mode == 'live':
                try:
                    snapshot = await self.engine.broker.reconcile()
                    if not snapshot.get('reconciled'):
                        self.message = 'LIVE 계좌 동기화 미완료 · 주문을 보류했습니다.'
                        return
                except Exception as exc:
                    self.message = f'LIVE 계좌 동기화 실패 · 주문 보류: {type(exc).__name__}'
                    return
            # Manual account purchases can arrive after startup. Adopt only
            # quantities not already covered by filled orders or prior adoption.
            await self.adopt_positions()
            await self.select_day()
            sell_filled = False
            # Manage automatic and manual holdings even if the theme list or scan fails.
            for day in list(self.days.values()):
                orders = self.daily_orders(day)
                for symbol in sorted({o.symbol for o in orders if o.side is Side.BUY} | set(day.get('adopted', {}))):
                    quantity = self.outstanding(day, symbol)
                    if quantity <= 0:
                        continue
                    try:
                        position = self.engine.broker.positions.get(symbol)
                        if not position:
                            raise RuntimeError(f'{symbol}: 주문 기록과 보유 수량 불일치')
                        quote = await self.quote(symbol)
                        signal = await self.exit_signal(symbol, quote, position, day)
                        if signal['sell'] and self.engine.settings.mode == 'live':
                            # Recheck the executable bid after the potentially slow analysis read.
                            quote = await self.quote(symbol)
                            signal = await self.exit_signal(symbol, quote, position, day)
                        if signal['sell'] and self.active() and self.market_open():
                            await self.record_order_attempt(Side.SELL, day=day)
                            order = await self.engine.broker.place_market_order(
                                client_order_id=f"auto-{day['id']}-{symbol}-swing-sell", quote=quote,
                                side=Side.SELL, quantity=min(position.quantity, quantity))
                            if order.status is OrderStatus.FILLED:
                                sell_filled = True
                                day['outcome'] = signal['exit_reason']
                                day['targets'][symbol]['exit_order_reason'] = signal['exit_reason']
                                self.message = f"{symbol} {signal['exit_reason']} 완료"
                                await self.save()
                    except Exception as exc:
                        self.message = f'스윙 매도 확인 보류: {exc}'
                        self.session.setdefault('diagnostics', {})['last_error'] = str(exc)
                        await self.save()
            await self.averaging.tick()
            try:
                payload = await asyncio.wait_for(self.recommend(), timeout=75)
                if self.engine.settings.mode == 'live':
                    self.engine.broker.risk.set_recommended_symbols(
                        c['symbol'] for c in payload.get('candidates', [])
                        if c.get('eligible') and c.get('currency', 'KRW') == 'KRW')
                await self.record_scan(payload=payload)
                filled_before = self.session.get('diagnostics', {}).get('buy_orders_filled', 0)
                rejected_this_scan = False
                minimum, themes = load_universe()
                for candidate in payload['candidates']:
                    symbol = candidate['symbol']
                    if (self.engine.settings.mode == 'live' and self.engine.settings.live_symbol_policy == 'allowlist'
                            and symbol not in self.engine.settings.live_allowed_symbols):
                        continue
                    if (not candidate.get('eligible') or symbol in self.engine.broker.positions
                            or symbol in self.session['targets'] or len(self.engine.broker.positions) >= 5):
                        continue
                    stock = await asyncio.wait_for(self.engine.toss_client.stock_info(symbol), timeout=10)
                    if self.engine.settings.mode == 'live':
                        # Complete slow analysis before obtaining the final order book.
                        daily = await asyncio.wait_for(self.engine.toss_client.candles(symbol, '1d', 100), timeout=10)
                        weekly = await asyncio.wait_for(self.engine.candles(symbol, '1w', 60), timeout=20)
                        four_hour = await asyncio.wait_for(self.engine.candles(symbol, '4h', 10), timeout=40)
                    quote = await self.quote(symbol)
                    if not membership(stock, quote.price, minimum, themes):
                        continue
                    signal = (swing_signal(daily, quote.ask_price, self.clock(), four_hour, weekly)
                              if self.engine.settings.mode == 'live' else await self.signal(symbol, quote))
                    if not signal['eligible']:
                        continue
                    allowance = self.buy_budget(symbol)
                    settings = self.engine.settings
                    fill = (quote.ask_price if settings.mode == 'live' else
                            quote.price * (1 + settings.slippage_bps / D(10000)))
                    order_limit = settings.live_max_order_amount_krw if settings.mode == 'live' else settings.max_order_amount_krw
                    quantity = int(allowance // (fill * (1 + settings.fee_rate)))
                    if order_limit > 0:
                        quantity = min(quantity, int(order_limit // fill))
                    if quantity < 1 or not self.active() or not self.market_open() or not self.buy_window_open():
                        continue
                    self.session['targets'][symbol] = {'symbol': symbol, 'name': candidate.get('name') or symbol,
                                                       'themes': themes[symbol], 'entered_at': self.clock().isoformat()}
                    # Persist intent before placing one order per symbol/day, including restart.
                    await self.save()
                    if not self.active() or not self.market_open():
                        return
                    await self.record_order_attempt(Side.BUY)
                    order = await self.engine.broker.place_market_order(
                        client_order_id=f"auto-{self.session['id']}-{symbol}-swing-buy", quote=quote,
                        side=Side.BUY, quantity=D(quantity), order_budget=allowance)
                    if order.status is OrderStatus.FILLED:
                        data = self.session.setdefault('diagnostics', {})
                        data['buy_orders_filled'] = data.get('buy_orders_filled', 0) + 1
                        self.session['outcome'] = '스윙 보유 · 상단 매도 대기'
                        self.message = f'{symbol} {quantity}주 스윙 매수'
                    elif order.status is OrderStatus.REJECTED:
                        rejected_this_scan = True
                        self.session['outcome'] = f'{symbol} 주문 거절: {order.reason}'
                        self.session.setdefault('diagnostics', {})['last_error'] = order.reason
                        self.message = self.session['outcome']
                    await self.save()
                filled_after = self.session.get('diagnostics', {}).get('buy_orders_filled', 0)
                if filled_after == filled_before and not sell_filled and not rejected_this_scan:
                    self.session['outcome'] = '매수 조건 대기'
                    self.message = ('검색 완료 · 현재 매수 조건을 충족한 후보 없음' if not payload['candidates']
                                    else '검색 완료 · 후보의 보유 여부·예산·주문 전 조건에 따라 신규 매수 없음')
                    await self.save()
            except Exception as exc:
                await self.record_scan(error=exc)
                self.message = f'스윙 신규 매수 보류: {exc}'
