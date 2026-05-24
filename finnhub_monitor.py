#!/usr/bin/env python3
"""
Finnhub 标的 MA10 趋势监控脚本
监控标的: BTC-USD, ETH-USD
时间周期: 日K、60分钟、15分钟
指标: MA10 连续上涨 / 连续下跌 / 震荡
"""

import os
import time
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import requests

# ============ 配置 ============
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "d88msopr01qq4343qr2gd88msopr01qq4343qr30")
BASE_URL = "https://finnhub.io/api/v1"

# 标的配置 (尝试多种可能的 symbol 映射)
SYMBOLS_CONFIG = {
    "BTC-USD": {
        "aliases": ["COINBASE:BTC-USD", "BINANCE:BTCUSDT", "BTCUSDT", "BTC-USD"],
        "resolutions": ["D", "60", "15"],
        "names": {"D": "日K", "60": "60分钟", "15": "15分钟"}
    },
    "ETH-USD": {
        "aliases": ["COINBASE:ETH-USD", "BINANCE:ETHUSDT", "ETHUSDT", "ETH-USD"],
        "resolutions": ["D", "60", "15"],
        "names": {"D": "日K", "60": "60分钟", "15": "15分钟"}
    }
}

# MA10 需要至少 10 + 3 = 13 根K线来判断连续趋势
MIN_CANDLES = 20


class FinnhubMonitor:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """发送 GET 请求并返回 JSON"""
        params = params or {}
        params["token"] = self.api_key
        url = f"{BASE_URL}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"    请求失败: {e}")
            return None

    def resolve_symbol(self, alias: str) -> bool:
        """测试 symbol 是否可用（通过 quote 接口快速验证）"""
        data = self._get("/quote", {"symbol": alias})
        if data and "c" in data and data["c"] > 0:
            return True
        return False

    def find_working_symbol(self, aliases: List[str]) -> Optional[str]:
        """在别名列表中找到第一个可用的 symbol"""
        for alias in aliases:
            if self.resolve_symbol(alias):
                return alias
        return None

    def fetch_candles(self, symbol: str, resolution: str, count: int = 100) -> Optional[Dict]:
        """
        获取K线数据
        对于不同分辨率，计算合适的 from/to 时间范围
        """
        end = int(time.time())
        # 多请求一些数据，确保有足够计算MA10
        multipliers = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600, "D": 86400, "W": 604800, "M": 2592000}
        sec_per_candle = multipliers.get(resolution, 86400)
        # 预留 2 倍数据量
        start = end - count * sec_per_candle * 2

        # Finnhub crypto 和 stock 使用相同 /stock/candle 接口处理很多情况
        # 但官方文档有 /crypto/candle，先尝试 /stock/candle，这是通用端点
        data = self._get("/stock/candle", {
            "symbol": symbol,
            "resolution": resolution,
            "from": start,
            "to": end
        })

        if data and data.get("s") == "ok":
            return data

        # 如果失败，尝试 /crypto/candle (仅对加密货币有效)
        data = self._get("/crypto/candle", {
            "symbol": symbol,
            "resolution": resolution,
            "from": start,
            "to": end
        })
        if data and data.get("s") == "ok":
            return data

        return None

    @staticmethod
    def calculate_ma10(closes: List[float]) -> List[float]:
        """计算 MA10，返回与 closes 等长的列表，前9个为 None"""
        ma = []
        for i in range(len(closes)):
            if i < 9:
                ma.append(None)
            else:
                ma.append(sum(closes[i-9:i+1]) / 10)
        return ma

    @staticmethod
    def analyze_trend(ma_values: List[float], lookback: int = 5) -> Tuple[str, int, List[float]]:
        """
        分析 MA10 连续趋势
        返回: (trend_str, consecutive_count, last_ma_slice)
        trend_str: 连续上涨 / 连续下跌 / 震荡 / 数据不足
        """
        valid_ma = [v for v in ma_values if v is not None]
        if len(valid_ma) < lookback + 1:
            return "数据不足", 0, valid_ma

        recent = valid_ma[-lookback-1:]  # 取最后 lookback+1 个，判断 lookback 次变化
        # 判断连续上涨: 每一个都 > 前一个
        # 判断连续下跌: 每一个都 < 前一个
        up_count = 0
        down_count = 0

        for i in range(1, len(recent)):
            if recent[i] > recent[i-1]:
                up_count += 1
                down_count = 0
            elif recent[i] < recent[i-1]:
                down_count += 1
                up_count = 0
            else:
                # 相等，趋势中断
                up_count = 0
                down_count = 0

        # 我们需要的是“连续”次数，上面的逻辑在每次变化时重置了
        # 重新计算更准确的连续次数
        consecutive_up = 0
        consecutive_down = 0
        for i in range(len(recent)-1, 0, -1):
            if recent[i] > recent[i-1]:
                if consecutive_down > 0:
                    break
                consecutive_up += 1
            elif recent[i] < recent[i-1]:
                if consecutive_up > 0:
                    break
                consecutive_down += 1
            else:
                break

        if consecutive_up >= 3:
            return "连续上涨", consecutive_up, recent
        elif consecutive_down >= 3:
            return "连续下跌", consecutive_down, recent
        elif consecutive_up > 0:
            return "短期上涨", consecutive_up, recent
        elif consecutive_down > 0:
            return "短期下跌", consecutive_down, recent
        else:
            return "震荡", 0, recent

    def run(self):
        print("=" * 70)
        print(f"Finnhub MA10 趋势监控  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        for label, config in SYMBOLS_CONFIG.items():
            print(f"\n【标的: {label}】")
            print("-" * 50)

            # 1. 寻找可用 symbol
            working_symbol = self.find_working_symbol(config["aliases"])
            if not working_symbol:
                print(f"  错误: 无法找到可用的 symbol 映射，已尝试: {config['aliases']}")
                continue
            print(f"  使用 Symbol: {working_symbol}")

            # 2. 获取实时报价
            quote = self._get("/quote", {"symbol": working_symbol})
            if quote:
                print(f"  最新价: {quote.get('c', 'N/A')}  "
                      f"涨跌: {quote.get('d', 'N/A')} ({quote.get('dp', 'N/A')}%)  "
                      f"高: {quote.get('h', 'N/A')} 低: {quote.get('l', 'N/A')}")

            # 3. 分分辨率获取K线并计算MA10趋势
            for res in config["resolutions"]:
                name = config["names"][res]
                print(f"\n  [{name}] 获取K线 (resolution={res})...")
                candles = self.fetch_candles(working_symbol, res, count=MIN_CANDLES)

                if not candles:
                    print(f"    失败: 无法获取K线数据")
                    continue

                t_list = candles.get("t", [])
                c_list = candles.get("c", [])
                o_list = candles.get("o", [])
                h_list = candles.get("h", [])
                l_list = candles.get("l", [])
                v_list = candles.get("v", [])

                if len(c_list) < MIN_CANDLES:
                    print(f"    警告: 仅获取到 {len(c_list)} 根K线，需要至少 {MIN_CANDLES} 根")
                    continue

                print(f"    成功: 获取 {len(c_list)} 根K线，最新收盘 {c_list[-1]:.2f}")

                # 计算 MA10
                ma10 = self.calculate_ma10(c_list)
                valid_ma = [v for v in ma10 if v is not None]
                print(f"    MA10 最新值: {valid_ma[-1]:.4f} (共 {len(valid_ma)} 个有效值)")

                # 分析趋势
                trend, consecutive, recent_vals = self.analyze_trend(ma10, lookback=5)
                print(f"    MA10 趋势判断: 【{trend}】", end="")
                if consecutive > 0:
                    print(f" (连续 {consecutive} 周期)")
                else:
                    print()

                # 打印最近几个MA值供参考
                recent_str = " -> ".join([f"{v:.4f}" for v in recent_vals])
                print(f"    近期MA10序列: {recent_str}")

                # 保存结果到结构体（方便后续扩展）
                result = {
                    "symbol_label": label,
                    "symbol_api": working_symbol,
                    "resolution": res,
                    "resolution_name": name,
                    "latest_price": c_list[-1],
                    "ma10_latest": valid_ma[-1],
                    "trend": trend,
                    "consecutive": consecutive,
                    "recent_ma10": recent_vals,
                    "candles_count": len(c_list)
                }
                # 可在此将 result 推送到数据库/消息队列/日志

        print("\n" + "=" * 70)
        print("监控完成")
        print("=" * 70)


if __name__ == "__main__":
    monitor = FinnhubMonitor(FINNHUB_API_KEY)
    monitor.run()
