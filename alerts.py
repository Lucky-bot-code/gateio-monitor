"""
转折预警 — SAR↔MA10 双向验证框架 v3
"""
from typing import List, Dict, Optional, Tuple


# ============ SAR 翻转检测 ============

def detect_sar_flip(sar_values: List[float], closes: List[float],
                    highs: List[float], lows: List[float]) -> Optional[str]:
    """检测最新K线是否发生SAR翻转。
    返回: 'bullish' (多头翻转, SAR下穿价格), 'bearish' (空头翻转, SAR上穿价格), None
    """
    if len(sar_values) < 2 or len(closes) < 2:
        return None

    prev_sar = sar_values[-2]
    cur_sar = sar_values[-1]
    prev_close = closes[-2]
    cur_close = closes[-1]

    if prev_sar is None or cur_sar is None:
        return None

    # 多头翻转: SAR从价格上方 → 价格下方
    if prev_sar > prev_close and cur_sar < cur_close:
        return "bullish"

    # 空头翻转: SAR从价格下方 → 价格上方
    if prev_sar < prev_close and cur_sar > cur_close:
        return "bearish"

    return None


# ============ MA10 转折条件检测 ============

def check_ma10_condition(consecutive: int, close: float, ma10: float,
                         volume: float, avg_volume_10: float,
                         prev_volume: float, prev2_volume: float,
                         open_price: float, prev_open: float,
                         direction: str) -> Optional[int]:
    """检查MA10是否满足三种转折条件之一。

    Args:
        direction: 'bullish' 多头 / 'bearish' 空头

    Returns: 满足的条件编号 (1/2/3) 或 None
    """
    if close is None or ma10 is None:
        return None

    is_bullish = (direction == "bullish")

    # 公用条件: 价格在MA10同侧
    price_ok = close > ma10 if is_bullish else close < ma10
    if not price_ok:
        return None

    # 类型1: 连续转折周期 = 2 + 价格确认 + 开盘价确认
    if consecutive == 2 and open_price is not None and prev_open is not None:
        if is_bullish and open_price > prev_open:
            return 1
        elif not is_bullish and open_price < prev_open:
            return 1

    # 类型2: 连续转折周期 = 1 + 量 > 10周期均量
    if consecutive == 1 and avg_volume_10 is not None and avg_volume_10 > 0:
        if volume > avg_volume_10:
            return 2

    # 类型3: 连续转折周期 = 1 + 量递增
    if consecutive == 1 and prev_volume is not None and prev2_volume is not None:
        if volume > prev_volume > prev2_volume:
            return 3

    return None


# ============ 主转折分析 ============

def _ma10_direction(consecutive: int, trend: str) -> Optional[str]:
    """从MA10趋势判断方向。"""
    if consecutive < 1:
        return None
    if "上涨" in trend:
        return "bullish"
    if "下跌" in trend:
        return "bearish"
    return None


def _opposite(direction: str) -> str:
    return "bearish" if direction == "bullish" else "bullish"


def analyze_turning_points(data: List[Dict], tp_state: Dict) -> Tuple[List[Dict], Dict, List[Dict]]:
    """SAR↔MA10 双向验证转折预警。

    Args:
        data: monitor 全量数据
        tp_state: 历史转折状态 {symbol: {interval: {...}}}

    Returns:
        (alerts, new_state, pending) — 预警列表 + 更新状态 + 待中途检查项
    """
    alerts = []
    pending = []
    new_state = {}

    for item in data:
        symbol = item["symbol"]
        new_state[symbol] = new_state.get(symbol, {})

        for iv in item.get("intervals", []):
            if iv.get("trend") == "数据不足":
                continue

            interval = iv["interval"]
            close = iv.get("close")
            ma10 = iv.get("ma10")
            consecutive = iv.get("consecutive", 0)
            trend = iv.get("trend", "")
            volume = iv.get("volume", 0)
            avg_volume_10 = iv.get("avg_volume_10")
            prev_volume = iv.get("prev_volume", 0)
            prev2_volume = iv.get("prev2_volume", 0)
            open_price = iv.get("open")
            prev_open = iv.get("prev_open")
            sar_trend = iv.get("sar_trend", "")
            sar_consecutive = iv.get("sar_consecutive", 0)
            sar_flip = iv.get("sar_flip")  # 'bullish' / 'bearish' / None
            sar_direction = iv.get("sar_direction", "neutral")

            # 获取历史状态
            prev_state = tp_state.get(symbol, {}).get(interval, {})
            prev_sar_dir = prev_state.get("sar_direction", "neutral")

            alert = None

            # --- 路径A: SAR先翻转 → 验证MA10 ---
            if sar_flip and sar_flip != prev_sar_dir:
                ma10_type = check_ma10_condition(
                    consecutive, close, ma10, volume, avg_volume_10,
                    prev_volume, prev2_volume, open_price, prev_open,
                    sar_flip
                )
                if ma10_type:
                    alert = {
                        "symbol": symbol,
                        "interval_name": iv["name"],
                        "interval": interval,
                        "signal": "买入信号" if sar_flip == "bullish" else "卖出信号",
                        "path": "SAR先翻转",
                        "ma10_type": ma10_type,
                        "sar_direction": sar_flip,
                        "ma10_consecutive": consecutive,
                        "sar_consecutive": sar_consecutive,
                        "close": close,
                        "ma10": ma10,
                        "timestamp": None,  # 由调用方填充
                    }

            # --- 路径B: MA10先满足 → 验证SAR ---
            if not alert:
                ma10_dir = _ma10_direction(consecutive, trend)
                if ma10_dir:
                    ma10_type = check_ma10_condition(
                        consecutive, close, ma10, volume, avg_volume_10,
                        prev_volume, prev2_volume, open_price, prev_open,
                        ma10_dir
                    )
                    if ma10_type and sar_direction != "neutral":
                        # SAR已同向翻转且未再反向
                        if sar_direction == ma10_dir:
                            alert = {
                                "symbol": symbol,
                                "interval_name": iv["name"],
                                "interval": interval,
                                "signal": "买入信号" if ma10_dir == "bullish" else "卖出信号",
                                "path": "MA10先满足",
                                "ma10_type": ma10_type,
                                "sar_direction": sar_direction,
                                "ma10_consecutive": consecutive,
                                "sar_consecutive": sar_consecutive,
                                "close": close,
                                "ma10": ma10,
                                "timestamp": None,
                            }

            if alert:
                # 去重：同一标的+周期+信号类型不重复告警
                key = (symbol, interval, alert["signal"])
                prev_alerts = prev_state.get("alerts_sent", [])
                prev_keys = [(a["symbol"], a["interval"], a["signal"]) for a in prev_alerts]
                if key not in prev_keys:
                    alerts.append(alert)
                    prev_alerts.append(alert)

                new_state[symbol][interval] = {
                    "sar_direction": alert["sar_direction"],
                    "last_flip_time": None,
                    "alerts_sent": prev_alerts[-10:],
                }
            else:
                new_state[symbol][interval] = {
                    "sar_direction": sar_direction if sar_flip else prev_sar_dir,
                    "last_flip_time": prev_state.get("last_flip_time"),
                    "alerts_sent": prev_state.get("alerts_sent", []),
                }

                # --- 检测待中途检查的类型1 ---
                # 连续转折周期=1 且价格在MA10同侧 但不满足类型2/3 → 需要中途检查
                if consecutive == 1 and close is not None and ma10 is not None:
                    price_ok = (close > ma10 if sar_direction == "bullish" else close < ma10)
                    if price_ok:
                        # 排除类型2
                        is_type2 = (avg_volume_10 is not None and avg_volume_10 > 0 and volume > avg_volume_10)
                        # 排除类型3
                        is_type3 = (prev_volume is not None and prev2_volume is not None and volume > prev_volume > prev2_volume)
                        if not is_type2 and not is_type3:
                            pending.append({
                                "symbol": symbol,
                                "interval_name": iv["name"],
                                "interval": interval,
                                "direction": sar_direction,
                                "ma10_consecutive": consecutive,
                                "close": close,
                                "ma10": ma10,
                                "open": open_price,
                                "prev_open": prev_open,
                            })

    return alerts, new_state, pending
