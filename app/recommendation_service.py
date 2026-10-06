from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import HTTPException
from app.recommendations import analyze_candidate
from app.swing_recommendations import build_swing_recommendations


def _remember_live_recommendations(engine, result: dict) -> None:
    risk = getattr(engine.broker, 'risk', None)
    if engine.settings.mode != 'live' or risk is None:
        return
    candidates = result.get('candidates') if isinstance(result, dict) else None
    risk.set_recommended_symbols(
        candidate.get('symbol') for candidate in (candidates or [])
        if isinstance(candidate, dict) and candidate.get('eligible') is True
        and candidate.get('currency', 'KRW') == 'KRW'
    )


async def build_recommendations(engine, report_direction="neutral") -> dict:
    if str(getattr(engine.automation, 'strategy', '')).startswith('swing-v'):
        try:
            result = await build_swing_recommendations(engine)
        except Exception as exc:
            risk = getattr(engine.broker, 'risk', None)
            if risk:
                risk.clear_recommended_symbols()
            engine.last_error = f"recommendations: {type(exc).__name__}: {exc}"
            raise HTTPException(status_code=502, detail="스윙 후보 조회 실패: 시세 API 연결과 설정을 확인하세요.") from exc
        if engine.last_error and engine.last_error.startswith('recommendations:'):
            engine.last_error = None
        _remember_live_recommendations(engine, result)
        return result
    if getattr(engine.automation, 'strategy', None) == 'morning-v1':
        try:
            result = await build_morning_recommendations(engine)
        except Exception:
            risk = getattr(engine.broker, 'risk', None)
            if risk:
                risk.clear_recommended_symbols()
            raise
        _remember_live_recommendations(engine, result)
        return result
    account = engine.broker.account(engine.quotes)
    equity = Decimal(account["total_equity"]["KRW"])
    cash = Decimal(account["cash"]["KRW"])
    budget = min(cash, equity * engine.settings.recommended_trade_ratio)
    allocation = budget / 5
    if engine.automation and engine.automation.session:
        budget = max(Decimal("0"), Decimal(engine.automation.session["budget"]) - engine.automation.spent())
        allocation = Decimal(engine.automation.session["budget"]) / 5
    try:
        ranking_result = await engine.toss_client.rankings(100)
        ranking_items = [
            item for item in ranking_result.get("rankings", [])
            if item.get("currency") == "KRW"
            and Decimal(item.get("price", {}).get("lastPrice", "0")) > 0
            and Decimal(item.get("price", {}).get("lastPrice", "0")) <= budget
        ][:30]
        symbols = [item["symbol"] for item in ranking_items]
        stock_items = await engine.toss_client.stocks_info(symbols)
        stocks_by_symbol = {item["symbol"]: item for item in stock_items}
        risk_filtered: list[tuple[dict, dict, Decimal, Decimal]] = []
        for ranking in ranking_items:
            stock = stocks_by_symbol.get(ranking["symbol"], {})
            if stock.get("securityType") != "STOCK" or stock.get("isCommonShare") is not True:
                continue
            price = Decimal(ranking["price"]["lastPrice"])
            market_cap = price * Decimal(stock.get("sharesOutstanding") or "0")
            if market_cap >= Decimal("1000000000000"):
                risk_filtered.append((ranking, stock, price, market_cap))
        # Analyze a different pre-screened subset on every request.
        eligible = random.sample(risk_filtered, min(12, len(risk_filtered)))
        candle_results = await asyncio.wait_for(
            asyncio.gather(
                *(engine.toss_client.candles(item[0]["symbol"], "1d", 120) for item in eligible),
                return_exceptions=True,
            ),
            timeout=20,
        )
    except Exception as exc:
        risk = getattr(engine.broker, 'risk', None)
        if risk:
            risk.clear_recommended_symbols()
        engine.last_error = f"recommendations: {type(exc).__name__}: {exc}"
        raise HTTPException(status_code=502, detail="추천 후보를 분석하지 못했습니다.") from exc

    candidates: list[dict] = []
    macro_adjustment = 3 if report_direction == "risk_on" else -7 if report_direction == "defensive" else 0
    for (ranking, stock, price, market_cap), candles in zip(eligible, candle_results):
        if isinstance(candles, Exception):
            continue
        engine._buy_candles[ranking["symbol"]] = (datetime.now(timezone.utc), sorted(candles, key=lambda item: item.timestamp))
        change_rate = Decimal(ranking["price"].get("changeRate", "0"))
        analysis = analyze_candidate(candles, price, change_rate)
        if not analysis or not analysis["eligible"]:
            continue
        analysis["score"] = max(0, min(100, analysis["score"] + macro_adjustment))
        fill = price * (1 + engine.settings.slippage_bps / Decimal("10000"))
        unit_cost = fill * (1 + engine.settings.fee_rate)
        allowance = min(budget, allocation / 2 if report_direction == "defensive" else allocation, cash)
        suggested_quantity = min(int(allowance // unit_cost), int(engine.settings.max_order_amount_krw // fill))
        candidates.append(
            {
                "symbol": ranking["symbol"],
                "name": stock.get("name") or ranking["symbol"],
                "price": str(price),
                "market_cap": str(market_cap.quantize(Decimal("1"))),
                "currency": "KRW",
                "quantity": suggested_quantity,
                "change_rate": str(change_rate),
                "weekly_direction": report_direction,
                "macro_adjustment": macro_adjustment,
                **analysis,
            }
        )
    random.shuffle(candidates)
    selected_candidates = candidates[:5]
    result = {
        "budget": str(budget.quantize(Decimal("1"))),
        "ratio": str(engine.settings.recommended_trade_ratio),
        "ranked_at": ranking_result.get("rankedAt"),
        "weekly_direction": report_direction,
        "funnel": {
            "universe": int(ranking_result.get("totalCount") or 2601),
            "budget_liquidity": len(ranking_items),
            "risk_filtered": len(eligible),
            "analyzed": len(candle_results),
            "qualified": len(candidates),
        },
        "candidates": selected_candidates,
        "disclaimer": "상승 추세·과열 제외·저항 여력과 지지선 또는 볼린저 하단 접촉 조건을 통과한 PAPER 후보입니다. 조건에 맞는 종목만 최대 5개 표시하며 투자 권유가 아닙니다.",
    }
    _remember_live_recommendations(engine, result)
    return result


async def build_morning_recommendations(engine):
    from app.morning_trader import morning_signal
    from collections import Counter
    auto = engine.automation
    now = auto.clock()
    account = engine.broker.account(engine.quotes)
    budget = Decimal(account['cash']['KRW'])
    rankings = await engine.toss_client.rankings(100)
    ranked = [r for r in rankings.get('rankings', []) if r.get('currency') == 'KRW' and 0 < Decimal(r.get('price', {}).get('lastPrice', '0')) <= budget][:30]
    stocks = {s['symbol']: s for s in await engine.toss_client.stocks_info([r['symbol'] for r in ranked])}
    rejection_counts = Counter()
    large = []
    for row in ranked:
        stock = stocks.get(row['symbol'])
        if not stock:
            rejection_counts['종목 정보 없음'] += 1
            continue
        if stock.get('isCommonShare') is not True or stock.get('securityType') != 'STOCK':
            rejection_counts['보통주 아님'] += 1
            continue
        market_cap = Decimal(row['price']['lastPrice']) * Decimal(stock.get('sharesOutstanding') or '0')
        if market_cap < Decimal('1000000000000'):
            rejection_counts['시가총액 1조 원 미만'] += 1
            continue
        large.append(row)
        if len(large) == 12:
            break
    async def inspect(row):
        daily, minutes = await asyncio.gather(engine.toss_client.candles(row['symbol'], '1d', 25), engine.toss_client.candles(row['symbol'], '1m', 5))
        return morning_signal(daily, minutes, now)
    results = await asyncio.wait_for(asyncio.gather(*(inspect(r) for r in large), return_exceptions=True), timeout=25)
    candidates = []
    symbols = []
    for row, result in zip(large, results):
        name = stocks[row['symbol']].get('name') or row['symbol']
        if isinstance(result, Exception):
            reasons = ['시세 분석 오류']
            rejection_counts[reasons[0]] += 1
            symbols.append({'symbol': row['symbol'], 'name': name, 'eligible': False, 'reasons': reasons})
            continue
        if not result or not result['eligible']:
            reasons = (result or {}).get('rejection_reasons') or ['신호 데이터 없음']
            rejection_counts.update(reasons)
            symbols.append({'symbol': row['symbol'], 'name': name, 'eligible': False, 'reasons': reasons})
            continue
        price = Decimal(result['price'])
        fill = price * (1 + engine.settings.slippage_bps / Decimal(10000))
        quantity = int((budget / 10) // (fill * (1 + engine.settings.fee_rate)))
        candidates.append({**result, 'symbol': row['symbol'], 'name': stocks[row['symbol']].get('name') or row['symbol'],
                           'currency': 'KRW', 'quantity': quantity, 'strategy': 'morning-v1',
                           'market_cap': str(price * Decimal(stocks[row['symbol']]['sharesOutstanding']))})
        symbols.append({'symbol': row['symbol'], 'name': name, 'eligible': True, 'reasons': []})
    return {'budget': str(budget), 'candidates': candidates[:5], 'funnel': {'universe': rankings.get('totalCount', 0), 'budget_liquidity': len(ranked), 'risk_filtered': len(large), 'analyzed': len(results)},
            'diagnostics': {'analyzed_count': len(results), 'qualified_count': len(candidates),
                            'rejection_counts': dict(rejection_counts), 'symbols': symbols},
            'disclaimer': '08:00~08:05 첫 진입 · 하루 1종목 10분할 · 가용 원화 전액 예산 · 20분 간격 확인 + 첫 매수가 대비 0.2% 하락 조건 · 매도 신호 시 전량 청산 · 15:10 청산 시도.'}
