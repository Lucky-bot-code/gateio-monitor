"""
MA10 转折预警 / 多周期背离检测 / 企业微信推送
"""
from datetime import datetime
from typing import List, Dict, Optional

import requests

WECOM_WEBHOOK_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
    "7bef2a11-7838-4859-a9c0-b65b6cf2dc36"
)


def _trend_direction(trend: str) -> str:
    if "上涨" in trend:
        return "up"
    elif "下跌" in trend:
        return "down"
    return "neutral"


def get_active_alert_intervals() -> set:
    """返回当前时刻应执行转折检测的周期集合。北京时间第59分钟触发。"""
    now = datetime.now()
    minute = now.minute
    hour = now.hour
    active = {"15分钟"}
    if minute == 59:
        active.add("60分钟")
        if hour % 4 == 3:
            active.add("4小时")
        if hour == 7:
            active.add("日K")
    return active


def detect_reversal(prev_trend: str, curr_trend: str, curr_consecutive: int) -> Optional[str]:
    """检测趋势转折。返回 'reversal_up' / 'reversal_down' / None"""
    if curr_consecutive < 1 or curr_consecutive > 3:
        return None
    prev_dir = _trend_direction(prev_trend)
    curr_dir = _trend_direction(curr_trend)
    if prev_dir == "up" and curr_dir == "down":
        return "reversal_down"
    if prev_dir == "down" and curr_dir == "up":
        return "reversal_up"
    return None


def _price_conditions(iv: Dict, is_up: bool) -> List[str]:
    """构建价格方向条件（cons==1 和 cons==2 共用）"""
    close = iv["close"]
    ma10 = iv["ma10"]
    conds = []
    if is_up:
        prev_high = iv.get("prev_high", 0)
        if close is not None and ma10 is not None and close > ma10:
            conds.append("价格>MA10")
        if close is not None and prev_high > 0 and close > prev_high:
            conds.append("价格>前高")
    else:
        prev_low = iv.get("prev_low", float("inf"))
        if close is not None and ma10 is not None and close < ma10:
            conds.append("价格<MA10")
        if close is not None and prev_low != float("inf") and close < prev_low:
            conds.append("价格<前低")
    return conds


def check_reversal_strength(iv: Dict, rev: str) -> Optional[str]:
    """判断转折预警是否满足强度条件。"""
    if iv.get("trend") in ("数据不足", "震荡") or iv["consecutive"] < 1:
        return None
    cons = iv["consecutive"]
    if cons >= 3:
        return None

    is_up = (rev == "reversal_up")
    conds = _price_conditions(iv, is_up)

    if cons == 1:
        vol = iv.get("volume", 0)
        prev_vol = iv.get("prev_volume", 0)
        if not (prev_vol > 0 and vol >= prev_vol * 2):
            return None
        if not conds:
            return None
        return f"量能放大{vol/prev_vol:.1f}x+" + "+".join(conds)

    # cons == 2
    if not conds:
        return None
    return "+".join(conds)


def _iter_reversals(prev_state: Dict, new_state: Dict, data: List[Dict],
                    active_intervals: set):
    """遍历所有活跃周期中的转折事件，生成 (iv_name, item, rev, iv_data, prev_iv, new_iv)"""
    for iv_name in active_intervals:
        prev_iv_state = prev_state.get(iv_name, {})
        new_iv_state = new_state.get(iv_name, {})
        if not prev_iv_state:
            continue
        for item in data:
            sym = item["symbol"]
            if sym not in prev_iv_state or sym not in new_iv_state:
                continue
            prev_iv = prev_iv_state[sym]
            new_iv = new_iv_state[sym]
            rev = detect_reversal(prev_iv["trend"], new_iv["trend"], new_iv["consecutive"])
            if not rev:
                continue
            iv_data = next(
                (iv for iv in item["intervals"] if iv["name"] == iv_name), None
            )
            if not iv_data:
                continue
            yield iv_name, item, rev, iv_data, prev_iv, new_iv


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


def build_alert_state(data: List[Dict]) -> Dict:
    """interval-first 状态字典: {周期名: {symbol: {trend, consecutive}}}"""
    state: Dict[str, Dict] = {"日K": {}, "4小时": {}, "60分钟": {}, "15分钟": {}}
    for item in data:
        sym = item["symbol"]
        for iv in item.get("intervals", []):
            name = iv["name"]
            if name in state:
                state[name][sym] = {
                    "trend": iv["trend"],
                    "consecutive": iv["consecutive"],
                }
    return state


def send_wecom_alert(
    alerts: List[Dict], update_time: str, alert_type: str = "reversal"
) -> bool:
    """企业微信机器人推送。alert_type: "reversal" / "position" """
    if not WECOM_WEBHOOK_URL or not alerts:
        return False
    title = "MA10 转折预警" if alert_type == "reversal" else "持仓预警"
    lines = [f"**{title}**  \n更新时间: {update_time}  \n"]
    for a in alerts:
        arrow = "📉" if a["type"] == "reversal_down" else "📈"
        text = "下跌转折" if a["type"] == "reversal_down" else "上涨转折"
        pct = (
            f" 累计{a['reversal_pct']:+.2f}%"
            if a.get("reversal_pct") is not None
            else ""
        )
        if alert_type == "position":
            pos_label = "做多" if a["position"] == "long" else "做空"
            lines.append(
                f"{arrow} **{a['symbol']}** {a['interval']}{text}"
                f"(已{a['consecutive']}周期·{pos_label}){pct}  \n"
                f"> 前: {a['prev_trend']} → 现: {a['curr_trend']}  \n"
            )
        else:
            cond = a.get("condition", "")
            cond_str = f"  [{cond}]" if cond else ""
            lines.append(
                f"{arrow} **{a['symbol']}** {a['interval']}{text}"
                f"(已{a['consecutive']}周期){pct}{cond_str}  \n"
                f"> 前: {a['prev_trend']} → 现: {a['curr_trend']}  \n"
            )
    payload = {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}
    try:
        resp = requests.post(WECOM_WEBHOOK_URL, json=payload, timeout=15)
        return resp.status_code == 200
    except Exception:
        return False
