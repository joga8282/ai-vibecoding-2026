"""One persisted averaging attempt per holding cycle, with normal funding caps."""
import asyncio
from decimal import Decimal as D
from uuid import uuid4

from app.models import Currency, Side
from app.risk import TradingControl
from app.swing_universe import load_universe, membership


SNAPSHOT = 'swing_averaging_plans'
TRAILING_KEYS = ('upper_band_touched', 'exit_peak', 'profit_trailing_armed',
                 'profit_trailing_armed_at', 'profit_trailing_min_percent', 'last_exit_check')


class SwingAveraging:
    def __init__(self, trader):
        self.trader = trader
        self.plans = {}
        self.checks = {}

    async def restore(self):
        saved = await self.trader.engine.repository.load(SNAPSHOT) or {}
        self.plans = saved.get('plans', {})
        self.checks = saved.get('checks', {})

    async def save(self):
        await self.trader.engine.repository.save(SNAPSHOT, {'plans': self.plans, 'checks': self.checks})

    def status(self):
        settings = self.trader.engine.settings
        return {'enabled': settings.swing_averaging_enabled,
                'trigger_loss_percent': str(settings.swing_averaging_trigger_percent),
                'max_additions_per_position': 1,
                'checks': [item for symbol, item in self.checks.items()
                           if symbol in self.trader.engine.broker.positions]}

    def owners(self, symbol):
        return [day for day in self.trader.days.values() if self.trader.outstanding(day, symbol) > 0]

    def check(self, symbol, status, reason, **fields):
        self.checks[symbol] = {'symbol': symbol, 'status': status, 'reason': reason,
                               'checked_at': self.trader.clock().isoformat(), **fields}

    def budget(self, original_amount):
        engine = self.trader.engine
        settings, broker = engine.settings, engine.broker
        account = broker.account(engine.quotes)
        equity, cash = D(account['total_equity']['KRW']), D(account['cash']['KRW'])
        limit = equity * settings.live_auto_exposure_ratio_limit
        pending = D(0)
        if settings.mode == 'live':
            if not broker.reconciled:
                return D(0)
            pending = broker._pending_buy_exposure()
            if pending is None:
                return D(0)
            pending *= 1 + settings.fee_rate
            invested = sum(broker.position_market_values.values(), D(0))
        else:
            invested = sum((p.quantity * (engine.quotes[p.symbol].price if p.symbol in engine.quotes
                                          else p.average_price)
                            for p in broker.positions.values() if p.currency is Currency.KRW), D(0))
        if settings.live_max_total_exposure_krw > 0:
            limit = min(limit, settings.live_max_total_exposure_krw)
        ratio = min(settings.recommended_trade_ratio, settings.live_auto_ratio_limit)
        return max(D(0), min(original_amount, equity * ratio, cash - pending,
                             limit - invested - pending - D(1)))

    def reset_trailing(self, symbol):
        for day in self.trader.days.values():
            target = day.get('targets', {}).get(symbol)
            if target:
                for key in TRAILING_KEYS:
                    target.pop(key, None)

    async def apply_result(self, plan, order):
        plan.update({'status': order.status.value, 'filled_quantity': str(order.quantity),
                     'resolved_at': self.trader.clock().isoformat()})
        if order.quantity > D(plan.get('reset_applied_quantity', '0')):
            self.reset_trailing(plan['symbol'])
            await self.trader.save()
            plan['reset_applied_quantity'] = str(order.quantity)
        self.check(plan['symbol'], order.status.value,
                   f'추가 매수 1회 결과 {order.status.value} · 체결 {order.quantity}주 · 반복 주문 없음',
                   filled_quantity=str(order.quantity), budget=plan['budget'])
        await self.save()

    async def refresh_results(self):
        # Reconciliation may finish an interrupted or partially filled order.
        for plan in self.plans.values():
            client_id = plan.get('client_order_id')
            order = next((item for item in self.trader.engine.broker.orders
                          if item.client_order_id == client_id), None) if client_id else None
            if order and (plan.get('status') != order.status.value
                          or D(plan.get('filled_quantity', '0')) != order.quantity
                          or D(plan.get('reset_applied_quantity', '0')) < order.quantity):
                await self.apply_result(plan, order)
            broker = self.trader.engine.broker
            synced = self.trader.engine.settings.mode != 'live' or broker.reconciled
            pending = any(str(item.get('symbol', '')).upper() == plan['symbol']
                          for item in getattr(broker, 'open_orders', []))
            if (synced and plan['symbol'] not in broker.positions and not pending and not plan.get('closed_at')
                    and plan['status'] not in {'RESERVED', 'SUBMITTING', 'UNKNOWN'}):
                plan['closed_at'] = self.trader.clock().isoformat()
                await self.save()

    async def tick(self):
        await self.refresh_results()
        auto, engine = self.trader, self.trader.engine
        settings, broker = engine.settings, engine.broker
        if not settings.swing_averaging_enabled or not auto.active() or not auto.buy_window_open():
            return
        minimum, themes = load_universe()
        for symbol in sorted(list(broker.positions)):
            position = broker.positions.get(symbol)
            if not position or position.currency is not Currency.KRW:
                continue
            owners = self.owners(symbol)
            if not owners or sum((auto.outstanding(day, symbol) for day in owners), D(0)) != position.quantity:
                self.check(symbol, 'BLOCKED', '보유 수량과 스윙 기록 불일치 · 추가 매수 보류')
                continue
            owner_ids = {day['id'] for day in owners}
            # Orders may be truncated in the broker snapshot. A still-open
            # symbol keeps its cycle even if its original owner day changes.
            plan = next((item for item in self.plans.values() if item['symbol'] == symbol
                         and not item.get('closed_at')), None)
            if plan is None:
                plan_id = uuid4().hex
                plan = {'id': plan_id, 'symbol': symbol, 'owner_day_ids': sorted(owner_ids),
                        'original_amount': str(position.quantity * position.average_price * (1 + settings.fee_rate)),
                        'basis_at': auto.clock().isoformat(), 'status': 'WAITING'}
                self.plans[plan_id] = plan
                await self.save()
            elif not owner_ids.issubset(set(plan['owner_day_ids'])):
                plan['owner_day_ids'] = sorted(owner_ids | set(plan['owner_day_ids']))
                await self.save()
            if plan.get('attempted_at'):
                if plan['status'] in {'RESERVED', 'SUBMITTING', 'UNKNOWN'}:
                    self.check(symbol, plan['status'], '추가 주문 접수·체결 상태 확인 필요 · 반복 주문 없음')
                continue
            try:
                if settings.mode == 'live':
                    if broker.risk.control is not TradingControl.ACTIVE:
                        raise RuntimeError('LIVE 매수 통제 활성 · 추가 매수 보류')
                    if broker.risk.daily_loss_limit_reached:
                        raise RuntimeError('일일 손실 한도 도달 · 추가 매수 보류')
                    if any(str(item.get('symbol', '')).upper() == symbol for item in broker.open_orders):
                        raise RuntimeError('해당 종목 미체결 주문 확인 필요')
                if symbol not in themes:
                    raise RuntimeError('등록 테마 밖의 보유 종목 · 추가 매수 제외')
                quote = await auto.quote(symbol)
                buy_price = quote.ask_price if settings.mode == 'live' else quote.price
                trigger = position.average_price * (1 - settings.swing_averaging_trigger_percent / D(100))
                if buy_price > trigger:
                    self.check(symbol, 'WAITING', f'-{settings.swing_averaging_trigger_percent:g}% 추가 매수 기준 미도달',
                               trigger_price=str(trigger))
                    continue
                stock = await asyncio.wait_for(engine.toss_client.stock_info(symbol), timeout=10)
                if settings.mode == 'live':
                    snapshot = await broker.reconcile()
                    if not snapshot.get('reconciled') or not auto.active():
                        raise RuntimeError('추가 매수 전 계좌 동기화·무장 확인 필요')
                # The account or price can change during the stock lookup.
                position = broker.positions.get(symbol)
                if not position:
                    raise RuntimeError('현재 보유분 없음')
                quote = await auto.quote(symbol)
                buy_price = quote.ask_price if settings.mode == 'live' else quote.price
                trigger = position.average_price * (1 - settings.swing_averaging_trigger_percent / D(100))
                if buy_price > trigger:
                    self.check(symbol, 'WAITING', '최신 매수 호가가 추가 매수 기준 위로 회복')
                    continue
                if not membership(stock, quote.price, minimum, themes):
                    raise RuntimeError('추가 매수 대상 시가총액·보통주 조건 미충족')
                allowance = self.budget(D(plan['original_amount']))
                fill = buy_price if settings.mode == 'live' else buy_price * (1 + settings.slippage_bps / D(10000))
                quantity = int(allowance // (fill * (1 + settings.fee_rate)))
                order_cap = settings.live_max_order_amount_krw if settings.mode == 'live' else settings.max_order_amount_krw
                if order_cap > 0:
                    quantity = min(quantity, int(order_cap // fill))
                if quantity < 1:
                    raise RuntimeError('1회 비율·합산 한도·현금·미체결 적용 후 1주 예산 부족')
                if not auto.active() or not auto.buy_window_open():
                    raise RuntimeError('추가 매수 실행시간·엔진 상태 변경')
                client_id = f"auto-{owners[0]['id']}-{symbol}-swing-average-{plan['id']}"
                plan.update({'status': 'RESERVED', 'attempted_at': auto.clock().isoformat(),
                             'client_order_id': client_id, 'quantity': str(quantity), 'budget': str(allowance),
                             'holding_quantity': str(position.quantity), 'holding_average': str(position.average_price),
                             'trigger_price': str(trigger)})
                # One attempt, even after a rejection, partial fill, timeout, or restart.
                await self.save()
                await auto.record_order_attempt(Side.BUY, day=owners[0])
                kwargs = {'client_order_id': client_id, 'quote': quote, 'side': Side.BUY,
                          'quantity': D(quantity), 'order_budget': allowance}
                if settings.mode == 'live':
                    kwargs['averaging_plan_id'] = plan['id']
                    order = await broker.place_averaging_order(**kwargs)
                else:
                    order = await broker.place_market_order(**kwargs)
                await self.apply_result(plan, order)
                auto.message = self.checks[symbol]['reason']
            except Exception as exc:
                if plan.get('attempted_at'):
                    uncertain = any(item.get('originalClientOrderId') == plan['client_order_id']
                                    and item.get('status') in {'SUBMITTING', 'UNKNOWN', 'ACCEPTED', 'CANCEL_REQUESTED'}
                                    for item in getattr(broker, 'order_journal', {}).values())
                    plan['status'] = 'UNKNOWN' if uncertain else 'BLOCKED'
                    plan['error_type'] = type(exc).__name__
                self.check(symbol, plan['status'] if plan.get('attempted_at') else 'BLOCKED', str(exc))
                if not auto.active():
                    break
        await self.save()
