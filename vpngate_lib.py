"""vpngate SSTP 项目共用：抓列表、建隧道、写出结果"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

SITE = os.environ.get("VPNGATE_URL", "https://www.vpngate.net/cn/")
API_URL = os.environ.get("VPNGATE_API", "https://www.vpngate.net/api/iphone/")
IPAPI = os.environ.get("IPAPI_URL", "https://cks5.115567.xyz/ip.json")
# 兜底接口（主接口失败时用）：老的自建 ip 回显
IPAPI_FALLBACK = os.environ.get("IPAPI_FALLBACK", "https://ip.5671234.xyz/json/")
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "8"))
CONNECT_TIMEOUT = int(os.environ.get("CONNECT_TIMEOUT", "8"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

API_HOST = urlparse(IPAPI).hostname or "ip.5671234.xyz"
API_PORT = urlparse(IPAPI).port or 443


def log(*a):
    print(*a, flush=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def http_get(url: str, timeout: int = 30, retries: int = 3) -> str:
    """带重试的 GET：VPN Gate 偶发读超时，单次失败不该让整轮 run 挂掉"""
    last = None
    for i in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                wait = 3 * (i + 1)
                log(f"  [warn] GET 失败({type(e).__name__}: {e})，{wait}s 后第 {i + 2}/{retries} 次重试")
                time.sleep(wait)
    raise last  # type: ignore[misc]


def _int(v):
    try:
        return int(v)
    except Exception:
        return 0


# ---------------------------------------------------------------- 采集

def api_list():
    """官方 CSV：清单 + ping/speed/国家（没有 SSTP 端口）"""
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
            "uptime_days": round(_int(r.get("Uptime")) / 86400000, 2),
        })
    return out


def sstp_ports():
    """列表页：host -> SSTP 端口（API 唯一缺的字段）"""
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


# ---------------------------------------------------------------- 隧道

def ppp0_exists() -> bool:
    return run(["ip", "link", "show", "ppp0"], 5).returncode == 0


def ppp0_up() -> bool:
    out = run(["ip", "-4", "addr", "show", "ppp0"], 5)
    return out.returncode == 0 and "inet " in out.stdout


def _host_of(url: str):
    u = urlparse(url)
    return u.hostname, (u.port or 443)


def normalize_ip_info(d: dict) -> dict:
    """兼容两套返回：
    cks5:  {ip, country, region, city, timezone, colo, asn(int), asOrganization, org}
    ip567: {ip, location:{country,countryName,region,city,timezone}, network:{asn,org,colo}}
    """
    d = d or {}
    loc = d.get("location") or {}
    net = d.get("network") or {}
    asn = net.get("asn") or ""
    if not asn and d.get("asn"):
        asn = f"AS{d['asn']}"
    return {
        "ip": d.get("ip") or "",
        "ipType": d.get("ipType") or "",
        "country": loc.get("country") or d.get("country") or "",
        "countryName": loc.get("countryName") or d.get("countryName") or d.get("country") or "",
        "region": loc.get("region") or d.get("region") or "",
        "city": loc.get("city") or d.get("city") or "",
        "asn": asn,
        "org": net.get("org") or d.get("asOrganization") or d.get("org") or "",
        "timezone": loc.get("timezone") or d.get("timezone") or "",
        "colo": net.get("colo") or d.get("colo") or "",
    }


def api_via_tunnel(url: str = ""):
    """只把该接口的 A 记录钉到 ppp0，查完立刻删路由（不动默认路由）"""
    url = url or IPAPI
    host, port = _host_of(url)
    ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, socket.AF_INET)})
    if not ips:
        raise RuntimeError("DNS 解析 %s 失败" % host)
    added = []
    try:
        for ip in ips:
            if run(["ip", "route", "replace", f"{ip}/32", "dev", "ppp0"], 5).returncode == 0:
                added.append(ip)
        if not added:
            raise RuntimeError("加 ppp0 路由失败")
        if "ppp0" not in run(["ip", "route", "get", ips[0]], 5).stdout:
            raise RuntimeError("路由未走 ppp0")
        raw = run(["curl", "-sS", "-4", "--max-time", str(FETCH_TIMEOUT),
                   "--resolve", f"{host}:{port}:{ips[0]}", url,
                   "-H", f"User-Agent: {UA}"], FETCH_TIMEOUT + 5).stdout
        return json.loads(raw)
    finally:
        for ip in added:
            run(["ip", "route", "del", f"{ip}/32", "dev", "ppp0"], 5)


def kill_vpn(proc):
    """快速清理：能 TERM 就 TERM，短暂等待后直接 KILL + 删 ppp0，不空等"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    for _ in range(3):
        if proc.poll() is not None:
            break
        time.sleep(0.15)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    run(["pkill", "-9", "-f", "sstpc"], 5)
    if ppp0_exists():
        run(["ip", "link", "del", "ppp0"], 5)


def probe(host_port: str, idx: int, total: int, my_ip: str = ""):
    """真实建立 SSTP，返回 detail dict 或 None"""
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
            time.sleep(0.4)
        if not ppp0_up():
            log(f"[{idx}/{total}] {host_port} 连不上 ({time.time()-t0:.1f}s)")
            return None
        connect_s = time.time() - t0
        detail = None
        last_err = None
        for api_url in (IPAPI, IPAPI_FALLBACK):
            if not api_url:
                continue
            try:
                detail = normalize_ip_info(api_via_tunnel(api_url))
                detail["src_api"] = api_url
                break
            except Exception as e:
                last_err = e
                log(f"[{idx}/{total}] {host_port} 取 IP 失败({api_url}): {e}")
        if not detail:
            log(f"[{idx}/{total}] {host_port} 所有 IP 接口都失败: {last_err}")
            return None
        ip = detail.get("ip") or ""
        if not ip or (my_ip and ip == my_ip):
            log(f"[{idx}/{total}] {host_port} 出口无效 ({ip})")
            return None
        log(f"[{idx}/{total}] {host_port} OK {connect_s:.1f}s -> {ip} {detail['country']}/{detail['city']} {detail['org']}")
        return {"sstp": f"sstp://vpn:vpn@{host_port}", "host": host_port,
                "connect_s": round(connect_s, 2), "detail": detail}
    finally:
        kill_vpn(proc)


def my_ip() -> str:
    for url in (IPAPI, IPAPI_FALLBACK):
        if not url:
            continue
        try:
            return normalize_ip_info(json.loads(http_get(url, 10))).get("ip") or ""
        except Exception:
            continue
    return ""
