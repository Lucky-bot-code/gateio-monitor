#!/usr/bin/env python3
"""
Gate.io U本位合约 MA10 趋势监控 - Web可视化面板
Flask后端 + 纯前端，局域网/本机访问
"""

import os
import sys
import json
import time
import socket
import subprocess
import threading
import logging
import importlib.metadata
from concurrent.futures import ThreadPoolExecutor, as_completed
import webbrowser
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from flask import Flask, jsonify, render_template_string, request

# ============ 配置 ============
BASE_URL = "https://api.gateio.ws/api/v4"
CONFIG_FILE = "gateio_available_symbols.json"
PORT = 5000
AUTO_REFRESH_INTERVAL = 300  # 保留作为后备间隔(秒)，实际使用对齐K线收盘的动态调度
REQUEST_DELAY = 0.12  # API 请求间隔（并行模式下仅影响同标的连续请求）
KLINES_LIMIT = 50

# 下一次自动刷新的启动时间戳（auto_refresh_loop 启动后更新）
_next_refresh_at = time.time() + AUTO_REFRESH_INTERVAL

app = Flask(__name__)

# 全局缓存
cache = {
    "data": [],
    "last_update": None,
    "updating": False,
    "error": None,
    "alerts": [],
    "position_alerts": [],
    "divergence": []
}

# 浏览器自动打开标志（只打开一次）
_browser_opened = False

# 预警持久化文件（用于跨重启保留上一次状态）
ALERT_STATE_FILE = ".ma10_state.json"

# 持仓标记持久化文件
POSITIONS_FILE = "positions.json"

# 微信通知配置（企业微信机器人 Webhook）
# 获取方式：在企业微信群 → 添加群机器人 → 复制 Webhook 地址
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=7bef2a11-7838-4859-a9c0-b65b6cf2dc36"


def send_wecom_alert(alerts: List[Dict], update_time: str, alert_type: str = "reversal") -> bool:
    """
    通过企业微信机器人推送预警消息。
    alert_type: "reversal" 转折预警 / "position" 持仓预警
    """
    if not WECOM_WEBHOOK_URL or not alerts:
        return False
    if alert_type == "reversal":
        title = "MA10 转折预警"
    else:
        title = "持仓预警"
    lines = [f"**{title}**  \n更新时间: {update_time}  \n"]
    for a in alerts:
        arrow = "📉" if a["type"] == "reversal_down" else "📈"
        text = "下跌转折" if a["type"] == "reversal_down" else "上涨转折"
        pct = f" 累计{a['reversal_pct']:+.2f}%" if a.get("reversal_pct") is not None else ""
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
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": "\n".join(lines)}
    }
    try:
        resp = requests.post(WECOM_WEBHOOK_URL, json=payload, timeout=15)
        return resp.status_code == 200
    except Exception:
        return False


def _trend_direction(trend: str) -> str:
    """提取趋势方向"""
    if "上涨" in trend:
        return "up"
    elif "下跌" in trend:
        return "down"
    return "neutral"


def get_active_alert_intervals() -> set:
    """
    返回当前时刻应该执行转折检测的周期集合。
    规则（北京时间，第59分钟刷新触发，蜡烛临近收盘时检测）：
    - 15分钟：每次刷新都检测
    - 60分钟：每小时第59分钟（xx:59）
    - 4小时：每4小时K线第59分钟（北京时 3/7/11/15/19/23 点的 59 分）
    - 日K：每日K线第59分钟（北京时 7:59，对应 UTC 23:59）
    """
    now = datetime.now()
    minute = now.minute
    hour = now.hour

    active = {"15分钟"}

    if minute == 59:
        active.add("60分钟")
        # 4小时: 收盘 北京时 0/4/8/12/16/20，第59分钟是 3/7/11/15/19/23 点的 59 分 → hour % 4 == 3
        if hour % 4 == 3:
            active.add("4小时")
        # 日K: 北京时 8:00 收盘，第59分钟是 7:59
        if hour == 7:
            active.add("日K")

    return active


def detect_reversal(prev_trend: str, curr_trend: str, curr_consecutive: int) -> Optional[str]:
    """
    检测趋势转折。
    返回: 'reversal_up' (下跌转上涨) / 'reversal_down' (上涨转下跌) / None
    """
    if curr_consecutive < 1 or curr_consecutive > 3:
        return None
    prev_dir = _trend_direction(prev_trend)
    curr_dir = _trend_direction(curr_trend)
    if prev_dir == "up" and curr_dir == "down":
        return "reversal_down"
    if prev_dir == "down" and curr_dir == "up":
        return "reversal_up"
    return None


def check_reversal_strength(iv: Dict, rev: str) -> Optional[str]:
    """
    判断转折预警是否满足强度条件。所有周期通用。

    rev = 'reversal_up' (下跌转上涨): 价格需要向上突破
    rev = 'reversal_down' (上涨转下跌): 价格需要向下突破

    consecutive=1: 量能放大2x + 方向价格条件
    consecutive=2: 方向价格条件
    consecutive>=3: 不预警
    """
    if iv.get("trend") in ("数据不足", "震荡") or iv["consecutive"] < 1:
        return None
    cons = iv["consecutive"]
    close = iv["close"]
    ma10 = iv["ma10"]

    if cons >= 3:
        return None

    is_up = (rev == "reversal_up")

    if cons == 1:
        # 量能条件
        vol = iv.get("volume", 0)
        prev_vol = iv.get("prev_volume", 0)
        if not (prev_vol > 0 and vol >= prev_vol * 2):
            return None
        # 方向价格条件
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
        if not conds:
            return None
        return f"量能放大{vol/prev_vol:.1f}x+" + "+".join(conds)

    if cons == 2:
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
        if conds:
            return "+".join(conds)
        return None

    return None


def analyze_divergence(data: List[Dict]) -> List[Dict]:
    """
    检测多周期背离信号。
    规则：大周期连续 >= 5 个同向周期，且相邻小周期方向相反。
    """
    signals = []
    hierarchy = [
        ("日K", "4小时"),
        ("4小时", "60分钟"),
        ("60分钟", "15分钟"),
    ]

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

            if big["consecutive"] >= 5 and big_dir in ("up", "down"):
                if (big_dir == "up" and small_dir == "down") or (big_dir == "down" and small_dir == "up"):
                    signal_type = "卖出信号" if big_dir == "up" else "买入信号"
                    signals.append({
                        "symbol": symbol,
                        "big_interval": big_name,
                        "small_interval": small_name,
                        "big_trend": big["trend"],
                        "big_consecutive": big["consecutive"],
                        "small_trend": small["trend"],
                        "small_consecutive": small["consecutive"],
                        "signal": signal_type,
                        "last": item.get("last"),
                        "change_pct": item.get("change_pct")
                    })

    # 按标的聚合，同一标的多个层级合并展示
    return signals


def build_alert_state(data: List[Dict]) -> Dict:
    """
    将数据转换为 interval-first 状态字典。
    结构: {周期名: {symbol: {trend, consecutive}}}
    每个周期独立保存，以便各周期按各自边界时间对比。
    """
    state: Dict[str, Dict] = {"日K": {}, "4小时": {}, "60分钟": {}, "15分钟": {}}
    for item in data:
        sym = item["symbol"]
        for iv in item.get("intervals", []):
            name = iv["name"]
            if name in state:
                state[name][sym] = {"trend": iv["trend"], "consecutive": iv["consecutive"]}
    return state


def load_prev_state() -> Dict[str, Dict]:
    """
    加载上次保存的状态。兼容旧 symbol-first 格式，自动转换为 interval-first。
    """
    empty: Dict[str, Dict] = {"日K": {}, "4小时": {}, "60分钟": {}, "15分钟": {}}
    if not os.path.exists(ALERT_STATE_FILE):
        return empty
    try:
        with open(ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return empty
    if not raw:
        return empty
    # 检测格式：新 interval-first 格式的 key 是周期名
    first_key = next(iter(raw))
    if first_key in ("日K", "4小时", "60分钟", "15分钟"):
        # 新格式，确保所有键存在
        for k in empty:
            if k not in raw:
                raw[k] = {}
        return raw
    # 旧 symbol-first 格式，转换
    converted: Dict[str, Dict] = {"日K": {}, "4小时": {}, "60分钟": {}, "15分钟": {}}
    for sym, intervals in raw.items():
        if isinstance(intervals, dict):
            for iv_name, iv_data in intervals.items():
                if iv_name in converted:
                    converted[iv_name][sym] = iv_data
    return converted


def save_state(state: Dict):
    try:
        with open(ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_positions() -> Dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_positions(positions: Dict):
    try:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def print_banner():
    """打印黑客风格启动画面"""
    print(r"""
   +=======================================================+
   |                                                       |
   |     G A T E . I O   M A 1 0   M O N I T O R         |
   |              Protocol v1.3.2  //  ONLINE                |
   |                                                       |
   +=======================================================+
""")
    time.sleep(0.05)
    print("[INIT] Loading core modules...")
    time.sleep(0.05)
    print("[INIT] Engaging network handshake protocol...")
    time.sleep(0.05)
    print("[ OK ] Module: requests  v" + requests.__version__)
    print("[ OK ] Module: flask     v" + importlib.metadata.version("flask"))
    print("[ OK ] Module: threading (ready)")
    print("-" * 55)


def print_progress(current: int, total: int, symbol: str = ""):
    """在同一行打印进度条"""
    bar_len = 30
    filled = int(bar_len * current // total)
    bar = "▓" * filled + "▒" * (bar_len - filled)
    pct = int(100 * current / total)
    sym = f" | {symbol:<8}" if symbol else ""
    line = f"\r[PROG] [{bar}] {pct:>3}%{sym}"
    try:
        print(line, end="", flush=True)
    except UnicodeEncodeError:
        bar2 = "#" * filled + "." * (bar_len - filled)
        line2 = f"\r[PROG] [{bar2}] {pct:>3}%{sym}"
        print(line2, end="", flush=True)
    if current >= total:
        print()


# 全局持仓标记（从文件加载）
user_positions = load_positions()


class MonitorCore:
    def __init__(self):
        self.session = requests.Session()
        self.symbols = self._load_symbols()

    def _load_symbols(self) -> List[Dict]:
        if not os.path.exists(CONFIG_FILE):
            return []
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("available", [])

    def fetch_ticker(self, contract: str, session=None) -> Optional[Dict]:
        s = session or self.session
        try:
            url = f"{BASE_URL}/futures/usdt/tickers"
            resp = s.get(url, params={"contract": contract}, timeout=15)
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception:
            pass
        return None

    def fetch_klines(self, contract: str, interval: str, limit: int = 100, session=None) -> Optional[List[Dict]]:
        s = session or self.session
        try:
            url = f"{BASE_URL}/futures/usdt/candlesticks"
            resp = s.get(url, params={"contract": contract, "interval": interval, "limit": limit}, timeout=15)
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def calculate_ma(closes: List[float], period: int = 10) -> List[Optional[float]]:
        ma = []
        for i in range(len(closes)):
            if i < period - 1:
                ma.append(None)
            else:
                ma.append(sum(closes[i - period + 1:i + 1]) / period)
        return ma

    @staticmethod
    def analyze_trend(ma_values: List[Optional[float]], min_consecutive: int = 3) -> Tuple[str, int, List[float]]:
        valid_ma = [v for v in ma_values if v is not None]
        if len(valid_ma) < min_consecutive + 1:
            return "数据不足", 0, valid_ma

        consecutive_up = 0
        consecutive_down = 0
        for i in range(len(valid_ma) - 2, -1, -1):
            curr = valid_ma[i]
            nxt = valid_ma[i + 1]
            if nxt > curr:
                if consecutive_down > 0:
                    break
                consecutive_up += 1
            elif nxt < curr:
                if consecutive_up > 0:
                    break
                consecutive_down += 1
            else:
                break

        display_window = valid_ma[-7:]
        if consecutive_up >= min_consecutive:
            return "连续上涨", consecutive_up, display_window
        elif consecutive_down >= min_consecutive:
            return "连续下跌", consecutive_down, display_window
        elif consecutive_up > 0:
            return "短期上涨", consecutive_up, display_window
        elif consecutive_down > 0:
            return "短期下跌", consecutive_down, display_window
        else:
            return "震荡", 0, display_window

    def _fetch_symbol_data(self, sym_info: Dict, session) -> Dict:
        """获取单个标的的 ticker + 4 周期 K 线数据"""
        contract = sym_info["contract"]
        user_symbol = sym_info.get("user_symbol", contract)
        result = {"symbol": user_symbol, "contract": contract, "intervals": []}

        # Ticker
        ticker = self.fetch_ticker(contract, session)
        if ticker:
            result["last"] = float(ticker.get("last", 0))
            result["change_pct"] = float(ticker.get("change_percentage", 0))
            result["mark_price"] = float(ticker.get("mark_price", 0))
            result["index_price"] = float(ticker.get("index_price", 0))
            result["funding_rate"] = float(ticker.get("funding_rate", 0))
        else:
            result["last"] = None
            result["change_pct"] = None

        time.sleep(REQUEST_DELAY)

        # 各周期
        for interval, interval_name in [("1d", "日K"), ("4h", "4小时"), ("1h", "60分钟"), ("15m", "15分钟")]:
            klines = self.fetch_klines(contract, interval, KLINES_LIMIT, session)
            time.sleep(REQUEST_DELAY)

            if not klines or len(klines) < 20:
                result["intervals"].append({
                    "name": interval_name,
                    "interval": interval,
                    "trend": "数据不足",
                    "consecutive": 0,
                    "ma10": None,
                    "close": None,
                    "deviation": None,
                    "candles_count": len(klines) if klines else 0
                })
                continue

            klines_sorted = sorted(klines, key=lambda x: int(x["t"]))
            closes = [float(k["c"]) for k in klines_sorted]
            ma10 = self.calculate_ma(closes, period=10)
            valid_ma = [v for v in ma10 if v is not None]
            trend, consecutive, recent_ma = self.analyze_trend(ma10, min_consecutive=3)
            deviation = (closes[-1] - valid_ma[-1]) / valid_ma[-1] * 100 if valid_ma else 0

            cur_k = klines_sorted[-1]
            prev_k = klines_sorted[-2] if len(klines_sorted) >= 2 else None
            prev2_k = klines_sorted[-3] if len(klines_sorted) >= 3 else None
            volume = float(cur_k["v"])
            prev_volume = float(prev_k["v"]) if prev_k else 0
            prev_high = float(prev_k["h"]) if prev_k else 0
            prev2_high = float(prev2_k["h"]) if prev2_k else 0
            prev_low = float(prev_k["l"]) if prev_k else float("inf")
            prev2_low = float(prev2_k["l"]) if prev2_k else float("inf")

            reversal_pct = None
            if 1 <= consecutive <= 3 and trend not in ("数据不足", "震荡"):
                if len(closes) >= consecutive + 2:
                    start_close = closes[-consecutive - 1]
                    if start_close != 0:
                        reversal_pct = (closes[-1] - start_close) / start_close * 100

            result["intervals"].append({
                "name": interval_name,
                "interval": interval,
                "trend": trend,
                "consecutive": consecutive,
                "ma10": round(valid_ma[-1], 4) if valid_ma else None,
                "close": round(closes[-1], 4),
                "deviation": round(deviation, 2),
                "reversal_pct": round(reversal_pct, 2) if reversal_pct is not None else None,
                "candles_count": len(klines),
                "ma_series": [round(v, 2) for v in recent_ma] if recent_ma else [],
                "volume": round(volume, 2),
                "prev_volume": round(prev_volume, 2),
                "prev_high": round(prev_high, 4),
                "prev2_high": round(prev2_high, 4),
                "prev_low": round(prev_low, 4) if prev_low != float("inf") else float("inf"),
                "prev2_low": round(prev2_low, 4) if prev2_low != float("inf") else float("inf")
            })

        return result

    def analyze_all(self) -> List[Dict]:
        results = []
        total = len(self.symbols)
        if total == 0:
            return results
        workers = min(8, max(3, total // 10))
        print(f"[SYNC] Acquiring market data stream (parallel mode, {workers} workers)...")

        progress_lock = threading.Lock()
        completed = [0]

        def process_one(idx_sym):
            idx, sym_info = idx_sym
            session = requests.Session()
            data = self._fetch_symbol_data(sym_info, session)
            session.close()
            with progress_lock:
                completed[0] += 1
                print_progress(completed[0], total, sym_info.get("user_symbol", sym_info["contract"]))
            return idx, data

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_one, (i, s)): i for i, s in enumerate(self.symbols)}
            results = [None] * total
            for future in as_completed(futures):
                idx, data = future.result()
                results[idx] = data

        results = [r for r in results if r is not None]
        print(f"[ OK ] Data acquisition complete. {total} assets monitored.")
        return results


def refresh_data():
    """后台刷新数据"""
    global cache
    if cache["updating"]:
        return
    cache["updating"] = True
    cache["error"] = None
    print("[SYNC] Initiating market data synchronization...")
    try:
        monitor = MonitorCore()
        if not monitor.symbols:
            cache["error"] = f"未找到 {CONFIG_FILE}，请先运行扫描脚本"
            cache["updating"] = False
            return
        data = monitor.analyze_all()
        prev_state = load_prev_state()
        new_state = build_alert_state(data)
        active_intervals = get_active_alert_intervals()
        alerts = []

        # 转折预警：仅对当前时刻活跃的周期检测
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
                iv_data = next((iv for iv in item["intervals"] if iv["name"] == iv_name), None)
                if not iv_data:
                    continue
                cond = check_reversal_strength(iv_data, rev)
                if not cond:
                    continue
                alerts.append({
                    "symbol": sym,
                    "interval": iv_name,
                    "type": rev,
                    "prev_trend": prev_iv["trend"],
                    "curr_trend": new_iv["trend"],
                    "consecutive": new_iv["consecutive"],
                    "reversal_pct": iv_data.get("reversal_pct"),
                    "condition": cond
                })

        cache["data"] = data
        cache["alerts"] = alerts

        # 持仓预警：同样仅对活跃周期检测
        position_alerts = []
        for iv_name in active_intervals:
            prev_iv_state = prev_state.get(iv_name, {})
            new_iv_state = new_state.get(iv_name, {})
            if not prev_iv_state:
                continue
            for item in data:
                sym = item["symbol"]
                pos = user_positions.get(sym)
                if not pos:
                    continue
                if sym not in prev_iv_state or sym not in new_iv_state:
                    continue
                prev_iv = prev_iv_state[sym]
                new_iv = new_iv_state[sym]
                rev = detect_reversal(prev_iv["trend"], new_iv["trend"], new_iv["consecutive"])
                if rev:
                    if (pos == "long" and rev == "reversal_down") or (pos == "short" and rev == "reversal_up"):
                        iv_data = next((iv for iv in item["intervals"] if iv["name"] == iv_name), None)
                        position_alerts.append({
                            "symbol": sym,
                            "interval": iv_name,
                            "type": rev,
                            "position": pos,
                            "prev_trend": prev_iv["trend"],
                            "curr_trend": new_iv["trend"],
                            "consecutive": new_iv["consecutive"],
                            "reversal_pct": iv_data.get("reversal_pct") if iv_data else None
                        })

        cache["position_alerts"] = position_alerts
        cache["divergence"] = analyze_divergence(data)
        cache["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 只更新活跃周期的状态，未活跃周期保留旧值
        merged_state = dict(prev_state)
        for iv_name in active_intervals:
            if iv_name in new_state:
                merged_state[iv_name] = new_state[iv_name]
        save_state(merged_state)

        if alerts:
            print(f"\n{'='*60}")
            print(f"  检测到 {len(alerts)} 个 MA10 转折预警")
            print(f"{'='*60}")
            for a in alerts:
                arrow = "↓ 下跌转折" if a["type"] == "reversal_down" else "↑ 上涨转折"
                pct = a.get("reversal_pct")
                pct_str = f" 累计{a['reversal_pct']:+.2f}%" if pct is not None else ""
                cond = a.get("condition", "")
                cond_str = f" [{cond}]" if cond else ""
                print(f"  [{a['symbol']}] {a['interval']} {arrow} (已{a['consecutive']}周期){pct_str}{cond_str}")
                print(f"     前趋势: {a['prev_trend']} → 现趋势: {a['curr_trend']}")
            print(f"{'='*60}\n")
            # 推送微信通知
            send_wecom_alert(alerts, cache["last_update"])

        if position_alerts:
            print(f"\n{'='*60}")
            print(f"  检测到 {len(position_alerts)} 个持仓预警")
            print(f"{'='*60}")
            for a in position_alerts:
                pos_label = "做多" if a["position"] == "long" else "做空"
                arrow = "↓ 下跌转折" if a["type"] == "reversal_down" else "↑ 上涨转折"
                pct = a.get("reversal_pct")
                pct_str = f" 累计{a['reversal_pct']:+.2f}%" if pct is not None else ""
                print(f"  [{a['symbol']}] {a['interval']} {arrow} ({pos_label}){pct_str}")
                print(f"     前趋势: {a['prev_trend']} → 现趋势: {a['curr_trend']}")
            print(f"{'='*60}\n")
            send_wecom_alert(position_alerts, cache["last_update"], alert_type="position")
    except Exception as e:
        cache["error"] = str(e)
    finally:
        cache["updating"] = False
        global _browser_opened
        if not _browser_opened:
            _browser_opened = True
            url = f"http://127.0.0.1:{PORT}"
            def delayed_open():
                time.sleep(1)
                webbrowser.open(url)
                print(f"[AUTO] Browser opened: {url}")
            threading.Thread(target=delayed_open, daemon=True).start()


def _next_refresh_delay() -> float:
    """计算到下一个对齐15分钟K线收盘的时间(秒)。
    提前55秒启动刷新(~45s刷新耗时 + ~10s缓冲)，数据在收盘前就绪。"""
    now = datetime.now()
    next_boundary = ((now.minute // 15) + 1) * 15
    target = now.replace(second=0, microsecond=0)
    if next_boundary >= 60:
        target = target.replace(minute=0) + timedelta(hours=1)
    else:
        target = target.replace(minute=next_boundary)
    target -= timedelta(seconds=55)
    delay = (target - now).total_seconds()
    if delay < 5:
        delay += 900  # 错过窗口，等下一个15分钟
    return delay


def auto_refresh_loop():
    """后台自动刷新线程 - 对齐K线收盘时间调度"""
    global _next_refresh_at
    while True:
        delay = _next_refresh_delay()
        _next_refresh_at = time.time() + delay  # 刷新即将开始
        time.sleep(delay)
        refresh_data()
        _next_refresh_at = time.time()  # 刷新完成，前端可立即拉取


# ============ HTML模板 ============
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gate.io MA10 趋势监控面板</title>
    <style>
        :root {
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-card: #334155;
            --text-primary: #f1f5f9;
            --text-secondary: #94a3b8;
            --up: #10b981;
            --up-bg: #064e3b;
            --down: #ef4444;
            --down-bg: #7f1d1d;
            --neutral: #6b7280;
            --neutral-bg: #374151;
            --border: #475569;
            --accent: #3b82f6;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            padding-bottom: 40px;
        }
        .header {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border);
            padding: 16px 20px;
            position: sticky;
            top: 0;
            z-index: 100;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }
        .header h1 { font-size: 1.2rem; color: var(--text-primary); }
        .header .subtitle { font-size: 0.75rem; color: var(--text-secondary); margin-top: 4px; }
        .header-right { display: flex; align-items: center; gap: 12px; }
        .update-info { font-size: 0.75rem; color: var(--text-secondary); }
        .btn {
            background: var(--accent);
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 0.85rem;
            cursor: pointer;
            transition: opacity 0.2s;
        }
        .btn:hover { opacity: 0.9; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .countdown { font-size: 0.75rem; color: var(--text-secondary); min-width: 80px; text-align: right; }
        .search-bar {
            display: flex; align-items: center; gap: 12px;
            margin-bottom: 12px; flex-wrap: wrap;
        }
        .search-input {
            background: var(--bg-secondary); border: 1px solid var(--border);
            border-radius: 8px; padding: 10px 14px;
            color: var(--text-primary); font-size: 0.9rem;
            width: 260px; max-width: 100%; outline: none;
            transition: border-color 0.2s;
        }
        .search-input:focus { border-color: var(--accent); }
        .search-input::placeholder { color: var(--text-secondary); }
        .search-count { font-size: 0.8rem; color: var(--text-secondary); white-space: nowrap; }
        .stats-bar {
            display: flex;
            gap: 16px;
            padding: 12px 20px;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border);
            overflow-x: auto;
            font-size: 0.8rem;
        }
        .stats-bar .stat { white-space: nowrap; }
        .stats-bar .stat span { color: var(--text-secondary); }
        .container { padding: 16px 20px; max-width: 1400px; margin: 0 auto; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
        @media (max-width: 480px) {
            .grid { grid-template-columns: 1fr; }
            .header h1 { font-size: 1rem; }
        }
        .card {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.3); }
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border);
        }
        .card-title { font-size: 1.1rem; font-weight: 600; display: flex; align-items: center; gap: 8px; }
        .card-pos {
            cursor: pointer; font-size: 0.9rem; font-weight: 700;
            width: 28px; height: 28px; border-radius: 6px;
            display: inline-flex; align-items: center; justify-content: center;
            border: 1px solid var(--border); color: var(--text-secondary);
            transition: all 0.2s; user-select: none; background: transparent;
        }
        .card-pos:hover { transform: scale(1.1); border-color: var(--text-secondary); }
        .card-pos.long { background: var(--up-bg); color: var(--up); border-color: var(--up); }
        .card-pos.short { background: var(--down-bg); color: var(--down); border-color: var(--down); }
        .card.long { border-color: var(--up); box-shadow: 0 0 0 1px var(--up), 0 8px 24px rgba(16,185,129,0.12); }
        .card.short { border-color: var(--down); box-shadow: 0 0 0 1px var(--down), 0 8px 24px rgba(239,68,68,0.12); }
        .pos-dropdown {
            position: fixed; z-index: 200;
            background: var(--bg-secondary); border: 1px solid var(--border);
            border-radius: 8px; padding: 4px;
            display: flex; flex-direction: column; gap: 2px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.4); min-width: 70px;
        }
        .pos-dropdown-option {
            padding: 8px 16px; border-radius: 4px;
            cursor: pointer; font-size: 0.85rem; font-weight: 600;
            text-align: center; border: none; background: transparent;
            color: var(--text-secondary); transition: background 0.15s;
            white-space: nowrap;
        }
        .pos-dropdown-option:hover { background: var(--bg-card); }
        .pos-dropdown-option.long { color: var(--up); }
        .pos-dropdown-option.short { color: var(--down); }
        .pos-dropdown-option.active { background: var(--bg-card); outline: 1px solid var(--border); }
        .card-price { text-align: right; }
        .card-price .last { font-size: 1.1rem; font-weight: 600; }
        .card-price .change { font-size: 0.8rem; margin-top: 2px; }
        .change-up { color: var(--up); }
        .change-down { color: var(--down); }
        .intervals { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
        @media (max-width: 560px) { .intervals { grid-template-columns: repeat(2, 1fr); } }
        @media (max-width: 400px) { .intervals { grid-template-columns: 1fr; } }
        .interval-item {
            border-radius: 8px;
            padding: 10px;
            text-align: center;
            border: 1px solid transparent;
            overflow: hidden;
        }
        .interval-item .name { font-size: 0.7rem; color: var(--text-secondary); margin-bottom: 4px; }
        .interval-item .trend { font-size: 0.8rem; font-weight: 600; margin-bottom: 2px; }
        .interval-item .detail { font-size: 0.7rem; color: var(--text-secondary); }
        .trend-up { background: var(--up-bg); border-color: var(--up); }
        .trend-up .trend { color: var(--up); }
        .trend-down { background: var(--down-bg); border-color: var(--down); }
        .trend-down .trend { color: var(--down); }
        .trend-neutral { background: var(--neutral-bg); border-color: var(--neutral); }
        .trend-neutral .trend { color: var(--text-secondary); }
        .trend-up-short { background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.4); }
        .trend-up-short .trend { color: var(--up); }
        .trend-down-short { background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.4); }
        .trend-down-short .trend { color: var(--down); }
        .loading {
            display: flex; justify-content: center; align-items: center; height: 60vh;
            flex-direction: column; gap: 16px; color: var(--text-secondary);
        }
        .spinner {
            width: 40px; height: 40px; border: 3px solid var(--border);
            border-top-color: var(--accent); border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .error-box {
            background: var(--down-bg); color: var(--down);
            padding: 16px; border-radius: 8px; margin: 20px;
            border: 1px solid var(--down);
        }
        .empty { text-align: center; color: var(--text-secondary); padding: 40px; }
        .alert-bar {
            display: none;
            padding: 12px 20px;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }
        .alert-bar.active { display: flex; }
        .alert-bar.alert-down { background: rgba(220, 38, 38, 0.15); border-bottom: 1px solid rgba(220, 38, 38, 0.3); }
        .alert-bar.alert-up { background: rgba(5, 150, 105, 0.15); border-bottom: 1px solid rgba(5, 150, 105, 0.3); }
        .alert-bar.alert-mixed { background: rgba(59, 130, 246, 0.15); border-bottom: 1px solid rgba(59, 130, 246, 0.3); }
        .alert-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.2s, transform 0.2s;
        }
        .alert-badge:hover { opacity: 0.85; transform: translateY(-1px); }
        .alert-badge.down { background: var(--down-bg); color: var(--down); border: 1px solid var(--down); }
        .alert-badge.up { background: var(--up-bg); color: var(--up); border: 1px solid var(--up); }
        .card-alert {
            margin-bottom: 10px;
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .card-alert.down { background: rgba(220, 38, 38, 0.2); color: #fca5a5; border: 1px solid rgba(220, 38, 38, 0.4); }
        .card-alert.up { background: rgba(5, 150, 105, 0.2); color: #6ee7b7; border: 1px solid rgba(5, 150, 105, 0.4); }
        @media (max-width: 480px) {
            .grid { grid-template-columns: 1fr; }
            .header h1 { font-size: 1rem; }
            .alert-bar { padding: 10px 12px; }
        }
        .tabs {
            display: flex;
            gap: 0;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border);
            padding: 0 20px;
        }
        .tab {
            padding: 10px 20px;
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-secondary);
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
            background: none;
            border-top: none;
            border-left: none;
            border-right: none;
        }
        .tab:hover { color: var(--text-primary); }
        .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
        .view { display: none; }
        .view.active { display: block; }
        .divergence-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; padding: 16px 20px; max-width: 1400px; margin: 0 auto; }
        @media (max-width: 480px) { .divergence-grid { grid-template-columns: 1fr; } }
        .divergence-card {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .divergence-card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.3); }
        .divergence-card.buy { border-color: var(--up); box-shadow: 0 0 0 1px var(--up), 0 8px 24px rgba(16,185,129,0.12); }
        .divergence-card.sell { border-color: var(--down); box-shadow: 0 0 0 1px var(--down), 0 8px 24px rgba(239,68,68,0.12); }
        .divergence-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .divergence-symbol { font-size: 1.1rem; font-weight: 600; }
        .divergence-signal {
            padding: 4px 12px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 700;
        }
        .divergence-signal.buy { background: var(--up-bg); color: var(--up); border: 1px solid var(--up); }
        .divergence-signal.sell { background: var(--down-bg); color: var(--down); border: 1px solid var(--down); }
        .divergence-detail { font-size: 0.85rem; color: var(--text-secondary); line-height: 1.6; }
        .divergence-detail .hl { color: var(--text-primary); font-weight: 600; }
        .divergence-empty { text-align: center; color: var(--text-secondary); padding: 60px 20px; }
        .divergence-desc {
            padding: 12px 20px;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border);
            font-size: 0.8rem;
            color: var(--text-secondary);
            max-width: 1400px;
            margin: 0 auto;
        }
        .tab-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 18px;
            height: 18px;
            padding: 0 5px;
            border-radius: 9px;
            font-size: 0.65rem;
            font-weight: 700;
            margin-left: 6px;
            vertical-align: middle;
        }
        .tab-badge.buy { background: var(--up-bg); color: var(--up); }
        .tab-badge.sell { background: var(--down-bg); color: var(--down); }
        .tab-badge.mixed { background: var(--accent); color: white; }
        .card-div-badge {
            display: inline-flex;
            align-items: center;
            gap: 3px;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.65rem;
            font-weight: 700;
            cursor: pointer;
            margin-left: 6px;
            transition: opacity 0.2s;
            vertical-align: middle;
        }
        .card-div-badge:hover { opacity: 0.85; }
        .card-div-badge.buy { background: var(--up-bg); color: var(--up); border: 1px solid var(--up); }
        .card-div-badge.sell { background: var(--down-bg); color: var(--down); border: 1px solid var(--down); }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>Gate.io U本位合约 MA10 监控</h1>
            <div class="subtitle">数据来自 Gate.io U本位永续合约市场</div>
        </div>
        <div class="header-right">
            <div class="update-info" id="updateInfo">加载中...</div>
            <button class="btn" id="refreshBtn" onclick="manualRefresh()">刷新数据</button>
            <div class="countdown" id="countdown"></div>
        </div>
    </div>

    <div class="tabs">
        <button class="tab active" id="tab-monitor" onclick="switchTab('monitor')">监控面板</button>
        <button class="tab" id="tab-divergence" onclick="switchTab('divergence')">周期背离<span id="divergenceBadge" class="tab-badge" style="display:none"></span></button>
    </div>

    <div id="view-monitor" class="view active">
    <div class="stats-bar" id="statsBar" style="display:none">
        <div class="stat">标的数: <span id="statTotal">-</span></div>
        <div class="stat">日K上涨: <span id="statDayUp">-</span></div>
        <div class="stat">日K下跌: <span id="statDayDown">-</span></div>
        <div class="stat">4小时上涨: <span id="stat4hUp">-</span></div>
        <div class="stat">4小时下跌: <span id="stat4hDown">-</span></div>
        <div class="stat">60分钟上涨: <span id="statHourUp">-</span></div>
        <div class="stat">60分钟下跌: <span id="statHourDown">-</span></div>
        <div class="stat">15分钟上涨: <span id="statMinUp">-</span></div>
        <div class="stat">15分钟下跌: <span id="statMinDown">-</span></div>
    </div>

    <div class="alert-bar" id="alertBar">
        <div style="font-weight:600; font-size:0.8rem; margin-right:8px;">转折预警:</div>
        <div id="alertList" style="display:flex; gap:8px; flex-wrap:wrap;"></div>
    </div>

    <div class="alert-bar" id="posAlertBar">
        <div style="font-weight:600; font-size:0.8rem; margin-right:8px; color:var(--accent);">持仓预警:</div>
        <div id="posAlertList" style="display:flex; gap:8px; flex-wrap:wrap;"></div>
    </div>

    <div class="container">
        <div id="loading" class="loading">
            <div class="spinner"></div>
            <div>正在加载监控数据...</div>
        </div>
        <div id="errorBox" class="error-box" style="display:none"></div>
        <div class="search-bar" id="searchBar" style="display:none">
            <input type="text" id="searchInput" class="search-input" placeholder="搜索合约...">
            <span class="search-count" id="searchCount"></span>
        </div>
        <div id="grid" class="grid" style="display:none"></div>
    </div>
    </div>

    <div id="view-divergence" class="view">
    <div class="divergence-desc">
        筛选规则：大周期（日K/4小时/60分钟）连续 ≥5 个周期同向，且相邻小周期方向相反时产生信号。
    </div>
    <div id="divergenceLoading" class="loading">
        <div class="spinner"></div>
        <div>正在分析周期背离...</div>
    </div>
    <div id="divergenceEmpty" class="divergence-empty" style="display:none">
        <div style="font-size:1.2rem; margin-bottom:8px;">暂无背离信号</div>
        <div>当前没有大周期连续 ≥5 周期且小周期反向的标的</div>
    </div>
    <div id="divergenceGrid" class="divergence-grid" style="display:none"></div>
    </div>

    <script>
        let countdownRemaining = 300;
        let countdownTimer;
        let dataCache = null;
        let alertsCache = [];
        let currentSearchQuery = '';

        function applyFilter() {
            var query = currentSearchQuery;
            var cards = document.querySelectorAll('#grid .card');
            var visibleCount = 0;
            cards.forEach(function(card) {
                var symbol = card.id.replace('card-', '').toLowerCase();
                var match = !query || symbol.indexOf(query) !== -1;
                card.style.display = match ? '' : 'none';
                if (match) visibleCount++;
            });
            var countEl = document.getElementById('searchCount');
            if (countEl) {
                countEl.textContent = '显示 ' + visibleCount + ' / ' + cards.length + ' 个标的';
            }
        }
        const POSITIONS_KEY = 'ma10_positions';
        let positionsCache = {};

        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.getElementById('tab-' + name).classList.add('active');
            document.getElementById('view-' + name).classList.add('active');
            if (name === 'divergence') {
                if (divergenceCache === null) {
                    loadDivergence();
                } else {
                    renderDivergence(divergenceCache);
                }
            }
        }

        let divergenceCache = null;
        let positionAlertsCache = [];

        async function loadDivergence() {
            const loading = document.getElementById('divergenceLoading');
            const empty = document.getElementById('divergenceEmpty');
            const grid = document.getElementById('divergenceGrid');
            loading.style.display = 'flex';
            empty.style.display = 'none';
            grid.style.display = 'none';
            try {
                const res = await fetch('/api/divergence');
                const payload = await res.json();
                divergenceCache = payload.data || [];
                renderDivergence(divergenceCache);
            } catch (e) {
                loading.style.display = 'none';
                empty.style.display = 'block';
                empty.innerHTML = '<div style="color:var(--down)">加载失败: ' + e.message + '</div>';
            }
        }

        function renderDivergence(signals) {
            const loading = document.getElementById('divergenceLoading');
            const empty = document.getElementById('divergenceEmpty');
            const grid = document.getElementById('divergenceGrid');
            loading.style.display = 'none';

            // 更新标签页 badge
            const badge = document.getElementById('divergenceBadge');
            if (signals && signals.length > 0) {
                const buyCount = signals.filter(s => s.signal === '买入信号').length;
                const sellCount = signals.length - buyCount;
                let badgeCls = 'mixed';
                if (buyCount > 0 && sellCount === 0) badgeCls = 'buy';
                else if (sellCount > 0 && buyCount === 0) badgeCls = 'sell';
                badge.className = 'tab-badge ' + badgeCls;
                badge.textContent = signals.length;
                badge.style.display = '';
            } else {
                badge.style.display = 'none';
            }

            if (!signals || signals.length === 0) {
                empty.style.display = 'block';
                grid.style.display = 'none';
                return;
            }

            empty.style.display = 'none';
            grid.style.display = 'grid';

            // 按信号类型分组排序：买入在前，卖出在后
            const sorted = [...signals].sort((a, b) => {
                if (a.signal === b.signal) return a.symbol.localeCompare(b.symbol);
                return a.signal === '买入信号' ? -1 : 1;
            });

            let html = '';
            sorted.forEach(s => {
                const cardClass = s.signal === '买入信号' ? 'divergence-card buy' : 'divergence-card sell';
                const signalClass = s.signal === '买入信号' ? 'divergence-signal buy' : 'divergence-signal sell';
                const changeClass = s.change_pct > 0 ? 'change-up' : (s.change_pct < 0 ? 'change-down' : '');
                const changeSign = s.change_pct > 0 ? '+' : '';
                const priceStr = s.last !== null ? s.last.toLocaleString('en-US', {maximumFractionDigits: 4}) : '-';
                const pctStr = s.change_pct !== null ? changeSign + s.change_pct.toFixed(2) + '%' : '-';

                html += `
                    <div class="${cardClass}">
                        <div class="divergence-header">
                            <div class="divergence-symbol">${s.symbol}</div>
                            <div class="${signalClass}">${s.signal}</div>
                        </div>
                        <div class="divergence-detail">
                            <div>价格: <span class="hl">${priceStr}</span> <span class="${changeClass}">${pctStr}</span></div>
                            <div>大周期: <span class="hl">${s.big_interval}</span> ${s.big_trend} (${s.big_consecutive}周期)</div>
                            <div>小周期: <span class="hl">${s.small_interval}</span> ${s.small_trend} (${s.small_consecutive}周期)</div>
                            <div style="margin-top:6px; color:var(--text-secondary); font-size:0.75rem;">
                                ${s.big_interval}连续${s.big_consecutive}周期${s.big_trend.includes('上涨') ? '上涨' : '下跌'}，
                                ${s.small_interval}与之反向，出现${s.signal === '买入信号' ? '潜在抄底' : '潜在见顶'}机会
                            </div>
                        </div>
                    </div>
                `;
            });
            grid.innerHTML = html;
        }

        async function loadPositionsFromServer() {
            try {
                const res = await fetch('/api/positions');
                const data = await res.json();
                positionsCache = data || {};
                localStorage.setItem(POSITIONS_KEY, JSON.stringify(positionsCache));
            } catch (e) {
                const raw = localStorage.getItem(POSITIONS_KEY);
                positionsCache = raw ? JSON.parse(raw) : {};
            }
        }

        function loadPositions() {
            return positionsCache;
        }

        function savePositions(positions) {
            positionsCache = positions;
            localStorage.setItem(POSITIONS_KEY, JSON.stringify(positions));
        }

        function scrollToCard(symbol) {
            const el = document.getElementById('card-' + symbol);
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                el.style.transition = 'box-shadow 0.3s';
                const originalShadow = el.style.boxShadow;
                el.style.boxShadow = '0 0 0 2px var(--accent), 0 8px 24px rgba(59,130,246,0.3)';
                setTimeout(() => { el.style.boxShadow = originalShadow; }, 1500);
            }
        }

        function showPositionDropdown(symbol, event) {
            event.stopPropagation();
            closePositionDropdown();

            var trigger = event.currentTarget;
            var rect = trigger.getBoundingClientRect();
            var positions = loadPositions();
            var current = positions[symbol] || null;

            var dropdown = document.createElement('div');
            dropdown.className = 'pos-dropdown';
            dropdown.id = 'posDropdown';

            var options = [
                { value: 'long', label: '多', cls: 'long' },
                { value: 'short', label: '空', cls: 'short' },
                { value: null, label: '无', cls: 'none' }
            ];

            options.forEach(function(opt) {
                var btn = document.createElement('button');
                btn.className = 'pos-dropdown-option ' + opt.cls;
                if (current === opt.value) btn.classList.add('active');
                btn.textContent = opt.label;
                btn.onclick = function(e) {
                    e.stopPropagation();
                    selectPosition(symbol, opt.value);
                };
                dropdown.appendChild(btn);
            });

            var left = Math.max(4, rect.left);
            if (left + 70 > window.innerWidth - 4) left = window.innerWidth - 74;
            dropdown.style.top = (rect.bottom + 4) + 'px';
            dropdown.style.left = left + 'px';

            document.body.appendChild(dropdown);
            setTimeout(function() {
                document.addEventListener('click', closePositionDropdown, { once: true });
            }, 0);
        }

        function closePositionDropdown() {
            var existing = document.getElementById('posDropdown');
            if (existing) existing.remove();
        }

        function selectPosition(symbol, newPos) {
            closePositionDropdown();

            var positions = JSON.parse(JSON.stringify(loadPositions()));
            if (newPos) {
                positions[symbol] = newPos;
            } else {
                delete positions[symbol];
            }
            savePositions(positions);
            updateCardPositionDOM(symbol, newPos);

            fetch('/api/position', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: symbol, position: newPos })
            }).catch(function(e) {
                console.error('同步持仓到服务器失败', e);
            });
        }

        function updateCardPositionDOM(symbol, newPos) {
            var card = document.getElementById('card-' + symbol);
            if (!card) return;

            card.classList.remove('long', 'short');
            if (newPos) card.classList.add(newPos);

            var badge = card.querySelector('.card-pos');
            if (badge) {
                badge.classList.remove('long', 'short');
                if (newPos) {
                    badge.classList.add(newPos);
                    badge.textContent = newPos === 'long' ? '多' : '空';
                } else {
                    badge.textContent = '＋';
                }
            }
        }

        function escapeHtml(str) {
            if (!str) return '';
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        function getTrendClass(trend) {
            if (trend === '连续上涨') return 'trend-up';
            if (trend === '连续下跌') return 'trend-down';
            if (trend === '短期上涨') return 'trend-up-short';
            if (trend === '短期下跌') return 'trend-down-short';
            return 'trend-neutral';
        }

        function formatPrice(price) {
            if (price === null || price === undefined) return '-';
            if (price >= 1000) return price.toLocaleString('en-US', {maximumFractionDigits: 2});
            if (price >= 1) return price.toFixed(4);
            return price.toFixed(6);
        }

        function sparklineSvg(data, trendClass) {
            if (!data || data.length < 2) return '';
            const vals = data.map(Number);
            const mn = Math.min(...vals);
            const mx = Math.max(...vals);
            const range = mx - mn || 1;
            const n = vals.length;
            const vw = n * 10;
            const vh = 16;
            const pts = vals.map((v, i) => {
                const x = i * (vw / (n - 1));
                const y = vh - 1 - ((v - mn) / range * (vh - 2));
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            });
            const color = trendClass.includes('up') ? '#10b981' : (trendClass.includes('down') ? '#ef4444' : '#6b7280');
            return `<svg width="100%" height="${vh}" viewBox="0 0 ${vw} ${vh}" style="display:block;margin:1px auto 0;max-width:100%">
                <polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="1" vector-effect="non-scaling-stroke"/>
            </svg>`;
        }

        function renderCards(data, alerts) {
            const grid = document.getElementById('grid');
            let html = '';
            let stats = { total: data.length, dayUp: 0, dayDown: 0, fourHUp: 0, fourHDown: 0, hourUp: 0, hourDown: 0, minUp: 0, minDown: 0 };
            const positions = loadPositions();

            const symAlerts = {};
            alerts.forEach(a => {
                if (!symAlerts[a.symbol]) symAlerts[a.symbol] = [];
                symAlerts[a.symbol].push(a);
            });

            // 构建标的→背离信号映射
            const symDivergence = {};
            (divergenceCache || []).forEach(d => {
                if (!symDivergence[d.symbol]) symDivergence[d.symbol] = [];
                symDivergence[d.symbol].push(d);
            });

            // 有持仓的卡片置顶 (short > long > 无)
            const posWeight = (p) => p === 'short' ? 2 : (p === 'long' ? 1 : 0);
            const sortedData = [...data].sort((a, b) => {
                return posWeight(positions[b.symbol]) - posWeight(positions[a.symbol]);
            });

            sortedData.forEach(item => {
                const pos = positions[item.symbol] || null;
                const changeClass = item.change_pct > 0 ? 'change-up' : (item.change_pct < 0 ? 'change-down' : '');
                const changeSign = item.change_pct > 0 ? '+' : '';

                let intervalsHtml = '';
                item.intervals.forEach(iv => {
                    const trendClass = getTrendClass(iv.trend);
                    const detail = iv.ma10 !== null
                        ? `MA10: ${formatPrice(iv.ma10)}<br>偏离: ${iv.deviation > 0 ? '+' : ''}${iv.deviation}%`
                        : (iv.candles_count < 20 ? `K线不足(${iv.candles_count})` : '无数据');
                    const countInfo = iv.consecutive > 0 ? `(${iv.consecutive}周期)` : '';

                    intervalsHtml += `
                        <div class="interval-item ${trendClass}">
                            <div class="name">${iv.name}</div>
                            <div class="trend">${iv.trend}${countInfo}</div>
                            <div class="detail">${detail}</div>
                            ${sparklineSvg(iv.ma_series, trendClass)}
                        </div>
                    `;

                    if (iv.name === '日K') {
                        if (iv.trend === '连续上涨' || iv.trend === '短期上涨') stats.dayUp++;
                        if (iv.trend === '连续下跌' || iv.trend === '短期下跌') stats.dayDown++;
                    }
                    if (iv.name === '4小时') {
                        if (iv.trend === '连续上涨' || iv.trend === '短期上涨') stats.fourHUp++;
                        if (iv.trend === '连续下跌' || iv.trend === '短期下跌') stats.fourHDown++;
                    }
                    if (iv.name === '60分钟') {
                        if (iv.trend === '连续上涨' || iv.trend === '短期上涨') stats.hourUp++;
                        if (iv.trend === '连续下跌' || iv.trend === '短期下跌') stats.hourDown++;
                    }
                    if (iv.name === '15分钟') {
                        if (iv.trend === '连续上涨' || iv.trend === '短期上涨') stats.minUp++;
                        if (iv.trend === '连续下跌' || iv.trend === '短期下跌') stats.minDown++;
                    }
                });

                const alertsForItem = symAlerts[item.symbol] || [];
                let cardAlertsHtml = '';
                alertsForItem.forEach(a => {
                    const cls = a.type === 'reversal_up' ? 'up' : 'down';
                    const arrow = a.type === 'reversal_up' ? '↑' : '↓';
                    const text = a.type === 'reversal_up' ? '上涨转折' : '下跌转折';
                    const pct = a.reversal_pct !== null && a.reversal_pct !== undefined ? ` 累计${a.reversal_pct > 0 ? '+' : ''}${a.reversal_pct}%` : '';
                    const cond = escapeHtml(a.condition);
                    const condStr = cond ? ` [${cond}]` : '';
                    cardAlertsHtml += `<div class="card-alert ${cls}">${arrow} ${a.interval}${text} (已${a.consecutive}周期)${pct}${condStr}</div>`;
                });

                const posLabel = pos ? (pos === 'long' ? '多' : '空') : '';
                const posClass = pos || '';
                const cardClass = pos ? `card ${pos}` : 'card';

                const divForSym = symDivergence[item.symbol] || [];
                const hasBuy = divForSym.some(d => d.signal === '买入信号');
                const hasSell = divForSym.some(d => d.signal === '卖出信号');
                let divBadgeHtml = '';
                if (divForSym.length > 0) {
                    const bCls = hasBuy && !hasSell ? 'buy' : (hasSell && !hasBuy ? 'sell' : 'mixed');
                    const bLabel = hasBuy && hasSell ? 'B/S' : (hasBuy ? 'B' : 'S');
                    const bCount = divForSym.length > 1 ? divForSym.length : '';
                    const bTitle = divForSym.map(d => `${d.signal}(${d.big_interval}→${d.small_interval})`).join('; ');
                    divBadgeHtml = `<span class="card-div-badge ${bCls}" onclick="event.stopPropagation();switchTab('divergence')" title="背离: ${bTitle}">${bLabel}${bCount}</span>`;
                }

                html += `
                    <div class="${cardClass}" id="card-${item.symbol}">
                        ${cardAlertsHtml}
                        <div class="card-header">
                            <div class="card-title">
                                <span class="card-pos ${posClass}" onclick="showPositionDropdown('${item.symbol}', event)" data-symbol="${item.symbol}" title="点击设置持仓">${posLabel || '＋'}</span>
                                ${item.symbol}${divBadgeHtml}
                            </div>
                            <div class="card-price">
                                <div class="last">${formatPrice(item.last)}</div>
                                <div class="change ${changeClass}">${changeSign}${item.change_pct !== null ? item.change_pct.toFixed(2) : '-'}%</div>
                            </div>
                        </div>
                        <div class="intervals">${intervalsHtml}</div>
                    </div>
                `;
            });

            grid.innerHTML = html;
            grid.style.display = 'grid';

            document.getElementById('searchBar').style.display = 'flex';
            applyFilter();

            document.getElementById('statsBar').style.display = 'flex';
            document.getElementById('statTotal').textContent = stats.total;
            document.getElementById('statDayUp').textContent = stats.dayUp;
            document.getElementById('statDayDown').textContent = stats.dayDown;
            document.getElementById('stat4hUp').textContent = stats.fourHUp;
            document.getElementById('stat4hDown').textContent = stats.fourHDown;
            document.getElementById('statHourUp').textContent = stats.hourUp;
            document.getElementById('statHourDown').textContent = stats.hourDown;
            document.getElementById('statMinUp').textContent = stats.minUp;
            document.getElementById('statMinDown').textContent = stats.minDown;

            const alertBar = document.getElementById('alertBar');
            const alertList = document.getElementById('alertList');
            if (alerts.length > 0) {
                let hasUp = false, hasDown = false;
                let alertHtml = '';
                alerts.forEach(a => {
                    const cls = a.type === 'reversal_up' ? 'up' : 'down';
                    const arrow = a.type === 'reversal_up' ? '↑' : '↓';
                    const text = a.type === 'reversal_up' ? '上涨转折' : '下跌转折';
                    const pct = a.reversal_pct !== null && a.reversal_pct !== undefined ? ` ${a.reversal_pct > 0 ? '+' : ''}${a.reversal_pct}%` : '';
                    const cond = escapeHtml(a.condition);
                    const condStr = cond ? `[${cond}]` : '';
                    alertHtml += `<span class="alert-badge ${cls}" onclick="scrollToCard('${a.symbol}')" title="点击跳转到 ${a.symbol} 卡片; 触发: ${cond || '-'}">${arrow} ${a.symbol} ${a.interval}${text}${pct} ${condStr}</span>`;
                    if (a.type === 'reversal_up') hasUp = true;
                    else hasDown = true;
                });
                alertList.innerHTML = alertHtml;
                alertBar.classList.add('active');
                alertBar.classList.remove('alert-up', 'alert-down', 'alert-mixed');
                if (hasUp && hasDown) alertBar.classList.add('alert-mixed');
                else if (hasUp) alertBar.classList.add('alert-up');
                else alertBar.classList.add('alert-down');
            } else {
                alertBar.classList.remove('active');
                alertList.innerHTML = '';
            }

            // 持仓预警栏
            const posAlertBar = document.getElementById('posAlertBar');
            const posAlertList = document.getElementById('posAlertList');
            const posAlerts = positionAlertsCache || [];
            if (posAlerts.length > 0) {
                let html = '';
                posAlerts.forEach(a => {
                    const cls = a.type === 'reversal_up' ? 'up' : 'down';
                    const arrow = a.type === 'reversal_up' ? '↑' : '↓';
                    const text = a.type === 'reversal_up' ? '上涨转折' : '下跌转折';
                    const posLabel = a.position === 'long' ? '多' : '空';
                    const pct = a.reversal_pct !== null && a.reversal_pct !== undefined ? ` ${a.reversal_pct > 0 ? '+' : ''}${a.reversal_pct}%` : '';
                    html += `<span class="alert-badge ${cls}" onclick="scrollToCard('${a.symbol}')" title="持仓${posLabel} ${a.symbol} ${a.interval}${text}">${arrow} ${a.symbol} ${a.interval}${text} (${posLabel})${pct}</span>`;
                });
                posAlertList.innerHTML = html;
                posAlertBar.classList.add('active');
            } else {
                posAlertBar.classList.remove('active');
                posAlertList.innerHTML = '';
            }
        }

        async function loadData() {
            try {
                const res = await fetch('/api/data');
                const payload = await res.json();
                document.getElementById('loading').style.display = 'none';

                if (payload.error) {
                    document.getElementById('errorBox').style.display = 'block';
                    document.getElementById('errorBox').textContent = payload.error;
                    return;
                }

                dataCache = payload.data;
                alertsCache = payload.alerts || [];
                divergenceCache = payload.divergence || [];
                positionAlertsCache = payload.position_alerts || [];
                renderCards(dataCache, alertsCache);
                document.getElementById('updateInfo').textContent = '更新于: ' + payload.last_update;
                document.getElementById('errorBox').style.display = 'none';
                countdownRemaining = payload.next_refresh_in || 300;
                // 如果当前在背离页面，同步刷新
                if (document.getElementById('view-divergence').classList.contains('active')) {
                    renderDivergence(divergenceCache);
                }
            } catch (e) {
                document.getElementById('loading').style.display = 'none';
                document.getElementById('errorBox').style.display = 'block';
                document.getElementById('errorBox').textContent = '加载失败: ' + e.message;
            }
        }

        async function manualRefresh() {
            const btn = document.getElementById('refreshBtn');
            btn.disabled = true;
            btn.textContent = '刷新中...';
            document.getElementById('loading').style.display = 'flex';
            document.getElementById('grid').style.display = 'none';

            try {
                await fetch('/api/refresh', { method: 'POST' });
                // 等待后台刷新完成
                let retries = 0;
                const check = setInterval(async () => {
                    const res = await fetch('/api/data');
                    const payload = await res.json();
                    if (!payload.updating || retries > 60) {
                        clearInterval(check);
                        await loadData();
                        btn.disabled = false;
                        btn.textContent = '刷新数据';
                    }
                    retries++;
                }, 1000);
            } catch (e) {
                btn.disabled = false;
                btn.textContent = '刷新数据';
                alert('刷新失败: ' + e.message);
            }
        }

        function startCountdown() {
            if (countdownTimer) clearInterval(countdownTimer);
            let isReloading = false;
            let lastReloadTime = 0;
            countdownTimer = setInterval(() => {
                countdownRemaining--;
                const m = Math.floor(Math.max(0, countdownRemaining) / 60);
                const s = Math.max(0, countdownRemaining) % 60;
                document.getElementById('countdown').textContent = `下次刷新: ${m}:${s.toString().padStart(2, '0')}`;
                if (countdownRemaining <= 0 && !isReloading && Date.now() - lastReloadTime > 15000) {
                    isReloading = true;
                    lastReloadTime = Date.now();
                    loadData().finally(() => { isReloading = false; });
                }
            }, 1000);
        }

        document.getElementById('searchInput').addEventListener('input', function() {
            currentSearchQuery = this.value.toLowerCase().trim();
            applyFilter();
        });

        loadPositionsFromServer().then(() => {
            loadData();
            startCountdown();
        });
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/data")
def api_data():
    return jsonify({
        "data": cache["data"],
        "last_update": cache["last_update"],
        "updating": cache["updating"],
        "error": cache["error"],
        "alerts": cache["alerts"],
        "position_alerts": cache["position_alerts"],
        "divergence": cache["divergence"],
        "next_refresh_in": max(0, _next_refresh_at - time.time())
    })


@app.route("/api/divergence")
def api_divergence():
    return jsonify({
        "data": cache["divergence"],
        "last_update": cache["last_update"]
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=refresh_data, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/positions", methods=["GET"])
def api_positions():
    return jsonify(user_positions)


@app.route("/api/position", methods=["POST"])
def api_position():
    global user_positions
    data = request.get_json() or {}
    symbol = data.get("symbol")
    position = data.get("position")
    if symbol:
        if position in ("long", "short"):
            user_positions[symbol] = position
        else:
            user_positions.pop(symbol, None)
        save_positions(user_positions)
    return jsonify({"status": "ok", "positions": user_positions})


def is_port_in_use(port: int) -> bool:
    """检测端口是否被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_process_on_port(port: int):
    """Windows 下结束占用指定端口的进程"""
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
        killed = []
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    if pid not in killed:
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                        killed.append(pid)
        if killed:
            print(f"  已结束占用端口 {port} 的进程: PID {', '.join(killed)}")
            time.sleep(1)
    except Exception:
        pass


if __name__ == "__main__":
    print_banner()

    if is_port_in_use(PORT):
        print(f"[WARN] Port {PORT} occupied. Terminating process...")
        kill_process_on_port(PORT)
        if is_port_in_use(PORT):
            print(f"[WARN] Fallback to alternate port {PORT + 1}")
            PORT += 1

    print(f"[ OK ] Service endpoint: http://127.0.0.1:{PORT}")
    print(f"[ OK ] LAN endpoint:    http://<local-ip>:{PORT}")
    print("[INIT] Spawning background sync thread...")
    print("-" * 55)

    # 后台立即刷新 + 自动刷新
    threading.Thread(target=refresh_data, daemon=True).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()

    # 抑制 Flask 默认启动日志，避免与后台进度条交错
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("flask.app").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
