"""
Gate.io U本位合约 数据获取 + MA 计算
"""
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Optional

import requests

BASE_URL = "https://api.gateio.ws/api/v4"
CONFIG_FILE = "gateio_available_symbols.json"
KLINES_LIMIT = 200

# SOCKS5 代理（仅用于 Gate.io API，不影响系统网络）
# 设为 None 则不使用代理，走直连
# 示例: PROXY_URL = "socks5://127.0.0.1:10808"
PROXY_URL = None

# 线程本地 Session（连接池复用）
_session_local = threading.local()

# 全局 API 并发限流（Gate.io 公开 API 约 20 req/s，安全阈值 ~10 req/s）
_api_sem = threading.Semaphore(5)
REQUEST_DELAY = 0.10


def get_session() -> requests.Session:
    if not hasattr(_session_local, "session"):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20, pool_maxsize=20, max_retries=1
        )
        s.mount("https://", adapter)
        if PROXY_URL:
            s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        _session_local.session = s
    return _session_local.session


# ========== MonitorCore ==========

class MonitorCore:
    _symbols_cache = None
    _symbols_cache_mtime = 0

    def __init__(self):
        self.symbols = self._load_symbols()

    @classmethod
    def _load_symbols(cls) -> List[Dict]:
        if not os.path.exists(CONFIG_FILE):
            return []
        mtime = os.path.getmtime(CONFIG_FILE)
        if cls._symbols_cache is not None and cls._symbols_cache_mtime == mtime:
            return cls._symbols_cache
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cls._symbols_cache = data.get("available", [])
        cls._symbols_cache_mtime = mtime
        return cls._symbols_cache

    def fetch_ticker(self, contract: str, session=None) -> Optional[Dict]:
        s = session or get_session()
        last_error = None
        for attempt in range(3):
            try:
                url = f"{BASE_URL}/futures/usdt/tickers"
                with _api_sem:
                    resp = s.get(url, params={"contract": contract}, timeout=(10, 15))
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    return data[0]
                last_error = data
                time.sleep(1.0 * (attempt + 1))
            except Exception as e:
                last_error = str(e)
                time.sleep(0.5 * (attempt + 1))
        if last_error:
            print(f"[WARN] fetch_ticker failed for {contract}: {last_error}", file=sys.stderr)
        return None

    def fetch_klines(
        self, contract: str, interval: str, limit: int = 100, session=None
    ) -> Optional[List[Dict]]:
        s = session or get_session()
        last_error = None
        for attempt in range(3):
            try:
                url = f"{BASE_URL}/futures/usdt/candlesticks"
                with _api_sem:
                    resp = s.get(
                        url,
                        params={"contract": contract, "interval": interval, "limit": limit},
                        timeout=(10, 15),
                    )
                data = resp.json()
                if isinstance(data, list):
                    return data
                last_error = data
                # 限流错误等更久，普通错误线性退避
                if isinstance(data, dict) and "TOO_MANY_REQUESTS" in str(data.get("label", "")):
                    time.sleep(3.0 * (attempt + 1))
                else:
                    time.sleep(1.0 * (attempt + 1))
            except Exception as e:
                last_error = str(e)
                time.sleep(0.5 * (attempt + 1))
        if last_error:
            print(f"[WARN] fetch_klines failed for {contract}/{interval}: {last_error}", file=sys.stderr)
        return None

    @staticmethod
    def calculate_ma(closes: List[float], period: int = 10) -> List[Optional[float]]:
        ma = []
        for i in range(len(closes)):
            if i < period - 1:
                ma.append(None)
            else:
                ma.append(sum(closes[i - period + 1 : i + 1]) / period)
        return ma

    @staticmethod
    def calculate_sar(highs, lows, af_start=0.02, af_step=0.02, af_max=0.20):
        """计算 Parabolic SAR，返回每个周期的 SAR 值列表"""
        n = len(highs)
        sar = [None] * n
        if n < 2:
            return sar
        is_long = True
        sar_val = lows[0]
        ep = highs[0]
        af = af_start
        sar[0] = sar_val
        for i in range(1, n):
            if is_long:
                sar_val = sar_val + af * (ep - sar_val)
                if i >= 1:
                    sar_val = min(sar_val, lows[i - 1])
                if i >= 2:
                    sar_val = min(sar_val, lows[i - 2])
                if lows[i] < sar_val:
                    is_long = False
                    sar_val = ep
                    ep = lows[i]
                    af = af_start
                sar[i] = sar_val
                if is_long and highs[i] > ep:
                    ep = highs[i]
                    af = min(af + af_step, af_max)
            else:
                sar_val = sar_val - af * (sar_val - ep)
                if i >= 1:
                    sar_val = max(sar_val, highs[i - 1])
                if i >= 2:
                    sar_val = max(sar_val, highs[i - 2])
                if highs[i] > sar_val:
                    is_long = True
                    sar_val = ep
                    ep = highs[i]
                    af = af_start
                sar[i] = sar_val
                if not is_long and lows[i] < ep:
                    ep = lows[i]
                    af = min(af + af_step, af_max)
        return sar

    @staticmethod
    def analyze_sar_trend(sar_values, closes, min_consecutive=3):
        """分析 SAR 相对价格的连续方向"""
        n = min(len(sar_values), len(closes))
        if n < min_consecutive + 1:
            return "数据不足", 0
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
        if consecutive_bull >= min_consecutive:
            return "连续上涨", consecutive_bull
        elif consecutive_bear >= min_consecutive:
            return "连续下跌", consecutive_bear
        elif consecutive_bull > 0:
            return "短期上涨", consecutive_bull
        elif consecutive_bear > 0:
            return "短期下跌", consecutive_bear
        else:
            return "震荡", 0

    @staticmethod
    def analyze_trend(
        ma_values: List[Optional[float]], min_consecutive: int = 3
    ) -> Tuple[str, int, List[float]]:
        valid_ma = [v for v in ma_values if v is not None]
        if len(valid_ma) < min_consecutive + 1:
            return "数据不足", 0, valid_ma

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

    def _process_interval(self, contract: str, interval: str, interval_name: str,
                          session) -> Dict:
        """获取单个周期K线并计算所有指标，返回该周期的 iv_data"""
        klines = self.fetch_klines(contract, interval, KLINES_LIMIT, session)
        empty = {
            "name": interval_name, "interval": interval,
            "trend": "数据不足", "consecutive": 0,
            "ma10": None, "close": None, "open": None,
            "reversal_pct": None, "sar_flip": None, "sar_direction": "neutral",
            "candles_count": len(klines) if klines else 0,
        }
        if not klines or len(klines) < 20:
            return empty

        klines_sorted = sorted(klines, key=lambda x: int(x["t"]))
        closes = [float(k["c"]) for k in klines_sorted]
        ma10 = self.calculate_ma(closes, period=10)
        valid_ma = [v for v in ma10 if v is not None]
        trend, consecutive, recent_ma = self.analyze_trend(ma10, min_consecutive=3)

        reversal_pct = None
        if consecutive >= 1 and trend not in ("数据不足", "震荡"):
            if len(closes) >= consecutive + 2:
                start_close = closes[-consecutive - 1]
                if start_close != 0:
                    reversal_pct = (closes[-1] - start_close) / start_close * 100

        # SAR 计算
        highs = [float(k["h"]) for k in klines_sorted]
        lows = [float(k["l"]) for k in klines_sorted]
        sar_values = MonitorCore.calculate_sar(highs, lows)
        sar_trend, sar_consecutive = MonitorCore.analyze_sar_trend(sar_values, closes)

        # SAR 翻转检测 + 方向判定
        sar_flip = None
        sar_direction = "neutral"
        if len(sar_values) >= 2 and sar_values[-2] is not None and sar_values[-1] is not None:
            prev_s = sar_values[-2]
            cur_s = sar_values[-1]
            cur_c = closes[-1]
            if cur_s < cur_c:
                sar_direction = "bullish"
            elif cur_s > cur_c:
                sar_direction = "bearish"
            prev_c = closes[-2]
            if prev_s > prev_c and cur_s < cur_c:
                sar_flip = "bullish"
            elif prev_s < prev_c and cur_s > cur_c:
                sar_flip = "bearish"

        # 价格/量数据
        cur_k = klines_sorted[-1]
        prev_k = klines_sorted[-2] if len(klines_sorted) >= 2 else None
        prev2_k = klines_sorted[-3] if len(klines_sorted) >= 3 else None
        open_price = float(cur_k["o"])
        prev_open = float(prev_k["o"]) if prev_k else None
        volume = float(cur_k["v"])
        prev_volume = float(prev_k["v"]) if prev_k else 0
        prev2_volume = float(prev2_k["v"]) if prev2_k else 0
        prev_high = float(prev_k["h"]) if prev_k else 0
        prev_low = float(prev_k["l"]) if prev_k else float("inf")
        volumes_10 = [float(k["v"]) for k in klines_sorted[-10:]]
        avg_volume_10 = round(sum(volumes_10) / len(volumes_10), 2) if volumes_10 else None

        # --- 极偏信号指标计算 (consecutive >= 5) ---
        ext_dev_avg = None
        ext_dev_max = None
        ext_dev_cur = None
        ext_chg_avg = None
        ext_chg_max = None
        ext_chg_cur = None
        if consecutive >= 5 and len(closes) > consecutive and len(ma10) > consecutive:
            n = consecutive
            devs = []
            chgs = []
            for i in range(-n, 0):
                c = closes[i]
                m = ma10[i]
                if m is not None and m != 0:
                    devs.append(abs((c - m) / m * 100))
                if i > -len(closes):
                    prev_c = closes[i - 1]
                    if prev_c != 0:
                        chgs.append(abs((c - prev_c) / prev_c * 100))
            if devs:
                ext_dev_cur = round(devs[-1], 4)
                ext_dev_avg = round(sum(devs) / len(devs), 4)
                ext_dev_max = round(max(devs), 4)
            if chgs:
                ext_chg_cur = round(chgs[-1], 4)
                ext_chg_avg = round(sum(chgs) / len(chgs), 4)
                ext_chg_max = round(max(chgs), 4)

        return {
            "name": interval_name,
            "interval": interval,
            "trend": trend,
            "consecutive": consecutive,
            "ma10": round(valid_ma[-1], 8) if valid_ma else None,
            "close": round(closes[-1], 8),
            "open": round(open_price, 8),
            "prev_open": round(prev_open, 8) if prev_open is not None else None,
            "reversal_pct": round(reversal_pct, 2) if reversal_pct is not None else None,
            "candles_count": len(klines),
            "ma_series": [round(v, 2) for v in recent_ma] if recent_ma else [],
            "volume": round(volume, 2),
            "prev_volume": round(prev_volume, 2),
            "prev2_volume": round(prev2_volume, 2),
            "avg_volume_10": avg_volume_10,
            "prev_high": round(prev_high, 8),
            "prev_low": round(prev_low, 8) if prev_low != float("inf") else float("inf"),
            "sar_trend": sar_trend,
            "sar_consecutive": sar_consecutive,
            "sar_flip": sar_flip,
            "sar_direction": sar_direction,
            # 极偏信号指标
            "ext_dev_avg": ext_dev_avg,
            "ext_dev_max": ext_dev_max,
            "ext_dev_cur": ext_dev_cur,
            "ext_chg_avg": ext_chg_avg,
            "ext_chg_max": ext_chg_max,
            "ext_chg_cur": ext_chg_cur,
        }

    def _fetch_symbol_data(self, sym_info: Dict) -> Dict:
        """获取单个标的的 ticker + 4 周期 K 线 + 技术指标"""
        contract = sym_info["contract"]
        user_symbol = sym_info.get("user_symbol", contract)
        result = {"symbol": user_symbol, "contract": contract, "intervals": []}

        session = get_session()

        # Ticker（带限流保护）
        ticker = self.fetch_ticker(contract, session)
        if ticker:
            result["last"] = float(ticker.get("last", 0))
            result["change_pct"] = float(ticker.get("change_percentage", 0))
            result["mark_price"] = float(ticker.get("mark_price", 0))
            result["index_price"] = float(ticker.get("index_price", 0))
            result["funding_rate"] = float(ticker.get("funding_rate", 0))
            result["volume_24h"] = float(ticker.get("volume_24h_quote", 0) or 0)
        else:
            result["last"] = None
            result["change_pct"] = None
            result["volume_24h"] = None

        # 各周期串行获取 + 短延迟限流
        for interval, interval_name in [
            ("1d", "日K"), ("4h", "4小时"), ("1h", "60分钟"), ("15m", "15分钟")
        ]:
            time.sleep(REQUEST_DELAY)
            try:
                iv_data = self._process_interval(contract, interval, interval_name, session)
            except Exception as e:
                print(f"[WARN] _process_interval failed for {contract}/{interval}: {e}", file=sys.stderr)
                iv_data = {
                    "name": interval_name, "interval": interval,
                    "trend": "数据不足", "consecutive": 0,
                    "ma10": None, "close": None, "open": None,
                    "reversal_pct": None, "sar_flip": None, "sar_direction": "neutral",
                    "candles_count": 0,
                }
            result["intervals"].append(iv_data)

        return result

    def analyze_all(self, progress_callback=None) -> List[Dict]:
        results = []
        total = len(self.symbols)
        if total == 0:
            return results
        workers = min(5, max(3, total // 20))
        print(f"[SYNC] Acquiring market data stream (parallel mode, {workers} workers)...")

        progress_lock = threading.Lock()
        completed = [0]

        def process_one(idx_sym):
            idx, sym_info = idx_sym
            data = self._fetch_symbol_data(sym_info)
            with progress_lock:
                completed[0] += 1
                if progress_callback:
                    progress_callback(completed[0], total, sym_info.get("user_symbol", sym_info["contract"]))
            return idx, data

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_one, (i, s)): i
                for i, s in enumerate(self.symbols)
            }
            results = [None] * total
            for future in as_completed(futures):
                try:
                    idx, data = future.result()
                except Exception as e:
                    orig_idx = futures[future]
                    sym = self.symbols[orig_idx].get("user_symbol", self.symbols[orig_idx]["contract"])
                    print(f"[WARN] {sym} fetch failed: {e}", file=sys.stderr)
                    continue
                results[idx] = data

        results = [r for r in results if r is not None]
        print(f"[ OK ] Data acquisition complete. {total} assets monitored.")
        return results
