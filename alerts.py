"""
多周期背离检测
"""
from typing import List, Dict


def _trend_direction(trend: str) -> str:
    if "上涨" in trend:
        return "up"
    elif "下跌" in trend:
        return "down"
    return "neutral"


def analyze_divergence(data: List[Dict]) -> List[Dict]:
    """检测多周期背离信号：大周期≥10同向，小周期反向 + 价格确认。"""
    signals = []
    hierarchy = [("日K", "4小时"), ("4小时", "60分钟"), ("60分钟", "15分钟")]
    for item in data:
        symbol = item["symbol"]
        intervals = {iv["name"]: iv for iv in item.get("intervals", [])}
        for big_name, small_name in hierarchy:
            big = intervals.get(big_name)
            small = intervals.get(small_name)
            if not big or not small:
                continue
            if big["trend"] == "数据不足" or small["trend"] == "数据不足":
                continue
            big_dir = _trend_direction(big["trend"])
            small_dir = _trend_direction(small["trend"])
            if big["consecutive"] >= 10 and big_dir in ("up", "down"):
                cur_price = item.get("last")
                big_ma10 = big.get("ma10")
                if cur_price is None or big_ma10 is None:
                    continue
                if big_dir == "up" and small_dir == "down" and cur_price < big_ma10:
                    signal_type = "卖出信号"
                elif big_dir == "down" and small_dir == "up" and cur_price > big_ma10:
                    signal_type = "买入信号"
                else:
                    continue
                signals.append({
                    "symbol": symbol,
                    "big_interval": big_name,
                    "small_interval": small_name,
                    "big_trend": big["trend"],
                    "big_consecutive": big["consecutive"],
                    "small_trend": small["trend"],
                    "small_consecutive": small["consecutive"],
                    "signal": signal_type,
                    "last": cur_price,
                    "change_pct": item.get("change_pct"),
                })
    return signals
