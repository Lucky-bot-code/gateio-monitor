#!/usr/bin/env python3
"""
Binance API MA10 趋势监控脚本
监控标的: BTCUSDT, ETHUSDT (对应 USD 锚定价格)
时间周期: 日K(1d)、60分钟(1h)、15分钟(15m)
指标: MA10 连续上涨 / 连续下跌 / 震荡
"""

import time
import requests
from datetime import datetime
from typing import List, Dict, Tuple, Optional

# ============ 配置 ============
BASE_URL = "https://api.binance.com"

SYMBOLS = {
    "BTC-USD": {"binance": "BTCUSDT", "name": "比特币"},
    "ETH-USD": {"binance": "ETHUSDT", "name": "以太坊"},
}

INTERVALS = {
    "1d":  "日K",
    "1h":  "60分钟",
    "15m": "15分钟",
}

# 获取 K 线数量：MA10 需要 10 根，再多取 10 根用于判断趋势
KLINES_LIMIT = 30


class BinanceMonitor:
    def __init__(self):
        self.session = requests.Session()

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """发送 GET 请求并返回 JSON"""
        url = f"{BASE_URL}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"    请求失败: {e}")
            return None

    def fetch_klines(self, symbol: str, interval: str, limit: int = 100) -> Optional[List[List]]:
        """
        获取K线数据
        返回: [[open_time, open, high, low, close, volume, close_time, ...], ...]
        """
        data = self._get("/api/v3/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })
        return data

    def fetch_price(self, symbol: str) -> Optional[float]:
        """获取最新价格"""
        data = self._get("/api/v3/ticker/price", {"symbol": symbol})
        if data and "price" in data:
            return float(data["price"])
        return None

    def fetch_24h_stats(self, symbol: str) -> Optional[Dict]:
        """获取24小时统计"""
        data = self._get("/api/v3/ticker/24hr", {"symbol": symbol})
        return data

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
        返回: (trend_str, consecutive_count, last_valid_ma_list)
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
        symbol = config["binance"]
        name = config["name"]
        print(f"\n【标的: {label} | {name}】")
        print("-" * 60)

        # 1. 最新价格与24h统计
        price = self.fetch_price(symbol)
        stats = self.fetch_24h_stats(symbol)
        if price:
            if stats:
                change = float(stats.get("priceChange", 0))
                change_pct = float(stats.get("priceChangePercent", 0))
                high_24h = float(stats.get("highPrice", 0))
                low_24h = float(stats.get("lowPrice", 0))
                print(f"  最新价: {price:,.2f} USDT")
                print(f"  24h涨跌: {change:+.2f} ({change_pct:+.2f}%)")
                print(f"  24h最高: {high_24h:,.2f}  最低: {low_24h:,.2f}")
            else:
                print(f"  最新价: {price:,.2f} USDT")
        else:
            print("  警告: 无法获取最新价格")

        # 2. 各周期 K 线与 MA10 分析
        for interval, interval_name in INTERVALS.items():
            print(f"\n  [{interval_name}] (interval={interval})")
            klines = self.fetch_klines(symbol, interval, limit=KLINES_LIMIT)

            if not klines:
                print(f"    错误: 无法获取K线数据")
                continue

            if len(klines) < 20:
                print(f"    警告: 仅返回 {len(klines)} 根K线，数据不足")
                continue

            # 解析收盘价
            closes = [float(k[4]) for k in klines]
            open_times = [datetime.fromtimestamp(k[0] / 1000).strftime("%m-%d %H:%M") for k in klines]

            # 计算 MA10
            ma10 = self.calculate_ma(closes, period=10)
            valid_ma = [v for v in ma10 if v is not None]

            print(f"    获取到 {len(klines)} 根K线 | 最新收盘: {closes[-1]:,.2f}")
            print(f"    MA10 最新值: {valid_ma[-1]:,.4f}")

            # 趋势分析
            trend, consecutive, recent_ma = self.analyze_ma_trend(ma10, min_consecutive=3)

            # 输出结果
            if "连续" in trend:
                print(f"    MA10 趋势: 【{trend}】 已连续 {consecutive} 周期")
            else:
                print(f"    MA10 趋势: 【{trend}】")

            # 展示最近 MA10 变化序列
            recent_str = " -> ".join([f"{v:,.2f}" for v in recent_ma])
            print(f"    MA10序列: {recent_str}")

            # 额外：计算当前价与 MA10 的位置关系
            current_close = closes[-1]
            current_ma10 = valid_ma[-1]
            deviation = (current_close - current_ma10) / current_ma10 * 100
            if deviation > 0:
                print(f"    价偏离MA10: +{deviation:.2f}% (价格在均线上方)")
            else:
                print(f"    价偏离MA10: {deviation:.2f}% (价格在均线下方)")

    def run(self):
        print("=" * 70)
        print(f"Binance MA10 趋势监控  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        print("说明: MA10连续上涨/下跌 = 最近N个MA10值单调递增/递减")
        print("      用于判断中短期趋势方向与强度")

        for label, config in SYMBOLS.items():
            self.analyze_symbol(label, config)

        print("\n" + "=" * 70)
        print("监控完成")
        print("=" * 70)


if __name__ == "__main__":
    monitor = BinanceMonitor()
    monitor.run()
