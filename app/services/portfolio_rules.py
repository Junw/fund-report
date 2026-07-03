from __future__ import annotations


def score_returns(returns: dict[str, float | None]) -> tuple[int, list[str]]:
    score = 50
    signals: list[str] = []
    r1 = returns.get("1d")
    r3 = returns.get("3d")
    r7 = returns.get("7d")
    r1m = returns.get("1m")

    if r1m is not None:
        if r1m >= 10:
            score += 16
            signals.append("近1月趋势较强")
        elif r1m >= 3:
            score += 9
            signals.append("近1月保持正收益")
        elif r1m <= -10:
            score -= 16
            signals.append("近1月回撤较深")
        elif r1m < 0:
            score -= 8
            signals.append("近1月偏弱")

    if r7 is not None:
        if r7 >= 5:
            score += 14
            signals.append("近7日动量较强")
        elif r7 >= 1:
            score += 7
            signals.append("近7日温和走强")
        elif r7 <= -5:
            score -= 14
            signals.append("近7日短线转弱")
        elif r7 < 0:
            score -= 6
            signals.append("近7日小幅回落")

    if r3 is not None and r7 is not None and r1m is not None:
        if r1m >= 6 and r7 < 0:
            score += 3
            signals.append("中期强势后的短线回调，可观察企稳")
        if r1m < 0 and r7 >= 2 and r3 >= 1:
            score += 5
            signals.append("弱势区间出现初步修复，等待持续性确认")

    if r1 is not None:
        if r1 >= 5:
            score -= 5
            signals.append("当日涨幅偏高，谨慎追高")
        elif r1 <= -4:
            score -= 5
            signals.append("当日跌幅较大，先观察风险释放")

    if not signals:
        signals.append("历史数据不足或趋势信号不明显")
    return max(0, min(100, score)), signals


def score_level(score: int) -> str:
    if score >= 80:
        return "强势"
    if score >= 65:
        return "偏强"
    if score >= 45:
        return "中性"
    if score >= 30:
        return "偏弱"
    return "高风险"
