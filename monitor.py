"""
Gate.io U本位合约 数据获取 + MA/MACD/RSI/布林带 计算
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
REQUEST_DELAY = 0.10
KLINES_LIMIT = 120

# 线程本地 Session（连接池复用）
_session_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_session_local, "session"):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20, pool_maxsize=20, max_retries=2
        )
        s.mount("https://", adapter)
        _session_local.session = s
    return _session_local.session


# ========== 技术指标计算 ==========

def calculate_ema(values: List[float], period: int) -> List[Optional[float]]:
    """指数移动平均，SMA 做种子"""
    k = 2.0 / (period + 1)
    n = len(values)
    ema = [None] * n
    if n < period:
        return ema
    ema[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def calculate_macd(closes: List[float]) -> Tuple[
    List[Optional[float]], List[Optional[float]], List[Optional[float]]
]:
    """返回 (dif, dea, histogram)，参数 EMA(12,26,9)"""
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    n = len(closes)
    dif = [None] * n
    for i in range(n):
        if ema12[i] is not None and ema26[i] is not None:
            dif[i] = ema12[i] - ema26[i]
    # DEA = EMA(DIF, 9)，DIF 有效后才计算
    padded = [(d if d is not None else 0.0) for d in dif]
    dea = calculate_ema(padded, 9)
    for i in range(n):
        if dif[i] is None:
            dea[i] = None
    histogram = [None] * n
    for i in range(n):
        if dif[i] is not None and dea[i] is not None:
            histogram[i] = (dif[i] - dea[i]) * 2
    return dif, dea, histogram


def calculate_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """Wilder 平滑 RSI"""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi = [None] * period
    rsi.append(100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss != 0 else 100.0)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss != 0:
            rs = avg_gain / avg_loss
            rsi.append(100.0 - (100.0 / (1.0 + rs)))
        else:
            rsi.append(100.0)
    return rsi


def calculate_bollinger(
    closes: List[float], period: int = 20, k: float = 2.0
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """返回 (upper, middle, lower)"""
    n = len(closes)
    upper = [None] * n
    middle = [None] * n
    lower = [None] * n
    if n < period:
        return upper, middle, lower
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        middle[i] = mean
        upper[i] = mean + k * std
        lower[i] = mean - k * std
    return upper, middle, lower


# ========== MonitorCore ==========

class MonitorCore:
    def __init__(self):
        self.symbols = self._load_symbols()

    def _load_symbols(self) -> List[Dict]:
        if not os.path.exists(CONFIG_FILE):
            return []
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("available", [])

    def fetch_ticker(self, contract: str, session=None) -> Optional[Dict]:
        s = session or get_session()
        try:
            url = f"{BASE_URL}/futures/usdt/tickers"
            resp = s.get(url, params={"contract": contract}, timeout=15)
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception:
            pass
        return None

    def fetch_klines(
        self, contract: str, interval: str, limit: int = 100, session=None
    ) -> Optional[List[Dict]]:
        s = session or get_session()
        last_error = None
        for attempt in range(3):
            try:
                url = f"{BASE_URL}/futures/usdt/candlesticks"
                resp = s.get(
                    url,
                    params={"contract": contract, "interval": interval, "limit": limit},
                    timeout=15,
                )
                data = resp.json()
                if isinstance(data, list):
                    return data
                # API returned error dict — likely rate limiting
                last_error = data
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
            if nxt > curr:
                if consecutive_down > 0:
                    break
                consecutive_up += 1
            elif nxt < curr:
                if consecutive_up > 0:
                    break
                consecutive_down += 1
            else:
                break

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

    def _fetch_symbol_data(self, sym_info: Dict) -> Dict:
        """获取单个标的的 ticker + 4 周期 K 线 + 技术指标"""
        contract = sym_info["contract"]
        user_symbol = sym_info.get("user_symbol", contract)
        result = {"symbol": user_symbol, "contract": contract, "intervals": []}

        session = get_session()

        # Ticker
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

        # 各周期 K 线 + 指标
        for interval, interval_name in [
            ("1d", "日K"), ("4h", "4小时"), ("1h", "60分钟"), ("15m", "15分钟")
        ]:
            time.sleep(REQUEST_DELAY)
            klines = self.fetch_klines(contract, interval, KLINES_LIMIT, session)

            if not klines or len(klines) < 20:
                result["intervals"].append({
                    "name": interval_name,
                    "interval": interval,
                    "trend": "数据不足",
                    "consecutive": 0,
                    "ma10": None,
                    "close": None,
                    "deviation": None,
                    "consecutive_dev_avg": None,
                    "consecutive_dev_max": None,
                    "candles_count": len(klines) if klines else 0,
                })
                continue

            klines_sorted = sorted(klines, key=lambda x: int(x["t"]))
            closes = [float(k["c"]) for k in klines_sorted]
            ma10 = self.calculate_ma(closes, period=10)
            valid_ma = [v for v in ma10 if v is not None]
            trend, consecutive, recent_ma = self.analyze_trend(ma10, min_consecutive=3)
            deviation = (
                (closes[-1] - valid_ma[-1]) / valid_ma[-1] * 100 if valid_ma else 0
            )

            # 连续周期的偏离统计（均偏、极偏）
            consecutive_dev_avg = None
            consecutive_dev_max = None
            if trend in ("连续上涨", "连续下跌") and consecutive > 0 and valid_ma:
                dev_series = []
                for i in range(len(closes)):
                    if ma10[i] is not None and ma10[i] != 0:
                        dev_series.append((closes[i] - ma10[i]) / ma10[i] * 100)
                    else:
                        dev_series.append(None)
                n = min(consecutive + 1, len(dev_series))
                devs = [d for d in dev_series[-n:] if d is not None]
                if devs:
                    consecutive_dev_avg = round(sum(devs) / len(devs), 2)
                    consecutive_dev_max = round(max(abs(d) for d in devs), 2)

            cur_k = klines_sorted[-1]
            prev_k = klines_sorted[-2] if len(klines_sorted) >= 2 else None
            volume = float(cur_k["v"])
            prev_volume = float(prev_k["v"]) if prev_k else 0
            prev_high = float(prev_k["h"]) if prev_k else 0
            prev_low = float(prev_k["l"]) if prev_k else float("inf")

            reversal_pct = None
            if 1 <= consecutive <= 3 and trend not in ("数据不足", "震荡"):
                if len(closes) >= consecutive + 2:
                    start_close = closes[-consecutive - 1]
                    if start_close != 0:
                        reversal_pct = (closes[-1] - start_close) / start_close * 100

            # 技术指标
            macd_dif, macd_dea, macd_hist = calculate_macd(closes)
            rsi6 = calculate_rsi(closes, 6)
            rsi12 = calculate_rsi(closes, 12)
            rsi24 = calculate_rsi(closes, 24)
            bb_upper, bb_middle, bb_lower = calculate_bollinger(closes, 20, 2.0)

            iv_data = {
                "name": interval_name,
                "interval": interval,
                "trend": trend,
                "consecutive": consecutive,
                "ma10": round(valid_ma[-1], 4) if valid_ma else None,
                "close": round(closes[-1], 4),
                "deviation": round(deviation, 2),
                "consecutive_dev_avg": consecutive_dev_avg,
                "consecutive_dev_max": consecutive_dev_max,
                "reversal_pct": round(reversal_pct, 2) if reversal_pct is not None else None,
                "candles_count": len(klines),
                "ma_series": [round(v, 2) for v in recent_ma] if recent_ma else [],
                "volume": round(volume, 2),
                "prev_volume": round(prev_volume, 2),
                "prev_high": round(prev_high, 4),
                "prev_low": round(prev_low, 4) if prev_low != float("inf") else float("inf"),
                "macd": {
                    "dif": round(macd_dif[-1], 4) if macd_dif[-1] is not None else None,
                    "dea": round(macd_dea[-1], 4) if macd_dea[-1] is not None else None,
                    "histogram": round(macd_hist[-1], 4) if macd_hist[-1] is not None else None,
                } if macd_dif[-1] is not None else None,
                "rsi": {
                    "rsi6": round(rsi6[-1], 1) if rsi6[-1] is not None else None,
                    "rsi12": round(rsi12[-1], 1) if rsi12[-1] is not None else None,
                    "rsi24": round(rsi24[-1], 1) if rsi24[-1] is not None else None,
                } if rsi6[-1] is not None else None,
                "bollinger": {
                    "upper": round(bb_upper[-1], 4) if bb_upper[-1] is not None else None,
                    "middle": round(bb_middle[-1], 4) if bb_middle[-1] is not None else None,
                    "lower": round(bb_lower[-1], 4) if bb_lower[-1] is not None else None,
                } if bb_upper[-1] is not None else None,
            }
            result["intervals"].append(iv_data)

        return result

    def analyze_all(self, progress_callback=None) -> List[Dict]:
        results = []
        total = len(self.symbols)
        if total == 0:
            return results
        workers = min(5, max(3, total // 10))
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
                idx, data = future.result()
                results[idx] = data

        results = [r for r in results if r is not None]
        print(f"[ OK ] Data acquisition complete. {total} assets monitored.")
        return results
