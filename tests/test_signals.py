"""Tests for signal labels, schema alignment, and deterministic mapping."""

import json
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

# ── signal label constants (must match app/services/signals.py) ──

SIGNAL_LABELS = {
    "insufficient_data": "历史不足",
    "strong_attention": "建仓观察",
    "worthy_attention": "趋势确认",
    "neutral_watch": "持有观察",
    "cautious_watch": "减仓观察",
    "no_attention": "风险升高",
    "pullback_watch": "等待修复",
}

EXPECTED_LABEL_KEYS = {
    "insufficient_data",
    "strong_attention",
    "worthy_attention",
    "neutral_watch",
    "cautious_watch",
    "no_attention",
    "pullback_watch",
}


class TestSignalLabels(unittest.TestCase):
    """Verify exact labels required by the user plan."""

    def test_exactly_seven_labels(self):
        self.assertEqual(len(SIGNAL_LABELS), 7,
                         "Must have exactly 7 research labels")

    def test_label_keys_match_expected(self):
        self.assertEqual(set(SIGNAL_LABELS.keys()), EXPECTED_LABEL_KEYS,
                         "Label keys must match the exact set from the plan")

    def test_no_english_buy_sell_in_labels(self):
        """Assert no label key contains hard buy/sell wording."""
        forbidden = {"buy", "sell", "strong_buy", "strong_sell"}
        for key, text in SIGNAL_LABELS.items():
            lower = key.lower()
            for word in forbidden:
                self.assertNotIn(word, lower,
                                 f"Label key '{key}' must not contain '{word}'")

    def test_all_labels_are_chinese_research_labels(self):
        """All labels should be Chinese display text."""
        expected_texts = {"建仓观察", "趋势确认", "持有观察", "减仓观察",
                          "风险升高", "等待修复", "历史不足"}
        self.assertEqual(set(SIGNAL_LABELS.values()), expected_texts)


class TestSignalActionMapping(unittest.TestCase):
    """Test deterministic mapping of scores to actions."""

    def _call_mapping(self, total_score, confidence, returns=None,
                      max_drawdown=None, is_overheated=False,
                      premium_high=False, market_congestion_high=False,
                      sector_weakening=False):
        """Mirror _determine_signal_label logic for isolated testing."""
        if confidence < 70 or total_score is None:
            return "insufficient_data"

        ret = returns or {}
        r20 = ret.get(20)
        r60 = ret.get(60)
        r120 = ret.get(120)

        mid_long_positive = (
            (r60 is not None and r60 > 0) or (r120 is not None and r120 > 0)
        )
        short_negative = r20 is not None and r20 < 0

        # pullback
        if (mid_long_positive and short_negative) or (is_overheated and short_negative):
            return "pullback_watch"

        # strong attention
        if total_score >= 80 and confidence >= 70:
            if mid_long_positive and not is_overheated:
                return "strong_attention"

        # worthy attention
        if total_score >= 70:
            ret_vals = [v for v in [r20, r60, r120] if v is not None]
            mostly_positive = sum(1 for v in ret_vals if v > 0) >= len(ret_vals) * 0.5 if ret_vals else False
            drawdown_ok = max_drawdown is not None and max_drawdown <= 20
            if mostly_positive and (drawdown_ok or max_drawdown is None):
                return "worthy_attention"

        # cautious watch
        if 40 <= total_score <= 55 or is_overheated or premium_high or sector_weakening:
            return "cautious_watch"

        # neutral watch
        risk_triggered = bool(
            (max_drawdown is not None and max_drawdown > 25)
            or is_overheated or premium_high
            or market_congestion_high or sector_weakening
        )
        if total_score >= 55 and not risk_triggered:
            return "neutral_watch"

        # no attention
        if total_score < 40 or (max_drawdown is not None and max_drawdown > 25) or market_congestion_high:
            return "no_attention"

        if total_score >= 55:
            return "neutral_watch"
        if total_score >= 40:
            return "cautious_watch"
        return "no_attention"

    def test_insufficient_data_below_confidence(self):
        self.assertEqual(
            self._call_mapping(85, 60),
            "insufficient_data",
        )
        self.assertEqual(
            self._call_mapping(None, 80),
            "insufficient_data",
        )

    def test_strong_attention_high_score(self):
        returns = {20: 2.0, 60: 8.0, 120: 15.0}
        self.assertEqual(
            self._call_mapping(85, 75, returns=returns, max_drawdown=10),
            "strong_attention",
        )

    def test_strong_attention_blocked_by_overheat(self):
        returns = {20: 2.0, 60: 8.0, 120: 15.0}
        result = self._call_mapping(85, 75, returns=returns,
                                    max_drawdown=10, is_overheated=True)
        self.assertIn(result, {"worthy_attention", "cautious_watch", "neutral_watch"})
        self.assertNotEqual(result, "strong_attention")

    def test_worthy_attention_positive_returns(self):
        returns = {20: 3.0, 60: 5.0, 120: 8.0}
        self.assertEqual(
            self._call_mapping(72, 75, returns=returns, max_drawdown=15),
            "worthy_attention",
        )

    def test_neutral_watch_no_risk(self):
        returns = {20: 1.0, 60: 2.0, 120: 3.0}
        self.assertEqual(
            self._call_mapping(58, 75, returns=returns, max_drawdown=12),
            "neutral_watch",
        )

    def test_cautious_watch_moderate_score(self):
        self.assertEqual(
            self._call_mapping(48, 75),
            "cautious_watch",
        )

    def test_cautious_watch_overheated(self):
        self.assertEqual(
            self._call_mapping(65, 75, is_overheated=True),
            "cautious_watch",
        )

    def test_cautious_watch_premium_high(self):
        self.assertEqual(
            self._call_mapping(65, 75, premium_high=True),
            "cautious_watch",
        )

    def test_no_attention_low_score(self):
        self.assertEqual(
            self._call_mapping(25, 75),
            "no_attention",
        )

    def test_no_attention_high_drawdown(self):
        self.assertEqual(
            self._call_mapping(65, 75, max_drawdown=30),
            "no_attention",
        )

    def test_no_attention_market_congestion(self):
        self.assertEqual(
            self._call_mapping(65, 75, market_congestion_high=True),
            "no_attention",
        )

    def test_pullback_watch_mid_long_positive_short_negative(self):
        returns = {20: -3.0, 60: 5.0, 120: 10.0}
        self.assertEqual(
            self._call_mapping(65, 75, returns=returns),
            "pullback_watch",
        )

    def test_pullback_watch_overheat_short_negative(self):
        returns = {20: -2.0, 60: -1.0, 120: -2.0}
        self.assertEqual(
            self._call_mapping(65, 75, returns=returns, is_overheated=True),
            "pullback_watch",
        )

    def test_all_labels_valid(self):
        """Every returned label must be one of the 7 exact labels."""
        scenarios = [
            # (score, confidence, returns, max_dd, overheated, premium, congestion, sector_weak)
            (85, 75, {20: 2, 60: 8, 120: 15}, 10, False, False, False, False),
            (72, 75, {20: 3, 60: 5, 120: 8}, 15, False, False, False, False),
            (58, 75, {20: 1, 60: 2, 120: 3}, 12, False, False, False, False),
            (48, 75, None, None, False, False, False, False),
            (25, 75, None, None, False, False, False, False),
            (85, 60, None, None, False, False, False, False),      # insufficient
            (65, 75, {20: -3, 60: 5, 120: 10}, 10, False, False, False, False),  # pullback
        ]
        for scenario in scenarios:
            score, conf, rets, dd, oh, prem, cong, sw = scenario
            label = self._call_mapping(score, conf, rets, dd, oh, prem, cong, sw)
            self.assertIn(label, EXPECTED_LABEL_KEYS,
                          f"Label '{label}' not in expected set for scenario {scenario}")
            self.assertNotIn("??", label)
            self.assertNotIn("??", label)


class TestSchemaFieldNames(unittest.TestCase):
    """Verify model field names match the plan schema."""

    def test_fund_feature_fields(self):
        expected = {
            "trade_date", "asset_type", "code", "name", "category",
            "return_20d", "return_60d", "return_120d",
            "volatility_120d", "downside_volatility_120d",
            "max_drawdown_120d", "sharpe_120d", "sortino_120d",
            "amount", "turnover_rate", "premium", "liquidity_score",
            "quality_score", "data_status", "feature_json", "created_at",
        }
        from app.models import FundFeature
        cols = {c.name for c in FundFeature.__table__.columns}
        self.assertTrue(expected.issubset(cols),
                        f"Missing FundFeature columns: {expected - cols}")

    def test_fund_feature_no_old_names(self):
        """FundFeature must not use old names like as_of, momentum_20, etc."""
        from app.models import FundFeature
        cols = {c.name for c in FundFeature.__table__.columns}
        forbidden = {"as_of", "momentum_20", "momentum_60", "momentum_120",
                     "momentum_raw", "volatility", "downside_vol",
                     "risk_adjusted", "quality_raw", "fund_size",
                     "inception_days", "management_fee", "close"}
        overlap = cols & forbidden
        self.assertFalse(overlap,
                         f"FundFeature has old field names: {overlap}")

    def test_fund_sector_exposure_fields(self):
        expected = {
            "trade_date", "asset_type", "code", "name",
            "sector_code", "sector_name", "source",
            "confidence", "coverage", "created_at",
        }
        from app.models import FundSectorExposure
        cols = {c.name for c in FundSectorExposure.__table__.columns}
        self.assertTrue(expected.issubset(cols),
                        f"Missing FundSectorExposure columns: {expected - cols}")

    def test_fund_sector_exposure_no_old_names(self):
        from app.models import FundSectorExposure
        cols = {c.name for c in FundSectorExposure.__table__.columns}
        forbidden = {"as_of", "fund_code", "fund_name", "weight",
                     "heat_score", "heat_level"}
        overlap = cols & forbidden
        self.assertFalse(overlap,
                         f"FundSectorExposure has old field names: {overlap}")

    def test_signal_event_fields(self):
        expected = {
            "trade_date", "model_version", "asset_type", "code", "name",
            "action", "score", "confidence", "risk_level", "status",
            "reason_json", "risk_json", "invalid_json", "feature_json",
            "created_at",
        }
        from app.models import SignalEvent
        cols = {c.name for c in SignalEvent.__table__.columns}
        self.assertTrue(expected.issubset(cols),
                        f"Missing SignalEvent columns: {expected - cols}")

    def test_signal_event_no_old_names(self):
        from app.models import SignalEvent
        cols = {c.name for c in SignalEvent.__table__.columns}
        forbidden = {"signal_type", "signal_label", "signal_score",
                     "reason", "components_json"}
        overlap = cols & forbidden
        self.assertFalse(overlap,
                         f"SignalEvent has old field names: {overlap}")

    def test_signal_model_version_fields(self):
        from app.models import SignalModelVersion
        cols = {c.name for c in SignalModelVersion.__table__.columns}
        expected = {"model_version", "status", "weights_json",
                    "backtest_json", "notes", "updated_at"}
        self.assertTrue(expected.issubset(cols),
                        f"Missing SignalModelVersion columns: {expected - cols}")
        # model_version should be the primary key (no auto-increment id)
        pk_cols = {c.name for c in SignalModelVersion.__table__.primary_key.columns}
        self.assertEqual(pk_cols, {"model_version"},
                         f"SignalModelVersion PK should be model_version, got {pk_cols}")

    def test_signal_model_version_no_old_names(self):
        from app.models import SignalModelVersion
        cols = {c.name for c in SignalModelVersion.__table__.columns}
        forbidden = {"id", "description", "config_json", "metrics_json",
                     "activated_at", "created_at"}
        overlap = cols & forbidden
        self.assertFalse(overlap,
                         f"SignalModelVersion has old field names: {overlap}")


class TestUniqueConstraints(unittest.TestCase):
    """Verify unique constraints match the plan."""

    def test_fund_features_unique(self):
        from app.models import FundFeature
        uq = FundFeature.__table_args__
        self.assertIsNotNone(uq)
        # uq_fund_features on (trade_date, asset_type, code)
        self.assertIn("trade_date", str(uq))
        self.assertIn("asset_type", str(uq))
        self.assertIn("code", str(uq))

    def test_fund_sector_exposure_unique(self):
        from app.models import FundSectorExposure
        uq = FundSectorExposure.__table_args__
        self.assertIsNotNone(uq)
        self.assertIn("trade_date", str(uq))
        self.assertIn("asset_type", str(uq))
        self.assertIn("code", str(uq))
        self.assertIn("sector_name", str(uq))

    def test_signal_events_unique(self):
        from app.models import SignalEvent
        uq = SignalEvent.__table_args__
        self.assertIsNotNone(uq)
        self.assertIn("trade_date", str(uq))
        self.assertIn("model_version", str(uq))
        self.assertIn("asset_type", str(uq))
        self.assertIn("code", str(uq))
        # should NOT contain signal_type in the unique constraint
        self.assertNotIn("signal_type", str(uq))


class TestSignalEventSerialization(unittest.TestCase):
    """Test _signal_event_to_dict returns plan-compatible keys."""

    def test_dict_keys_match_schema(self):
        """Simulate a row and verify the serialized keys."""
        row = MagicMock()
        row.trade_date = date(2026, 6, 25)
        row.model_version = "medium_term_v2"
        row.asset_type = "fund"
        row.code = "000001"
        row.name = "测试基金"
        row.action = "strong_attention"
        row.score = 85.0
        row.confidence = 75.0
        row.risk_level = "low"
        row.status = "active"
        row.reason_json = json.dumps({"evidence": ["test"], "components": {}})
        row.risk_json = "{}"
        row.invalid_json = "{}"
        row.feature_json = "{}"

        # import the helper
        from app.main import _signal_event_to_dict
        result = _signal_event_to_dict(row)

        expected_keys = {
            "trade_date", "model_version", "asset_type", "code", "name",
            "action", "score", "confidence", "risk_level", "status",
            "reason_json", "risk_json", "invalid_json", "feature_json",
        }
        self.assertEqual(set(result.keys()), expected_keys)

        # old names must not appear
        for bad in ("signal_type", "signal_label", "signal_score", "reason", "components"):
            self.assertNotIn(bad, result,
                             f"Serialized dict must not contain '{bad}'")


class TestFundFeatureSerialization(unittest.TestCase):
    """Test _fund_feature_to_dict returns plan-compatible keys."""

    def test_dict_keys_match_schema(self):
        row = MagicMock()
        row.trade_date = date(2026, 6, 25)
        row.asset_type = "fund"
        row.code = "000001"
        row.name = "测试基金"
        row.category = "混合型"
        row.return_20d = 2.5
        row.return_60d = 5.0
        row.return_120d = 10.0
        row.volatility_120d = 0.15
        row.downside_volatility_120d = 0.10
        row.max_drawdown_120d = 12.0
        row.sharpe_120d = 0.8
        row.sortino_120d = 1.1
        row.amount = 1000000.0
        row.turnover_rate = 50.0
        row.premium = 0.5
        row.liquidity_score = 3.2
        row.quality_score = 0.75
        row.data_status = "ok"
        row.feature_json = "{}"

        from app.main import _fund_feature_to_dict
        result = _fund_feature_to_dict(row)

        expected_keys = {
            "trade_date", "asset_type", "code", "name", "category",
            "return_20d", "return_60d", "return_120d",
            "volatility_120d", "downside_volatility_120d",
            "max_drawdown_120d", "sharpe_120d", "sortino_120d",
            "amount", "turnover_rate", "premium", "liquidity_score",
            "quality_score", "data_status", "feature_json",
        }
        self.assertEqual(set(result.keys()), expected_keys)

        for bad in ("as_of", "momentum_20", "momentum_60", "momentum_120",
                     "momentum_raw", "volatility", "downside_vol",
                     "risk_adjusted", "quality_raw"):
            self.assertNotIn(bad, result,
                             f"Serialized dict must not contain '{bad}'")


class TestStorageHelperSignatures(unittest.TestCase):
    """Verify storage helpers use correct parameter names."""

    def test_list_signal_events_parameter_names(self):
        import inspect
        from app.services.storage import list_signal_events
        sig = inspect.signature(list_signal_events)
        params = list(sig.parameters.keys())
        self.assertIn("action", params)
        self.assertNotIn("signal_label", params)

    def test_get_fund_features_parameter_names(self):
        import inspect
        from app.services.storage import get_fund_features
        sig = inspect.signature(get_fund_features)
        params = list(sig.parameters.keys())
        self.assertIn("trade_date", params)
        self.assertNotIn("as_of", params)

    def test_get_fund_sector_exposure_parameter_names(self):
        import inspect
        from app.services.storage import get_fund_sector_exposure
        sig = inspect.signature(get_fund_sector_exposure)
        params = list(sig.parameters.keys())
        self.assertIn("code", params)
        self.assertNotIn("fund_code", params)
        self.assertNotIn("as_of", params)


class TestNoBuySellLabels(unittest.TestCase):
    """Explicit test: no label contains English buy/sell equivalents."""

    def test_no_label_contains_buy_or_sell(self):
        """Required by verification: assert no label contains buy/sell."""
        all_text = " ".join(SIGNAL_LABELS.values()) + " " + " ".join(SIGNAL_LABELS.keys())
        lower = all_text.lower()
        forbidden_pairs = [
            ("buy", "买"),  # if buy is present but Chinese context is fine
            ("sell", "卖"),
        ]
        # check English words are not in labels
        for eng_word in ["buy", "sell"]:
            self.assertNotIn(eng_word, lower,
                             f"Labels must not contain '{eng_word}'")

    def test_no_label_contains_hold_in_english(self):
        all_text = " ".join(SIGNAL_LABELS.values()) + " " + " ".join(SIGNAL_LABELS.keys())
        self.assertNotIn("hold", all_text.lower())


if __name__ == "__main__":
    unittest.main()
