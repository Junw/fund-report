from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.schemas import QuoteRecord
from app.services.calculations import calculate_rankings, pct_change


class CalculationTests(unittest.TestCase):
    def test_pct_change_handles_zero_and_missing(self) -> None:
        self.assertEqual(round(pct_change(110, 100) or 0, 2), 10.0)
        self.assertIsNone(pct_change(100, 0))
        self.assertIsNone(pct_change(None, 100))

    def test_window_rankings_use_local_history(self) -> None:
        start = date(2026, 6, 1)
        quotes = [
            QuoteRecord(start + timedelta(days=index), "industry", "BK001", "半导体", close=100 + index, change_pct=1)
            for index in range(7)
        ]
        quotes.extend(
            QuoteRecord(start + timedelta(days=index), "industry", "BK002", "银行", close=100 - index, change_pct=-1)
            for index in range(7)
        )

        rows = calculate_rankings(quotes, "industry", start + timedelta(days=6), limit=5)
        gain_3d = [row for row in rows if row.window == "3d" and row.rank_type == "gain"]

        self.assertEqual(gain_3d[0].code, "BK001")
        self.assertGreater(gain_3d[0].value or 0, 0)

    def test_history_shortage_excludes_window_rankings(self) -> None:
        as_of = date(2026, 6, 1)
        quotes = [QuoteRecord(as_of, "concept", "BK999", "机器人", close=10, change_pct=2)]

        rows = calculate_rankings(quotes, "concept", as_of, limit=5)

        self.assertTrue(any(row.window == "1d" for row in rows))
        self.assertFalse(any(row.window == "3d" and row.rank_type == "gain" for row in rows))

    def test_concept_ranking_note_includes_source_verification(self) -> None:
        as_of = date(2026, 6, 30)
        quotes = [
            QuoteRecord(
                as_of,
                "concept",
                "emchg:CPO",
                "CPO概念",
                change_pct=5.21,
                extra={
                    "source": "eastmoney_board_change",
                    "verified_by_ths": True,
                    "ths_name": "共封装光学(CPO)",
                },
            )
        ]

        rows = calculate_rankings(quotes, "concept", as_of, limit=5)
        gain = next(row for row in rows if row.window == "1d" and row.rank_type == "gain")

        self.assertIn("东方财富异动", gain.note or "")
        self.assertIn("同花顺校验: 共封装光学(CPO)", gain.note or "")


if __name__ == "__main__":
    unittest.main()
