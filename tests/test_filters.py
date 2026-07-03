from __future__ import annotations

import unittest

from app.services.fund_filters import (
    fund_family_key, fund_share_class, is_c_share_class, is_domestic_etf, is_equity_fund, primary_fund_codes,
)


class FundFilterTests(unittest.TestCase):
    def test_equity_fund_scope(self) -> None:
        self.assertTrue(is_equity_fund("中证新能源指数A", "指数型"))
        self.assertTrue(is_equity_fund("优选成长混合", "混合型"))
        self.assertFalse(is_equity_fund("安心债券A", "债券型"))
        self.assertFalse(is_equity_fund("纳斯达克QDII", "股票型"))

    def test_domestic_etf_scope(self) -> None:
        self.assertTrue(is_domestic_etf("创业板ETF"))
        self.assertFalse(is_domestic_etf("纳斯达克ETF"))
        self.assertFalse(is_domestic_etf("黄金ETF"))


    def test_share_family_selects_a_over_e_c_i(self) -> None:
        rows = [("000001", "成长混合A"), ("000002", "成长混合C"), ("000003", "成长混合E"), ("000004", "价值混合")]
        self.assertEqual(primary_fund_codes(rows), {"000001", "000004"})
        self.assertEqual(fund_family_key("成长混合 E类"), fund_family_key("成长混合A"))
        self.assertEqual(fund_share_class("成长混合I"), "I")
    def test_c_share_class(self) -> None:
        self.assertTrue(is_c_share_class("新能源主题混合C"))
        self.assertTrue(is_c_share_class("消费精选 C类"))
        self.assertFalse(is_c_share_class("新能源主题混合A"))
        self.assertFalse(is_c_share_class("中证500指数"))


if __name__ == "__main__":
    unittest.main()
