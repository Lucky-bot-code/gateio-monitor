#!/usr/bin/env python3
"""
扫描 Gate.io U本位合约市场，筛选用户指定标的中可用的合约
"""
import requests
import json

BASE_URL = "https://api.gateio.ws/api/v4"

USER_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT", "ZECUSDT", "NEARUSDT",
    "CLUSDT", "BSBUSDT", "XRPUSDT", "BEATUSDT", "DOGEUSDT", "SUIUSDT",
    "ONDOUSDT", "GMTUSDT", "INUSDT", "GRASSUSDT", "XAUUSDT", "XAGUSDT",
    "BILLUSDT", "TAOUSDT", "1000PEPEUSDT", "EDENUSDT", "WLDUSDT",
    "MUUSDT", "SNDKUSDT", "NVDAUSDT", "QQQUSDT", "SOXLUSDT", "CRCLUSDT",
    "EWYUSDT", "INTCUSDT", "MSTRUSDT", "SPYUSDT", "TSLAUSDT", "DRAMUSDT",
    "CBRSUSDT", "AMDUSDT", "QCOMUSDT", "GOOGLUSDT", "COINUSDT",
    "NATGASUSDT", "TSMUSDT", "AMZNUSDT"
]


def to_gateio_futures_format(sym: str) -> str:
    """将用户格式转换为 Gate.io 合约格式"""
    if sym.endswith("USDT"):
        return sym[:-4] + "_USDT"
    return sym


def fetch_all_contracts():
    url = f"{BASE_URL}/futures/usdt/contracts"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"获取合约列表失败: {e}")
        return []


def fetch_ticker(contract: str):
    url = f"{BASE_URL}/futures/usdt/tickers"
    try:
        resp = requests.get(url, params={"contract": contract}, timeout=10)
        data = resp.json()
        if data and len(data) > 0 and data[0].get("contract") == contract:
            return data[0]
    except Exception:
        pass
    return None


def main():
    print("=" * 70)
    print("Gate.io U本位合约标的可用性扫描")
    print("=" * 70)

    # 1. 获取所有合约名称
    print("\n正在获取 Gate.io U本位合约全量列表...")
    contracts = fetch_all_contracts()
    all_names = set(c.get("name", "") for c in contracts)
    print(f"Gate.io U本位合约总数: {len(all_names)}")

    # 2. 逐一匹配并测试
    print(f"\n开始测试 {len(USER_SYMBOLS)} 个标的...")
    print(f"{'用户标的':<18s} {'合约名称':<20s} {'状态':<10s} {'最新价':<15s} {'24h涨跌':<10s}")
    print("-" * 85)

    available = []
    unavailable = []

    for sym in USER_SYMBOLS:
        contract = to_gateio_futures_format(sym)

        if contract not in all_names:
            print(f"{sym:<18s} {contract:<20s} {'不存在':<10s} {'-':<15s} {'-':<10s}")
            unavailable.append(sym)
            continue

        ticker = fetch_ticker(contract)
        if ticker:
            last = float(ticker.get("last", 0))
            change = float(ticker.get("change_percentage", 0))
            print(f"{sym:<18s} {contract:<20s} {'可用':<10s} {last:<15.4f} {change:+.2f}%")
            available.append({
                "user_symbol": sym,
                "contract": contract,
                "last": last,
                "change_percentage": change
            })
        else:
            print(f"{sym:<18s} {contract:<20s} {'Ticker失败':<10s} {'-':<15s} {'-':<10s}")
            unavailable.append(sym)

    # 3. 输出汇总
    print("-" * 85)
    print(f"\n可用标的: {len(available)}/{len(USER_SYMBOLS)}")
    if available:
        names = [a["contract"] for a in available]
        print(f"  {names}")

    print(f"\n不可用标的: {len(unavailable)}/{len(USER_SYMBOLS)}")
    if unavailable:
        print(f"  {unavailable}")

    # 4. 保存可用列表到 JSON
    with open("gateio_available_symbols.json", "w", encoding="utf-8") as f:
        json.dump({
            "available": available,
            "unavailable": unavailable,
            "total": len(USER_SYMBOLS)
        }, f, ensure_ascii=False, indent=2)
    print(f"\n可用列表已保存至 gateio_available_symbols.json")


if __name__ == "__main__":
    main()
