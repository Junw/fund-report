from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.main import _validate_historical_report_date


class HistoricalJobTests(unittest.TestCase):
    def test_historical_report_date_must_be_recent_30_days(self) -> None:
        with patch("app.main.today_in_timezone", return_value=date(2026, 7, 3)):
            _validate_historical_report_date(date(2026, 6, 4))
            with self.assertRaises(ValueError):
                _validate_historical_report_date(date(2026, 6, 3))
            with self.assertRaises(ValueError):
                _validate_historical_report_date(date(2026, 7, 4))
            with self.assertRaises(ValueError):
                _validate_historical_report_date(None)


if __name__ == "__main__":
    unittest.main()
