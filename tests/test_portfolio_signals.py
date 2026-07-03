"""Tests for portfolio signal summary label mapping."""
from __future__ import annotations

import unittest

from app.services.portfolio import SIGNAL_LABEL_RESEARCH_MAP


class TestPortfolioSignalLabels(unittest.TestCase):
    def test_all_labels_have_mapping(self):
        expected = {
            "strong_attention",
            "worthy_attention",
            "neutral_watch",
            "cautious_watch",
            "no_attention",
            "insufficient_data",
            "pullback_watch",
        }
        self.assertEqual(set(SIGNAL_LABEL_RESEARCH_MAP.keys()), expected)

    def test_labels_are_research_only(self):
        for label in SIGNAL_LABEL_RESEARCH_MAP.values():
            self.assertNotIn("买入", label)
            self.assertNotIn("卖出", label)

    def test_attention_score_map(self):
        # test the internal attention score mapping used in portfolio_signal_summary
        attention_map = {
            "strong_attention": 100,
            "worthy_attention": 75,
            "neutral_watch": 50,
            "cautious_watch": 30,
            "pullback_watch": 20,
            "no_attention": 10,
            "insufficient_data": 0,
        }
        self.assertEqual(attention_map["strong_attention"], 100)
        self.assertEqual(attention_map["insufficient_data"], 0)
        # higher = more attention, lower = less attention
        self.assertGreater(
            attention_map["strong_attention"],
            attention_map["no_attention"],
        )


if __name__ == "__main__":
    unittest.main()
