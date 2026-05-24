#!/usr/bin/env python3
"""测试多个加密货币 API 在中国大陆的可访问性"""
import requests
import time

apis = [
    ("Binance", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
    ("OKX", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
    ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"),
    ("Gate.io", "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT"),
    ("Bitget", "https://api.bitget.com/api/v2/spot/market/tickers?symbol=BTCUSDT"),
    ("MEXC", "https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT"),
    ("Coinbase", "https://api.coinbase.com/v2/exchange-rates?currency=BTC"),
    ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"),
    ("Bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"),
]

print("测试各 API 可达性（超时 10s）...\n")
for name, url in apis:
    try:
        start = time.time()
        r = requests.get(url, timeout=10)
        elapsed = (time.time() - start) * 1000
        if r.status_code == 200:
            print(f"[OK]   {name:12s} | {elapsed:7.1f}ms | HTTP {r.status_code}")
        else:
            print(f"[WARN] {name:12s} | {elapsed:7.1f}ms | HTTP {r.status_code}")
    except Exception as e:
        print(f"[FAIL] {name:12s} | ---    | {type(e).__name__}: {str(e)[:50]}")
