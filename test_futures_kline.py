#!/usr/bin/env python3
"""测试 Gate.io 合约K线返回格式"""
import requests
import json

BASE_URL = "https://api.gateio.ws/api/v4"

def test_kline(contract, interval):
    url = f"{BASE_URL}/futures/usdt/candlesticks"
    try:
        resp = requests.get(url, params={"contract": contract, "interval": interval, "limit": 5}, timeout=15)
        data = resp.json()
        print(f"\n{contract} | {interval}")
        print(f"  类型: {type(data)}")
        if isinstance(data, list):
            print(f"  长度: {len(data)}")
            if len(data) > 0:
                print(f"  第一条类型: {type(data[0])}")
                print(f"  第一条内容: {data[0]}")
                print(f"  最后一条内容: {data[-1]}")
        else:
            print(f"  内容: {json.dumps(data, indent=2)[:500]}")
    except Exception as e:
        print(f"  错误: {e}")

# 测试几个合约和周期
test_kline("BTC_USDT", "1d")
test_kline("BTC_USDT", "1h")
test_kline("BTC_USDT", "15m")
test_kline("AMD_USDT", "1d")
