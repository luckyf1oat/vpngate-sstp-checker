#!/usr/bin/env python3
"""每两小时检测：上次有效的 + 全库（按"上次可用→久未检测"排序，可设上限）

产物：
- result.txt / result.json   当前可用节点（按连接耗时排序）
- sstpall.json               回写每台的 last_checked / last_ok / ok_count / fail_count
"""
from __future__ import annotations

import json
import os

import ranking
import vpngate_lib as lib

POOL_TXT = os.environ.get("POOL_TXT", "sstpall.txt")
POOL_JSON = os.environ.get("POOL_JSON", "sstpall.json")
OUT_TXT = os.environ.get("OUT_TXT", "result.txt")
OUT_JSON = os.environ.get("OUT_JSON", "result.json")
OUT_RANK = os.environ.get("OUT_RANK", "result-ranked.txt")
MAX_TEST = int(os.environ.get("MAX_TEST", "0"))
STATE_OUT = os.environ.get("STATE_OUT", POOL_JSON)   # 分片时写本片状态
SHARD = os.environ.get("SHARD", "")                  # "k/n"：多 runner 并行分片
PRIORITY_FIRST = os.environ.get("PRIORITY_FIRST", "1") == "1"


def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def build_targets(pool, last_result):
    """返回 [(host:port, 来源)]：上次可用优先，其余按 从未检测→最久未检测 排"""
    servers = pool.get("servers") or {}
    prev_ok = []
    for r in (last_result.get("results") or []):
        hp = (r.get("host") or "").replace("sstp://vpn:vpn@", "")
        if hp:
            prev_ok.append(hp)

    targets, seen = [], set()
    if PRIORITY_FIRST:
        for hp in prev_ok:
            if hp and hp not in seen:
                seen.add(hp)
                targets.append((hp, "prev-ok"))

    rest = []
    for hp, rec in servers.items():
        if hp in seen:
            continue
        never = rec.get("last_checked") is None
        rest.append((0 if never else 1, rec.get("last_checked") or "", hp))
    rest.sort(key=lambda x: (x[0], x[1]))
    for _, _, hp in rest:
        targets.append((hp, "pool"))

    if MAX_TEST > 0:
        targets = targets[:MAX_TEST]
    return targets


def main():
    if lib.ppp0_exists():
        lib.log("本机已有 ppp0（别的 VPN 隧道），pppd 2.4 会撞名，退出")
        return 3

    pool = load_json(POOL_JSON, None)
    if not pool:
        lib.log("没有 sstpall.json，先跑 collect.py")
        return 1
    last = load_json(OUT_JSON, {})

    mip = lib.my_ip()
    lib.log(f"本机出口 IP: {mip}")

    targets = build_targets(pool, last)
    if SHARD:
        k, n = (int(v) for v in SHARD.split("/"))
        targets = targets[k::n]
        lib.log(f"分片 {k}/{n}：库内 {len(pool.get('servers') or {})} 台，本片 {len(targets)} 台")
    else:
        lib.log(f"库内 {len(pool.get('servers') or {})} 台，本轮测 {len(targets)} 台"
                f"（上次有效 {sum(1 for _, s in targets if s == 'prev-ok')} 台优先）")
    if not targets:
        return 1

    servers = pool["servers"]
    touched = {}
    results = []
    for i, (hp, src) in enumerate(targets, 1):
        try:
            r = lib.probe(hp, i, len(targets), mip)
        except Exception as e:
            lib.log(f"[{i}/{len(targets)}] {hp} 异常: {e}")
            r = None
        rec = dict(servers.get(hp) or {})
        rec["last_checked"] = lib.now_iso()
        rec["last_ok"] = bool(r)
        if r:
            rec["ok_count"] = int(rec.get("ok_count") or 0) + 1
            rec["fail_count"] = 0
            r["detail"]["src"] = src
            results.append(r)
            write_outputs(results, len(targets))
        else:
            rec["fail_count"] = int(rec.get("fail_count") or 0) + 1
        servers[hp] = rec
        touched[hp] = {k: rec[k] for k in ("last_checked", "last_ok", "ok_count", "fail_count")}

    # 打分（家宽/带宽/在线时长/延迟/稳定/负载）
    for r in results:
        hp = (r.get("host") or "").replace("sstp://vpn:vpn@", "")
        total, parts = ranking.score_node(r.get("detail") or {}, servers.get(hp) or {}, r.get("connect_s"))
        r["score"] = total
        r["score_parts"] = parts

    results.sort(key=lambda x: x["connect_s"])
    write_outputs(results, len(targets))
    write_ranked(results)
    if SHARD:
        with open(STATE_OUT, "w", encoding="utf-8") as f:
            json.dump({"shard": SHARD, "at": lib.now_iso(), "servers": touched}, f, ensure_ascii=False, indent=1)
    else:
        pool["last_check_at"] = lib.now_iso()
        with open(STATE_OUT, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=1, sort_keys=True)
    lib.log(f"完成：{len(results)}/{len(targets)} 可用（库内 {len(servers)} 台）")
    return 0 if results else 2


def write_ranked(results):
    ranked = sorted(results, key=lambda x: x.get("score", 0), reverse=True)
    lines = []
    for r in ranked:
        d = dict(r["detail"])
        d["score"] = r.get("score")
        d["score_parts"] = r.get("score_parts")
        lines.append(f"{r['sstp']}  {json.dumps(d, ensure_ascii=False, separators=(',', ':'))}")
    with open(OUT_RANK, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def write_outputs(results, tested):
    lines = [f"{r['sstp']}  {json.dumps(r['detail'], ensure_ascii=False, separators=(',', ':'))}"
             for r in results]
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": lib.now_iso(),
            "tested": tested,
            "alive": len(results),
            "results": results,
        }, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
