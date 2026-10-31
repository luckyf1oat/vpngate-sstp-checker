#!/usr/bin/env python3
"""每小时采集：把当前能看到的 SSTP 服务器合进 sstpall.txt 库（去重累积）

产物：
- sstpall.txt      纯文本库，一行一个 sstp://vpn:vpn@host:port
- sstpall.json     带元数据（国家/ping/带宽/首次与最后出现/上次检测结果）
"""
from __future__ import annotations

import json
import os
import re

import vpngate_lib as lib

POOL_TXT = os.environ.get("POOL_TXT", "sstpall.txt")
POOL_JSON = os.environ.get("POOL_JSON", "sstpall.json")


def load_pool():
    if os.path.exists(POOL_JSON):
        try:
            return json.load(open(POOL_JSON, encoding="utf-8"))
        except Exception:
            pass
    return {"updated_at": None, "servers": {}}


def main():
    pool = load_pool()
    servers = pool.setdefault("servers", {})
    # 清掉早期版本写进来的非法主机名（laud.opengw.net / public-vpn-0 之类）
    valid = re.compile(r"^(public-vpn-\d+|vpn\d+)\.opengw\.net:\d+$")
    bad = [hp for hp in servers if not valid.match(hp)]
    for hp in bad:
        del servers[hp]
    if bad:
        lib.log(f"清掉非法条目 {len(bad)} 个")
    before = len(servers)

    api = lib.api_list()
    ports = lib.sstp_ports()
    now = lib.now_iso()

    added = 0
    for s in api:
        name = s["host"]
        if name not in ports:
            continue
        hp = f"{name}.opengw.net:{ports[name]}"
        rec = servers.get(hp)
        if rec is None:
            servers[hp] = {
                "sstp": f"sstp://vpn:vpn@{hp}",
                "country": s["country"],
                "ping": s["ping"],
                "speed_mbps": s["speed_mbps"],
                "sessions": s["sessions"],
                "uptime_days": s.get("uptime_days", 0),
                "first_seen": now,
                "last_seen": now,
                "last_checked": None,
                "last_ok": None,
                "ok_count": 0,
                "fail_count": 0,
            }
            added += 1
        else:
            rec.update(country=s["country"], ping=s["ping"], speed_mbps=s["speed_mbps"],
                       sessions=s["sessions"], uptime_days=s.get("uptime_days", 0), last_seen=now)

    # 清理：12 小时没在官网出现、且从未成功过的（不同 runner 视角会带来大量一次性条目）
    import datetime
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pruned = 0
    for hp in list(servers):
        rec = servers[hp]
        if (rec.get("last_seen") or "") < cutoff and not (rec.get("ok_count") or 0):
            del servers[hp]
            pruned += 1

    # 硬上限：超过 250 台就按「曾经成功过 + 最近出现」保留
    MAX_POOL = int(os.environ.get("MAX_POOL", "200"))
    if len(servers) > MAX_POOL:
        def rank_key(item):
            hp, rec = item
            # 新鲜度优先（新见到的节点也能留下被测），成功历史只做次要权重
            return (rec.get("last_seen") or "", 1 if (rec.get("ok_count") or 0) else 0)
        keep = dict(sorted(servers.items(), key=rank_key, reverse=True)[:MAX_POOL])
        pruned += len(servers) - len(keep)
        servers.clear()
        servers.update(keep)

    pool["updated_at"] = now
    pool["api_seen"] = len(api)
    pool["sstp_seen"] = len(ports)

    # 纯文本库（按国家+主机排序，稳定 diff）
    lines = sorted({r["sstp"] for r in servers.values()})
    with open(POOL_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    with open(POOL_JSON, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1, sort_keys=True)

    live = sum(1 for r in servers.values() if (r.get("last_seen") or "") >= (now[:13] + ":00:00Z"))
    lib.log(f"采集完成：本轮 API {len(api)} 台 / 页面 SSTP {len(ports)} 台；"
            f"新增 {added}，清理过期/超上限 {pruned}，库总 {len(servers)}（{before}→{len(servers)}），"
            f"本轮刷新 {live} 台")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
