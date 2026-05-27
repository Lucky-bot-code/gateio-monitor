#!/usr/bin/env python3
"""
Gate.io U本位合约市场标的扫描器
按 24h 成交额 (volume_24h_quote) 排名，取前 N 个可用标的
"""
import requests
import json
import time

BASE_URL = "https://api.gateio.ws/api/v4"
CONFIG_FILE = "gateio_available_symbols.json"
TOP_N = 100  # 取成交额排名前 N 个


def main():
    print("=" * 70)
    print("Gate.io U本位合约标的扫描（按成交额排名 TOP {}）".format(TOP_N))
    print("=" * 70)

    # 1. 获取全量 ticker，按 24h 成交额排序
    print("\n正在获取全量 ticker（按 24h 成交额排名）...")
    try:
        resp = requests.get(f"{BASE_URL}/futures/usdt/tickers", timeout=30)
        resp.raise_for_status()
        all_tickers = resp.json()
    except Exception as e:
        print(f"获取 ticker 列表失败: {e}")
        return

    print(f"Gate.io U本位合约总数: {len(all_tickers)}")

    # 按 volume_24h_quote 降序排列
    sorted_tickers = sorted(
        all_tickers,
        key=lambda x: float(x.get("volume_24h_quote", 0)),
        reverse=True
    )

    # 2. 取前 N 个
    top_tickers = sorted_tickers[:TOP_N]
    print(f"取成交额前 {TOP_N} 个标的，正在验证...")
    print(f"{'排名':<6s} {'合约':<22s} {'最新价':<15s} {'24h涨跌':<10s} {'24h成交额(USD)':<18s}")
    print("-" * 75)

    available = []
    unavailable = []

    for i, t in enumerate(top_tickers, 1):
        contract = t["contract"]
        last = float(t.get("last", 0))
        change = float(t.get("change_percentage", 0))
        vol_quote = float(t.get("volume_24h_quote", 0))

        # 合约名转回用户符号名（去掉下划线）
        user_symbol = contract.replace("_", "")

        print(f"{i:<6d} {contract:<22s} {last:<15.4f} {change:+.2f}%     ${vol_quote:,.0f}")

        available.append({
            "user_symbol": user_symbol,
            "contract": contract,
            "last": last,
            "change_percentage": change,
            "volume_24h_quote": vol_quote,
            "rank": i
        })

    # 3. 输出汇总
    print("-" * 75)
    print(f"\n可用标的: {len(available)}/{TOP_N}")
    print(f"成交额范围: ${available[-1]['volume_24h_quote']:,.0f} ~ ${available[0]['volume_24h_quote']:,.0f}")

    # 按品类统计
    crypto_count = sum(1 for a in available if not any(a["user_symbol"].startswith(p) for p in ["MU", "SNDK", "NVDA", "QQQ", "SOXL", "CRCL", "EWY", "INTC", "MSTR", "SPY", "TSLA", "DRAM", "CBRS", "AMD", "QCOM", "GOOGL", "COIN", "NATGAS", "TSM", "AMZN", "BILL", "XAU", "XAG"]))
    print(f"其中加密货币/代币: ~{crypto_count}, 美股/ETF/商品代币: ~{len(available) - crypto_count}")

    # 4. 保存
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "available": available,
            "unavailable": [],
            "total": TOP_N,
            "source": "top_{}_by_volume_24h_quote".format(TOP_N)
        }, f, ensure_ascii=False, indent=2)
    print(f"\n可用列表已保存至 {CONFIG_FILE}")


if __name__ == "__main__":
    main()
