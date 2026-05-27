#!/usr/bin/env python3
"""
Gate.io U本位合约 MA10 趋势监控脚本
自动读取可用标的，不可用的已剔除
支持日K(1d)、60分钟(1h)、15分钟(15m)三个周期的MA10趋势判断
"""

import os
import sys
import json
import time
import requests
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============ 配置 ============
BASE_URL = "https://api.gateio.ws/api/v4"
CONFIG_FILE = "gateio_available_symbols.json"

INTERVALS = {
    "1d":  "日K",
    "4h":  "4小时",
    "1h":  "60分钟",
    "15m": "15分钟",
}

KLINES_LIMIT = 50  # 取50根K线
REQUEST_DELAY = 0.12  # 请求间隔(秒)，防限流（并行模式下仅影响同标的连续请求）
STATE_FILE = ".monitor_state.json"
POSITIONS_FILE = "positions.json"

# 微信通知配置（企业微信机器人 Webhook）
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=7bef2a11-7838-4859-a9c0-b65b6cf2dc36"


def send_wecom_alert(alerts: List[Dict], update_time: str, alert_type: str = "reversal") -> bool:
    """
    通过企业微信机器人推送预警消息。
    alert_type: "reversal" 转折预警 / "position" 持仓预警
    """
    if not WECOM_WEBHOOK_URL or not alerts:
        return False
    title = "MA10 转折预警" if alert_type == "reversal" else "持仓预警"
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
    if "上涨" in trend:
        return "up"
    elif "下跌" in trend:
        return "down"
    return "neutral"


def detect_reversal(prev_trend: str, curr_trend: str, curr_consecutive: int) -> Optional[str]:
    """检测趋势转折"""
    if curr_consecutive < 1 or curr_consecutive > 3:
        return None
    prev_dir = _trend_direction(prev_trend)
    curr_dir = _trend_direction(curr_trend)
    if prev_dir == "up" and curr_dir == "down":
        return "reversal_down"
    if prev_dir == "down" and curr_dir == "up":
        return "reversal_up"
    return None


def check_reversal_strength(iv: Dict) -> Optional[str]:
    """
    判断转折预警是否满足强度条件。
    日K 不预警。只对 4小时/60分钟/15分钟生效。

    consecutive=1: 量能放大2x + (价格>MA10 或 价格>前高)
    consecutive=2: 价格>MA10 或 价格>前二周期最高价
    consecutive>=3: 不预警
    """
    if iv["name"] == "日K":
        return None
    if iv.get("trend") in ("数据不足", "震荡") or iv["consecutive"] < 1:
        return None
    cons = iv["consecutive"]
    close = iv["close"]
    ma10 = iv["ma10"]

    if cons >= 3:
        return None

    if cons == 1:
        vol = iv.get("volume", 0)
        prev_vol = iv.get("prev_volume", 0)
        if not (prev_vol > 0 and vol >= prev_vol * 2):
            return None
        prev_high = iv.get("prev_high", 0)
        conds = []
        if close is not None and ma10 is not None and close > ma10:
            conds.append("价格>MA10")
        if close is not None and prev_high > 0 and close > prev_high:
            conds.append("价格>前高")
        if not conds:
            return None
        return f"量能放大{vol/prev_vol:.1f}x+" + "+".join(conds)

    if cons == 2:
        prev_high = iv.get("prev_high", 0)
        conds = []
        if close is not None and ma10 is not None and close > ma10:
            conds.append("价格>MA10")
        if close is not None and prev_high > 0 and close > prev_high:
            conds.append("价格>前高")
        if conds:
            return "+".join(conds)
        return None

    return None


def load_state() -> Optional[Dict]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_state(state: Dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  警告: 无法保存状态文件: {e}")


def load_positions() -> Dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def build_state(results: List[Dict]) -> Dict:
    state = {}
    for item in results:
        sym = item["symbol"]
        state[sym] = {iv["name"]: {"trend": iv["trend"], "consecutive": iv["consecutive"]} for iv in item.get("intervals", [])}
    return state


class GateioFuturesMonitor:
    def __init__(self):
        self.session = requests.Session()
        self.symbols = self._load_symbols()

    def _load_symbols(self) -> List[Dict]:
        """加载可用标的列表"""
        if not os.path.exists(CONFIG_FILE):
            print(f"错误: 未找到 {CONFIG_FILE}，请先运行扫描脚本获取可用标的。")
            sys.exit(1)
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        available = data.get("available", [])
        print(f"已加载 {len(available)} 个可用标的")
        return available

    def _get(self, endpoint: str, params: Dict = None, session=None) -> Optional[Dict]:
        """发送 GET 请求并返回 JSON"""
        s = session or self.session
        url = f"{BASE_URL}{endpoint}"
        try:
            resp = s.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            return None

    def _get_list(self, endpoint: str, params: Dict = None, session=None) -> Optional[List]:
        """发送 GET 请求并返回列表"""
        s = session or self.session
        url = f"{BASE_URL}{endpoint}"
        try:
            resp = s.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            return None

    def fetch_klines(self, contract: str, interval: str, limit: int = 100, session=None) -> Optional[List[List]]:
        """
        获取合约K线数据
        Gate.io futures candlesticks 返回: [[time, volume, close, high, low, open], ...]
        """
        data = self._get_list("/futures/usdt/candlesticks", {
            "contract": contract,
            "interval": interval,
            "limit": limit
        }, session=session)
        return data

    def fetch_ticker(self, contract: str, session=None) -> Optional[Dict]:
        """获取合约最新 ticker"""
        data = self._get_list("/futures/usdt/tickers", {"contract": contract}, session=session)
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None

    @staticmethod
    def calculate_ma(closes: List[float], period: int = 10) -> List[Optional[float]]:
        """计算 MA"""
        ma = []
        for i in range(len(closes)):
            if i < period - 1:
                ma.append(None)
            else:
                ma.append(sum(closes[i - period + 1:i + 1]) / period)
        return ma

    @staticmethod
    def analyze_ma_trend(ma_values: List[Optional[float]], min_consecutive: int = 3) -> Tuple[str, int, List[float]]:
        """分析 MA10 连续趋势"""
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
        """获取单个标的的 ticker + 各周期 K 线数据（并行安全）"""
        contract = sym_info["contract"]
        user_symbol = sym_info.get("user_symbol", contract)
        result = {"symbol": user_symbol, "contract": contract, "intervals": []}

        ticker = self.fetch_ticker(contract, session=session)
        if ticker:
            result["last"] = float(ticker.get("last", 0))
            result["change_pct"] = float(ticker.get("change_percentage", 0))
        else:
            result["last"] = None
            result["change_pct"] = None
        time.sleep(REQUEST_DELAY)

        for interval, interval_name in INTERVALS.items():
            klines = self.fetch_klines(contract, interval, limit=KLINES_LIMIT, session=session)
            time.sleep(REQUEST_DELAY)
            if not klines or len(klines) < 20:
                result["intervals"].append({
                    "name": interval_name,
                    "trend": "数据不足",
                    "consecutive": 0
                })
                continue
            klines_sorted = sorted(klines, key=lambda x: int(x["t"]))
            closes = [float(k["c"]) for k in klines_sorted]
            ma10 = self.calculate_ma(closes, period=10)
            valid_ma = [v for v in ma10 if v is not None]
            trend, consecutive, _ = self.analyze_ma_trend(ma10, min_consecutive=3)

            cur_k = klines_sorted[-1]
            prev_k = klines_sorted[-2] if len(klines_sorted) >= 2 else None
            prev2_k = klines_sorted[-3] if len(klines_sorted) >= 3 else None
            volume = float(cur_k["v"])
            prev_volume = float(prev_k["v"]) if prev_k else 0
            prev_high = float(prev_k["h"]) if prev_k else 0
            prev2_high = float(prev2_k["h"]) if prev2_k else 0

            reversal_pct = None
            if 1 <= consecutive <= 3 and trend not in ("数据不足", "震荡"):
                if len(closes) >= consecutive + 2:
                    start_close = closes[-consecutive - 1]
                    if start_close != 0:
                        reversal_pct = (closes[-1] - start_close) / start_close * 100

            result["intervals"].append({
                "name": interval_name,
                "trend": trend,
                "consecutive": consecutive,
                "reversal_pct": round(reversal_pct, 2) if reversal_pct is not None else None,
                "close": round(closes[-1], 4),
                "ma10": round(valid_ma[-1], 4) if valid_ma else None,
                "volume": round(volume, 2),
                "prev_volume": round(prev_volume, 2),
                "prev_high": round(prev_high, 4),
                "prev2_high": round(prev2_high, 4)
            })
        return result

    def _print_symbol_result(self, info: Dict):
        """打印已获取的单个标的分析结果（不发起网络请求）"""
        contract = info["contract"]
        user_symbol = info.get("user_symbol", contract)

        print(f"\n【{user_symbol} ({contract})】")
        print("-" * 60)

        if info.get("last"):
            change_pct = info.get("change_pct", 0)
            print(f"  最新价: {info['last']:,.4f}  24h涨跌: {change_pct:+.2f}%")

        for iv in info.get("intervals", []):
            iv_name = iv["name"]
            trend = iv.get("trend", "数据不足")
            consecutive = iv.get("consecutive", 0)
            close = iv.get("close")
            ma10 = iv.get("ma10")

            print(f"\n  [{iv_name}]")
            if trend == "数据不足":
                print(f"    数据不足")
                continue
            print(f"    最新价: {close:,.4f}" if close else "    最新价: N/A")
            print(f"    MA10: {ma10:,.4f}" if ma10 else "    MA10: N/A")

            if trend == "连续上涨":
                print(f"    趋势: >>> 【{trend}】 <<<  已连续 {consecutive} 周期")
            elif trend == "连续下跌":
                print(f"    趋势: >>> 【{trend}】 <<<  已连续 {consecutive} 周期")
            elif "上涨" in trend or "下跌" in trend:
                print(f"    趋势: 【{trend}】 ({consecutive} 周期)")
            else:
                print(f"    趋势: 【{trend}】")

            if close and ma10:
                deviation = (close - ma10) / ma10 * 100
                pos = "上方" if deviation > 0 else "下方"
                print(f"    偏离: {deviation:+.2f}% (价格在MA10{pos})")

    def analyze_symbol(self, symbol_info: Dict):
        """分析单个标的的全部周期"""
        contract = symbol_info["contract"]
        user_symbol = symbol_info.get("user_symbol", contract)

        print(f"\n【{user_symbol} ({contract})】")
        print("-" * 60)

        # 1. 最新价格
        ticker = self.fetch_ticker(contract)
        if ticker:
            last = float(ticker.get("last", 0))
            mark_price = float(ticker.get("mark_price", 0))
            index_price = float(ticker.get("index_price", 0))
            change_pct = float(ticker.get("change_percentage", 0))
            funding_rate = float(ticker.get("funding_rate", 0))
            print(f"  最新价: {last:,.4f}  标记价: {mark_price:,.4f}  指数价: {index_price:,.4f}")
            print(f"  24h涨跌: {change_pct:+.2f}%  资金费率: {funding_rate:.4f}%")
        else:
            print("  警告: 无法获取最新价格")

        # 2. 各周期 K 线与 MA10 分析
        for interval, interval_name in INTERVALS.items():
            print(f"\n  [{interval_name}]")
            klines = self.fetch_klines(contract, interval, limit=KLINES_LIMIT)
            time.sleep(REQUEST_DELAY)

            if not klines:
                print(f"    错误: 无法获取K线数据")
                continue

            if len(klines) < 20:
                print(f"    警告: 仅返回 {len(klines)} 根K线，数据不足")
                continue

            # Gate.io 合约K线返回字典列表: {t, v, c, h, l, o, sum}
            klines_sorted = sorted(klines, key=lambda x: int(x["t"]))
            times = [datetime.fromtimestamp(int(k["t"])).strftime("%m-%d %H:%M") for k in klines_sorted]
            closes = [float(k["c"]) for k in klines_sorted]
            opens = [float(k["o"]) for k in klines_sorted]
            highs = [float(k["h"]) for k in klines_sorted]
            lows = [float(k["l"]) for k in klines_sorted]

            # 计算 MA10
            ma10 = self.calculate_ma(closes, period=10)
            valid_ma = [v for v in ma10 if v is not None]

            print(f"    K线: {len(klines)}根 | {times[0]} ~ {times[-1]}")
            print(f"    最新: 收{closes[-1]:,.4f} 开{opens[-1]:,.4f} 高{highs[-1]:,.4f} 低{lows[-1]:,.4f}")
            print(f"    MA10: {valid_ma[-1]:,.4f}")

            # 趋势分析
            trend, consecutive, recent_ma = self.analyze_ma_trend(ma10, min_consecutive=3)

            if trend == "连续上涨":
                print(f"    趋势: >>> 【{trend}】 <<<  已连续 {consecutive} 周期")
            elif trend == "连续下跌":
                print(f"    趋势: >>> 【{trend}】 <<<  已连续 {consecutive} 周期")
            elif "上涨" in trend:
                print(f"    趋势: 【{trend}】 ({consecutive} 周期)")
            elif "下跌" in trend:
                print(f"    趋势: 【{trend}】 ({consecutive} 周期)")
            else:
                print(f"    趋势: 【{trend}】")

            recent_str = " -> ".join([f"{v:,.2f}" for v in recent_ma])
            print(f"    序列: {recent_str}")

            # 价偏离
            deviation = (closes[-1] - valid_ma[-1]) / valid_ma[-1] * 100
            pos = "上方" if deviation > 0 else "下方"
            print(f"    偏离: {deviation:+.2f}% (价格在MA10{pos})")

    def run(self):
        print("=" * 70)
        print(f"Gate.io U本位合约 MA10 趋势监控")
        print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"标的数: {len(self.symbols)}")
        print("=" * 70)
        print("说明: 数据来自 Gate.io U本位永续合约市场")
        print("      股权代币合约价格与真实股价存在 2-4% 溢价，趋势方向一致")
        print("=" * 70)

        total = len(self.symbols)
        results = [None] * total

        def process_one(idx_sym):
            idx, sym_info = idx_sym
            session = requests.Session()
            data = self._fetch_symbol_data(sym_info, session)
            session.close()
            print(f"\r  获取进度: {idx + 1}/{total} {data['symbol']}", end="", flush=True)
            return idx, data

        workers = min(8, max(3, total // 10))
        print(f"\n  正在并行获取数据 ({workers} 线程)...")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_one, (i, s)): i for i, s in enumerate(self.symbols)}
            for future in as_completed(futures):
                idx, data = future.result()
                results[idx] = data
        print("\n  数据获取完成。\n")

        results = [r for r in results if r is not None]
        for r in results:
            self._print_symbol_result(r)

        # 转折预警检测
        prev_state = load_state()
        alerts = []
        if prev_state:
            for item in results:
                sym = item["symbol"]
                if sym not in prev_state:
                    continue
                prev_sym = prev_state[sym]
                for iv in item["intervals"]:
                    iv_name = iv["name"]
                    if iv_name not in prev_sym:
                        continue
                    prev_iv = prev_sym[iv_name]
                    rev = detect_reversal(prev_iv["trend"], iv["trend"], iv["consecutive"])
                    if rev:
                        cond = check_reversal_strength(iv)
                        if not cond:
                            continue
                        alerts.append({
                            "symbol": sym,
                            "interval": iv_name,
                            "type": rev,
                            "prev_trend": prev_iv["trend"],
                            "curr_trend": iv["trend"],
                            "consecutive": iv["consecutive"],
                            "reversal_pct": iv.get("reversal_pct"),
                            "condition": cond
                        })

        if alerts:
            print("\n" + "=" * 70)
            print("  转折预警")
            print("=" * 70)
            for a in alerts:
                arrow = "↓ 下跌转折" if a["type"] == "reversal_down" else "↑ 上涨转折"
                pct = a.get("reversal_pct")
                pct_str = f" 累计{a['reversal_pct']:+.2f}%" if pct is not None else ""
                cond = a.get("condition", "")
                cond_str = f" [{cond}]" if cond else ""
                print(f"  [{a['symbol']}] {a['interval']} {arrow} (已{a['consecutive']}周期){pct_str}{cond_str}")
                print(f"     前: {a['prev_trend']} → 现: {a['curr_trend']}")
            print("=" * 70)
            # 推送微信通知
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ok = send_wecom_alert(alerts, now_str)
            if ok:
                print("  微信通知已发送")
            print("=" * 70)

        # 持仓预警
        positions = load_positions()
        position_alerts = []
        if prev_state and positions:
            for item in results:
                sym = item["symbol"]
                pos = positions.get(sym)
                if not pos:
                    continue
                if sym not in prev_state:
                    continue
                prev_sym = prev_state[sym]
                for iv in item["intervals"]:
                    iv_name = iv["name"]
                    if iv_name not in prev_sym:
                        continue
                    prev_iv = prev_sym[iv_name]
                    rev = detect_reversal(prev_iv["trend"], iv["trend"], iv["consecutive"])
                    if rev:
                        if (pos == "long" and rev == "reversal_down") or (pos == "short" and rev == "reversal_up"):
                            position_alerts.append({
                                "symbol": sym,
                                "interval": iv_name,
                                "type": rev,
                                "position": pos,
                                "prev_trend": prev_iv["trend"],
                                "curr_trend": iv["trend"],
                                "consecutive": iv["consecutive"],
                                "reversal_pct": iv.get("reversal_pct")
                            })

        if position_alerts:
            print("\n" + "=" * 70)
            print("  持仓预警")
            print("=" * 70)
            for a in position_alerts:
                pos_label = "做多" if a["position"] == "long" else "做空"
                arrow = "↓ 下跌转折" if a["type"] == "reversal_down" else "↑ 上涨转折"
                pct = a.get("reversal_pct")
                pct_str = f" 累计{a['reversal_pct']:+.2f}%" if pct is not None else ""
                print(f"  [{a['symbol']}] {a['interval']} {arrow} ({pos_label}){pct_str}")
                print(f"     前: {a['prev_trend']} → 现: {a['curr_trend']}")
            print("=" * 70)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            send_wecom_alert(position_alerts, now_str, alert_type="position")

        # 多周期背离检测
        divergence_signals = []
        hierarchy = [
            ("日K", "4小时"),
            ("4小时", "60分钟"),
            ("60分钟", "15分钟"),
        ]
        for item in results:
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
                        divergence_signals.append({
                            "symbol": symbol,
                            "big_interval": big_name,
                            "small_interval": small_name,
                            "big_trend": big["trend"],
                            "big_consecutive": big["consecutive"],
                            "small_trend": small["trend"],
                            "small_consecutive": small["consecutive"],
                            "signal": signal_type
                        })

        if divergence_signals:
            print("\n" + "=" * 70)
            print("  周期背离信号")
            print("=" * 70)
            for d in divergence_signals:
                arrow = "↑" if d["signal"] == "买入信号" else "↓"
                print(f"  {arrow} [{d['symbol']}] {d['signal']}")
                print(f"     {d['big_interval']}: {d['big_trend']} ({d['big_consecutive']}周期)")
                print(f"     {d['small_interval']}: {d['small_trend']} ({d['small_consecutive']}周期) — 与大周期反向")
            print("=" * 70)

        save_state(build_state(results))

        print("\n" + "=" * 70)
        print("监控完成")
        print("=" * 70)


if __name__ == "__main__":
    monitor = GateioFuturesMonitor()
    monitor.run()
