#!/usr/bin/env python3
"""
测试用户提供的标的是在 Gate.io 上是否可用（使用 Gate.io 格式 XXX_YYY）
"""
import requests

BASE_URL = "https://api.gateio.ws/api/v4"

# 用户想加入的标的（尝试自动转换格式）
USER_SYMBOLS_NO_FORMAT = [
    "ZECUSDT", "HYPEUSDT", "NEARUSDT", "MUUSDT", "TRUMPUSDT",
    "SNDKUSDT", "NVDAUSDT", "QQQUSDT", "EWYUSDT", "INTCUSDT",
    "AMDUSDT", "AMZNUSDT"
]

# 转换为 Gate.io 格式（假设最后3-4个字符是计价币，如 USDT）
def to_gateio_format(sym):
    if sym.endswith("USDT"):
        return sym[:-4] + "_USDT"
    elif sym.endswith("BTC"):
        return sym[:-3] + "_BTC"
    elif sym.endswith("ETH"):
        return sym[:-3] + "_ETH"
    return sym

SYMBOLS = [to_gateio_format(s) for s in USER_SYMBOLS_NO_FORMAT]

def check_symbol(symbol):
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

print(f"{'Gate.io格式':<18s} {'原始格式':<15s} {'状态':<10s} {'最新价':>15s} {'24h涨跌':>10s}")
print("-" * 75)

available = []
unavailable = []

for original, gateio in zip(USER_SYMBOLS_NO_FORMAT, SYMBOLS):
    ok, last, change = check_symbol(gateio)
    if ok:
        print(f"{gateio:<18s} {original:<15s} {'可用':<10s} {last:>15,.4f} {change:>+9.2f}%")
        available.append((original, gateio))
    else:
        print(f"{gateio:<18s} {original:<15s} {'不可用':<10s} {'-':>15s} {'-':>10s}")
        unavailable.append((original, gateio))

print("-" * 75)
print(f"可用: {len(available)}/{len(USER_SYMBOLS_NO_FORMAT)}")
if available:
    print(f"可用列表: {[g for _, g in available]}")
if unavailable:
    print(f"不可用列表: {[g for _, g in unavailable]}")
    print("\n提示: Gate.io 是加密货币交易所，传统美股(如NVDA/AMD/AMZN/QQQ/EWY/SNDK/MU/INTC等)")
    print("      属于股票标的，不在加密货币现货市场交易。")
    print("      加密货币标的(如ZEC/NEAR/TRUMP/HYPE)如果不可用，说明 Gate.io 未上线该币种。")
