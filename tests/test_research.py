from __future__ import annotations

import unittest
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Asset, DailyQuote, FundMetadata, MarketMetric, SectorHeat
from app.services.backtest import _forward_return
from app.services.signals import compute_signal_scores


class ResearchSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def test_signal_is_experimental_only_with_enough_factor_coverage(self) -> None:
        end = date(2026, 6, 22)
        self.session.add_all([
            Asset(asset_type="fund", code="000001", name="半导体成长混合A", category="混合型", family_key="半导体成长混合", share_class="A", is_primary=1),
            Asset(asset_type="fund", code="000002", name="半导体成长混合E", category="混合型", family_key="半导体成长混合", share_class="E", is_primary=0),
            FundMetadata(code="000001", fund_size=5_000_000_000, inception_date=date(2020, 1, 1), management_fee=1.2, data_date=end),
            SectorHeat(trade_date=end, asset_type="industry", code="ths:半导体", name="半导体", return_7d=5, return_1m=10, heat_score=80, heat_rank=1, heat_level="hot", data_status="ok"),
            MarketMetric(trade_date=end, metric="advancers", value=3000),
            MarketMetric(trade_date=end, metric="decliners", value=2000),
        ])
        for offset in range(140):
            day = end - timedelta(days=139 - offset)
            self.session.add(DailyQuote(trade_date=day, asset_type="fund", code="000001", name="半导体成长混合A", close=1 + offset * 0.002, change_pct=0.2))
            self.session.add(DailyQuote(trade_date=day, asset_type="fund", code="000002", name="半导体成长混合E", close=1 + offset * 0.002, change_pct=0.2))
        self.session.commit()
        rows = compute_signal_scores(self.session, end, [{"title": "政策支持半导体产业增长", "summary": "半导体规划"}])
        self.assertEqual([row["code"] for row in rows], ["000001"])
        self.assertEqual(rows[0]["status"], "experimental")
        self.assertGreaterEqual(rows[0]["confidence"], 70)

    def test_forward_return_requires_complete_future_window(self) -> None:
        rows = [(date(2026, 1, 1) + timedelta(days=index), 1 + index / 100) for index in range(10)]
        self.assertIsNone(_forward_return(rows, date(2026, 1, 5), 6))
        self.assertIsNotNone(_forward_return(rows, date(2026, 1, 1), 5))


if __name__ == "__main__":
    unittest.main()