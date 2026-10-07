"""Weekly/daily trend context with a four-hour swing entry."""
from datetime import time, timedelta, timezone
from decimal import Decimal as D
from app.models import Candle

KST = timezone(timedelta(hours=9))
ENTRY_BAND_FRACTION = D('0.25')
ENTRY_DAILY_RANGE_FRACTION = D('0.40')
ENTRY_DAILY_LOOKBACK = 20
ENTRY_WEEKLY_LOOKBACK = 20
ENTRY_WEEKLY_RANGE_FRACTION = D('0.40')


def _average(values):
    return sum(values, D(0)) / len(values)


def _completed_daily(candles, now):
    today = now.astimezone(KST).date()
    return sorted((c for c in candles if c.timestamp.astimezone(KST).date() < today), key=lambda c: c.timestamp)


def _completed_weekly(daily, now):
    current_week = now.astimezone(KST).date().isocalendar()[:2]
    grouped = {}
    for candle in daily:
        key = candle.timestamp.astimezone(KST).date().isocalendar()[:2]
        if key < current_week:
            grouped.setdefault(key, []).append(candle)
    return [Candle(items[0].timestamp, items[0].open_price,
                   max(c.high_price for c in items), min(c.low_price for c in items),
                   items[-1].close_price, sum((c.volume for c in items), D(0)), items[-1].currency)
            for _, items in sorted(grouped.items())]


def _four_hour_end(timestamp):
    local = timestamp.astimezone(KST)
    if local.time() < time(13):
        return local.replace(hour=13, minute=0, second=0, microsecond=0)
    return local.replace(hour=15, minute=30, second=0, microsecond=0)


def _completed_four_hour(candles, now):
    return sorted((c for c in candles if _four_hour_end(c.timestamp) <= now.astimezone(KST)), key=lambda c: c.timestamp)


def trend_context(candles, price, now, weekly_candles=None):
    """Return weekly/daily context and daily exit levels without intraday data."""
    daily = _completed_daily(candles, now)
    invalid = (not price.is_finite() or price <= 0 or
               any(not c.close_price.is_finite() or c.close_price <= 0 for c in daily))
    if len(daily) < 60 or invalid or now - daily[-1].timestamp > timedelta(days=7):
        return {'context_eligible': False, 'sell': False, 'rejection_reasons': ['완료 일봉 데이터 부족 또는 지연'],
                'conditions_passed': 0, 'conditions_total': 3}
    closes = [c.close_price for c in daily]
    ma20, ma60 = _average(closes[-20:]), _average(closes[-60:])
    prev20 = _average(closes[-25:-5])
    daily_uptrend = ma20 > ma60 or (price >= ma60 and ma20 > prev20)
    if weekly_candles is None:
        weekly = _completed_weekly(daily, now)
    else:
        current_week = now.astimezone(KST).date().isocalendar()[:2]
        weekly = sorted((c for c in weekly_candles
                         if c.timestamp.astimezone(KST).date().isocalendar()[:2] < current_week),
                        key=lambda c: c.timestamp)
    weekly_closes = [c.close_price for c in weekly]
    if len(weekly_closes) < 10:
        return {'context_eligible': False, 'sell': False, 'rejection_reasons': ['완료 주봉 데이터 부족'],
                'conditions_passed': int(daily_uptrend), 'conditions_total': 3}
    if weekly_candles is not None and len(weekly) < 52:
        return {'context_eligible': False, 'sell': False,
                'rejection_reasons': ['52주 전고점 확인에 필요한 완료 주봉 데이터 부족'],
                'conditions_passed': int(daily_uptrend), 'conditions_total': 3}
    if (len(weekly) < ENTRY_WEEKLY_LOOKBACK or now - weekly[-1].timestamp > timedelta(days=14)
            or any(not c.low_price.is_finite() or not c.high_price.is_finite()
                   or not c.close_price.is_finite() or not 0 < c.low_price <= c.close_price <= c.high_price
                   for c in weekly[-52:])):
        return {'context_eligible': False, 'sell': False,
                'rejection_reasons': ['주봉 매수 구간 확인에 필요한 완료 주봉 부족·지연 또는 유효하지 않은 가격'],
                'weekly_entry_checked': False, 'weekly_bottom_zone': False,
                'conditions_passed': int(daily_uptrend), 'conditions_total': 3}
    weekly_ma10 = _average(weekly_closes[-10:])
    previous_weekly_ma10 = _average(weekly_closes[-11:-1]) if len(weekly_closes) >= 11 else weekly_ma10
    weekly_uptrend = weekly_closes[-1] >= weekly_ma10 or weekly_ma10 > previous_weekly_ma10
    weekly_window = weekly[-52:]
    weekly_peak_high = max(c.high_price for c in weekly_window)
    # A confirmed weekly swing high has two completed weeks on both sides.
    # Find the nearest overhead pivot, which catches recent local resistance
    # even when price is well below the 52-week absolute high.
    swing_highs = [
        weekly_window[index].high_price
        for index in range(2, len(weekly_window) - 2)
        if weekly_window[index].high_price >= max(
            c.high_price for c in weekly_window[index - 2:index] + weekly_window[index + 1:index + 3]
        )
    ]
    overhead_highs = [high for high in [*swing_highs, weekly_peak_high] if high >= price]
    weekly_resistance_high = min(overhead_highs) if overhead_highs else D(0)
    weekly_peak_excluded = (weekly_resistance_high > 0 and
                            price >= weekly_resistance_high * D('0.95'))
    weekly_peak_distance = ((weekly_resistance_high - price) / weekly_resistance_high * D(100)
                            if weekly_resistance_high > 0 else D(100))
    recent_weekly = weekly[-ENTRY_WEEKLY_LOOKBACK:]
    weekly_recent_low = min(c.low_price for c in recent_weekly)
    weekly_recent_high = max(c.high_price for c in recent_weekly)
    weekly_entry_checked = weekly_recent_high > weekly_recent_low
    weekly_entry_ceiling = (weekly_recent_low +
                            (weekly_recent_high - weekly_recent_low) * ENTRY_WEEKLY_RANGE_FRACTION)
    weekly_support_floor = weekly_recent_low * D('0.97')
    weekly_support_broken = weekly_entry_checked and price < weekly_support_floor
    weekly_bottom_zone = weekly_entry_checked and weekly_support_floor <= price <= weekly_entry_ceiling
    weekly_range_position = ((price - weekly_recent_low) / (weekly_recent_high - weekly_recent_low) * D(100)
                             if weekly_entry_checked else D(999))
    std = (_average([(c - ma20) ** 2 for c in closes[-20:]])).sqrt()
    daily_lower, daily_upper = ma20 - D(2) * std, ma20 + D(2) * std
    reasons = []
    if not weekly_uptrend:
        reasons.append('주봉 MA10 상승 추세 미충족')
    if not daily_uptrend:
        reasons.append('일봉 상승 추세 미충족')
    if weekly_peak_excluded:
        reasons.append('최근 주봉 스윙 전고점 5% 이내 고점 구간')
    if not weekly_entry_checked:
        reasons.append('최근 20개 완료 주봉 고저 범위 확인 불가')
    elif weekly_support_broken:
        reasons.append('최근 20개 완료 주봉 저점 3% 하회 · 주봉 지지 이탈')
    elif not weekly_bottom_zone:
        reasons.append('최근 20개 완료 주봉 고저 범위 하위 40% 미충족 · 주봉 고점 구간 제외')
    return {'context_eligible': weekly_uptrend and daily_uptrend and not weekly_peak_excluded and weekly_bottom_zone,
            'sell': std > 0 and price >= daily_upper,
            'weekly_uptrend': weekly_uptrend, 'daily_uptrend': daily_uptrend,
            'ma_alignment': ma20 > ma60, 'ma20_rising': ma20 > prev20,
            'weekly_ma10': str(weekly_ma10), 'ma20': str(ma20), 'ma60': str(ma60),
            'weekly_peak_high': str(weekly_peak_high),
            'weekly_resistance_high': str(weekly_resistance_high),
            'weekly_peak_distance_percent': str(weekly_peak_distance.quantize(D('.01'))),
            'weekly_peak_excluded': weekly_peak_excluded, 'weekly_peak_checked': True,
            'weekly_entry_checked': weekly_entry_checked, 'weekly_bottom_zone': weekly_bottom_zone,
            'weekly_recent_low': str(weekly_recent_low), 'weekly_recent_high': str(weekly_recent_high),
            'weekly_entry_ceiling': str(weekly_entry_ceiling) if weekly_entry_checked else '0',
            'weekly_support_floor': str(weekly_support_floor), 'weekly_support_broken': weekly_support_broken,
            'weekly_range_position_percent': str(weekly_range_position.quantize(D('.01'))),
            'daily_bollinger_lower': str(daily_lower), 'daily_bollinger_upper': str(daily_upper),
            'price': str(price), 'conditions_passed': int(weekly_uptrend and weekly_bottom_zone and not weekly_peak_excluded) + int(daily_uptrend),
            'conditions_total': 3, 'signal_at': daily[-1].timestamp.isoformat(),
            'rejection_reasons': reasons}


def swing_signal(daily_candles, price, now, four_hour_candles=None, weekly_candles=None):
    """Buy in the 4h lower zone and recent daily low range within trend context."""
    # Keep the former three-argument API for integrations that only request daily analysis.
    if four_hour_candles is None:
        daily = _completed_daily(daily_candles, now)
        if len(daily) < 20 or now - daily[-1].timestamp > timedelta(days=7):
            return {'eligible': False, 'sell': False, 'rejection_reasons': ['완료 일봉 데이터 부족 또는 지연']}
        closes = [c.close_price for c in daily]
        if not price.is_finite() or price <= 0 or any(not c.is_finite() or c <= 0 for c in closes):
            return {'eligible': False, 'sell': False, 'rejection_reasons': ['유효하지 않은 가격']}
        ma20 = _average(closes[-20:])
        ma60 = _average(closes[-60:])
        prev20 = _average(closes[-25:-5])
        std = (_average([(c - ma20) ** 2 for c in closes[-20:]])).sqrt()
        lower, upper = ma20 - D(2) * std, ma20 + D(2) * std
        alignment = len(daily) >= 60 and ma20 > ma60
        rising = len(daily) >= 25 and ma20 > prev20
        touched = std > 0 and price <= lower
        passed = int(alignment) + int(rising) + int(touched)
        return {'eligible': alignment and rising and touched, 'sell': std > 0 and price >= upper,
                'uptrend': alignment and rising, 'ma_alignment': alignment, 'ma20_rising': rising,
                'bollinger_touched': touched, 'bollinger_lower': str(lower), 'bollinger_upper': str(upper),
                'ma20': str(ma20), 'ma60': str(ma60), 'price': str(price),
                'band_distance_percent': str(((price / lower - D(1)) * 100).quantize(D('.01'))),
                'conditions_passed': passed, 'conditions_total': 3, 'rejection_reasons': [],
                'score': 100 if alignment and rising and touched else passed * 30}
    context = trend_context(daily_candles, price, now, weekly_candles)
    bars = _completed_four_hour(four_hour_candles or [], now)
    period = 6
    invalid = (not price.is_finite() or price <= 0 or any(
        not c.close_price.is_finite() or c.close_price <= 0 for c in bars[-period:]))
    if len(bars) < period or invalid or now - bars[-1].timestamp > timedelta(days=7):
        reasons = list(context['rejection_reasons']) + ['완료 4시간봉 데이터 부족·지연 또는 유효하지 않은 가격']
        return {**context, 'eligible': False, 'bollinger_touched': False,
                'bollinger_lower': context.get('daily_bollinger_lower', '0'),
                'bollinger_upper': context.get('daily_bollinger_upper', '0'),
                'band_distance_percent': '999', 'rejection_reasons': reasons,
                'score': context.get('conditions_passed', 0) * 30,
                'entry_zone_eligible': False, 'daily_bottom_checked': False,
                'reason': '주봉·일봉 추세 확인 후 4시간봉 바닥권 눌림목 대기'}
    closes = [c.close_price for c in bars[-period:]]
    middle = _average(closes)
    std = (_average([(c - middle) ** 2 for c in closes])).sqrt()
    lower, upper = middle - D(2) * std, middle + D(2) * std
    near_lower = std > 0 and lower > 0 and price <= lower * D('1.03')
    # A price-relative 3% allowance can reach ABOVE the upper band when
    # volatility is low. Cap entry by its position inside the actual band.
    four_hour_ceiling = min(lower * D('1.03'), lower + (upper - lower) * ENTRY_BAND_FRACTION)
    lower_zone = std > 0 and lower > 0 and price <= four_hour_ceiling
    band_position = (price - lower) / (upper - lower) * D(100) if std > 0 else D(999)

    # A short band can follow a rally upward. Check a longer, completed-only
    # daily range as well, so a small dip near recent highs is not a low entry.
    daily_window = _completed_daily(daily_candles, now)[-ENTRY_DAILY_LOOKBACK:]
    daily_checked = len(daily_window) == ENTRY_DAILY_LOOKBACK and all(
        c.low_price.is_finite() and c.high_price.is_finite() and c.close_price.is_finite()
        and 0 < c.low_price <= c.close_price <= c.high_price for c in daily_window)
    recent_low = min(c.low_price for c in daily_window) if daily_checked else D(0)
    recent_high = max(c.high_price for c in daily_window) if daily_checked else D(0)
    daily_checked = daily_checked and recent_high > recent_low
    daily_ceiling = recent_low + (recent_high - recent_low) * ENTRY_DAILY_RANGE_FRACTION
    daily_bottom = daily_checked and price <= daily_ceiling
    daily_position = ((price - recent_low) / (recent_high - recent_low) * D(100)
                      if daily_checked else D(999))
    entry_zone = lower_zone and daily_bottom and context.get('weekly_bottom_zone', False)
    distance = (price / lower - D(1)) * D(100) if lower > 0 else D(999)
    reasons = list(context['rejection_reasons'])
    if not near_lower:
        reasons.append('4시간봉 볼린저 하단 3% 진입 구간 미도달')
    if not lower_zone:
        reasons.append('4시간봉 볼린저 하위 25% 미충족 · 중·상단 추격 매수 제외')
    if not daily_checked:
        reasons.append('최근 20개 완료 일봉 고저 범위 확인 불가')
    elif not daily_bottom:
        reasons.append('최근 20개 완료 일봉 고저 범위 하위 40% 미충족 · 고점 구간 제외')
    passed = context.get('conditions_passed', 0) + int(entry_zone)
    eligible = context.get('context_eligible', False) and entry_zone
    return {**context, 'eligible': eligible, 'bollinger_touched': near_lower,
            'bollinger_lower': str(lower), 'bollinger_upper': str(upper),
            'four_hour_middle': str(middle),
            'four_hour_lower_zone': lower_zone, 'entry_zone_eligible': entry_zone,
            'four_hour_entry_ceiling': str(four_hour_ceiling),
            'four_hour_band_position_percent': str(band_position.quantize(D('.01'))),
            'daily_bottom_checked': daily_checked, 'daily_bottom_zone': daily_bottom,
            'recent_daily_low': str(recent_low), 'recent_daily_high': str(recent_high),
            'daily_range_position_percent': str(daily_position.quantize(D('.01'))),
            'entry_ceiling': str(min(four_hour_ceiling, daily_ceiling, D(context['weekly_entry_ceiling'])))
                             if daily_checked and context.get('weekly_entry_checked') else '0',
            'entry_floor': context.get('weekly_support_floor', '0'),
            'entry_policy': 'pullback-low-zone-v2',
            'band_distance_percent': str(distance.quantize(D('0.01'))),
            'conditions_passed': passed, 'conditions_total': 3,
            'rejection_reasons': reasons, 'score': 100 if eligible else passed * 30,
            'reason': '주봉·일봉 추세 · 최근 20주와 20일 고저 범위 하위 40% · 4시간봉 BB(6,2) 하위 25% 및 하단 3% 이내 매수 / -3% 손절 / 4시간봉 상단 추적 매도'}


def four_hour_exit_signal(candles, price, now):
    """Return a 4h upper-band exit and evidence of an intrabar touch."""
    bars = sorted((c for c in candles if c.timestamp <= now), key=lambda c: c.timestamp)
    completed = _completed_four_hour(bars, now)
    recent = completed[-6:] + [c for c in bars if _four_hour_end(c.timestamp) > now.astimezone(KST)]
    invalid = any(any(not value.is_finite() or value <= 0 for value in (
        c.open_price, c.high_price, c.low_price, c.close_price))
        or c.high_price < max(c.open_price, c.close_price, c.low_price)
        or c.low_price > min(c.open_price, c.close_price, c.high_price) for c in recent)
    if (len(completed) < 6 or not price.is_finite() or price <= 0 or invalid
            or now - completed[-1].timestamp > timedelta(days=7)):
        return {'ready': False, 'upper_touched': False, 'sell_at_upper': False}
    closes = [c.close_price for c in completed[-6:]]
    middle = _average(closes)
    std = (_average([(c - middle) ** 2 for c in closes])).sqrt()
    upper = middle + D(2) * std
    active = [c for c in bars if _four_hour_end(c.timestamp) > now.astimezone(KST)]
    active_high = max([price] + [c.high_price for c in active])
    touched = std > 0 and active_high >= upper
    return {'ready': True, 'upper_touched': touched,
            'sell_at_upper': std > 0 and price >= upper,
            'bollinger_upper': str(upper), 'active_high': str(active_high)}
