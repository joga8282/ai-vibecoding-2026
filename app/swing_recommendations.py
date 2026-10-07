import asyncio
from collections import Counter
from decimal import Decimal as D
from urllib.error import HTTPError, URLError

from app.models import Currency
from app.swing_signals import swing_signal, trend_context
from app.swing_universe import load_universe, market_cap_label, membership


async def build_swing_recommendations(engine):
    minimum, themes = load_universe()
    auto = engine.automation
    _, _, remaining = auto.capital()
    symbols = sorted(themes)
    if symbols:
        stocks, quotes = await asyncio.wait_for(asyncio.gather(
            engine.toss_client.stocks_info(symbols), engine.toss_client.prices(symbols)), timeout=20)
    else:
        stocks, quotes = [], []
    stock_map = {s['symbol']: s for s in stocks}
    quote_map = {q.symbol: q for q in quotes if q.currency is Currency.KRW and q.price > 0}
    counts = Counter()
    details, eligible = [], []
    for symbol in symbols:
        stock, quote = stock_map.get(symbol), quote_map.get(symbol)
        if not quote or not membership(stock, quote.price, minimum, themes):
            reason = f'시가총액 {market_cap_label(minimum)} 이상 보통주 또는 시세·종목 정보 미충족'
            counts[reason] += 1
            details.append({'symbol': symbol, 'name': (stock or {}).get('name') or symbol,
                            'eligible': False, 'reasons': [reason]})
            continue
        eligible.append(symbol)

    semaphore = asyncio.Semaphore(2)
    deadline = asyncio.get_running_loop().time() + 65

    async def daily_context(symbol):
        async with asyncio.timeout_at(deadline):
            async with semaphore:
                for attempt in range(3):
                    try:
                        candles = await asyncio.wait_for(engine.toss_client.candles(symbol, '1d', 100), timeout=10)
                        weekly = await asyncio.wait_for(engine.candles(symbol, '1w', 60), timeout=20)
                        return candles, weekly, trend_context(candles, quote_map[symbol].price, auto.clock(), weekly)
                    except (URLError, TimeoutError) as exc:
                        if attempt == 2 or (isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504)):
                            raise
                        await asyncio.sleep(1)

    daily_results = await asyncio.gather(*(daily_context(s) for s in eligible), return_exceptions=True)

    async def finish_signal(symbol, daily, weekly, context):
        if not context.get('context_eligible'):
            return swing_signal(daily, quote_map[symbol].price, auto.clock(), [], weekly)
        # Toss exposes 1-minute and daily candles. engine.candles builds 4h session bars.
        # Only trend-qualified symbols pay this heavier lookup cost.
        async with asyncio.timeout_at(deadline):
            async with semaphore:
                four_hour = await asyncio.wait_for(engine.candles(symbol, '4h', 10), timeout=40)
        return swing_signal(daily, quote_map[symbol].price, auto.clock(), four_hour, weekly)

    tasks = []
    for symbol, result in zip(eligible, daily_results):
        if isinstance(result, Exception):
            tasks.append(result)
        else:
            daily, weekly, context = result
            tasks.append(asyncio.create_task(finish_signal(symbol, daily, weekly, context)))
    pending = [task for task in tasks if isinstance(task, asyncio.Task)]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results = [task.result() if isinstance(task, asyncio.Task) and not task.cancelled() and task.exception() is None
               else task.exception() if isinstance(task, asyncio.Task) and not task.cancelled()
               else task for task in tasks]

    candidates, watchlist = [], []
    for symbol, signal in zip(eligible, results):
        stock, quote = stock_map[symbol], quote_map[symbol]
        failed = isinstance(signal, Exception)
        if failed:
            signal = {'eligible': False, 'rejection_reasons': ['시세 분석 조회 실패']}
        reasons = signal['rejection_reasons']
        counts.update(reasons)
        detail = {'symbol': symbol, 'name': stock.get('name') or symbol,
                  'eligible': signal['eligible'], 'reasons': reasons}
        if not failed:
            detail.update({key: signal.get(key) for key in (
                'price', 'ma20', 'ma60', 'weekly_ma10', 'bollinger_lower', 'bollinger_upper',
                'band_distance_percent', 'conditions_passed', 'conditions_total',
                'entry_ceiling', 'four_hour_band_position_percent', 'daily_range_position_percent',
                'daily_bottom_zone', 'entry_zone_eligible', 'entry_policy',
                'weekly_bottom_zone', 'weekly_entry_checked', 'weekly_entry_ceiling',
                'weekly_range_position_percent', 'weekly_recent_low', 'weekly_recent_high',
                'weekly_support_floor', 'weekly_support_broken', 'entry_floor')})
        details.append(detail)
        if (signal.get('weekly_peak_excluded') or signal.get('weekly_peak_checked') is not True
                or signal.get('weekly_bottom_zone') is not True or signal.get('weekly_entry_checked') is not True):
            continue
        if failed:
            continue
        settings = engine.settings
        fill = quote.price * (1 + settings.slippage_bps / D(10000))
        allowance = auto.buy_budget(symbol)
        quantity = int(allowance // (fill * (1 + settings.fee_rate)))
        if settings.mode == 'live' and settings.live_max_order_amount_krw > 0:
            quantity = min(quantity, int(settings.live_max_order_amount_krw // fill))
        item = {**signal, 'symbol': symbol, 'name': stock.get('name') or symbol,
                'themes': themes[symbol], 'strategy': 'swing-v2-mtf-4h', 'currency': 'KRW',
                'quantity': quantity, 'market_cap': str(quote.price * D(stock['sharesOutstanding']))}
        if settings.mode == 'live':
            item['order_budget'] = str(allowance)
        if signal['eligible']:
            candidates.append(item)
        elif (signal.get('daily_bottom_zone') is True and D(signal.get('entry_ceiling', '0')) > 0
              and quote.price <= D(signal['entry_ceiling']) * D('1.03')):
            # Diagnostic rows retain every exclusion. Visible waiting stocks
            # must be near the entry price, rather than merely trend-qualified.
            watchlist.append(item)
    candidates.sort(key=lambda c: (-D(c['market_cap']), c['symbol']))
    watchlist.sort(key=lambda c: (-c.get('conditions_passed', 0), abs(D(c.get('band_distance_percent', '999'))), -D(c['market_cap'])))
    analyzed_count = sum(not isinstance(result, Exception) for result in results)
    return {'budget': str(remaining), 'per_symbol_budget': str(auto.buy_budget()),
            'minimum_market_cap_krw': str(minimum),
            'candidates': candidates[:5], 'watchlist': watchlist[:5],
            'funnel': {'universe': len(symbols), 'budget_liquidity': len(quote_map),
                       'risk_filtered': len(eligible), 'analyzed': analyzed_count, 'qualified': len(candidates)},
            'diagnostics': {'analyzed_count': analyzed_count, 'qualified_count': len(candidates),
                            'rejection_counts': dict(counts), 'symbols': details},
            'disclaimer': f'등록 장기 테마·시가총액 {market_cap_label(minimum)} 이상 보통주 · 주봉·일봉 추세 · 최근 20주와 20일 고저 범위 하위 40% · 4시간 BB(6,2) 하위 25% 및 하단 3% 이내에서 매수 추천 · 고점 종목은 추천·대기 목록에서 제외 · 평균가 -3% 손절 · 상단 + 예상 순수익 {engine.settings.swing_min_net_profit_percent:g}% 이상 익절 · 수익 기준 충족 후 최고가 -2% 보호 매도 · 최대 5종목'}
