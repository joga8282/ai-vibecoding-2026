from __future__ import annotations

from decimal import Decimal

from app.models import Candle


def _aggregate_weekly(candles: list[Candle]) -> list[Candle]:
    grouped: dict[tuple[int, int], list[Candle]] = {}
    for candle in sorted(candles, key=lambda item: item.timestamp):
        iso_year, iso_week, _ = candle.timestamp.isocalendar()
        grouped.setdefault((iso_year, iso_week), []).append(candle)
    return [
        Candle(
            timestamp=items[0].timestamp,
            open_price=items[0].open_price,
            high_price=max(item.high_price for item in items),
            low_price=min(item.low_price for item in items),
            close_price=items[-1].close_price,
            volume=sum((item.volume for item in items), Decimal("0")),
            currency=items[0].currency,
        )
        for items in grouped.values()
    ]


def _support_resistance(
    candles: list[Candle], current_price: Decimal, fallback_window: int
) -> tuple[Decimal, Decimal]:
    ordered = sorted(candles, key=lambda item: item.timestamp)
    history = ordered[:-1]
    swing_lows = [
        history[index].low_price
        for index in range(2, len(history) - 2)
        if history[index].low_price <= min(item.low_price for item in history[index - 2:index + 3])
    ]
    swing_highs = [
        history[index].high_price
        for index in range(2, len(history) - 2)
        if history[index].high_price >= max(item.high_price for item in history[index - 2:index + 3])
    ]
    support_candidates = [level for level in swing_lows if level <= current_price]
    resistance_candidates = [level for level in swing_highs if level > current_price]
    fallback = ordered[-fallback_window:]
    support = max(support_candidates) if support_candidates else min(item.low_price for item in fallback)
    resistance = min(resistance_candidates) if resistance_candidates else max(item.high_price for item in fallback)
    return support, resistance


def analyze_candidate(
    candles: list[Candle], current_price: Decimal, change_rate: Decimal
) -> dict | None:
    ordered = sorted(candles, key=lambda item: item.timestamp)
    if len(ordered) < 65 or current_price <= 0:
        return None
    if any(item.close_price <= 0 or item.low_price <= 0 for item in ordered):
        return None
    closes = [item.close_price for item in ordered]
    volumes = [item.volume for item in ordered]
    ma5 = sum(closes[-5:], Decimal("0")) / Decimal("5")
    ma20 = sum(closes[-20:], Decimal("0")) / Decimal("20")
    ma60 = sum(closes[-60:], Decimal("0")) / Decimal("60")
    previous_ma20 = sum(closes[-25:-5], Decimal("0")) / Decimal("20")
    previous_ma60 = sum(closes[-65:-5], Decimal("0")) / Decimal("60")
    uptrend = ma20 > ma60 and ma20 > previous_ma20 and ma60 > previous_ma60 and current_price > ma60

    # Population standard deviation of the same 20 closes, including the latest bar.
    deviation = (sum(((price - ma20) ** 2 for price in closes[-20:]), Decimal("0")) / Decimal("20")).sqrt()
    bollinger_lower = ma20 - Decimal("2") * deviation
    bollinger_upper = ma20 + Decimal("2") * deviation
    bollinger_touched = (
        deviation > 0
        and ordered[-1].low_price <= bollinger_lower <= ordered[-1].high_price
        and current_price >= bollinger_lower
    )

    deltas = [closes[index] - closes[index - 1] for index in range(len(closes) - 14, len(closes))]
    gains = sum((max(delta, Decimal("0")) for delta in deltas), Decimal("0")) / Decimal("14")
    losses = sum((max(-delta, Decimal("0")) for delta in deltas), Decimal("0")) / Decimal("14")
    rsi = (Decimal("50") if gains == 0 else Decimal("100")) if losses == 0 else Decimal("100") - Decimal("100") / (Decimal("1") + gains / losses)
    return5 = current_price / closes[-6] - Decimal("1")
    return20 = current_price / closes[-21] - Decimal("1")
    ma20_distance = current_price / ma20 - Decimal("1")
    overheating_reasons = []
    for overheated, label in [
        (rsi >= Decimal("70"), "RSI 70 이상"),
        (change_rate >= Decimal("0.07"), "당일 7% 이상 상승"),
        (return5 >= Decimal("0.10"), "5거래일 10% 이상 상승"),
        (return20 >= Decimal("0.20"), "20거래일 20% 이상 상승"),
        (ma20_distance >= Decimal("0.08"), "20일선 대비 8% 이상 상승"),
    ]:
        if overheated:
            overheating_reasons.append(label)

    average_volume = sum(volumes[-21:-1], Decimal("0")) / Decimal("20")
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else Decimal("0")

    daily = ordered[-60:]
    weekly = _aggregate_weekly(ordered[-120:])
    if len(weekly) < 12:
        return None
    daily_support, daily_resistance = _support_resistance(daily, current_price, 20)
    weekly_support, weekly_resistance = _support_resistance(weekly, current_price, 12)
    daily_support_distance = (current_price - daily_support) / daily_support
    weekly_support_distance = (current_price - weekly_support) / weekly_support
    daily_resistance_upside = (daily_resistance - current_price) / current_price
    weekly_resistance_upside = (weekly_resistance - current_price) / current_price
    daily_touch = Decimal("0") <= daily_support_distance <= Decimal("0.03")
    daily_intraday_touch = ordered[-1].low_price <= daily_support * Decimal("1.02") and current_price >= daily_support
    weekly_confirmed = Decimal("0") <= weekly_support_distance <= Decimal("0.12")
    resistance_room = daily_resistance_upside >= Decimal("0.03") and weekly_resistance_upside >= Decimal("0.05")
    support_touched = (daily_touch or daily_intraday_touch) and weekly_confirmed and resistance_room
    bollinger_entry = bollinger_touched and current_price >= daily_support and weekly_confirmed and resistance_room
    eligible = uptrend and not overheating_reasons and (support_touched or bollinger_entry)

    score = Decimal("0")
    if current_price > ma5:
        score += Decimal("25")
    if ma5 > ma20:
        score += Decimal("25")
    if Decimal("50") <= rsi <= Decimal("70"):
        score += Decimal("25")
    elif Decimal("40") <= rsi <= Decimal("75"):
        score += Decimal("15")
    else:
        score += Decimal("5")
    score += min(Decimal("15"), volume_ratio * Decimal("7.5"))
    if Decimal("0") <= change_rate <= Decimal("0.10"):
        score += Decimal("10")
    elif change_rate > Decimal("-0.03"):
        score += Decimal("5")
    if support_touched:
        proximity = max(Decimal("0"), Decimal("1") - daily_support_distance / Decimal("0.03"))
        score += Decimal("10") + proximity * Decimal("10")
    if bollinger_entry:
        score += Decimal("10")

    reasons: list[str] = []
    if current_price > ma5:
        reasons.append("현재가가 5일 이동평균 위")
    if ma5 > ma20:
        reasons.append("5일선이 20일선 위")
    if Decimal("50") <= rsi <= Decimal("70"):
        reasons.append("RSI가 상승 추세 구간")
    if volume_ratio >= Decimal("1.2"):
        reasons.append("거래량이 20일 평균보다 증가")
    if support_touched:
        reasons.insert(0, "일봉·주봉 지지 확인, 저항 여력 확보")
    if bollinger_entry:
        reasons.insert(0, "볼린저 하단 접촉 후 밴드 안 회복")
    if uptrend:
        reasons.insert(0, "20·60일선 상승 추세")
    if not reasons:
        reasons.append("거래대금 상위 종목")

    return {
        "score": int(min(Decimal("100"), score).quantize(Decimal("1"))),
        "ma5": str(ma5.quantize(Decimal("0.01"))),
        "ma20": str(ma20.quantize(Decimal("0.01"))),
        "ma60": str(ma60.quantize(Decimal("0.01"))),
        "bollinger_lower": str(bollinger_lower.quantize(Decimal("0.01"))),
        "bollinger_upper": str(bollinger_upper.quantize(Decimal("0.01"))),
        "bollinger_touched": bollinger_touched,
        "uptrend": uptrend,
        "overheated": bool(overheating_reasons),
        "overheating_reasons": overheating_reasons,
        "return_5d_percent": str((return5 * 100).quantize(Decimal("0.01"))),
        "return_20d_percent": str((return20 * 100).quantize(Decimal("0.01"))),
        "ma20_distance_percent": str((ma20_distance * 100).quantize(Decimal("0.01"))),
        "eligible": eligible,
        "rsi": str(rsi.quantize(Decimal("0.1"))),
        "volume_ratio": str(volume_ratio.quantize(Decimal("0.01"))),
        "support": str(daily_support.quantize(Decimal("0.01"))),
        "resistance": str(daily_resistance.quantize(Decimal("0.01"))),
        "daily_support": str(daily_support.quantize(Decimal("0.01"))),
        "daily_resistance": str(daily_resistance.quantize(Decimal("0.01"))),
        "weekly_support": str(weekly_support.quantize(Decimal("0.01"))),
        "weekly_resistance": str(weekly_resistance.quantize(Decimal("0.01"))),
        "support_distance_percent": str((daily_support_distance * Decimal("100")).quantize(Decimal("0.01"))),
        "weekly_support_distance_percent": str((weekly_support_distance * Decimal("100")).quantize(Decimal("0.01"))),
        "daily_resistance_upside_percent": str((daily_resistance_upside * Decimal("100")).quantize(Decimal("0.01"))),
        "weekly_resistance_upside_percent": str((weekly_resistance_upside * Decimal("100")).quantize(Decimal("0.01"))),
        "support_touched": support_touched,
        "reason": " · ".join(reasons[:4]),
    }
