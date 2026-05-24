#!/usr/bin/env python3
"""
最终验证：币安 U本位合约 API 在用户当前网络环境下的可达性
同时检测系统是否配置了代理
"""
import os
import requests

print("=" * 60)
print("币安 API 可达性最终验证")
print("=" * 60)

# 检查代理环境变量
proxies = {}
http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
if http_proxy:
    proxies["http"] = http_proxy
if https_proxy:
    proxies["https"] = https_proxy

print(f"\n系统代理检测:")
print(f"  HTTP_PROXY:  {http_proxy or '未设置'}")
print(f"  HTTPS_PROXY: {https_proxy or '未设置'}")

# 测试目标
urls = [
    ("币安现货", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
    ("币安U本位", "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"),
    ("币安币本位", "https://dapi.binance.com/dapi/v1/ticker/price?symbol=BTCUSD_PERP"),
]

print(f"\n直接连接测试 (超时 10s):")
for name, url in urls:
    try:
        r = requests.get(url, timeout=10)
        print(f"  [OK]   {name:10s} | HTTP {r.status_code} | {r.text[:80]}")
    except Exception as e:
        print(f"  [FAIL] {name:10s} | {type(e).__name__}: {str(e)[:60]}")

# 如果有代理，再测一次
if proxies:
    print(f"\n通过代理测试:")
    for name, url in urls:
        try:
            r = requests.get(url, timeout=15, proxies=proxies)
            print(f"  [OK]   {name:10s} | HTTP {r.status_code} | {r.text[:80]}")
        except Exception as e:
            print(f"  [FAIL] {name:10s} | {type(e).__name__}: {str(e)[:60]}")
else:
    print("\n未检测到系统代理，跳过代理测试。")

print("\n" + "=" * 60)
print("结论：")
print("  - 如果以上全部 [FAIL]，说明币安 API 在你当前网络下不可达。")
print("  - 如需使用币安，请配置 HTTP_PROXY/HTTPS_PROXY 环境变量后重试。")
print("=" * 60)
