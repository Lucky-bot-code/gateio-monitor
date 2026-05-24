#!/usr/bin/env python3
"""
Gate.io API MA10 趋势监控脚本
监控标的: BTC_USDT, ETH_USDT (对应 USD 锚定价格)
时间周期: 日K(1d)、60分钟(1h)、15分钟(15m)
指标: MA10 连续上涨 / 连续下跌 / 震荡
适用环境: 中国大陆网络（经测试 Gate.io API 可达）
"""

import time
import requests
from datetime import datetime
from typing import List, Dict, Tuple, Optional

# ============ 配置 ============
BASE_URL = "https://api.gateio.ws/api/v4"

SYMBOLS = {
    "BTC-USD": {"gateio": "BTC_USDT", "name": "比特币"},
    "ETH-USD": {"gateio": "ETH_USDT", "name": "以太坊"},
}

INTERVALS = {
    "1d":  "日K",
    "1h":  "60分钟",
    "15m": "15分钟",
}

KLINES_LIMIT = 50  # 取 50 根，确保 MA10 及后续趋势判断有足够数据


class GateioMonitor:
    def __init__(self):
        self.session = requests.Session()

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """发送 GET 请求并返回 JSON"""
        url = f"{BASE_URL}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"    请求失败: {e}")
            return None

    def fetch_klines(self, currency_pair: str, interval: str, limit: int = 100) -> Optional[List[List]]:
        """
        获取K线数据
        返回: [[time, volume, close, high, low, open], ...]
        注意 Gate.io 顺序: time, volume, close, high, low, open
        """
        data = self._get("/spot/candlesticks", {
            "currency_pair": currency_pair,
            "interval": interval,
            "limit": limit
        })
        return data

    def fetch_ticker(self, currency_pair: str) -> Optional[Dict]:
        """获取最新 ticker"""
        data = self._get("/spot/tickers", {"currency_pair": currency_pair})
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None

    @staticmethod
    def calculate_ma(closes: List[float], period: int = 10) -> List[Optional[float]]:
        """计算 MA，返回与 closes 等长的列表，前 period-1 个为 None"""
        ma = []
        for i in range(len(closes)):
            if i < period - 1:
                ma.append(None)
            else:
                ma.append(sum(closes[i - period + 1:i + 1]) / period)
        return ma

    @staticmethod
    def analyze_ma_trend(ma_values: List[Optional[float]], min_consecutive: int = 3) -> Tuple[str, int, List[float]]:
        """
        分析 MA10 连续趋势
        返回: (trend_str, consecutive_count, last_valid_ma_display_list)
        """
        valid_ma = [v for v in ma_values if v is not None]
        if len(valid_ma) < min_consecutive + 1:
            return "数据不足", 0, valid_ma

        # 从最新的 MA 往前数，计算连续同向次数
        consecutive_up = 0
        consecutive_down = 0

        # 从倒数第二个往前遍历，与后一个比较
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

        # 取最近 7 个值用于展示
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

    def analyze_symbol(self, label: str, config: Dict):
        """分析单个标的的全部周期"""
        pair = config["gateio"]
        name = config["name"]
        print(f"\n【标的: {label} | {name}】")
        print("-" * 60)

        # 1. 最新价格与 24h 统计
        ticker = self.fetch_ticker(pair)
        if ticker:
            last = float(ticker.get("last", 0))
            high_24h = float(ticker.get("high_24h", 0))
            low_24h = float(ticker.get("low_24h", 0))
            change_pct = float(ticker.get("change_percentage", 0))
            base_volume = float(ticker.get("base_volume", 0))
            print(f"  最新价: {last:,.2f} USDT")
            print(f"  24h涨跌: {change_pct:+.2f}%")
            print(f"  24h最高: {high_24h:,.2f}  最低: {low_24h:,.2f}")
            print(f"  24h成交量: {base_volume:,.4f} {pair.split('_')[0]}")
        else:
            print("  警告: 无法获取最新价格")

        # 2. 各周期 K 线与 MA10 分析
        for interval, interval_name in INTERVALS.items():
            print(f"\n  [{interval_name}] (interval={interval})")
            klines = self.fetch_klines(pair, interval, limit=KLINES_LIMIT)

            if not klines:
                print(f"    错误: 无法获取K线数据")
                continue

            if len(klines) < 20:
                print(f"    警告: 仅返回 {len(klines)} 根K线，数据不足")
                continue

            # Gate.io 返回顺序: [time, volume, close, high, low, open]
            # 按时间正序排列（Gate.io 默认似乎是倒序，最新的在前？需要确认）
            # 实际上通常最新的在前面，但为了安全，我们先按 time 排序
            klines_sorted = sorted(klines, key=lambda x: int(x[0]))

            times = [datetime.fromtimestamp(int(k[0])).strftime("%m-%d %H:%M") for k in klines_sorted]
            closes = [float(k[2]) for k in klines_sorted]  # index 2 = close
            opens = [float(k[5]) for k in klines_sorted]
            highs = [float(k[3]) for k in klines_sorted]
            lows = [float(k[4]) for k in klines_sorted]

            # 计算 MA10
            ma10 = self.calculate_ma(closes, period=10)
            valid_ma = [v for v in ma10 if v is not None]

            print(f"    获取到 {len(klines)} 根K线 | 时间范围: {times[0]} ~ {times[-1]}")
            print(f"    最新收盘: {closes[-1]:,.2f}  开盘: {opens[-1]:,.2f}  高: {highs[-1]:,.2f}  低: {lows[-1]:,.2f}")
            print(f"    MA10 最新值: {valid_ma[-1]:,.4f}")

            # 趋势分析
            trend, consecutive, recent_ma = self.analyze_ma_trend(ma10, min_consecutive=3)

            # 输出结果（带视觉强调）
            if trend == "连续上涨":
                print(f"    MA10 趋势: >>> 【{trend}】 <<<  已连续 {consecutive} 周期")
            elif trend == "连续下跌":
                print(f"    MA10 趋势: >>> 【{trend}】 <<<  已连续 {consecutive} 周期")
            elif "上涨" in trend:
                print(f"    MA10 趋势: 【{trend}】 ({consecutive} 周期)")
            elif "下跌" in trend:
                print(f"    MA10 趋势: 【{trend}】 ({consecutive} 周期)")
            else:
                print(f"    MA10 趋势: 【{trend}】")

            # 展示最近 MA10 变化序列
            recent_str = " -> ".join([f"{v:,.2f}" for v in recent_ma])
            print(f"    MA10序列: {recent_str}")

            # 额外：当前价与 MA10 的位置关系
            current_close = closes[-1]
            current_ma10 = valid_ma[-1]
            deviation = (current_close - current_ma10) / current_ma10 * 100
            if deviation > 0:
                print(f"    价偏离MA10: +{deviation:.2f}% (价格在均线上方)")
            else:
                print(f"    价偏离MA10: {deviation:.2f}% (价格在均线下方)")

    def run(self):
        print("=" * 70)
        print(f"Gate.io MA10 趋势监控  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        print("说明: MA10连续上涨/下跌 = 最近N个MA10值单调递增/递减")
        print("      用于判断中短期趋势方向与强度")
        print("=" * 70)

        for label, config in SYMBOLS.items():
            self.analyze_symbol(label, config)

        print("\n" + "=" * 70)
        print("监控完成")
        print("=" * 70)


if __name__ == "__main__":
    monitor = GateioMonitor()
    monitor.run()
