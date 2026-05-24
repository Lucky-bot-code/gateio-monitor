#!/usr/bin/env python3
"""
Finnhub API 稳定性与准确性测试脚本
测试内容：
1. 实时报价 (Quote) 延迟与准确性
2. K线历史数据完整性
3. 并发请求稳定性
4. 错误重试机制
"""

import os
import time
import asyncio
import aiohttp
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Any

# ============ 配置区域 ============
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "YOUR_API_KEY_HERE")
BASE_URL = "https://finnhub.io/api/v1"

# 测试标的（可自定义）
TEST_SYMBOLS = ["AAPL", "TSLA", "MSFT", "GOOGL", "0700.HK"]

# 并发与重试配置
CONCURRENT_REQUESTS = 10
MAX_RETRIES = 3
RETRY_DELAY = 1  # 秒


class FinnhubTester:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.results = []

    def _get(self, endpoint: str, params: Dict = None) -> Dict[str, Any]:
        """带重试的同步 GET 请求"""
        params = params or {}
        params["token"] = self.api_key
        url = f"{BASE_URL}{endpoint}"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.perf_counter()
                resp = self.session.get(url, params=params, timeout=10)
                latency = (time.perf_counter() - start) * 1000  # ms
                resp.raise_for_status()
                data = resp.json()
                return {"success": True, "latency_ms": latency, "data": data, "attempt": attempt}
            except requests.exceptions.RequestException as e:
                if attempt == MAX_RETRIES:
                    return {"success": False, "error": str(e), "attempt": attempt}
                time.sleep(RETRY_DELAY * attempt)
        return {"success": False, "error": "Max retries exceeded"}

    # -------------------- 测试用例 --------------------

    def test_quote(self, symbol: str) -> Dict:
        """测试实时报价接口"""
        print(f"  [Quote] 测试标的: {symbol} ...", end=" ")
        result = self._get("/quote", {"symbol": symbol})

        if result["success"]:
            data = result["data"]
            required_fields = ["c", "d", "dp", "h", "l", "o", "pc", "t"]
            missing = [f for f in required_fields if f not in data]

            if missing:
                print(f"FAIL (缺字段: {missing})")
                result["status"] = "FAIL"
                result["reason"] = f"Missing fields: {missing}"
            else:
                # 检查时间戳合理性（允许 5 分钟偏差）
                ts = data.get("t", 0)
                now = time.time()
                if abs(now - ts) > 300:
                    print(f"WARN (时间戳偏差 {abs(now-ts):.0f}s)")
                    result["status"] = "WARN"
                    result["reason"] = f"Timestamp drift: {abs(now-ts):.0f}s"
                else:
                    print(f"OK ({result['latency_ms']:.1f}ms)")
                    result["status"] = "PASS"
        else:
            print(f"FAIL ({result.get('error')})")
            result["status"] = "FAIL"

        result["symbol"] = symbol
        result["test"] = "quote"
        return result

    def test_candles(self, symbol: str, resolution: str = "D", count: int = 5) -> Dict:
        """测试 K 线历史数据"""
        print(f"  [Candles] 测试标的: {symbol} (周期: {resolution}) ...", end=" ")

        end = int(time.time())
        # 根据周期估算起始时间
        multipliers = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600, "D": 86400, "W": 604800, "M": 2592000}
        start = end - count * multipliers.get(resolution, 86400) * 2

        result = self._get("/stock/candle", {
            "symbol": symbol,
            "resolution": resolution,
            "from": start,
            "to": end
        })

        if result["success"]:
            data = result["data"]
            if data.get("s") == "no_data":
                print("FAIL (无数据)")
                result["status"] = "FAIL"
                result["reason"] = "No data returned"
            elif data.get("s") == "ok":
                o, h, l, c, v, t = data.get("o"), data.get("h"), data.get("l"), data.get("c"), data.get("v"), data.get("t")
                if all(isinstance(x, list) and len(x) >= count for x in [o, h, l, c, v, t]):
                    print(f"OK ({len(t)} 条, {result['latency_ms']:.1f}ms)")
                    result["status"] = "PASS"
                else:
                    print(f"WARN (数据条数不足: {len(t) if t else 0})")
                    result["status"] = "WARN"
                    result["reason"] = "Incomplete candle data"
            else:
                print(f"FAIL (状态: {data.get('s')})")
                result["status"] = "FAIL"
                result["reason"] = f"Status: {data.get('s')}"
        else:
            print(f"FAIL ({result.get('error')})")
            result["status"] = "FAIL"

        result["symbol"] = symbol
        result["test"] = "candles"
        return result

    async def test_concurrent(self, symbols: List[str]) -> List[Dict]:
        """测试并发请求稳定性"""
        print(f"\n[并发测试] {len(symbols)} 个标的，并发数 {CONCURRENT_REQUESTS} ...")

        semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

        async def fetch(symbol: str) -> Dict:
            async with semaphore:
                url = f"{BASE_URL}/quote"
                params = {"symbol": symbol, "token": self.api_key}
                start = time.perf_counter()
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            latency = (time.perf_counter() - start) * 1000
                            if resp.status == 200:
                                data = await resp.json()
                                return {"symbol": symbol, "status": "PASS", "latency_ms": latency, "data": data}
                            else:
                                return {"symbol": symbol, "status": "FAIL", "latency_ms": latency, "reason": f"HTTP {resp.status}"}
                except Exception as e:
                    latency = (time.perf_counter() - start) * 1000
                    return {"symbol": symbol, "status": "FAIL", "latency_ms": latency, "reason": str(e)}

        tasks = [fetch(s) for s in symbols]
        results = await asyncio.gather(*tasks)

        ok = sum(1 for r in results if r["status"] == "PASS")
        print(f"  结果: {ok}/{len(symbols)} 成功")
        latencies = [r["latency_ms"] for r in results if r["status"] == "PASS"]
        if latencies:
            print(f"  延迟: 平均 {sum(latencies)/len(latencies):.1f}ms, 最大 {max(latencies):.1f}ms, 最小 {min(latencies):.1f}ms")
        return results

    def run_all(self):
        """运行全部测试"""
        print("=" * 60)
        print("Finnhub API 稳定性与准确性测试")
        print(f"时间: {datetime.now().isoformat()}")
        print(f"API Key: {self.api_key[:4]}...{self.api_key[-4:] if len(self.api_key) > 8 else ''}")
        print("=" * 60)

        all_results = []

        # 1. 实时报价测试
        print("\n[实时报价测试]")
        for sym in TEST_SYMBOLS:
            all_results.append(self.test_quote(sym))

        # 2. K线数据测试
        print("\n[K线数据测试]")
        for sym in TEST_SYMBOLS[:3]:  # 取前3个减少请求量
            all_results.append(self.test_candles(sym, resolution="D", count=5))
            all_results.append(self.test_candles(sym, resolution="15", count=5))

        # 3. 并发测试
        loop = asyncio.get_event_loop()
        concurrent_results = loop.run_until_complete(self.test_concurrent(TEST_SYMBOLS))
        all_results.extend(concurrent_results)

        # 汇总
        print("\n" + "=" * 60)
        print("测试汇总")
        print("=" * 60)
        total = len(all_results)
        passed = sum(1 for r in all_results if r.get("status") == "PASS")
        warned = sum(1 for r in all_results if r.get("status") == "WARN")
        failed = sum(1 for r in all_results if r.get("status") == "FAIL")
        print(f"总计: {total} | 通过: {passed} | 警告: {warned} | 失败: {failed}")

        if failed > 0:
            print("\n失败详情:")
            for r in all_results:
                if r.get("status") == "FAIL":
                    print(f"  - {r.get('test', 'concurrent')}/{r.get('symbol', 'N/A')}: {r.get('reason', r.get('error', 'Unknown'))}")

        return all_results


if __name__ == "__main__":
    if FINNHUB_API_KEY == "YOUR_API_KEY_HERE":
        print("错误: 请设置 FINNHUB_API_KEY 环境变量，或修改脚本中的 FINNHUB_API_KEY 变量。")
        print("获取免费 API Key: https://finnhub.io/dashboard")
        exit(1)

    tester = FinnhubTester(FINNHUB_API_KEY)
    tester.run_all()
