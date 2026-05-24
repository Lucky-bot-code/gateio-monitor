#!/usr/bin/env python3
"""
测试用户提供的标的是在 Gate.io 上是否可用
"""
import requests

BASE_URL = "https://api.gateio.ws/api/v4"

# 用户想加入的标的
USER_SYMBOLS = [
    "ZECUSDT", "HYPEUSDT", "NEARUSDT", "MUUSDT", "TRUMPUSDT",
    "SNDKUSDT", "NVDAUSDT", "QQQUSDT", "EWYUSDT", "INTCUSDT",
    "AMDUSDT", "AMZNUSDT"
]

def check_symbol(symbol):
    """通过 ticker 接口检查标的是否存在"""
    url = f"{BASE_URL}/spot/tickers"
    try:
        resp = requests.get(url, params={"currency_pair": symbol}, timeout=15)
        data = resp.json()
        if data and len(data) > 0 and data[0].get("currency_pair") == symbol:
            ticker = data[0]
            last = float(ticker.get("last", 0))
            change = float(ticker.get("change_percentage", 0))
            return True, last, change
        return False, None, None
    except Exception as e:
        return False, None, str(e)

print(f"{'标的':<15s} {'状态':<10s} {'最新价':>15s} {'24h涨跌':>10s}")
print("-" * 60)

available = []
unavailable = []

for sym in USER_SYMBOLS:
    ok, last, change = check_symbol(sym)
    if ok:
        print(f"{sym:<15s} {'可用':<10s} {last:>15,.4f} {change:>+9.2f}%")
        available.append(sym)
    else:
        print(f"{sym:<15s} {'不可用':<10s} {'-':>15s} {'-':>10s}")
        unavailable.append(sym)

print("-" * 60)
print(f"可用: {len(available)}/{len(USER_SYMBOLS)}")
if unavailable:
    print(f"不可用: {unavailable}")
    print("\n提示: Gate.io 是加密货币交易所，传统美股(如NVDA/AMD/AMZN/QQQ等)")
    print("      需要通过股票数据源获取，Gate.io 上通常没有这些现货交易对。")
