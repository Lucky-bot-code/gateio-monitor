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
from datetime import datetime, timedelta, timezone

import gzip
import io

import requests
from flask import Flask, jsonify, render_template, request, Response

from monitor import MonitorCore, get_session, BASE_URL, CONFIG_FILE, _api_sem
from alerts import analyze_turning_points, analyze_extreme_signals
from state import (
    load_positions, save_positions,
    load_price_alerts, save_price_alerts,
    init_db, get_cached_klines, store_klines,
    load_tp_state, save_tp_state,
    load_wecom_subscriptions, save_wecom_subscriptions,
)

# ============ 配置 ============
PORT = 5000
AUTO_REFRESH_INTERVAL = 300

# 企业微信机器人 Webhook
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=7bef2a11-7838-4859-a9c0-b65b6cf2dc36"

app = Flask(__name__)

# Gzip 压缩 JSON 响应（>500 字节）
@app.after_request
def gzip_response(response):
    if (response.content_type and "application/json" in response.content_type
            and response.content_length is not None and response.content_length > 500):
        accept_encoding = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept_encoding:
            return response
        response.direct_passthrough = False
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=4) as gz:
            gz.write(response.get_data())
        response.set_data(buf.getvalue())
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = response.content_length
        response.headers["Vary"] = "Accept-Encoding"
    return response

# 全局缓存
cache = {
    "data": [],
    "last_update": None,
    "updating": False,
    "error": None,
    "turning_points": [],
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

# 极偏信号去重状态: {(symbol, interval, label): True}
_extreme_sent: dict = {}
EXTREME_SENT_FILE = "extreme_sent.json"

def _load_extreme_sent():
    global _extreme_sent
    try:
        with open(EXTREME_SENT_FILE, "r", encoding="utf-8") as f:
            _extreme_sent = json.load(f)
    except Exception:
        _extreme_sent = {}

def _save_extreme_sent():
    try:
        with open(EXTREME_SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(_extreme_sent, f, ensure_ascii=False)
    except Exception:
        pass

_browser_opened = False
_load_extreme_sent()
user_positions = load_positions()
user_price_alerts = load_price_alerts()
wecom_subscriptions = load_wecom_subscriptions()


def send_wecom_turning_alert(alert: dict) -> bool:
    """发送单条转折预警到企业微信。"""
    if not WECOM_WEBHOOK_URL:
        return False
    signal = alert.get("signal", "")
    emoji = "📈" if "买入" in signal else "📉"
    path = alert.get("path", "")
    ma10_type = alert.get("ma10_type", "")
    type_str = f"类型{ma10_type}" if ma10_type else ""
    path_str = f"{path} {type_str}" if path else ""
    content = f"""**🔔 转折预警订阅推送**
> {emoji} **{alert['symbol']}** · {alert['interval_name']} · **{signal}**
> 路径: {path_str}
> 价格: {alert.get('close', '-')} / MA10: {alert.get('ma10', '-')}
> 时间: {alert.get('timestamp', '')}"""
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(WECOM_WEBHOOK_URL, json=payload, timeout=15)
        return resp.status_code == 200
    except Exception:
        return False



def send_wecom_extreme_alerts(signals: list) -> bool:
    """批量推送极偏信号到企业微信（精简汇总，无需订阅）。"""
    if not WECOM_WEBHOOK_URL or not signals:
        return False
    lines = ["**⚡ 极偏信号汇总**", ""]
    for s in signals:
        emoji = "🟢" if s["label"] == "极多" else "🔴"
        lines.append(f"> {emoji} **{s['symbol']}** · {s['interval_name']} · **{s['label']}**")
    content = "\n".join(lines)
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(WECOM_WEBHOOK_URL, json=payload, timeout=15)
        return resp.status_code == 200
    except Exception:
        return False

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
   |              Protocol v3.0  //  ONLINE                |
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

        cache["data"] = data
        tp_state = load_tp_state()
        alerts, new_state, pending = analyze_turning_points(data, tp_state)
        save_tp_state(new_state)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for a in alerts:
            a["timestamp"] = now_str
        cache["turning_points"] = alerts

        # 企业微信订阅推送（推送后自动取消订阅）
        if alerts and wecom_subscriptions:
            pushed = 0
            unsub_list = []
            for a in alerts:
                sym = a["symbol"]
                iv = a["interval"]
                if sym in wecom_subscriptions and iv in wecom_subscriptions[sym]:
                    if send_wecom_turning_alert(a):
                        pushed += 1
                        print(f"  [企业微信推送] {sym} {a['interval_name']} {a['signal']}")
                        unsub_list.append((sym, iv))
            if pushed:
                for sym, iv in unsub_list:
                    if sym in wecom_subscriptions and iv in wecom_subscriptions[sym]:
                        wecom_subscriptions[sym].remove(iv)
                        if not wecom_subscriptions[sym]:
                            del wecom_subscriptions[sym]
                save_wecom_subscriptions(wecom_subscriptions)
                print(f"  [企业微信推送] 已推送 {pushed} 条预警（已自动取消订阅）")

        # 极偏信号检测（仅在每个周期的最后一次刷新时推送）
        extreme_signals = analyze_extreme_signals(data)
        if extreme_signals:
            global _extreme_sent
            new_signals = []
            for s in extreme_signals:
                iv = s["interval"]
                # 判断当前刷新是否是该周期的"最后一次"
                candle_end = _get_candle_end_time(iv)
                if candle_end - time.time() > AUTO_REFRESH_INTERVAL:
                    continue  # 非最后一次刷新，跳过
                # 去重 key 绑定 candle 起始时间，每个周期可重新触发
                candle_start = _get_candle_start(iv)
                key_parts = (s["symbol"], iv, s["label"], candle_start)
                key = "|".join(str(x) for x in key_parts)
                if key not in _extreme_sent:
                    _extreme_sent[key] = True
                    new_signals.append(s)
                    print(f"  [极偏推送] {s['symbol']} {s['interval_name']} {s['label']}")
            if new_signals:
                _save_extreme_sent()
                send_wecom_extreme_alerts(new_signals)
                print(f"  [极偏推送] 已推送 {len(new_signals)} 条极偏信号")
            # 裁剪过期去重缓存（保留最近 7 天）
            cutoff = int(time.time()) - 86400 * 7
            stale = [k for k in _extreme_sent if _extreme_sent.get(k) is True]
            if len(stale) > 2000:
                # 简单截断旧条目
                keys = list(_extreme_sent.keys())
                _extreme_sent = {k: True for k in keys[-1000:]}
                _save_extreme_sent()

        cache["last_update"] = now_str
        # 清理同标的+周期的旧调度项，再调度新的中途检查
        with _mid_checks_lock:
            new_keys = {(p["symbol"], p["interval"]) for p in pending}
            _mid_checks[:] = [c for c in _mid_checks
                              if (c["item"]["symbol"], c["item"]["interval"]) not in new_keys]
        for p in pending:
            schedule_mid_check(p)

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
            "turning_points": cache["turning_points"],
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


# ============ 中途检查调度器 ============

_HALF_PERIODS = {
    "1d": 43200,   # 12h
    "4h": 7200,    # 2h
    "1h": 1800,    # 30m
    "15m": 450,    # 7.5m
}
_MID_CHECK_BUFFER = 15  # 单标的fetch耗时约5s，留15s余量

_mid_checks = []
_mid_checks_lock = threading.Lock()


def _get_next_candle_start(interval: str) -> float:
    """计算下一个K线的起始UTC时间戳"""
    now = datetime.now(timezone.utc)
    if interval == "1d":
        ns = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif interval == "4h":
        block = now.hour // 4
        next_hour = ((block + 1) * 4) % 24
        if next_hour == 0:
            ns = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            ns = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    elif interval == "1h":
        ns = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    elif interval == "15m":
        block = now.minute // 15
        next_minute = (block + 1) * 15
        if next_minute >= 60:
            ns = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            ns = now.replace(minute=next_minute, second=0, microsecond=0)
    else:
        return time.time() + 3600
    return ns.timestamp()


def _get_candle_end_time(interval: str) -> float:
    """当前K线的收盘UTC时间戳（同 _get_next_candle_start）。"""
    return _get_next_candle_start(interval)


def _get_candle_start(interval: str) -> int:
    """当前K线的起始UTC时间戳（整数，用于去重key）。"""
    now = datetime.now(timezone.utc)
    if interval == "1d":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif interval == "4h":
        hour = (now.hour // 4) * 4
        start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    elif interval == "1h":
        start = now.replace(minute=0, second=0, microsecond=0)
    elif interval == "15m":
        minute = (now.minute // 15) * 15
        start = now.replace(minute=minute, second=0, microsecond=0)
    else:
        start = now
    return int(start.timestamp())


def schedule_mid_check(pending_item: dict):
    """将一个待检查项加入调度队列。fire_at 对齐下一个K线的半程点 - buffer。"""
    interval = pending_item["interval"]
    half_sec = _HALF_PERIODS.get(interval, 1800)
    next_start = _get_next_candle_start(interval)
    fire_at = next_start + half_sec - _MID_CHECK_BUFFER
    # 如果半程点已过（滞后于当前时间），跳过
    if fire_at <= time.time():
        print(f"  [转折预警] 中途检查已过期，跳过: {pending_item['symbol']} {pending_item['interval_name']}")
        return
    with _mid_checks_lock:
        existing = [c for c in _mid_checks if c["item"]["symbol"] == pending_item["symbol"]
                    and c["item"]["interval"] == pending_item["interval"]]
        if not existing:
            _mid_checks.append({"fire_at": fire_at, "item": pending_item})
            print(f"  [转折预警] 调度中途检查: {pending_item['symbol']} {pending_item['interval_name']}"
                  f" 将在 {datetime.fromtimestamp(fire_at).strftime('%H:%M:%S')} 触发")


def _do_mid_check(pending_item: dict):
    """执行中途检查：获取单标最新数据，验证类型1条件 + SAR同向确认。"""
    symbol = pending_item["symbol"]
    interval = pending_item["interval"]
    interval_name = pending_item["interval_name"]
    direction = pending_item["direction"]

    print(f"  [转折预警] 执行中途检查: {symbol} {interval_name}")
    try:
        is_bullish = (direction == "bullish")
        signal_label = "买入信号" if is_bullish else "卖出信号"

        # 先拉数据，拿到当前趋势后再去重（避免旧告警跨周期残留阻挡）
        monitor = MonitorCore()
        contract = None
        for s in monitor.symbols:
            if s.get("user_symbol") == symbol:
                contract = s["contract"]
                break
        if not contract:
            return

        session = get_session()
        iv = monitor._process_interval(contract, interval, interval_name, session)

        if iv.get("trend") == "数据不足":
            return

        close = iv.get("close")
        ma10 = iv.get("ma10")
        consecutive = iv.get("consecutive", 0)
        open_price = iv.get("open")
        prev_open_new = iv.get("prev_open")
        sar_direction = iv.get("sar_direction", "neutral")

        if close is None or ma10 is None or consecutive < 2:
            return

        # SAR 必须保持同向
        if sar_direction != direction:
            print(f"  [转折预警] 中途检查失败 {symbol} {interval_name}: SAR方向不一致"
                  f" (期望{direction} 实际{sar_direction})")
            return

        # 类型1条件：价格同侧 + 开盘价确认
        price_ok = close > ma10 if is_bullish else close < ma10
        if not price_ok:
            return

        open_ok = False
        if is_bullish and open_price is not None and prev_open_new is not None:
            open_ok = open_price > prev_open_new
        elif not is_bullish and open_price is not None and prev_open_new is not None:
            open_ok = open_price < prev_open_new

        if not open_ok:
            return

        # 条件满足，去重：检查 tp_state 是否已有同信号告警
        # 先清理跨周期残留（趋势震荡或方向反转时旧告警不再阻挡）
        tp_state = load_tp_state()
        prev_state = tp_state.get(symbol, {}).get(interval, {})
        prev_alerts = prev_state.get("alerts_sent", [])
        if iv.get("trend") == "震荡":
            prev_alerts = []
        else:
            prev_alerts = [a for a in prev_alerts if a.get("signal") == signal_label]
        for pa in prev_alerts:
            if pa.get("signal") == signal_label and pa.get("interval") == interval:
                print(f"  [转折预警] 中途检查跳过(已告警): {symbol} {interval_name}")
                return

        # 生成告警
        candle_change_pct = round((close - open_price) / open_price * 100, 2) if (open_price and open_price != 0) else None
        alert = {
            "symbol": symbol,
            "interval_name": interval_name,
            "interval": interval,
            "signal": signal_label,
            "path": "中途确认",
            "ma10_type": 1,
            "sar_direction": direction,
            "ma10_consecutive": consecutive,
            "sar_consecutive": iv.get("sar_consecutive", 0),
            "close": close,
            "ma10": ma10,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "volume_24h": pending_item.get("volume_24h"),
            "candle_change_pct": candle_change_pct,
        }

        # 写入 tp_state 防止后续重复告警
        prev_alerts.append(alert)
        tp_state[symbol] = tp_state.get(symbol, {})
        tp_state[symbol][interval] = {
            "sar_direction": sar_direction,
            "last_flip_time": prev_state.get("last_flip_time"),
            "alerts_sent": prev_alerts[-10:],
        }
        save_tp_state(tp_state)

        cache["turning_points"].append(alert)
        print(f"  [转折预警] 中途确认! {symbol} {interval_name} 类型1 {alert['signal']}")

        # SSE 实时推送给前端（中途确认不在常规刷新周期内，需主动推送）
        sse_manager.publish({
            "data": cache["data"],
            "turning_points": cache["turning_points"],
            "price_alerts": cache["price_alerts"],
            "last_update": cache["last_update"],
            "updating": False,
        })

    except Exception as e:
        print(f"  [转折预警] 中途检查失败 {symbol} {interval_name}: {e}")


def _mid_check_loop():
    """后台线程：每秒检查是否有到期的中途检查项。"""
    while True:
        try:
            time.sleep(1)
            now = time.time()
            with _mid_checks_lock:
                due = [c for c in _mid_checks if c["fire_at"] <= now]
                for c in due:
                    _mid_checks.remove(c)
            for c in due:
                do_thread = threading.Thread(target=_do_mid_check, args=(c["item"],), daemon=True)
                do_thread.start()
        except Exception:
            pass


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
        "turning_points": cache["turning_points"],
        "price_alerts": user_price_alerts,
        "next_refresh_in": get_next_refresh_in(),
    })


@app.route("/api/turning-points")
def api_turning_points():
    return jsonify({
        "data": cache["turning_points"],
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
                "turning_points": cache["turning_points"],
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

    # 从 API 获取（带并发限流）
    try:
        s = get_session()
        with _api_sem:
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
        "manual": True,
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


# ============ 企业微信预警订阅 API ============

@app.route("/api/wecom-subscriptions", methods=["GET"])
def api_wecom_subs_list():
    return jsonify(wecom_subscriptions)


@app.route("/api/wecom-subscription", methods=["POST"])
def api_wecom_sub_toggle():
    """切换某个 symbol+interval 的订阅状态。body: {symbol, interval}"""
    global wecom_subscriptions
    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip().upper()
    interval = (data.get("interval") or "").strip()
    if not symbol or not interval:
        return jsonify({"error": "symbol and interval required"}), 400
    if symbol not in wecom_subscriptions:
        wecom_subscriptions[symbol] = []
    if interval in wecom_subscriptions[symbol]:
        # 取消订阅
        wecom_subscriptions[symbol].remove(interval)
        if not wecom_subscriptions[symbol]:
            del wecom_subscriptions[symbol]
        save_wecom_subscriptions(wecom_subscriptions)
        return jsonify({"status": "ok", "subscribed": False, "subscriptions": wecom_subscriptions})
    else:
        # 订阅
        wecom_subscriptions[symbol].append(interval)
        save_wecom_subscriptions(wecom_subscriptions)
        return jsonify({"status": "ok", "subscribed": True, "subscriptions": wecom_subscriptions})


@app.route("/api/wecom-subscriptions", methods=["DELETE"])
def api_wecom_subs_reset():
    """一键重置所有企业微信订阅。"""
    global wecom_subscriptions
    n = sum(len(v) for v in wecom_subscriptions.values())
    wecom_subscriptions = {}
    save_wecom_subscriptions(wecom_subscriptions)
    print(f"  [企业微信] 已重置全部 {n} 条订阅")
    return jsonify({"status": "ok", "deleted": n, "subscriptions": {}})


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
    threading.Thread(target=_mid_check_loop, daemon=True).start()

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("flask.app").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
