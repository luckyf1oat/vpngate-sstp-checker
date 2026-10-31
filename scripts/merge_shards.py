#!/usr/bin/env python3
"""合并多 runner 分片产物 → result.txt / result.json / sstpall.json

输入（artifact 下载到当前目录）：
- result-<k>.json   每片的可用节点
- pool-<k>.json     每片碰过的库记录状态
输出：
- result.txt / result.json   合并、按出口 IP 去重、按连接耗时排序
- sstpall.json               把各片的 last_checked/last_ok/ok_count/fail_count 合回库
"""
import glob
import json
import os
import time

OUT_TXT = "result.txt"
OUT_JSON = "result.json"
POOL_JSON = "sstpall.json"


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main():
    results, tested = [], 0
    for f in sorted(glob.glob("result-*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        results += d.get("results") or []
        tested += int(d.get("tested") or 0)

    best = {}
    for r in results:
        ip = (r.get("detail") or {}).get("ip") or r["sstp"]
        cur = best.get(ip)
        if cur is None or r.get("connect_s", 99) < cur.get("connect_s", 99):
            best[ip] = r
    results = sorted(best.values(), key=lambda x: x.get("connect_s", 99))

    lines = [f"{r['sstp']}  {json.dumps(r['detail'], ensure_ascii=False, separators=(',', ':'))}"
             for r in results]
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now_iso(), "tested": tested, "alive": len(results),
                   "results": results}, f, ensure_ascii=False, indent=2)

    # 合并库状态
    try:
        pool = json.load(open(POOL_JSON, encoding="utf-8"))
    except Exception:
        pool = {"servers": {}}
    servers = pool.setdefault("servers", {})
    merged = 0
    for f in sorted(glob.glob("pool-*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for hp, rec in (d.get("servers") or {}).items():
            cur = servers.get(hp)
            if not cur:
                continue
            # 只接受比库里更新的状态
            if (rec.get("last_checked") or "") >= (cur.get("last_checked") or ""):
                cur.update(rec)
                merged += 1
    pool["last_check_at"] = now_iso()
    with open(POOL_JSON, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"合并完成：{tested} 台参与，{len(results)} 条可用（出口去重后），库状态更新 {merged} 条")


if __name__ == "__main__":
    raise SystemExit(main())
