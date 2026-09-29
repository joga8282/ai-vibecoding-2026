"""Daily swing signals. Intraday prices never enter the completed-bar bands."""
from datetime import timedelta, timezone
from decimal import Decimal as D

KST = timezone(timedelta(hours=9))


def swing_signal(candles, price, now):
    daily = sorted((c for c in candles if c.timestamp.astimezone(KST).date() < now.astimezone(KST).date()), key=lambda c: c.timestamp)
    if len(daily) < 20 or now - daily[-1].timestamp > timedelta(days=7):
        return {'eligible': False, 'sell': False, 'rejection_reasons': ['완료 일봉 부족 또는 오래된 데이터']}
    closes = [c.close_price for c in daily]
    if not price.is_finite() or price <= 0 or any(not c.is_finite() or c <= 0 for c in closes):
        return {'eligible': False, 'sell': False, 'rejection_reasons': ['유효하지 않은 가격']}
    ma20, ma60 = sum(closes[-20:]) / 20, sum(closes[-60:]) / 60
    prev20 = sum(closes[-25:-5]) / 20
    std = (sum((c - ma20) ** 2 for c in closes[-20:]) / 20).sqrt()
    lower, upper = ma20 - 2 * std, ma20 + 2 * std
    ma_alignment = len(daily) >= 60 and ma20 > ma60
    ma20_rising = len(daily) >= 25 and ma20 > prev20
    uptrend = ma_alignment and ma20_rising
    # A touch means the current executable price is at/below the lower band.
    # Historical intraday lows alone cannot create a buy after a rebound.
    touched = std > 0 and lower > 0 and price <= lower
    reasons = ([] if uptrend else ['MA20 > MA60 및 20일선 상승 미충족']) + ([] if touched else ['일봉 볼린저 하단 미도달'])
    band_distance = (price / lower - D('1')) * D('100') if lower > 0 else D('999')
    passed = int(ma_alignment) + int(ma20_rising) + int(touched)
    return {'eligible': uptrend and touched, 'sell': std > 0 and price >= upper,
            'uptrend': uptrend, 'bollinger_touched': touched,
            'ma_alignment': ma_alignment, 'ma20_rising': ma20_rising,
            'bollinger_lower': str(lower), 'bollinger_upper': str(upper),
            'ma20': str(ma20), 'ma60': str(ma60), 'price': str(price),
            'band_distance_percent': str(band_distance.quantize(D('0.01'))),
            'conditions_passed': passed, 'conditions_total': 3,
            'signal_at': daily[-1].timestamp.isoformat(), 'rejection_reasons': reasons,
            'score': 100 if uptrend and touched else passed * 30,
            'reason': '등록 장기 테마 · 대형주 · MA20 > MA60 · 20일선 상승 · 일봉 볼린저 하단 도달'}
