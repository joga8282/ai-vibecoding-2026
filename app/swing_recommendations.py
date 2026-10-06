import asyncio
from collections import Counter
from decimal import Decimal as D
from urllib.error import HTTPError, URLError

from app.models import Currency
from app.swing_signals import swing_signal, trend_context
from app.swing_universe import load_universe, membership


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
            reason = '시가총액 1조원 이상 보통주 또는 시세·종목 정보 미충족'
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
                'band_distance_percent', 'conditions_passed', 'conditions_total')})
        details.append(detail)
        if signal.get('weekly_peak_excluded') or signal.get('weekly_peak_checked') is not True:
            continue
        if failed:
            continue
        settings = engine.settings
        fill = quote.price * (1 + settings.slippage_bps / D(10000))
        quantity = int(remaining // (fill * (1 + settings.fee_rate)))
        if settings.mode == 'live' and settings.live_max_order_amount_krw > 0:
            quantity = min(quantity, int(settings.live_max_order_amount_krw // fill))
        item = {**signal, 'symbol': symbol, 'name': stock.get('name') or symbol,
                'themes': themes[symbol], 'strategy': 'swing-v2-mtf-4h', 'currency': 'KRW',
                'quantity': quantity, 'market_cap': str(quote.price * D(stock['sharesOutstanding']))}
        (candidates if signal['eligible'] else watchlist).append(item)
    candidates.sort(key=lambda c: (-D(c['market_cap']), c['symbol']))
    watchlist.sort(key=lambda c: (-c.get('conditions_passed', 0), abs(D(c.get('band_distance_percent', '999'))), -D(c['market_cap'])))
    analyzed_count = sum(not isinstance(result, Exception) for result in results)
    return {'budget': str(remaining), 'candidates': candidates[:5], 'watchlist': watchlist[:5],
            'funnel': {'universe': len(symbols), 'budget_liquidity': len(quote_map),
                       'risk_filtered': len(eligible), 'analyzed': analyzed_count, 'qualified': len(candidates)},
            'diagnostics': {'analyzed_count': analyzed_count, 'qualified_count': len(candidates),
                            'rejection_counts': dict(counts), 'symbols': details},
            'disclaimer': f'등록 장기 테마·시가총액 {minimum / D(10**12):g}조원 이상 보통주 · 주봉 MA10 방향과 일봉 상승 추세 확인 · 4시간봉 BB(6,2) 하단 3% 구간 매수 · 평균가 -3% 손절 · 4시간봉 상단 또는 터치 후 최고가 -2% 매도 · 최대 5종목'}
