#!/usr/bin/env python3
"""把多片 result-*.json 合并成 result.json + result.txt（按连接耗时排序、按出口IP去重）"""
import glob
import json
import os
import time

OUT_JSON = os.environ.get("OUT_JSON", "result.json")
OUT_TXT = os.environ.get("OUT_TXT", "result.txt")

results, tested = [], 0
for f in sorted(glob.glob("result-*.json")):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    results += d.get("results") or []
    tested += int(d.get("tested") or 0)

# 同一出口 IP 只留连接最快的那条（多个网关可能共用同一出口）
best = {}
for r in results:
    ip = (r.get("detail") or {}).get("ip") or r["sstp"]
    cur = best.get(ip)
    if cur is None or r.get("connect_s", 99) < cur.get("connect_s", 99):
        best[ip] = r
results = sorted(best.values(), key=lambda x: x.get("connect_s", 99))

lines = [f"{r['sstp']}  {json.dumps(r['detail'], ensure_ascii=False, separators=(',', ':'))}" for r in results]
with open(OUT_TXT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + ("\n" if lines else ""))

with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump({
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tested": tested,
        "alive": len(results),
        "source_api": "https://www.vpngate.net/api/iphone/",
        "source_page": "https://www.vpngate.net/cn/",
        "ip_api": "https://ip.5671234.xyz/json/",
        "servers": [{"sstp": r["sstp"], **r["detail"], "connect_s": r.get("connect_s")} for r in results],
        "results": results,
    }, f, ensure_ascii=False, indent=2)

print(f"合并完成：{tested} 台参与测试，{len(results)} 条可用（按出口 IP 去重后）")
