#!/usr/bin/env python3
"""
测试 Gate.io U本位合约市场是否有用户要的美股标的
"""
import requests

BASE_URL = "https://api.gateio.ws/api/v4"

TARGETS = ["NVDA", "AMD", "AMZN", "QQQ", "EWY", "SNDK", "MU", "INTC"]

def get_all_futures():
    url = f"{BASE_URL}/futures/usdt/contracts"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        return data
    except Exception as e:
        print(f"请求失败: {e}")
        return []

contracts = get_all_futures()
print(f"Gate.io U本位合约总数: {len(contracts)}\n")

# 提取所有合约名
all_names = [c.get("name", "") for c in contracts]

print(f"{'目标标的':<12s} {'合约名称':<18s} {'状态':<10s} {'最新价':>12s}")
print("-" * 60)

found = []
not_found = []

for target in TARGETS:
    # 尝试匹配
    pattern = f"{target}_USDT"
    matched = [n for n in all_names if n == pattern]
    if matched:
        # 获取ticker
        ticker_url = f"{BASE_URL}/futures/usdt/tickers"
        try:
            r = requests.get(ticker_url, params={"contract": pattern}, timeout=10)
            td = r.json()
            if td and len(td) > 0:
                last = float(td[0].get("last", 0))
                print(f"{target:<12s} {pattern:<18s} {'可用':<10s} {last:>12.4f}")
            else:
                print(f"{target:<12s} {pattern:<18s} {'可用*':<10s} {'-':>12s}")
        except Exception as e:
            print(f"{target:<12s} {pattern:<18s} {'可用*':<10s} {'-':>12s}")
        found.append(pattern)
    else:
        print(f"{target:<12s} {pattern:<18s} {'不可用':<10s} {'-':>12s}")
        not_found.append(target)

print("-" * 60)
print(f"可用: {len(found)}/{len(TARGETS)}")
if found:
    print(f"可用列表: {found}")
if not_found:
    print(f"不可用: {not_found}")
    print("\n提示: Gate.io 合约市场也没有这些传统美股标的。")
    print("      建议通过股票数据源(Alpha Vantage/Finnhub)获取美股数据。")
