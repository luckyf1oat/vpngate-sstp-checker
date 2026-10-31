#!/usr/bin/env python3
"""vpngate SSTP 定时测速/测活

抓 vpngate 列表 -> 取支持 MS-SSTP 的服务器 -> sstpc 真实建立 SSTP 连接（vpn/vpn）
-> 只给 ip.5671234.xyz 加 /32 路由走隧道 -> 取出口 IP 详情 -> 写 result.txt/json。

安全设计：
- **不改默认路由**，只把 IP 查询接口那一跳钉到 ppp0，查完立刻删路由。
- 启动前检查本机不能已有 ppp0（pppd 2.4 不支持自定义 ifname，会和既有 ppp 隧道撞名），
  已有则直接退出不碰。
- 每个服务器跑完都确保 ppp0 消失，残留就删掉。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

SITE = os.environ.get("VPNGATE_URL", "https://www.vpngate.net/cn/")
API_URL = os.environ.get("VPNGATE_API", "https://www.vpngate.net/api/iphone/")
IPAPI = os.environ.get("IPAPI_URL", "https://ip.5671234.xyz/json/")
LIMIT = int(os.environ.get("LIMIT", "100"))
MAX_PING = int(os.environ.get("MAX_PING", "400"))
CONNECT_TIMEOUT = int(os.environ.get("CONNECT_TIMEOUT", "10"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "8"))
OUT_TXT = os.environ.get("OUT_TXT", "result.txt")
OUT_JSON = os.environ.get("OUT_JSON", "result.json")
ALLOW_EXISTING_PPP0 = os.environ.get("ALLOW_EXISTING_PPP0", "0") == "1"
MY_IP = ""
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

API_HOST = urlparse(IPAPI).hostname or "ip.5671234.xyz"
API_PORT = urlparse(IPAPI).port or 443


def log(*a):
    print(*a, flush=True)


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def http_get(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def ppp0_exists() -> bool:
    return run(["ip", "link", "show", "ppp0"], 5).returncode == 0


def ppp0_up() -> bool:
    out = run(["ip", "-4", "addr", "show", "ppp0"], 5)
    return out.returncode == 0 and "inet " in out.stdout


def _int(v):
    try:
        return int(v)
    except Exception:
        return 0


def fetch_api_list():
    """官方 CSV API：服务器清单 + ping/speed/国家/会话数（CSV 里没有 SSTP 字段）"""
    raw = http_get(API_URL, 30)
    lines = [l for l in raw.splitlines() if l and not l.startswith("*")]
    if not lines:
        return []
    lines[0] = lines[0].lstrip("#")
    out = []
    for r in csv.DictReader(io.StringIO("\n".join(lines))):
        host = (r.get("HostName") or "").strip()
        ip = (r.get("IP") or "").strip()
        if not host or not ip:
            continue
        out.append({
            "host": host,
            "ip": ip,
            "country": (r.get("CountryLong") or "").strip(),
            "ping": _int(r.get("Ping")) or 9999,
            "speed_mbps": round(_int(r.get("Speed")) * 8 / 1e6, 2),
            "sessions": _int(r.get("NumVpnSessions")),
        })
    return out


def fetch_sstp_ports():
    """列表页：host -> SSTP 端口。这是 API 里唯一缺的字段。

    注意：不能用 OpenVPN 配置里的 `remote <ip> <port>` 推 SSTP 端口——
    实测两者并不总相等（例：vpn805893912 配置里是 995，SSTP 实际不通），
    所以端口必须以页面上的 `SSTP 主机名:端口` 为准。
    """
    html = http_get(SITE, 30)
    ports = {}
    for row in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S | re.I):
        if ".opengw.net" not in row:
            continue
        text = re.sub(r"<[^>]+>", "|", row)
        text = re.sub(r"&nbsp;?", " ", text)
        hosts = re.findall(r"([A-Za-z0-9][A-Za-z0-9.\-]*\.opengw\.net)", text)
        if not hosts:
            continue
        name = hosts[0].split(".")[0]
        if name in ports:
            continue
        m = re.search(r"SSTP\s*主机名\s*:?\s*\|+\s*([A-Za-z0-9.\-]+\.opengw\.net)(?::(\d{1,5}))?", text)
        if m:
            ports[name] = int(m.group(2)) if m.group(2) else 443
    return ports


def fetch_servers():
    """API 清单（权威+带元数据）∩ 页面 SSTP 端口表（补端口）"""
    api = fetch_api_list()
    log(f"API 清单 {len(api)} 台")
    ports = {}
    try:
        ports = fetch_sstp_ports()
        log(f"页面 SSTP 端口表 {len(ports)} 台")
    except Exception as e:
        log(f"页面取 SSTP 端口失败，退回 443 盲测: {e}")

    servers = []
    for s in api:
        name = s["host"]
        if ports:
            if name not in ports:
                continue          # 页面没标 SSTP ⇒ 这台不支持，跳过
            port = ports[name]
        else:
            port = 443
        servers.append({**s, "sstp": f"{name}.opengw.net:{port}"})

    servers.sort(key=lambda x: (x["ping"], -x["speed_mbps"]))
    servers = [s for s in servers if s["ping"] <= MAX_PING]
    return servers if LIMIT <= 0 else servers[:LIMIT]


def parse_servers(html: str):
    out = []
    for row in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S | re.I):
        if ".opengw.net" not in row:
            continue
        text = re.sub(r"<[^>]+>", "|", row)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        hosts = re.findall(r"([A-Za-z0-9][A-Za-z0-9.\-]*\.opengw\.net(?::\d{1,5})?)", text)
        if not hosts or "SSTP" not in text:
            continue
        m_ip = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text)
        m_ping = re.search(r"Ping:\s*\|*\s*(\d+)\s*ms", text)
        m_speed = re.search(r"([\d.]+)\s*Mbps", text)
        m_sess = re.search(r"(\d+)\s*(?:会话|sessions?)", text, re.I)
        m_country = re.search(r"\|\s*\|+\s*([A-Z][A-Za-z ().\-]{2,40})\s*\|+\s*" + re.escape(hosts[0]), text)
        sstp_host = hosts[-1] if ":" in hosts[-1] else hosts[-1] + ":443"
        if not re.match(r"^(public-vpn-\d+|vpn\d+)\.opengw\.net$", sstp_host.split(":")[0]):
            continue
        if not m_ip:
            continue
        out.append({
            "country": (m_country.group(1).strip() if m_country else ""),
            "host": hosts[0].split(":")[0],
            "ip": m_ip.group(1) if m_ip else "",
            "ping": int(m_ping.group(1)) if m_ping else 9999,
            "speed_mbps": float(m_speed.group(1)) if m_speed else 0.0,
            "sessions": int(m_sess.group(1)) if m_sess else 0,
            "sstp": sstp_host,
        })
    seen, uniq = set(), []
    for s in out:
        if s["sstp"] in seen:
            continue
        seen.add(s["sstp"])
        uniq.append(s)
    uniq.sort(key=lambda s: (s["ping"], -s["speed_mbps"]))
    picked = [s for s in uniq if s["ping"] <= MAX_PING]
    return picked if LIMIT <= 0 else picked[:LIMIT]


def api_via_tunnel():
    """给 API 的每个 A 记录加 /32 走 ppp0，查一次，然后删掉"""
    ips = sorted({ai[4][0] for ai in socket.getaddrinfo(API_HOST, API_PORT, socket.AF_INET)})
    if not ips:
        raise RuntimeError("DNS 解析 %s 失败" % API_HOST)
    added = []
    try:
        for ip in ips:
            if run(["ip", "route", "replace", f"{ip}/32", "dev", "ppp0"], 5).returncode == 0:
                added.append(ip)
        if not added:
            raise RuntimeError("加 ppp0 路由失败")
        # 确认路由真的落在 ppp0
        chk = run(["ip", "route", "get", ips[0]], 5).stdout
        if "ppp0" not in chk:
            raise RuntimeError("路由未走 ppp0: %s" % chk.strip())
        raw = run(["curl", "-sS", "-4", "--max-time", str(FETCH_TIMEOUT),
                   "--resolve", f"{API_HOST}:{API_PORT}:{ips[0]}", IPAPI,
                   "-H", f"User-Agent: {UA}"], FETCH_TIMEOUT + 5).stdout
        return json.loads(raw)
    finally:
        for ip in added:
            run(["ip", "route", "del", f"{ip}/32", "dev", "ppp0"], 5)


def kill_vpn(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    for _ in range(10):
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    run(["pkill", "-f", "sstpc"], 5)
    for _ in range(10):
        if not ppp0_exists():
            return
        time.sleep(0.5)
    run(["ip", "link", "del", "ppp0"], 5)


def probe(server, idx, total):
    host_port = server["sstp"]
    cmd = ["sstpc", "--user", "vpn", "--password", "vpn", "--cert-warn",
           "--save-server-route", "--log-level", "2", host_port,
           "--", "noauth", "nodetach", "noipdefault", "mtu", "1400",
           "ipcp-accept-local", "ipcp-accept-remote", "usepeerdns"]
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        deadline = t0 + CONNECT_TIMEOUT
        while time.time() < deadline:
            if ppp0_up():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if not ppp0_up():
            log(f"[{idx}/{total}] {host_port} 连不上 ({time.time()-t0:.1f}s)")
            return None
        connect_s = time.time() - t0
        try:
            info = api_via_tunnel()
        except Exception as e:
            log(f"[{idx}/{total}] {host_port} 隧道通但取 IP 失败: {e}")
            return None
        ip = info.get("ip") or ""
        if not ip:
            log(f"[{idx}/{total}] {host_port} 取不到出口 IP")
            return None
        if MY_IP and ip == MY_IP:
            log(f"[{idx}/{total}] {host_port} 出口仍是本机 IP ({ip})，隧道没生效")
            return None
        loc = info.get("location") or {}
        net = info.get("network") or {}
        detail = {
            "ip": ip,
            "country": loc.get("country") or "",
            "countryName": loc.get("countryName") or "",
            "region": loc.get("region") or "",
            "city": loc.get("city") or "",
            "asn": net.get("asn") or "",
            "org": net.get("org") or "",
            "timezone": loc.get("timezone") or "",
            "colo": net.get("colo") or "",
        }
        log(f"[{idx}/{total}] {host_port} OK {connect_s:.1f}s -> {ip} {detail['country']}/{detail['city']} {detail['org']}")
        return {
            "sstp": f"sstp://vpn:vpn@{host_port}",
            "host": host_port,
            "connect_s": round(connect_s, 2),
            "gateway_ping_ms": server["ping"],
            "gateway_speed_mbps": server["speed_mbps"],
            "gateway_country": server["country"],
            "detail": detail,
        }
    finally:
        kill_vpn(proc)


def write_outputs(results, tested):
    lines = [f"{r['sstp']}  {json.dumps(r['detail'], ensure_ascii=False, separators=(',', ':'))}"
             for r in results]
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tested": tested,
            "alive": len(results),
            "source": SITE,
            "ip_api": IPAPI,
            "results": results,
        }, f, ensure_ascii=False, indent=2)


def main():
    global MY_IP
    if ppp0_exists() and not ALLOW_EXISTING_PPP0:
        log("本机已有 ppp0（可能是别的 VPN 隧道）。pppd 2.4 不能改 ifname，会撞名，退出。")
        return 3
    try:
        MY_IP = (json.loads(http_get(IPAPI, 10)) or {}).get("ip") or ""
        log(f"本机出口 IP: {MY_IP}")
    except Exception as e:
        MY_IP = ""
        log(f"取本机出口 IP 失败（不阻断）: {e}")
    servers = fetch_servers()
    log(f"待测 {len(servers)} 台（端口取自列表页 SSTP 主机名）")
    if not servers:
        log("没拿到可测服务器（API 或页面结构可能变了）")
        return 1

    results = []
    for i, s in enumerate(servers, 1):
        try:
            r = probe(s, i, len(servers))
            if r:
                results.append(r)
                write_outputs(results, len(servers))
        except Exception as e:
            log(f"[{i}/{len(servers)}] {s['sstp']} 异常: {e}")
    results.sort(key=lambda x: x["connect_s"])
    write_outputs(results, len(servers))
    log(f"完成：{len(results)}/{len(servers)} 可用")
    return 0 if results else 2


if __name__ == "__main__":
    sys.exit(main())
