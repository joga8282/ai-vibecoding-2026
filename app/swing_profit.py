"""Expected exit proceeds; estimates never replace broker execution accounting."""
from decimal import Decimal as D

from app.models import Currency


def estimate_exit_profit(position, reference_price, settings):
    fee = settings.fee_rate
    tax = settings.live_sell_tax_rate if settings.mode == 'live' and position.currency is Currency.KRW else D(0)
    slip = settings.slippage_bps / D(10000)
    minimum = settings.swing_min_net_profit_percent
    values = (position.average_price, position.quantity, reference_price, fee, tax, slip, minimum)
    if (any(value is None or not value.is_finite() for value in values)
            or position.average_price <= 0 or position.quantity <= 0 or reference_price <= 0
            or not D(0) <= fee < 1 or not D(0) <= tax < 1 or fee + tax >= 1
            or not D(0) <= slip < 1 or not D(0) <= minimum <= 100):
        return {'ready': False, 'meets_minimum': False, 'net_return_percent': None}
    # Average price already includes the buy fill's slippage, but excludes fees.
    # Reserve configured buy and sell fees even when an old fill reports zero.
    cost = position.average_price * position.quantity * (1 + fee)
    sale = reference_price * (1 - slip) * position.quantity
    sell_fee, sell_tax = sale * fee, sale * tax
    proceeds = sale - sell_fee - sell_tax
    required = cost * (1 + minimum / D(100))
    return {'ready': True, 'meets_minimum': proceeds >= required,
            'net_return_percent': (proceeds - cost) / cost * 100,
            'purchase_cost': cost, 'net_proceeds': proceeds,
            'estimated_sell_fee': sell_fee, 'estimated_sell_tax': sell_tax,
            'minimum_reference_price': required / (position.quantity * (1 - slip) * (1 - fee - tax))}
