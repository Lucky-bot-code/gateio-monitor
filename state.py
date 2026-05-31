"""
状态持久化：MA10 状态 / 持仓标记 / SQLite K线缓存
"""
import json
import os
import sqlite3
import sys
import tempfile
from typing import Dict, List, Optional

ALERT_STATE_FILE = ".ma10_state.json"
POSITIONS_FILE = "positions.json"
PRICE_ALERTS_FILE = "price_alerts.json"
DB_PATH = "klines.db"


def _atomic_write(path: str, data):
    """原子写入：先写临时文件，再替换，防止崩溃损坏"""
    dirname = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=dirname, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=None, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[ERROR] Failed to write {path}: {e}", file=sys.stderr)


# ========== MA10 状态持久化 ==========

def load_prev_state() -> Dict[str, Dict]:
    """加载上次保存的状态。仅支持 interval-first 格式。"""
    empty: Dict[str, Dict] = {"日K": {}, "4小时": {}, "60分钟": {}, "15分钟": {}}
    if not os.path.exists(ALERT_STATE_FILE):
        return empty
    try:
        with open(ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return empty
    if not raw:
        return empty
    for k in empty:
        if k not in raw:
            raw[k] = {}
    return raw


def save_state(state: Dict):
    _atomic_write(ALERT_STATE_FILE, state)


# ========== 持仓标记持久化 ==========

def load_positions() -> Dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_positions(positions: Dict):
    _atomic_write(POSITIONS_FILE, positions)


# ========== 价格提醒持久化 ==========

def load_price_alerts() -> Dict:
    if os.path.exists(PRICE_ALERTS_FILE):
        try:
            with open(PRICE_ALERTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_price_alerts(alerts: Dict):
    _atomic_write(PRICE_ALERTS_FILE, alerts)


# ========== SQLite K线缓存 ==========

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            contract TEXT NOT NULL,
            interval TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (contract, interval, timestamp)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_klines_lookup
        ON klines(contract, interval, timestamp)
    """)
    conn.commit()
    conn.close()


def get_cached_klines(contract: str, interval: str, limit: int = 100) -> Optional[List[Dict]]:
    """缓存命中返回 K线列表，否则返回 None"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM klines
               WHERE contract=? AND interval=?
               ORDER BY timestamp DESC LIMIT ?""",
            (contract, interval, limit),
        ).fetchall()
        conn.close()
        if len(rows) >= limit:
            klines = [dict(r) for r in reversed(rows)]
            # 转换字段名为 Gate.io API 格式
            return [
                {"t": str(k["timestamp"]), "o": k["open"], "h": k["high"],
                 "l": k["low"], "c": k["close"], "v": k["volume"]}
                for k in klines
            ]
        return None
    except Exception:
        return None


def store_klines(contract: str, interval: str, klines: List[Dict]):
    """批量写入 K线 (INSERT OR REPLACE)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("BEGIN")
        for k in klines:
            conn.execute(
                """INSERT OR REPLACE INTO klines
                   (contract, interval, timestamp, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contract, interval, int(k["t"]),
                    float(k["o"]), float(k["h"]), float(k["l"]),
                    float(k["c"]), float(k["v"]),
                ),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Failed to store klines: {e}", file=sys.stderr)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
