import json
from pathlib import Path
from decimal import Decimal

THEMES_PATH = Path(__file__).with_name('swing_themes.json')


def market_cap_label(minimum):
    unit, suffix = (Decimal('1e12'), '조 원') if minimum >= Decimal('1e12') else (Decimal('1e8'), '억 원')
    amount = format(minimum / unit, ',f')
    if '.' in amount:
        amount = amount.rstrip('0').rstrip('.')
    return amount + suffix


def load_universe():
    data = json.loads(THEMES_PATH.read_text(encoding='utf-8'))
    minimum = Decimal(data['minimum_market_cap_krw'])
    if not minimum.is_finite() or minimum <= 0:
        raise ValueError('대형주 시가총액 기준은 양수여야 합니다.')
    symbols = {}
    for group in data['groups']:
        if group.get('enabled') is not True:
            continue
        for symbol in group['symbols']:
            if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit():
                raise ValueError('테마 종목 코드는 6자리 숫자여야 합니다.')
            symbols.setdefault(symbol, []).append(group['name'])
    return minimum, symbols


def membership(stock, price, minimum, themes):
    if not stock or stock.get('symbol') not in themes:
        return False
    shares = Decimal(stock.get('sharesOutstanding') or '0')
    return (stock.get('securityType') == 'STOCK' and stock.get('isCommonShare') is True
            and shares.is_finite() and shares > 0 and price.is_finite() and price > 0
            and price * shares >= minimum)
