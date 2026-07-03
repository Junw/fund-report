import unittest

from app.services.llm_utils import loads_json_array
from app.services.portfolio_rules import score_level, score_returns


class PortfolioTest(unittest.TestCase):
    def test_score_returns_flags_momentum_and_overheat(self):
        score, signals = score_returns({"1d": 6, "3d": 4, "7d": 8, "1m": 12})

        self.assertGreaterEqual(score, 70)
        self.assertIn("近1月趋势较强", signals)
        self.assertIn("当日涨幅偏高，谨慎追高", signals)

    def test_score_level_boundaries(self):
        self.assertEqual(score_level(80), "强势")
        self.assertEqual(score_level(65), "偏强")
        self.assertEqual(score_level(45), "中性")
        self.assertEqual(score_level(30), "偏弱")
        self.assertEqual(score_level(29), "高风险")

    def test_loads_json_array_from_fenced_content(self):
        parsed = loads_json_array('```json\n[{"code":"000001","shares":100.5}]\n```')

        self.assertEqual(parsed[0]["code"], "000001")
        self.assertEqual(parsed[0]["shares"], 100.5)


if __name__ == "__main__":
    unittest.main()
