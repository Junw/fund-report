from __future__ import annotations

import unittest

from app.services.calculations import RankingItem
from app.services.recommender import build_advice
from app.services.sector_heat import SectorHeatItem


class RecommenderTests(unittest.TestCase):
    def test_momentum_and_overheat_advice(self) -> None:
        rankings = [
            RankingItem("industry", window, "gain", 1, "BK001", "半导体", 10, 100, None)
            for window in ("3d", "7d", "1m")
        ]
        rankings.append(RankingItem("industry", "1d", "gain", 1, "BK002", "机器人", 6, 20, None))

        advice = build_advice(rankings, {"market_congestion": 0.5})
        titles = [item.title for item in advice]

        self.assertIn("趋势较强，可观察回调机会", titles)
        self.assertIn("当天涨幅偏高，谨慎追高", titles)
        self.assertIn("市场拥挤度偏高", titles)

    def test_news_theme_adds_contextual_advice(self) -> None:
        rankings = [RankingItem("industry", "1d", "gain", 1, "ths:电池", "电池", 4.5, None, None)]
        news = [{"title": "储能电池需求升温", "summary": "新能源和储能产业链活跃"}]

        advice = build_advice(rankings, {}, news)

        self.assertIn("新闻热点与盘面方向共振", [item.title for item in advice])

    def test_sector_heat_adds_advice(self) -> None:
        heat = [SectorHeatItem(__import__("datetime").date(2026, 6, 17), "industry", "ths:电池", "电池", 3.0, 8.0, 92.0, 1, "hot", "ok")]

        advice = build_advice([], {}, [], heat)

        self.assertIn("板块热度延续", [item.title for item in advice])


if __name__ == "__main__":
    unittest.main()
