from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from app.services.data_sources import AkshareClient


class FakeAkshare:
    def stock_zh_a_spot_em(self):
        raise RuntimeError("eastmoney unavailable")

    def stock_zh_a_spot(self):
        return pd.DataFrame(
            [
                {"代码": "sh600000", "名称": "浦发银行", "最新价": 10, "涨跌幅": 1.2, "成交量": 100, "成交额": 1000},
                {"代码": "bj920000", "名称": "安徽凤凰", "最新价": 13, "涨跌幅": -0.5, "成交量": 100, "成交额": 1000},
            ]
        )

    def fund_open_fund_info_em(self, symbol, indicator):
        return pd.DataFrame([
            {"净值日期": "2026-06-19", "单位净值": 1.0, "日增长率": 0.0},
            {"净值日期": "2026-06-22", "单位净值": 1.02, "日增长率": 2.0},
        ])

    def fund_individual_basic_info_xq(self, symbol):
        return pd.DataFrame([
            {"item": "基金规模", "value": "50.2亿元"},
            {"item": "成立时间", "value": "2020-01-02"},
            {"item": "基金经理", "value": "张三"},
            {"item": "管理费率", "value": "1.20%"},
        ])

    def fund_portfolio_hold_em(self, symbol, date):
        return pd.DataFrame([{"股票代码": "600000", "股票名称": "浦发银行", "占净值比例": 5.2, "季度": "2026年1季度"}])
    def stock_board_industry_name_em(self):
        raise RuntimeError("eastmoney unavailable")

    def stock_board_industry_summary_ths(self):
        return pd.DataFrame(
            [
                {
                    "板块": "电池",
                    "涨跌幅": 4.02,
                    "总成交量": 3319.78,
                    "总成交额": 1393.31,
                    "上涨家数": 99,
                    "下跌家数": 6,
                    "领涨股": "ST南都",
                }
            ]
        )

    def stock_board_concept_name_em(self):
        raise RuntimeError("eastmoney concept unavailable")

    def stock_board_concept_name_ths(self):
        return pd.DataFrame([{"name": "共封装光学(CPO)", "code": "309049"}])

    def stock_board_change_em(self):
        return pd.DataFrame(
            [
                {
                    "板块名称": "CPO概念",
                    "涨跌幅": 5.21,
                    "主力净流入": 1434846,
                    "板块异动总次数": 584,
                    "板块异动最频繁个股及所属类型-股票代码": "603459",
                    "板块异动最频繁个股及所属类型-股票名称": "红板科技",
                    "板块异动最频繁个股及所属类型-买卖方向": "大笔卖出",
                }
            ]
        )


class DataSourceFallbackTests(unittest.TestCase):
    def test_stock_spot_falls_back_to_sina(self) -> None:
        client = AkshareClient(FakeAkshare())

        rows = client._fetch_stock_spot(date(2026, 6, 16))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].code, "600000")
        self.assertEqual(rows[0].extra["source"], "sina")

    def test_industry_spot_falls_back_to_ths_summary(self) -> None:
        client = AkshareClient(FakeAkshare())

        rows = client._fetch_board_spot(date(2026, 6, 16), "industry")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "电池")
        self.assertEqual(rows[0].change_pct, 4.02)
        self.assertEqual(rows[0].amount, 1393.31 * 100_000_000)

    def test_concept_spot_falls_back_to_board_change_and_verifies_ths(self) -> None:
        client = AkshareClient(FakeAkshare())

        rows = client._fetch_board_spot(date(2026, 6, 30), "concept")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "CPO概念")
        self.assertEqual(rows[0].change_pct, 5.21)
        self.assertEqual(rows[0].extra["source"], "eastmoney_board_change")
        self.assertTrue(rows[0].extra["verified_by_ths"])
        self.assertEqual(rows[0].extra["ths_name"], "共封装光学(CPO)")


    def test_fund_history_contract(self) -> None:
        client = AkshareClient(FakeAkshare())
        rows = client.fetch_fund_history("000001", "成长混合A", "fund", date(2026, 6, 22))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1].close, 1.02)

    def test_fund_metadata_and_holdings_contract(self) -> None:
        client = AkshareClient(FakeAkshare())
        metadata = client.fetch_fund_metadata("000001", date(2026, 6, 22))
        report_date, holdings = client.fetch_fund_holdings("000001", date(2026, 6, 22))
        self.assertEqual(metadata["manager_name"], "张三")
        self.assertEqual(metadata["fund_size"], 50.2 * 100_000_000)
        self.assertEqual(report_date, date(2026, 3, 31))
        self.assertEqual(holdings[0]["stock_code"], "600000")

if __name__ == "__main__":
    unittest.main()
