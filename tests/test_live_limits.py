"""Full equity allocation still requires current recommendations and cash."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import TestCase
from unittest.mock import patch

from app.config import Settings
from app.models import Side
from app.risk import LiveRiskManager


class LiveLimitConfigTest(TestCase):
    def settings(self, **changes):
        env = {'TRADER_MODE': 'live', 'LIVE_TRADING_ENABLED': 'true', 'TOSS_ACCOUNT_SEQ': 'mock',
               'API_ACCESS_TOKEN': 'mock', 'RECOMMENDED_TRADE_RATIO': '1',
               'LIVE_MAX_TOTAL_EXPOSURE_RATIO': '1', 'LIVE_MAX_ORDER_AMOUNT_KRW': '0',
               'LIVE_MAX_TOTAL_EXPOSURE_KRW': '0', 'LIVE_SYMBOL_POLICY': 'recommended'}
        env.update(changes)
        with patch.dict(os.environ, env, clear=True), patch('app.config.load_dotenv'):
            return Settings.from_env()

    def test_full_equity_and_recommendation_policy_are_explicit_config_options(self):
        settings = self.settings()
        self.assertEqual(settings.live_max_total_exposure_ratio, D(1))
        self.assertEqual(settings.recommended_trade_ratio, D(1))
        self.assertEqual(settings.live_max_order_amount_krw, D(0))
        self.assertEqual(settings.live_max_total_exposure_krw, D(0))
        self.assertEqual(settings.live_symbol_policy, 'recommended')
        self.assertFalse(settings.live_allowed_symbols)
        self.assertEqual(settings.live_auto_max_total_exposure_ratio, D('.15'))
        self.assertEqual(settings.manual_trade_ratio, D('.15'))
        self.assertEqual(settings.swing_min_net_profit_percent, D(3))
        self.assertEqual(settings.live_sell_tax_rate, D('.002'))

    def test_invalid_limits_and_unknown_symbol_policy_are_rejected(self):
        for changes in ({'LIVE_MAX_TOTAL_EXPOSURE_RATIO': '1.01'},
                        {'LIVE_MAX_TOTAL_EXPOSURE_RATIO': '0'},
                        {'LIVE_MAX_ORDER_AMOUNT_KRW': '-1'},
                        {'LIVE_MAX_TOTAL_EXPOSURE_KRW': 'NaN'},
                        {'LIVE_MAX_DAILY_LOSS_KRW': '0'},
                        {'LIVE_SYMBOL_POLICY': 'all'},
                        {'RECOMMENDED_TRADE_RATIO': 'NaN'},
                        {'LIVE_AUTO_MAX_TOTAL_EXPOSURE_RATIO': '1.01'},
                        {'LIVE_AUTO_MAX_TOTAL_EXPOSURE_RATIO': 'NaN'},
                        {'MANUAL_TRADE_RATIO': '0'},
                        {'MANUAL_TRADE_RATIO': 'Infinity'},
                        {'SWING_MIN_NET_PROFIT_PERCENT': '-1'},
                        {'SWING_MIN_NET_PROFIT_PERCENT': 'NaN'},
                        {'SWING_MIN_NET_PROFIT_PERCENT': 'abc'},
                        {'LIVE_SELL_TAX_RATE': '-0.01'},
                        {'LIVE_SELL_TAX_RATE': '1'},
                        {'LIVE_SELL_TAX_RATE': 'Infinity'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.settings(**changes)

    def test_legacy_allowlist_requires_explicit_symbols(self):
        with self.assertRaisesRegex(RuntimeError, 'allowlist'):
            self.settings(LIVE_SYMBOL_POLICY='allowlist')

    def test_ten_percent_profit_target_loads_without_changing_other_limits(self):
        settings = self.settings(SWING_MIN_NET_PROFIT_PERCENT='10')
        self.assertEqual(settings.swing_min_net_profit_percent, D(10))
        self.assertEqual(settings.live_auto_max_total_exposure_ratio, D('.15'))
        self.assertEqual(settings.manual_trade_ratio, D('.15'))
        self.assertEqual(settings.live_sell_tax_rate, D('.002'))


class FullEquityRiskTest(TestCase):
    def setUp(self):
        self.settings = Settings(mode='live', live_trading_enabled=True, live_symbol_policy='recommended',
                                 live_max_total_exposure_ratio=D(1), live_max_order_amount_krw=D(0),
                                 live_max_total_exposure_krw=D(0))
        self.risk = LiveRiskManager(self.settings)
        self.risk.armed = self.risk.reconciled = True
        self.risk.set_recommended_symbols(['082740'])
        self.args = dict(symbol='082740', side=Side.BUY, quantity=D(1), price=D(4000000),
                         total_exposure=D(1000000), current_equity=D(5000000), position_count=1,
                         quote_at=datetime.now(timezone.utc))

    def test_one_hundred_percent_boundary_is_allowed_but_leverage_is_blocked(self):
        self.assertTrue(self.risk.validate(**self.args).allowed)
        self.assertEqual(self.risk.validate(**{**self.args, 'price': D(4000001)}).rule, 'max-equity-exposure')

    def test_zero_fixed_caps_do_not_remove_recommendation_requirement(self):
        self.assertEqual(self.risk.validate(**{**self.args, 'symbol': '012450'}).rule, 'recommendation')
        self.risk.recommendations_at -= timedelta(minutes=6)
        self.assertEqual(self.risk.validate(**self.args).rule, 'recommendation')

    def test_position_exits_do_not_require_a_new_buy_recommendation(self):
        self.risk.clear_recommended_symbols()
        self.assertTrue(self.risk.validate(**{**self.args, 'side': Side.SELL}).allowed)

    def test_quote_loss_and_position_controls_still_block_buys(self):
        self.assertEqual(self.risk.validate(**{**self.args, 'quote_at': datetime.now(timezone.utc) - timedelta(seconds=11)}).rule, 'stale-quote')
        self.assertEqual(self.risk.validate(**{**self.args, 'position_count': 5}).rule, 'max-positions')
        self.risk.daily_loss = self.settings.live_max_daily_loss_krw
        self.assertEqual(self.risk.validate(**self.args).rule, 'daily-loss')
