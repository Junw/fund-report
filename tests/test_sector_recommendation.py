from __future__ import annotations

import json
import unittest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Asset, DailyQuote
from app.services.sector_recommendation import SectorRecommendationEngine, recommendation_level, sector_terms


class SectorRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add_all(
            [
                Asset(asset_type="etf", code="512480", name="半导体ETF", category="ETF"),
                Asset(asset_type="fund", code="000001", name="芯片主题混合A", category="混合型"),
                Asset(asset_type="fund", code="000002", name="芯片主题混合C", category="混合型"),
                DailyQuote(
                    trade_date=date(2026, 6, 18),
                    asset_type="etf",
                    code="512480",
                    name="半导体ETF",
                    close=1.2,
                    change_pct=2.1,
                    extra_json=json.dumps({"premium": 0.3}),
                ),
            ]
        )
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()

    def test_related_funds_excludes_c_share_and_loads_etf_premium(self) -> None:
        service = SectorRecommendationEngine(self.session, date(2026, 6, 18))
        rows = service.related_funds("半导体")
        self.assertEqual([row["code"] for row in rows], ["512480", "000001"])
        self.assertEqual(rows[0]["premium"], 0.3)

    def test_score_has_explainable_components(self) -> None:
        service = SectorRecommendationEngine(
            self.session,
            date(2026, 6, 18),
            [{"title": "政策支持半导体产业加快突破", "summary": ""}],
        )
        result = service.score({"name": "半导体", "heat_score": 90, "return_7d": 8, "return_1m": 16})
        self.assertGreaterEqual(result["score"], 70)
        self.assertEqual(result["related_count"], 2)
        self.assertEqual(result["premium_score"], 15)
        self.assertTrue(result["matched_news"])

    def test_alias_and_level_boundaries(self) -> None:
        self.assertIn("芯片", sector_terms("半导体行业"))
        self.assertEqual(recommendation_level(80), "较高")
        self.assertEqual(recommendation_level(65), "偏高")
        self.assertEqual(recommendation_level(50), "中性")
        self.assertEqual(recommendation_level(35), "偏低")


if __name__ == "__main__":
    unittest.main()
