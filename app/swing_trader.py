from datetime import datetime, timedelta, time
from decimal import Decimal as D
from uuid import uuid4
import asyncio

from app.morning_trader import MorningTrader, KST
from app.models import Currency, Side, OrderStatus
from app.paper import PaperBroker
from app.swing_signals import four_hour_exit_signal, swing_signal, trend_context
from app.swing_universe import load_universe, membership


class SwingTrader(MorningTrader):
    """Multi-day paper holdings; no time exit, intraday averaging or fixed profit target."""
    strategy = 'swing-v2-mtf-4h'

    async def restore(self):
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
        await self.adopt_positions()
        self.observation_count = await self.engine.repository.signal_observation_count()
        self.session = self.days.get(self.clock().date().isoformat())

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
        invested = sum((p.quantity * p.average_price for p in self.engine.broker.positions.values() if p.currency is Currency.KRW), D(0))
        remaining = max(D(0), min(D(account['cash']['KRW']), budget - invested))
        if len(self.engine.broker.positions) >= 5:
            remaining = D(0)
        return budget, invested, remaining

    async def prepare(self):
        if self.engine.settings.mode != 'paper' or type(self.engine.broker) is not PaperBroker:
            raise RuntimeError('가상계좌 PAPER 주문만 지원합니다.')
        if not self.engine.toss_client:
            raise RuntimeError('스윙 자동매매에는 토스 시세 연결이 필요합니다.')
        await self.select_day()
        self.next_scan = datetime.min.replace(tzinfo=KST)
        self.message = '스윙투자 · 주봉·일봉 추세 확인 / 4시간봉 눌림목 매수 / 일봉 상단 매도'

    def status(self):
        budget, invested, remaining = self.capital()
        if self.engine.kill_switch:
            execution_state = '킬 스위치 활성 · 자동 주문 중지'
        elif not self.engine.running:
            execution_state = '자동매매 중지 · 시작 버튼을 눌러야 검색합니다'
        elif not self.market_open():
            execution_state = '장외 대기 · 평일 정규장 및 16:00~20:00 애프터마켓에 검색합니다'
        else:
            execution_state = '매도 감시 중 · 자동매수 평일 09:00~10:00, 12:00~14:00, 16:00~20:00'
        return {'strategy': self.strategy, 'budget': str(budget), 'spent': str(invested),
                'remaining': str(remaining), 'message': self.message,
                'execution_state': execution_state, 'market_open': self.market_open(),
                'trading_hours': '자동매수 평일 09:00~10:00, 12:00~14:00, 16:00~20:00 · PAPER 수동매수·매도 평일 09:00~15:30, 16:00~20:00',
                'signal_observation_count': getattr(self, 'observation_count', 0),
                'next_scan_at': max(self.clock(), self.next_scan).isoformat() if self.active() and self.market_open() else None,
                'targets': [target for day in self.days.values() for target in day['targets'].values()
                            if target['symbol'] in self.engine.broker.positions],
                'diagnostics': self.diagnostics(), 'weekly': self.report(),
                'holding_count': len(self.engine.broker.positions), 'max_positions': 5}

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
        now = self.clock().astimezone(KST)
        current = now.time()
        regular_buy_window = time(9) <= current < time(10) or time(12) <= current < time(14)
        after_market_buy_window = time(16) <= current < time(20)
        return now.weekday() < 5 and (regular_buy_window or after_market_buy_window)

    async def buy_qualified(self, symbol):
        """Recheck an eligible candidate and buy one asset-ratio allocation in PAPER."""
        async with self._lock:
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
                raise RuntimeError('시가총액 또는 국내 보통주 조건을 충족하지 않습니다.')
            signal = await self.signal(symbol, quote)
            if not signal.get('eligible'):
                raise RuntimeError('현재 자동매수 조건을 충족하지 않습니다.')
            budget = max(D(0), D(self.engine.broker.account(self.engine.quotes)['total_equity']['KRW'])
                         * self.engine.settings.recommended_trade_ratio)
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

    async def signal(self, symbol, quote):
        daily = await asyncio.wait_for(self.engine.toss_client.candles(symbol, '1d', 100), timeout=10)
        weekly = await asyncio.wait_for(self.engine.candles(symbol, '1w', 60), timeout=20)
        context = trend_context(daily, quote.price, self.clock(), weekly)
        four_hour = []
        if context.get('context_eligible'):
            four_hour = await asyncio.wait_for(self.engine.candles(symbol, '4h', 10), timeout=40)
        return swing_signal(daily, quote.price, self.clock(), four_hour, weekly)

    async def exit_signal(self, symbol, quote, position, day):
        four_hour = await asyncio.wait_for(self.engine.candles(symbol, '4h', 10), timeout=40)
        signal = four_hour_exit_signal(four_hour, quote.price, self.clock())
        target = day.setdefault('targets', {}).setdefault(symbol, {'symbol': symbol, 'name': symbol})
        peak = D(target.get('exit_peak', '0'))
        if signal.get('upper_touched'):
            target['upper_band_touched'] = True
            peak = max(peak, D(signal.get('active_high', quote.price)), quote.price)
            target['exit_peak'] = str(peak)
        elif target.get('upper_band_touched'):
            peak = max(peak, quote.price)
            target['exit_peak'] = str(peak)
        stop_loss = quote.price <= position.average_price * D('.97')
        trailing_exit = bool(target.get('upper_band_touched') and peak > 0
                             and quote.price <= peak * D('.98'))
        signal.update({'stop_loss': stop_loss, 'trailing_exit': trailing_exit,
                       'sell': stop_loss or signal.get('sell_at_upper', False) or trailing_exit,
                       'exit_reason': ('평균 매입가 대비 -3% 손절' if stop_loss else
                                       '4시간봉 볼린저 상단 매도' if signal.get('sell_at_upper') else
                                       '4시간봉 상단 터치 후 최고가 대비 -2% 매도' if trailing_exit else None)})
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
                raise RuntimeError('시가총액 1조 원 이상 국내 보통주 조건을 충족하지 않습니다.')
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
            if self.engine.settings.mode != 'paper' or type(self.engine.broker) is not PaperBroker:
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
                order = await self.engine.broker.place_market_order(
                    client_order_id=f"auto-{day['id']}-{symbol}-manual-{uuid4().hex}",
                    quote=quote, side=Side.SELL, quantity=quantity)
                if order.status is not OrderStatus.FILLED:
                    raise RuntimeError(f'매도 거절: {order.reason}')
                orders.append(order)
                day['outcome'] = '수동 전량 매도'
                await self.save()
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
            await self.select_day()
            sell_filled = False
            # Exit every inherited automated holding, even if the theme list or scan fails.
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
                        if signal['sell'] and self.active() and self.market_open():
                            await self.record_order_attempt(Side.SELL, day=day)
                            order = await self.engine.broker.place_market_order(
                                client_order_id=f"auto-{day['id']}-{symbol}-swing-sell", quote=quote,
                                side=Side.SELL, quantity=min(position.quantity, quantity))
                            if order.status is OrderStatus.FILLED:
                                sell_filled = True
                                day['outcome'] = signal['exit_reason']
                                self.message = f"{symbol} {signal['exit_reason']} 완료"
                                await self.save()
                    except Exception as exc:
                        self.message = f'스윙 매도 확인 보류: {exc}'
                        self.session.setdefault('diagnostics', {})['last_error'] = str(exc)
                        await self.save()
            try:
                payload = await asyncio.wait_for(self.recommend(), timeout=75)
                await self.record_scan(payload=payload)
                filled_before = self.session.get('diagnostics', {}).get('buy_orders_filled', 0)
                minimum, themes = load_universe()
                for candidate in payload['candidates']:
                    symbol = candidate['symbol']
                    if (not candidate.get('eligible') or symbol in self.engine.broker.positions
                            or symbol in self.session['targets'] or len(self.engine.broker.positions) >= 5):
                        continue
                    quote = await self.quote(symbol)
                    stock = await asyncio.wait_for(self.engine.toss_client.stock_info(symbol), timeout=10)
                    if not membership(stock, quote.price, minimum, themes):
                        continue
                    signal = await self.signal(symbol, quote)
                    if not signal['eligible']:
                        continue
                    _, _, allowance = self.capital()
                    settings = self.engine.settings
                    fill = quote.price * (1 + settings.slippage_bps / D(10000))
                    quantity = min(int(allowance // (fill * (1 + settings.fee_rate))), int(settings.max_order_amount_krw // fill))
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
                    await self.save()
                filled_after = self.session.get('diagnostics', {}).get('buy_orders_filled', 0)
                if filled_after == filled_before and not sell_filled:
                    self.session['outcome'] = '매수 조건 대기'
                    self.message = ('검색 완료 · 현재 매수 조건을 충족한 후보 없음' if not payload['candidates']
                                    else '검색 완료 · 후보의 보유 여부·예산·주문 전 조건에 따라 신규 매수 없음')
                    await self.save()
            except Exception as exc:
                await self.record_scan(error=exc)
                self.message = f'스윙 신규 매수 보류: {exc}'
