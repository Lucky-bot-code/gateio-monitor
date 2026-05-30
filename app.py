#!/usr/bin/env python3
"""
Gate.io U本位合约 MA10 趋势监控 - Web可视化面板
Flask后端 + 纯前端，局域网/本机访问
"""
import os
import sys
import json
import time
import socket
import subprocess
import threading
import logging
import importlib.metadata
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template, request, Response

from monitor import MonitorCore, get_session, BASE_URL, CONFIG_FILE
from alerts import (
    get_active_alert_intervals, check_reversal_strength,
    analyze_divergence, build_alert_state, send_wecom_alert, _iter_reversals,
)
from state import (
    load_prev_state, save_state, load_positions, save_positions,
    load_price_alerts, save_price_alerts,
    init_db, get_cached_klines, store_klines,
)

# ============ 配置 ============
PORT = 5000
AUTO_REFRESH_INTERVAL = 300

app = Flask(__name__)

# 全局缓存
cache = {
    "data": [],
    "last_update": None,
    "updating": False,
    "error": None,
    "alerts": [],
    "position_alerts": [],
    "divergence": [],
    "price_alerts": {},
}

# 下一次刷新时间戳 + 锁
_next_refresh_at = time.time() + AUTO_REFRESH_INTERVAL
_next_refresh_lock = threading.Lock()

def get_next_refresh_in() -> float:
    with _next_refresh_lock:
        return max(0, _next_refresh_at - time.time())

def set_next_refresh_at(val: float):
    global _next_refresh_at
    with _next_refresh_lock:
        _next_refresh_at = val

_browser_opened = False
user_positions = load_positions()
user_price_alerts = load_price_alerts()

# SSE 管理器
class SSEManager:
    def __init__(self):
        import queue
        self._subscribers = []
        self._lock = threading.Lock()

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, data: dict):
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

sse_manager = SSEManager()


def print_banner():
    """黑客风格启动画面"""
    print(r"""
   +=======================================================+
   |                                                       |
   |     G A T E . I O   M A 1 0   M O N I T O R         |
   |              Protocol v2.0  //  ONLINE                |
   |                                                       |
   +=======================================================+
""")
    time.sleep(0.05)
    print("[INIT] Loading core modules...")
    time.sleep(0.05)
    print("[INIT] Engaging network handshake protocol...")
    time.sleep(0.05)
    print("[ OK ] Module: requests  v" + requests.__version__)
    print("[ OK ] Module: flask     v" + importlib.metadata.version("flask"))
    print("[ OK ] Module: threading (ready)")
    print("[ OK ] Module: SQLite    (ready)")
    print("-" * 55)


def print_progress(current: int, total: int, symbol: str = ""):
    bar_len = 30
    filled = int(bar_len * current // total)
    bar = "▓" * filled + "▒" * (bar_len - filled)
    pct = int(100 * current / total)
    sym = f" | {symbol:<8}" if symbol else ""
    line = f"\r[PROG] [{bar}] {pct:>3}%{sym}"
    try:
        print(line, end="", flush=True)
    except UnicodeEncodeError:
        bar2 = "#" * filled + "." * (bar_len - filled)
        line2 = f"\r[PROG] [{bar2}] {pct:>3}%{sym}"
        print(line2, end="", flush=True)
    if current >= total:
        print()


def refresh_data():
    """后台刷新数据"""
    global cache
    if cache["updating"]:
        return
    cache["updating"] = True
    cache["error"] = None
    print("[SYNC] Initiating market data synchronization...")
    try:
        monitor = MonitorCore()
        if not monitor.symbols:
            cache["error"] = f"未找到 gateio_available_symbols.json，请先运行扫描脚本"
            cache["updating"] = False
            return

        def progress_cb(cur, total, sym):
            print_progress(cur, total, sym)

        data = monitor.analyze_all(progress_callback=progress_cb)
        prev_state = load_prev_state()
        new_state = build_alert_state(data)
        active_intervals = get_active_alert_intervals()
        alerts = []
        position_alerts = []

        # 转折预警
        for iv_name, item, rev, iv_data, prev_iv, new_iv in _iter_reversals(
            prev_state, new_state, data, active_intervals
        ):
            cond = check_reversal_strength(iv_data, rev)
            if cond:
                alerts.append({
                    "symbol": item["symbol"],
                    "interval": iv_name,
                    "type": rev,
                    "prev_trend": prev_iv["trend"],
                    "curr_trend": new_iv["trend"],
                    "consecutive": new_iv["consecutive"],
                    "reversal_pct": iv_data.get("reversal_pct"),
                    "condition": cond,
                })

        # 持仓预警
        for iv_name, item, rev, iv_data, prev_iv, new_iv in _iter_reversals(
            prev_state, new_state, data, active_intervals
        ):
            sym = item["symbol"]
            pos = user_positions.get(sym)
            if pos and (
                (pos == "long" and rev == "reversal_down")
                or (pos == "short" and rev == "reversal_up")
            ):
                position_alerts.append({
                    "symbol": sym,
                    "interval": iv_name,
                    "type": rev,
                    "position": pos,
                    "prev_trend": prev_iv["trend"],
                    "curr_trend": new_iv["trend"],
                    "consecutive": new_iv["consecutive"],
                    "reversal_pct": iv_data.get("reversal_pct"),
                })

        cache["data"] = data
        cache["alerts"] = alerts
        cache["position_alerts"] = position_alerts
        cache["divergence"] = analyze_divergence(data)
        cache["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        merged_state = dict(prev_state)
        for iv_name in active_intervals:
            if iv_name in new_state:
                merged_state[iv_name] = new_state[iv_name]
        save_state(merged_state)

        if alerts:
            print(f"\n{'='*60}")
            print(f"  检测到 {len(alerts)} 个 MA10 转折预警")
            print(f"{'='*60}")
            for a in alerts:
                arrow = "↓ 下跌转折" if a["type"] == "reversal_down" else "↑ 上涨转折"
                pct_str = f" 累计{a['reversal_pct']:+.2f}%" if a.get("reversal_pct") is not None else ""
                cond = a.get("condition", "")
                cond_str = f" [{cond}]" if cond else ""
                print(f"  [{a['symbol']}] {a['interval']} {arrow} (已{a['consecutive']}周期){pct_str}{cond_str}")
                print(f"     前趋势: {a['prev_trend']} → 现趋势: {a['curr_trend']}")
            print(f"{'='*60}\n")
            send_wecom_alert(alerts, cache["last_update"])

        if position_alerts:
            print(f"\n{'='*60}")
            print(f"  检测到 {len(position_alerts)} 个持仓预警")
            print(f"{'='*60}")
            for a in position_alerts:
                pos_label = "做多" if a["position"] == "long" else "做空"
                arrow = "↓ 下跌转折" if a["type"] == "reversal_down" else "↑ 上涨转折"
                pct_str = f" 累计{a['reversal_pct']:+.2f}%" if a.get("reversal_pct") is not None else ""
                print(f"  [{a['symbol']}] {a['interval']} {arrow} ({pos_label}){pct_str}")
                print(f"     前趋势: {a['prev_trend']} → 现趋势: {a['curr_trend']}")
            print(f"{'='*60}\n")
            send_wecom_alert(position_alerts, cache["last_update"], alert_type="position")

        # 价格提醒检测
        triggered_alerts = []
        for item in data:
            sym = item["symbol"]
            rules = user_price_alerts.get(sym, [])
            if not rules:
                continue
            last_price = item.get("last")
            if last_price is None:
                continue
            for rule in rules:
                target = rule.get("price")
                direction = rule.get("direction")
                triggered = rule.get("triggered", False)
                if target is None or triggered:
                    continue
                if (direction == "above" and last_price >= target) or \
                   (direction == "below" and last_price <= target):
                    rule["triggered"] = True
                    rule["triggered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    rule["triggered_price"] = last_price
                    triggered_alerts.append({
                        "symbol": sym,
                        "direction": direction,
                        "target": target,
                        "current_price": last_price,
                    })
        if triggered_alerts:
            save_price_alerts(user_price_alerts)
            cache["price_alerts"] = user_price_alerts
            print(f"\n  [价格提醒] 触发 {len(triggered_alerts)} 个:")
            for ta in triggered_alerts:
                dir_label = "向上突破" if ta["direction"] == "above" else "向下跌破"
                print(f"    {ta['symbol']} {dir_label} {ta['target']} (现价: {ta['current_price']})")

        # SSE 推送
        sse_manager.publish({
            "data": cache["data"],
            "alerts": cache["alerts"],
            "position_alerts": cache["position_alerts"],
            "divergence": cache["divergence"],
            "price_alerts": cache["price_alerts"],
            "last_update": cache["last_update"],
            "updating": False,
            "next_refresh_in": get_next_refresh_in(),
        })

    except Exception as e:
        cache["error"] = str(e)
        import traceback
        traceback.print_exc()
    finally:
        cache["updating"] = False
        set_next_refresh_at(time.time() + _next_refresh_delay())
        global _browser_opened
        if not _browser_opened:
            _browser_opened = True
            url = f"http://127.0.0.1:{PORT}"
            def delayed_open():
                time.sleep(1)
                import webbrowser
                webbrowser.open(url)
                print(f"[AUTO] Browser opened: {url}")
            threading.Thread(target=delayed_open, daemon=True).start()


def _next_refresh_delay() -> float:
    """计算到下一个对齐15分钟K线收盘的时间(秒)。提前115秒启动刷新，确保标的较多时也能在收盘前完成。"""
    now = datetime.now()
    next_boundary = ((now.minute // 15) + 1) * 15
    target = now.replace(second=0, microsecond=0)
    if next_boundary >= 60:
        target = target.replace(minute=0) + timedelta(hours=1)
    else:
        target = target.replace(minute=next_boundary)
    target -= timedelta(seconds=115)
    delay = (target - now).total_seconds()
    if delay < 5:
        delay += 900
    return delay


def auto_refresh_loop():
    """后台自动刷新线程 - 对齐K线收盘时间调度"""
    while True:
        delay = _next_refresh_delay()
        set_next_refresh_at(time.time() + delay)
        time.sleep(delay)
        refresh_data()


# ============ Flask 路由 ============

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    global user_price_alerts
    cache["price_alerts"] = user_price_alerts
    return jsonify({
        "data": cache["data"],
        "last_update": cache["last_update"],
        "updating": cache["updating"],
        "error": cache["error"],
        "alerts": cache["alerts"],
        "position_alerts": cache["position_alerts"],
        "divergence": cache["divergence"],
        "price_alerts": user_price_alerts,
        "next_refresh_in": get_next_refresh_in(),
    })


@app.route("/api/divergence")
def api_divergence():
    return jsonify({
        "data": cache["divergence"],
        "last_update": cache["last_update"],
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=refresh_data, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/positions", methods=["GET"])
def api_positions():
    return jsonify(user_positions)


@app.route("/api/position", methods=["POST"])
def api_position():
    global user_positions
    data = request.get_json() or {}
    symbol = data.get("symbol")
    position = data.get("position")
    if symbol:
        if position in ("long", "short"):
            user_positions[symbol] = position
        else:
            user_positions.pop(symbol, None)
        save_positions(user_positions)
    return jsonify({"status": "ok", "positions": user_positions})


@app.route("/api/stream")
def api_stream():
    """SSE 端点：推送数据更新"""
    def generate():
        q = sse_manager.subscribe()
        try:
            # 首次连接时发送当前缓存
            initial = {
                "data": cache["data"],
                "alerts": cache["alerts"],
                "position_alerts": cache["position_alerts"],
                "divergence": cache["divergence"],
                "price_alerts": cache["price_alerts"],
                "last_update": cache["last_update"],
                "updating": cache["updating"],
                "next_refresh_in": get_next_refresh_in(),
            }
            yield f"event: update\ndata: {json.dumps(initial, default=str)}\n\n"
            while True:
                data = q.get()
                yield f"event: update\ndata: {json.dumps(data, default=str)}\n\n"
        except GeneratorExit:
            sse_manager.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/klines")
def api_klines():
    """K线数据端点（用于图表）"""
    symbol = request.args.get("symbol", "")
    interval = request.args.get("interval", "1d")
    limit = int(request.args.get("limit", 200))

    # 查找合约名
    contract = None
    for item in cache["data"]:
        if item["symbol"] == symbol:
            contract = item["contract"]
            break
    if not contract:
        # fallback: 直接转换
        contract = symbol
        # 尝试从配置文件查找
        try:
            with open("gateio_available_symbols.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for s in cfg.get("available", []):
                if s.get("user_symbol") == symbol:
                    contract = s["contract"]
                    break
        except Exception:
            pass

    if not contract:
        return jsonify({"error": "symbol not found"}), 404

    # 先查 SQLite 缓存
    cached = get_cached_klines(contract, interval, limit)
    if cached:
        return jsonify({"klines": cached, "symbol": symbol, "interval": interval, "source": "cache"})

    # 从 API 获取
    try:
        s = get_session()
        resp = s.get(
            f"{BASE_URL}/futures/usdt/candlesticks",
            params={"contract": contract, "interval": interval, "limit": limit},
            timeout=15,
        )
        klines = resp.json()
        if klines:
            store_klines(contract, interval, klines)
        return jsonify({"klines": klines, "symbol": symbol, "interval": interval, "source": "api"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/symbols", methods=["GET"])
def api_symbols_list():
    """返回当前监控标的列表"""
    monitor = MonitorCore()
    return jsonify({
        "symbols": [s["user_symbol"] for s in monitor.symbols],
        "contracts": [s["contract"] for s in monitor.symbols],
        "count": len(monitor.symbols),
    })


@app.route("/api/symbols", methods=["POST"])
def api_symbols_add():
    """添加监控标的。输入简称自动推导 user_symbol 和 contract"""
    data = request.get_json() or {}
    raw = ((data.get("name") or data.get("symbol") or "").strip().upper())
    if not raw:
        return jsonify({"error": "请输入代币简称"}), 400
    base = raw
    if base.endswith("_USDT"):
        base = base[:-5]
    elif base.endswith("USDT"):
        base = base[:-4]
    base = base.strip("_")
    if not base:
        return jsonify({"error": "无法解析代币名称"}), 400
    user_symbol = base + "USDT"
    contract = base + "_USDT"
    # 允许直接传入 contract 覆盖推导结果（保持向下兼容）
    if data.get("contract"):
        contract = data["contract"].strip()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {"available": []}
    existing = {s["user_symbol"] for s in cfg.get("available", [])}
    if user_symbol in existing:
        return jsonify({"error": f"{user_symbol} already exists"}), 409
    # 验证合约是否真实存在于 Gate.io
    monitor = MonitorCore()
    ticker = monitor.fetch_ticker(contract)
    if ticker is None:
        return jsonify({"error": f"合约不存在: {contract}，请检查合约名是否正确"}), 400
    cfg["available"].append({
        "user_symbol": user_symbol,
        "contract": contract,
        "last": 0,
        "change_percentage": 0,
        "volume_24h_quote": 0,
    })
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"status": "ok", "symbol": user_symbol})


@app.route("/api/symbols", methods=["DELETE"])
def api_symbols_remove():
    """移除监控标的"""
    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return jsonify({"error": "config not found"}), 500
    before = len(cfg.get("available", []))
    cfg["available"] = [s for s in cfg.get("available", []) if s["user_symbol"] != symbol]
    if len(cfg["available"]) == before:
        return jsonify({"error": f"{symbol} not found"}), 404
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"status": "ok", "symbol": symbol})


@app.route("/api/price-alerts", methods=["GET"])
def api_price_alerts():
    global user_price_alerts
    return jsonify(user_price_alerts)


@app.route("/api/price-alerts", methods=["POST"])
def api_price_alerts_set():
    global user_price_alerts
    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip().upper()
    direction = data.get("direction")
    price = data.get("price")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    rules = user_price_alerts.get(symbol, [])
    if direction == "clear":
        user_price_alerts.pop(symbol, None)
    elif direction in ("above", "below") and price is not None:
        rules.append({
            "price": float(price),
            "direction": direction,
            "triggered": False,
            "triggered_at": None,
            "triggered_price": None,
        })
        user_price_alerts[symbol] = rules
    else:
        return jsonify({"error": "invalid direction or missing price"}), 400
    save_price_alerts(user_price_alerts)
    cache["price_alerts"] = user_price_alerts
    return jsonify({"status": "ok", "alerts": user_price_alerts})


@app.route("/api/price-alerts", methods=["DELETE"])
def api_price_alerts_delete():
    global user_price_alerts
    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip().upper()
    index = data.get("index")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if symbol not in user_price_alerts:
        return jsonify({"error": "no alerts for symbol"}), 404
    if index is not None:
        rules = user_price_alerts[symbol]
        if 0 <= index < len(rules):
            rules.pop(index)
            if rules:
                user_price_alerts[symbol] = rules
            else:
                user_price_alerts.pop(symbol, None)
    else:
        user_price_alerts.pop(symbol, None)
    save_price_alerts(user_price_alerts)
    cache["price_alerts"] = user_price_alerts
    return jsonify({"status": "ok", "alerts": user_price_alerts})


# ============ 启动工具 ============

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_process_on_port(port: int):
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
        killed = []
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    if pid not in killed:
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                        killed.append(pid)
        if killed:
            print(f"  已结束占用端口 {port} 的进程: PID {', '.join(killed)}")
            time.sleep(1)
    except Exception:
        pass


if __name__ == "__main__":
    print_banner()

    # 初始化 SQLite
    init_db()
    print("[ OK ] SQLite database initialized")

    if is_port_in_use(PORT):
        print(f"[WARN] Port {PORT} occupied. Terminating process...")
        kill_process_on_port(PORT)
        if is_port_in_use(PORT):
            print(f"[WARN] Fallback to alternate port {PORT + 1}")
            PORT += 1

    print(f"[ OK ] Service endpoint: http://127.0.0.1:{PORT}")
    print(f"[ OK ] LAN endpoint:    http://<local-ip>:{PORT}")
    print("[INIT] Spawning background sync thread...")
    print("-" * 55)

    threading.Thread(target=refresh_data, daemon=True).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("flask.app").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
