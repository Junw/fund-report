from __future__ import annotations

import unittest
from datetime import date

from app.schemas import QuoteRecord
from app.services.calculations import RankingItem
from app.services.report_summary import build_report_summary
from app.services.sector_heat import SectorHeatItem


class ReportSummaryTests(unittest.TestCase):
    def test_summary_includes_loss_rankings(self) -> None:
        trade_date = date(2026, 6, 16)
        rankings = [
            RankingItem("fund", "1d", "gain", 1, "001001", "成长混合A", 5.2, 1.1, None),
            RankingItem("fund", "1d", "loss", 1, "002002", "资源股票A", -4.1, 0.9, None),
        ]
        quotes = [
            QuoteRecord(trade_date, "fund", "001001", "成长混合A"),
            QuoteRecord(trade_date, "fund", "002002", "资源股票A"),
        ]

        summary = build_report_summary(
            trade_date,
            quotes,
            rankings,
            {"advancers": 100, "decliners": 80, "total_amount": 100000000000},
            [],
            [{"title": "AI 算力需求升温"}],
        )

        self.assertEqual(summary["top"]["fund"][0]["code"], "001001")
        self.assertEqual(summary["bottom"]["fund"][0]["code"], "002002")
        self.assertEqual(summary["bottom"]["fund"][0]["value"], -4.1)
        self.assertEqual(summary["news"][0]["title"], "AI 算力需求升温")
        self.assertTrue(any("概念题材当天数据为空" in warning for warning in summary["warnings"]))
        self.assertEqual(summary["data_quality"]["status"], "partial")

    def test_summary_exposes_industry_and_concept_heat(self) -> None:
        trade_date = date(2026, 6, 30)
        heat = [
            SectorHeatItem(trade_date, "industry", "BK001", "半导体", 5.0, 10.0, 91.0, 1, "hot", "ok"),
            SectorHeatItem(trade_date, "concept", "emchg:CPO", "CPO概念", None, None, 86.0, 1, "hot", "partial"),
        ]

        summary = build_report_summary(
            trade_date,
            [
                QuoteRecord(trade_date, "fund", "001001", "成长混合A"),
                QuoteRecord(trade_date, "etf", "510300", "沪深300ETF"),
                QuoteRecord(trade_date, "industry", "BK001", "半导体"),
                QuoteRecord(trade_date, "concept", "emchg:CPO", "CPO概念"),
            ],
            [],
            {"advancers": 3000, "decliners": 2000, "total_amount": 1000000000000},
            [],
            sector_heat=heat,
        )

        self.assertEqual(summary["sector_heat_by_asset"]["industry"]["top"][0]["action_label"], "趋势确认")
        self.assertEqual(summary["sector_heat_by_asset"]["concept"]["status"], "partial")
        self.assertEqual(summary["sector_heat_by_asset"]["concept"]["top"][0]["action_label"], "当日异动")


if __name__ == "__main__":
    unittest.main()
