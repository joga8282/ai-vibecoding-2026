import asyncio
from collections import Counter
from decimal import Decimal as D
from urllib.error import HTTPError, URLError

from app.models import Currency
from app.swing_signals import swing_signal
from app.swing_universe import load_universe, membership


async def build_swing_recommendations(engine):
    minimum, themes = load_universe()
    auto = engine.automation
    budget, _, remaining = auto.capital()
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
            counts['대형 보통주 기준 미충족 또는 시세·종목 정보 없음'] += 1
            details.append({'symbol': symbol, 'name': (stock or {}).get('name') or symbol,
                            'eligible': False, 'reasons': ['대형 보통주 기준 미충족 또는 시세·종목 정보 없음']})
            continue
        eligible.append(symbol)
    semaphore = asyncio.Semaphore(4)
    deadline = asyncio.get_running_loop().time() + 22
    async def inspect(symbol):
        # Bound the whole scan without discarding successful symbols when others time out.
        async with asyncio.timeout_at(deadline):
            async with semaphore:
                for attempt in range(2):
                    try:
                        candles = await asyncio.wait_for(engine.toss_client.candles(symbol, '1d', 100), timeout=10)
                        break
                    except (URLError, TimeoutError) as exc:
                        if attempt or (isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504)):
                            raise
                        await asyncio.sleep(0.5)
                return swing_signal(candles, quote_map[symbol].price, auto.clock())
    results = await asyncio.gather(*(inspect(s) for s in eligible), return_exceptions=True)
    candidates = []
    watchlist = []
    for symbol, signal in zip(eligible, results):
        stock, quote = stock_map[symbol], quote_map[symbol]
        signal_failed = isinstance(signal, Exception)
        if signal_failed:
            signal = {'eligible': False, 'rejection_reasons': ['일봉 조회 실패']}
        reasons = signal['rejection_reasons']
        counts.update(reasons)
        detail = {'symbol': symbol, 'name': stock.get('name') or symbol,
                  'eligible': signal['eligible'], 'reasons': reasons}
        if not signal_failed:
            detail.update({key: signal[key] for key in (
                'price', 'ma20', 'ma60', 'bollinger_lower', 'bollinger_upper',
                'band_distance_percent', 'conditions_passed', 'conditions_total')})
        details.append(detail)
        if signal_failed:
            continue
        settings = engine.settings
        fill = quote.price * (1 + settings.slippage_bps / D(10000))
        quantity = min(int(min(remaining, budget / 5) // (fill * (1 + settings.fee_rate))),
                       int(settings.max_order_amount_krw // fill))
        item = {**signal, 'symbol': symbol, 'name': stock.get('name') or symbol,
                'themes': themes[symbol], 'strategy': 'swing-v1', 'currency': 'KRW',
                'quantity': quantity, 'market_cap': str(quote.price * D(stock['sharesOutstanding']))}
        if signal['eligible']:
            candidates.append(item)
        else:
            watchlist.append(item)
    candidates.sort(key=lambda c: (-D(c['market_cap']), c['symbol']))
    watchlist.sort(key=lambda c: (-c['conditions_passed'], abs(D(c['band_distance_percent'])), -D(c['market_cap'])))
    analyzed_count = sum(not isinstance(result, Exception) for result in results)
    return {'budget': str(remaining), 'candidates': candidates[:5], 'watchlist': watchlist[:5],
            'funnel': {'universe': len(symbols), 'budget_liquidity': len(quote_map),
                       'risk_filtered': len(eligible), 'analyzed': analyzed_count, 'qualified': len(candidates)},
            'diagnostics': {'analyzed_count': analyzed_count, 'qualified_count': len(candidates),
                            'rejection_counts': dict(counts), 'symbols': details},
            'disclaimer': f'등록 테마·시가총액 {minimum / D(10**12):g}조 원 이상 보통주 · 완료 일봉 MA20 > MA60 + 20일선 상승 (60일선 상승 필수 아님) · BB(20,2) 하단 이하 매수 / 상단 이상 매도 · 다일 보유. 테마는 수동 관리 목록이며 장기 지속성을 자동 검증하지 않습니다.'}
