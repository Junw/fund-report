from __future__ import annotations

import unittest
from datetime import date

from app.services.calendar import is_probable_trading_day


class CalendarTests(unittest.TestCase):
    def test_weekend_skipped(self) -> None:
        self.assertTrue(is_probable_trading_day(date(2026, 6, 15)))
        self.assertFalse(is_probable_trading_day(date(2026, 6, 14)))


if __name__ == "__main__":
    unittest.main()

