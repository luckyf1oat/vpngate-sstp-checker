"""节点评分：按 ISP 是否家宽 / 带宽 / 在线时长 / 延迟 / 稳定性 / 负载 综合打分

分数范围 0–100，权重：
  家宽 30 / 带宽 20 / 在线时长 15 / 延迟 15 / 稳定性 15 / 负载 3 / 建链 2
"""
from __future__ import annotations

import math

# 机房/云/代理厂商关键词 —— 命中判 IDC
HOSTING = (
    "amazon", "aws", "google", "microsoft", "azure", "oracle", "digitalocean", "linode",
    "vultr", "hetzner", "ovh", "contabo", "alibaba", "aliyun", "tencent", "cloudflare",
    "choopa", "leaseweb", "m247", "hostinger", "akamai", "gcore", "scaleway", "upcloud",
    "psychz", "quadranet", "colocation", "colo", "datacenter", "data center", "hosting",
    "server", "cloud", "softether", "packethub", "driftnet", "it7", "dmit", "xtom",
    "vpn", "proxy", "cdn", "virtual", "vps", "dedicated",
)

# 运营商关键词 —— 命中判家宽/商用宽带
ISP_HINTS = (
    "telecom", "communications", "broadband", "fiber", "fibre", "cable", "internet",
    "mobile", "wireless", "telephone", "telekom", "kddi", "softbank", "comcast",
    "verizon", "charter", "cox", "centurylink", "frontier", "ziggo", "kpn", "orange",
    "vodafone", "telefonica", "singtel", "hinet", "chunghwa", "kt corp", "korea telecom",
    "sk broadband", "lg u+", "skb", "nuro", "sonet", "so-net", "optic", "optage",
    "tokai", "chubu", "cable net", "network", "net service", "isp",
)


def classify(org: str, asn_label: str = "") -> str:
    """返回 res / idc / unknown"""
    s = f"{org} {asn_label}".lower()
    if not s.strip():
        return "unknown"
    if any(k in s for k in HOSTING):
        return "idc"
    if any(k in s for k in ISP_HINTS):
        return "res"
    return "unknown"


HOUSE_SCORE = {"res": 1.0, "unknown": 0.55, "idc": 0.15}


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score_node(detail: dict, meta: dict, connect_s: float | None) -> tuple[float, dict]:
    """detail：出口信息（org/asn）；meta：库记录（speed/ping/sessions/uptime/ok/fail）"""
    kind = classify(detail.get("org") or "", detail.get("asn") or "")
    house = HOUSE_SCORE[kind]

    speed = float(meta.get("speed_mbps") or 0)
    bw = _clamp(math.log10(max(speed, 1)) / math.log10(800))  # 800Mbps 满分

    up_days = float(meta.get("uptime_days") or 0)
    uptime = _clamp(up_days / 30.0)                            # 30 天满分

    ping = float(meta.get("ping") or 999)
    lat = _clamp(1 - ping / 250.0)

    ok = float(meta.get("ok_count") or 0)
    fail = float(meta.get("fail_count") or 0)
    stable = (ok + 1) / (ok + fail + 2)                        # 拉普拉斯平滑

    sessions = float(meta.get("sessions") or 0)
    load = _clamp(1 - sessions / 150.0)

    conn = _clamp(1 - (connect_s or 12) / 12.0)

    parts = {
        "house": round(house * 30, 1),
        "bw": round(bw * 20, 1),
        "uptime": round(uptime * 15, 1),
        "lat": round(lat * 15, 1),
        "stable": round(stable * 15, 1),
        "load": round(load * 3, 1),
        "conn": round(conn * 2, 1),
    }
    total = round(sum(parts.values()), 1)
    parts["kind"] = kind
    parts["uptime_days"] = round(up_days, 2)
    return total, parts
