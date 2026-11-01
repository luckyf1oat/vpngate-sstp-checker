#!/usr/bin/env python3
"""GitHub Actions 定时兜底：GH 自带 cron 不稳时，由本机补发 workflow_dispatch。

规则（每 20 分钟跑一次）：
  collect.yml  超过 75 分钟没跑  → 补发
  check.yml    超过 150 分钟没跑 → 补发
任一 workflow 有 queued/in_progress 的 run 时跳过，避免叠加。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

REPO = "luckyf1oat/vpngate-sstp-checker"
TOKEN_FILE = "/root/turnstile-login/gh_vpngate_token.txt"
TOKEN_MAIN = "/root/turnstile-login/cf-main-account.txt"
TOKEN_FALLBACK_GREP = "/root/turnstile-login"

RULES = [(".github/workflows/collect.yml", 75 * 60, "collect"),
         (".github/workflows/check.yml", 150 * 60, "check")]


def token():
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
        if t:
            return t
    import re
    import glob
    if os.path.exists(TOKEN_MAIN):
        m = re.search(r"gh[pousr]_[A-Za-z0-9]{30,}", open(TOKEN_MAIN, errors="ignore").read())
        if m:
            return m.group(0)
    for p in glob.glob(TOKEN_FALLBACK_GREP + "/*.txt"):
        try:
            m = re.search(r"gh[pousr]_[A-Za-z0-9]{30,}", open(p, errors="ignore").read())
        except Exception:
            continue
        if m:
            return m.group(0)
    return ""


TOK = token()


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Authorization": "Bearer " + TOK, "Accept": "application/vnd.github+json",
         "User-Agent": "hermes-dispatch"}
    if data:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request("https://api.github.com" + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def main():
    if not TOK:
        print("no token"); return 1
    st, d = api(f"/repos/{REPO}/actions/runs?per_page=50")
    if st != 200:
        print("runs api fail", st, d); return 1
    runs = d.get("workflow_runs", []) if isinstance(d, dict) else []
    now = time.time()
    # 两个 workflow 共用 concurrency group，只要有一个在跑就整体等待
    global_busy = [r for r in runs if r.get("path", "").endswith(("collect.yml", "check.yml"))
                   and r.get("status") in ("queued", "in_progress")]
    if global_busy:
        print(f"[跳过] 仓库已有任务在跑: {global_busy[0]['name']} ({global_busy[0]['status']})")
        return 0
    acted = 0
    for path, max_age, name in RULES:
        mine = [r for r in runs if r.get("path") == path]
        busy = [r for r in mine if r.get("status") in ("queued", "in_progress")]
        if busy:
            print(f"{name}: 有任务在跑({busy[0]['status']})，跳过")
            continue
        last = 0
        if mine:
            ts = max(r["created_at"] for r in mine)
            last = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        age = now - last
        if age < max_age:
            print(f"{name}: 上次 {int(age/60)} 分钟前，正常")
            continue
        st2, r2 = api(f"/repos/{REPO}/actions/workflows/{path.split('/')[-1]}/dispatches",
                      "POST", {"ref": "main"})
        print(f"{name}: 已停 {int(age/60)} 分钟 → 补发 dispatch: {st2} {r2 if st2!=204 else 'ok'}")
        acted += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
