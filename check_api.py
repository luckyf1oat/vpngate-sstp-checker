#!/usr/bin/env python3
"""通过 cks5 检测接口测 SSTP（无需 pppd/root，一把并发跑完）

接口：GET https://cks5.115567.xyz/check?sstp=vpn:vpn@host:port
返回：{success, colo, responseTime, exit:{ip, country, country_code, city, region,
        timezone, asn:{asn,name,org}}, error}
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import time
import urllib.parse
import urllib.request

import ranking

POOL_TXT = os.environ.get("POOL_TXT", "sstpall.txt")
POOL_JSON = os.environ.get("POOL_JSON", "sstpall.json")
OUT_TXT = os.environ.get("OUT_TXT", "result.txt")
OUT_JSON = os.environ.get("OUT_JSON", "result.json")
OUT_RANK = os.environ.get("OUT_RANK", "result-ranked.txt")
CHECK_API = os.environ.get("CHECK_API", "https://cks5.115567.xyz/check")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "16"))
REQ_TIMEOUT = int(os.environ.get("REQ_TIMEOUT", "45"))
MAX_TEST = int(os.environ.get("MAX_TEST", "0"))
SHARD = os.environ.get("SHARD", "")
STATE_OUT = os.environ.get("STATE_OUT", POOL_JSON)
UA = "Mozilla/5.0 (compatible; vpngate-sstp-checker/1.0)"


def log(*a):
    print(*a, flush=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def build_targets(pool, last_result):
    servers = pool.get("servers") or {}
    prev_ok = []
    for r in (last_result.get("results") or []):
        hp = (r.get("host") or "").replace("sstp://vpn:vpn@", "")
        if hp:
            prev_ok.append(hp)

    targets, seen = [], set()
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


def check_one(hp: str):
    """返回 (hp, detail|None, worker_ms, error)"""
    url = f"{CHECK_API}?sstp=vpn:vpn%40{urllib.parse.quote(hp)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return hp, None, int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"

    if not d.get("success"):
        return hp, None, d.get("responseTime") or int((time.time() - t0) * 1000), d.get("error") or "unknown"

    e = d.get("exit") or {}
    asn = e.get("asn") or {}
    detail = {
        "ip": e.get("ip") or "",
        "ipType": "ipv4" if "." in (e.get("ip") or "") else "",
        "country": e.get("country_code") or "",
        "countryName": e.get("country") or "",
        "region": e.get("region") or "",
        "city": e.get("city") or "",
        "asn": f"AS{asn['asn']}" if asn.get("asn") else "",
        "org": asn.get("org") or asn.get("name") or "",
        "timezone": e.get("timezone") or "",
        "colo": e.get("colo") or d.get("colo") or "",
        "src_api": CHECK_API,
    }
    return hp, detail, d.get("responseTime") or int((time.time() - t0) * 1000), None


def main():
    pool = load_json(POOL_JSON, None)
    if not pool:
        log("没有 sstpall.json，先跑 collect.py")
        return 1
    last = load_json(OUT_JSON, {})

    targets = build_targets(pool, last)
    if SHARD:
        k, n = (int(v) for v in SHARD.split("/"))
        targets = targets[k::n]
    log(f"库内 {len(pool['servers'])} 台，本轮测 {len(targets)} 台，并发 {CONCURRENCY}")
    if not targets:
        return 1

    servers = pool["servers"]
    # 上次有效但已经不在库里的条目（库被清理过/从别处继承）先补一条最小记录，避免 KeyError
    for hp, _src in targets:
        if hp not in servers:
            servers[hp] = {"sstp": f"sstp://vpn:vpn@{hp}", "first_seen": now_iso() if 'now_iso' in dir() else "",
                           "last_seen": "", "ok_count": 0, "fail_count": 0,
                           "last_checked": None, "last_ok": None}
    touched, results = {}, []
    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(check_one, hp): (hp, src) for hp, src in targets}
        for fut in cf.as_completed(futs):
            hp, src = futs[fut]
            try:
                _, detail, ms, err = fut.result()
            except Exception as e:
                detail, ms, err = None, 0, str(e)
            done += 1
            rec = dict(servers.get(hp) or {})
            rec["last_checked"] = now_iso()
            rec["last_ok"] = bool(detail)
            if detail:
                rec["ok_count"] = int(rec.get("ok_count") or 0) + 1
                rec["fail_count"] = 0
                detail["src"] = src
                results.append({"sstp": f"sstp://vpn:vpn@{hp}", "host": hp,
                                "connect_s": round(ms / 1000, 2), "detail": detail})
            else:
                rec["fail_count"] = int(rec.get("fail_count") or 0) + 1
            servers[hp] = rec
            touched[hp] = {k: rec.get(k, 0 if k.endswith("count") else None)
                           for k in ("last_checked", "last_ok", "ok_count", "fail_count")}
            if done % 20 == 0 or done == len(targets):
                log(f"进度 {done}/{len(targets)}  已存活 {len(results)}  用时 {time.time()-t0:.0f}s")

    # 打分
    for r in results:
        hp = r["host"]
        total, parts = ranking.score_node(r["detail"], servers.get(hp) or {}, r["connect_s"])
        r["score"] = total
        r["score_parts"] = parts

    results.sort(key=lambda x: x["connect_s"])
    write_outputs(results, len(targets))
    write_ranked(results)

    if SHARD:
        with open(STATE_OUT, "w", encoding="utf-8") as f:
            json.dump({"shard": SHARD, "at": now_iso(), "servers": touched}, f, ensure_ascii=False, indent=1)
    else:
        pool["last_check_at"] = now_iso()
        with open(STATE_OUT, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=1, sort_keys=True)

    log(f"完成：{len(results)}/{len(targets)} 可用，总耗时 {time.time()-t0:.1f}s")
    return 0 if results else 2


def write_outputs(results, tested):
    lines = [f"{r['sstp']}  {json.dumps(r['detail'], ensure_ascii=False, separators=(',', ':'))}"
             for r in results]
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now_iso(), "tested": tested, "alive": len(results),
                   "results": results}, f, ensure_ascii=False, indent=2)


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


if __name__ == "__main__":
    raise SystemExit(main())
