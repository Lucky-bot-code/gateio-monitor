"""
极偏信号回测脚本
加载成交额前100标的，拉取日K/4h/1h各1000根K线
逐日滚动计算四条件，统计多组(K,J)触发次数
"""
import json
import time
import sys
from collections import defaultdict
from monitor import MonitorCore, get_session, _api_sem

KLINES_LIMIT = 1000
EXCLUDE_INTERVALS = {"15m"}

# 测试的 (K, J) 组合
COMBO_MATRIX = [
    (2.0, 2.0),
    (2.5, 2.5),
    (3.0, 3.0),
]

TOLERANCE = 0.01


def analyze_trend_at_pos(ma_values, pos, min_consecutive=3):
    """在指定位置分析 MA10 趋势，返回 (trend, consecutive)"""
    valid_ma = [v for v in ma_values[:pos + 1] if v is not None]
    if len(valid_ma) < min_consecutive + 1:
        return "数据不足", 0
    consecutive_up = 0
    consecutive_down = 0
    for i in range(len(valid_ma) - 2, -1, -1):
        curr = valid_ma[i]
        nxt = valid_ma[i + 1]
        if nxt - curr > 1e-10:
            if consecutive_down > 0:
                break
            consecutive_up += 1
        elif curr - nxt > 1e-10:
            if consecutive_up > 0:
                break
            consecutive_down += 1
    if consecutive_up >= min_consecutive:
        return "连续上涨", consecutive_up
    elif consecutive_down >= min_consecutive:
        return "连续下跌", consecutive_down
    elif consecutive_up > 0:
        return "短期上涨", consecutive_up
    elif consecutive_down > 0:
        return "短期下跌", consecutive_down
    return "震荡", 0


def analyze_sar_at_pos(sar_values, closes, pos, min_consecutive=3):
    """在指定位置分析 SAR 趋势"""
    n = min(pos + 1, min(len(sar_values), len(closes)))
    if n < min_consecutive + 1:
        return "数据不足", 0, "neutral"
    consecutive_bull = 0
    consecutive_bear = 0
    for i in range(n - 1, -1, -1):
        sv = sar_values[i]
        cl = closes[i]
        if sv is None or cl is None:
            break
        if sv < cl:
            if consecutive_bear > 0:
                break
            consecutive_bull += 1
        elif sv > cl:
            if consecutive_bull > 0:
                break
            consecutive_bear += 1
    direction = "neutral"
    if sar_values[pos] is not None and closes[pos] is not None:
        if sar_values[pos] < closes[pos]:
            direction = "bullish"
        elif sar_values[pos] > closes[pos]:
            direction = "bearish"
    if consecutive_bull >= min_consecutive:
        return "连续上涨", consecutive_bull, direction
    elif consecutive_bear >= min_consecutive:
        return "连续下跌", consecutive_bear, direction
    elif consecutive_bull > 0:
        return "短期上涨", consecutive_bull, direction
    elif consecutive_bear > 0:
        return "短期下跌", consecutive_bear, direction
    return "震荡", 0, direction


def backtest_interval(klines, interval_name):
    """对单个周期回测，返回 {(K,J): {'极多': N, '极空': N}}"""
    if not klines or len(klines) < 20:
        return {}

    klines_sorted = sorted(klines, key=lambda x: int(x["t"]))
    closes = [float(k["c"]) for k in klines_sorted]
    highs = [float(k["h"]) for k in klines_sorted]
    lows = [float(k["l"]) for k in klines_sorted]

    # 预计算全量 MA10 和 SAR
    ma10_all = MonitorCore.calculate_ma(closes, period=10)
    sar_all = MonitorCore.calculate_sar(highs, lows)

    results = defaultdict(lambda: {"极多": 0, "极空": 0})

    for pos in range(20, len(closes)):
        trend, consecutive = analyze_trend_at_pos(ma10_all, pos)
        if consecutive < 5:
            continue

        sar_trend, sar_consecutive, sar_dir = analyze_sar_at_pos(sar_all, closes, pos)
        if sar_consecutive < 5:
            continue

        # 方向一致性
        if "上涨" in trend:
            ma10_dir = "bullish"
        elif "下跌" in trend:
            ma10_dir = "bearish"
        else:
            continue
        if sar_dir != ma10_dir:
            continue

        # 计算连续周期内的偏离和涨跌幅
        n = consecutive
        devs = []
        chgs = []
        for i in range(pos - n + 1, pos + 1):
            m = ma10_all[i]
            c = closes[i]
            if m is not None and m != 0:
                devs.append(abs((c - m) / m * 100))
            if i > 0:
                prev_c = closes[i - 1]
                if prev_c != 0:
                    chgs.append(abs((c - prev_c) / prev_c * 100))

        if not devs or not chgs:
            continue

        dev_cur = devs[-1]
        dev_avg = sum(devs) / len(devs)
        dev_max = max(devs)
        chg_cur = chgs[-1]
        chg_avg = sum(chgs) / len(chgs)
        chg_max = max(chgs)

        for K, J in COMBO_MATRIX:
            if (abs(dev_cur - dev_max) < TOLERANCE
                    and dev_cur >= dev_avg * K
                    and abs(chg_cur - chg_max) < TOLERANCE
                    and chg_cur >= chg_avg * J):
                label = "极多" if ma10_dir == "bullish" else "极空"
                results[(K, J)][label] += 1

    return dict(results)


def main():
    print("=" * 60)
    print("  极偏信号回测 — (K, J) 三组对比")
    print(f"  组合: {COMBO_MATRIX}")
    print("=" * 60)

    # 加载标的
    with open("gateio_available_symbols.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    symbols = cfg.get("available", [])
    if not symbols:
        print("[ERROR] 无可用标的")
        return
    print(f"\n[INFO] 加载 {len(symbols)} 个标的")

    intervals_to_test = [("1d", "日K"), ("4h", "4小时"), ("1h", "60分钟")]

    # 汇总统计
    totals = defaultdict(lambda: {"极多": 0, "极空": 0})
    interval_totals = defaultdict(lambda: defaultdict(lambda: {"极多": 0, "极空": 0}))

    # 每个标的结果
    detail = {}

    session = get_session()
    total = len(symbols)
    for idx, sym_info in enumerate(symbols):
        contract = sym_info["contract"]
        user_symbol = sym_info.get("user_symbol", contract)

        print(f"\r[{idx + 1}/{total}] {user_symbol} ...", end="", flush=True)

        sym_result = {}
        for interval, iv_name in intervals_to_test:
            try:
                with _api_sem:
                    resp = session.get(
                        "https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                        params={"contract": contract, "interval": interval, "limit": KLINES_LIMIT},
                        timeout=30,
                    )
                klines = resp.json()
                if not isinstance(klines, list) or len(klines) < 20:
                    continue
            except Exception as e:
                print(f"\n  [WARN] {user_symbol} {iv_name} 拉取失败: {e}")
                continue

            result = backtest_interval(klines, iv_name)
            if result:
                sym_result[iv_name] = result
                for (K, J), counts in result.items():
                    for label in ("极多", "极空"):
                        if counts[label] > 0:
                            totals[(K, J)][label] += counts[label]
                            interval_totals[iv_name][(K, J)][label] += counts[label]

        if sym_result:
            detail[user_symbol] = sym_result

        time.sleep(0.15)  # 限流

    print("\n")

    # ======== 输出结果 ========
    print("=" * 70)
    print("  回测结果汇总")
    print("=" * 70)

    for K, J in COMBO_MATRIX:
        key = (K, J)
        t = totals[key]
        total_count = t["极多"] + t["极空"]
        print(f"\n  [K={K:.1f}, J={J:.1f}]")
        print(f"    极多: {t['极多']:>5}  极空: {t['极空']:>5}  合计: {total_count:>5}")

        for iv_name in ["日K", "4小时", "60分钟"]:
            it = interval_totals[iv_name][key]
            it_total = it["极多"] + it["极空"]
            bar = "█" * min(50, it_total) if it_total > 0 else ""
            print(f"    {iv_name:6s}: 极多 {it['极多']:>4}  极空 {it['极空']:>4}  {bar}")

    # 触发最多的标的 TOP 10
    print(f"\n{'=' * 70}")
    print("  各标的触发次数 TOP 15")
    print(f"{'=' * 70}")
    sym_counts = {}
    for sym, intervals in detail.items():
        total_c = 0
        for iv_name, result in intervals.items():
            for (K, J), counts in result.items():
                if K == 2.0 and J == 2.0:  # 用最宽松组合展示
                    total_c += counts["极多"] + counts["极空"]
        if total_c > 0:
            sym_counts[sym] = total_c

    for rank, (sym, count) in enumerate(sorted(sym_counts.items(), key=lambda x: -x[1])[:15], 1):
        bar = "█" * min(40, count)
        print(f"  {rank:>2}. {sym:<14s} {count:>4} 次  {bar}")

    print(f"\n  (以上为 K=2.0,J=2.0 宽松组合的触发次数)")

    # 每日/每周平均
    print(f"\n{'=' * 70}")
    print("  频率估算（基于历史数据跨度）")
    print(f"{'=' * 70}")
    for K, J in COMBO_MATRIX:
        key = (K, J)
        t = totals[key]
        total_count = t["极多"] + t["极空"]
        # 日K约 4 年 (1000根交易日≈4年)，4h≈166天，1h≈41天
        print(f"\n  [K={K:.1f}, J={J:.1f}]  合计 {total_count} 次")
        daily_count = interval_totals["日K"][key]["极多"] + interval_totals["日K"][key]["极空"]
        fh_count = interval_totals["4小时"][key]["极多"] + interval_totals["4小时"][key]["极空"]
        h_count = interval_totals["60分钟"][key]["极多"] + interval_totals["60分钟"][key]["极空"]
        print(f"    日K:     {daily_count} 次 / ~4年 ≈ {daily_count/4:.1f} 次/年 = {daily_count/(4*365):.3f} 次/天")
        print(f"    4小时:   {fh_count} 次 / ~166天 ≈ {fh_count/166*30:.1f} 次/月")
        print(f"    60分钟:  {h_count} 次 / ~41天 ≈ {h_count/41*30:.1f} 次/月")


if __name__ == "__main__":
    main()
