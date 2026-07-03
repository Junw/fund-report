from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.schemas import QuoteRecord
from app.services.sector_heat import calculate_sector_heat, heat_action_label, heat_level, heat_status_label


class SectorHeatTests(unittest.TestCase):
    def test_heat_level_boundaries(self) -> None:
        self.assertEqual(heat_level(80), "hot")
        self.assertEqual(heat_level(60), "warming")
        self.assertEqual(heat_level(40), "neutral")
        self.assertEqual(heat_level(20), "cooling")
        self.assertEqual(heat_level(19.99), "cold")
        self.assertIsNone(heat_level(None))

    def test_history_insufficient(self) -> None:
        as_of = date(2026, 6, 17)
        quotes = [QuoteRecord(as_of, "industry", "ths:电池", "电池", close=100)]

        rows = calculate_sector_heat(quotes, "industry", as_of)

        self.assertEqual(rows[0].data_status, "history_insufficient")
        self.assertIsNone(rows[0].heat_score)

    def test_partial_heat_uses_intraday_change_when_history_is_short(self) -> None:
        as_of = date(2026, 6, 30)
        quotes = [
            QuoteRecord(as_of, "concept", "emchg:CPO", "CPO概念", change_pct=5.21),
            QuoteRecord(as_of, "concept", "emchg:AI", "AI芯片", change_pct=3.0),
            QuoteRecord(as_of, "concept", "emchg:弱势", "弱势概念", change_pct=-2.0),
        ]

        rows = calculate_sector_heat(quotes, "concept", as_of)
        by_code = {row.code: row for row in rows}

        self.assertEqual(by_code["emchg:CPO"].data_status, "partial")
        self.assertEqual(by_code["emchg:CPO"].heat_rank, 1)
        self.assertEqual(heat_action_label(by_code["emchg:CPO"]), "当日异动")
        self.assertEqual(heat_status_label("partial"), "临时热度")

    def test_percentile_heat_and_tie_rank(self) -> None:
        start = date(2026, 5, 18)
        quotes = []
        for index in range(21):
            day = start + timedelta(days=index)
            quotes.extend(
                [
                    QuoteRecord(day, "industry", "A", "强板块", close=100 + index * 2, change_pct=2),
                    QuoteRecord(day, "industry", "B", "并列板块1", close=100 + index, change_pct=1),
                    QuoteRecord(day, "industry", "C", "并列板块2", close=100 + index, change_pct=1),
                    QuoteRecord(day, "industry", "D", "弱板块", close=100 - index, change_pct=-1),
                ]
            )

        rows = calculate_sector_heat(quotes, "industry", start + timedelta(days=20))
        by_code = {row.code: row for row in rows}

        self.assertEqual(by_code["A"].heat_rank, 1)
        self.assertEqual(by_code["B"].heat_rank, by_code["C"].heat_rank)
        self.assertGreater(by_code["A"].heat_score or 0, by_code["B"].heat_score or 0)
        self.assertEqual(by_code["A"].heat_level, "hot")


if __name__ == "__main__":
    unittest.main()
