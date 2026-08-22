#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash SpeedBench — Clash Verge Rev / Mihomo 节点综合测速
- Reads the running Mihomo controller API (TCP or Unix socket)
- Measures per-node latency with Mihomo's /delay API
- Temporarily switches Mihomo to GLOBAL mode for real download tests
- Downloads through the running mixed-port with curl
- Fetches exit-IP profile per node via ip-api.com (ASN/国家/ISP/机房托管·移动·代理标记)
- Restores the original mode and proxy selections on exit
- Renders a star-rated box table and writes a CSV report

No third-party Python packages required.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import http.client
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Clash Verge Rev (recent versions) launches mihomo with -ext-ctl-unix instead of
# a TCP external-controller, so the Unix socket is probed first.
DEFAULT_CONTROLLERS = (
    "unix:///tmp/verge/verge-mihomo.sock",
    "http://127.0.0.1:9097",
    "http://127.0.0.1:9090",
)
DEFAULT_DELAY_URL = "https://cp.cloudflare.com/generate_204"
DEFAULT_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
# ip-api.com 免费端点（仅 HTTP）；每个节点经各自出口 IP 查询，45 次/分钟限制足够
DEFAULT_IP_API_URL = (
    "http://ip-api.com/json/?fields=status,message,query,country,countryCode,"
    "regionName,city,isp,org,as,asname,mobile,proxy,hosting&lang=zh-CN"
)

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance"}
SELECTABLE_GROUP_TYPES = {"Selector", "URLTest", "Fallback"}
BUILTIN_SKIP_TYPES = {"Direct", "Reject", "RejectDrop", "Pass", "PassRule", "Compatible"}
BUILTIN_SKIP_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE"}


class ApiError(RuntimeError):
    pass


class Unauthorized(ApiError):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal HTTP-over-Unix-socket connection for Mihomo's -ext-ctl-unix."""

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class MihomoAPI:
    def __init__(self, base: str, secret: str = "", timeout: float = 5.0):
        base = base.rstrip("/")
        self.unix_path: Optional[str] = None
        if base.startswith("unix://"):
            self.unix_path = base[len("unix://"):]
            self.base = "http://localhost"
        else:
            self.base = base
        self.secret = secret
        self.timeout = timeout

    def request(self, method: str, path: str, data: Optional[dict] = None) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        conn: http.client.HTTPConnection
        if self.unix_path is not None:
            conn = UnixHTTPConnection(self.unix_path, timeout=self.timeout)
        else:
            parsed = urllib.parse.urlsplit(self.base)
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                                   timeout=self.timeout)
            else:
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80,
                                                  timeout=self.timeout)

        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise ApiError(f"{method} {path}: {e}") from e
        finally:
            conn.close()

        if resp.status in (401, 403):
            raise Unauthorized(f"Controller returned HTTP {resp.status}")
        if resp.status >= 400:
            detail = raw.decode("utf-8", "replace")[:500]
            raise ApiError(f"{method} {path}: HTTP {resp.status}: {detail}")
        if not raw:
            return None
        if raw[:1] in (b"{", b"["):
            return json.loads(raw.decode("utf-8"))
        return raw.decode("utf-8", "replace")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def put(self, path: str, data: Optional[dict] = None) -> Any:
        return self.request("PUT", path, data)

    def patch(self, path: str, data: dict) -> Any:
        return self.request("PATCH", path, data)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def encoded_proxy_path(self, name: str) -> str:
        return "/proxies/" + urllib.parse.quote(name, safe="")

    def select(self, group: str, child: str) -> None:
        self.put(self.encoded_proxy_path(group), {"name": child})

    def proxy_delay(self, name: str, url: str, timeout_ms: int) -> Optional[int]:
        params = urllib.parse.urlencode({"url": url, "timeout": timeout_ms})
        try:
            result = self.get(self.encoded_proxy_path(name) + "/delay?" + params)
            return int(result["delay"])
        except Exception:
            return None


@dataclass
class IpInfo:
    exit_ip: str = ""
    country: str = ""
    country_code: str = ""
    region: str = ""
    city: str = ""
    isp: str = ""
    org: str = ""
    asn: str = ""           # 例如 "AS13335 Cloudflare, Inc."
    asname: str = ""        # 例如 "CLOUDFLARENET"
    kind: str = ""          # 代理/VPN / 机房托管 / 移动网络 / ISP/非托管 / 未知
    # ip-api 原始布尔标记：仅表示"被识别为"，全 false 不等于住宅，故如实保留
    proxy: bool = False
    hosting: bool = False
    mobile: bool = False
    ok: bool = False


@dataclass
class Result:
    name: str
    provider: str
    proto: str
    latency_ms: Optional[int]
    speeds_mbps: List[float]
    median_mbps: Optional[float]
    best_mbps: Optional[float]
    status: str
    ip: Optional[IpInfo] = None
    score: float = 0.0
    tags: str = ""
    jitter_ms: Optional[float] = None    # 延迟抖动（多次延迟采样的标准差）
    connect_ms: Optional[float] = None   # TCP+TLS 建连耗时（curl time_appconnect）
    multi_mbps: Optional[float] = None   # 4 路并发流合计峰值带宽（--multi）
    sample_mb: Optional[int] = None      # 实际带宽样本大小 MB（自适应时逐节点不同）


def detect_controller(secret: str, explicit: Optional[str]) -> Tuple[str, bool]:
    candidates = [explicit.rstrip("/")] if explicit else list(DEFAULT_CONTROLLERS)
    for base in candidates:
        if base.startswith("unix://") and not os.path.exists(base[len("unix://"):]):
            continue
        api = MihomoAPI(base, secret=secret)
        try:
            api.get("/version")
            return base, False
        except Unauthorized:
            return base, True
        except ApiError:
            continue
    raise ApiError(
        "找不到 Mihomo External Controller。已尝试: "
        + ", ".join(candidates)
        + "\n请确认 Clash Verge Rev 正在运行并已开启「外部控制」。"
    )


def get_secret_if_needed(base: str, secret: str, needs_secret: bool) -> str:
    if not needs_secret:
        return secret
    if secret:
        return secret
    print(f"\n检测到 {base}，但 API 需要访问密钥。")
    print("可在 Clash Verge Rev → Clash 设置 → 外部控制 中查看/设置访问密钥。")
    return getpass.getpass("请输入 External Controller Secret（输入时不显示）: ").strip()


def leaf_nodes(proxies: Dict[str, dict]) -> Dict[str, dict]:
    out = {}
    for name, info in proxies.items():
        typ = str(info.get("type", ""))
        if name in BUILTIN_SKIP_NAMES:
            continue
        if typ in GROUP_TYPES or typ in BUILTIN_SKIP_TYPES:
            continue
        # Keep actual outbound proxy adapters only.
        out[name] = info
    return out


def build_selectable_graph(proxies: Dict[str, dict]) -> Dict[str, List[str]]:
    graph: Dict[str, List[str]] = {}
    for name, info in proxies.items():
        if info.get("type") in SELECTABLE_GROUP_TYPES and isinstance(info.get("all"), list):
            graph[name] = list(info["all"])
    return graph


def find_path(graph: Dict[str, List[str]], root: str, target: str) -> Optional[List[str]]:
    """
    Return [root, ..., target], traversing only selectable groups.
    target can be a leaf proxy.
    """
    if root == target:
        return [root]
    queue: List[List[str]] = [[root]]
    seen = {root}
    while queue:
        path = queue.pop(0)
        cur = path[-1]
        for child in graph.get(cur, []):
            if child == target:
                return path + [child]
            if child in graph and child not in seen:
                seen.add(child)
                queue.append(path + [child])
    return None


def apply_path(api: MihomoAPI, path: List[str], proxies: Dict[str, dict],
               saved: Dict[str, Tuple[str, Optional[str]]]) -> None:
    """
    Set each group along path to its next child.
    Save original state once:
      ('put', original_child) or ('delete', None) for an unpinned URLTest/Fallback.
    """
    for group, child in zip(path[:-1], path[1:]):
        info = proxies[group]
        if group not in saved:
            typ = info.get("type")
            if typ == "Selector":
                saved[group] = ("put", info.get("now"))
            else:
                # URLTest/Fallback: if fixed exists, restore it; otherwise DELETE clears the pin.
                fixed = info.get("fixed")
                if fixed:
                    saved[group] = ("put", fixed)
                else:
                    saved[group] = ("delete", None)
        api.select(group, child)


def refresh_proxy_snapshot(api: MihomoAPI) -> Dict[str, dict]:
    data = api.get("/proxies")
    return data.get("proxies", {})


def restore_groups(api: MihomoAPI, saved: Dict[str, Tuple[str, Optional[str]]]) -> None:
    # reverse order is safer for nested groups
    for group, (action, value) in reversed(list(saved.items())):
        try:
            if action == "put" and value:
                api.select(group, value)
            elif action == "delete":
                api.delete(api.encoded_proxy_path(group))
        except Exception as e:
            print(f"⚠️ 恢复策略组失败: {group}: {e}", file=sys.stderr)


def curl_speed(proxy_url: str, download_url: str, max_time: float,
               connect_timeout: float) -> Tuple[Optional[float], str, Optional[float], float]:
    """
    返回 (Mbps, status, connect_ms, size_mb)。curl speed_download 单位是 bytes/sec。
    有效样本严格判定：returncode 为 0 或 28（max-time 截断）、http_code==200、
    且下载量 >= 256KB；其余一律记为失败并在 status 里注明原因。
    connect_ms 取自 time_appconnect（TCP+TLS 建连耗时；非 TLS 时退化为 time_connect）。
    """
    cmd = [
        "curl",
        "--proxy", proxy_url,
        "--output", os.devnull,
        "--silent",
        "--show-error",
        "--location",
        "--connect-timeout", str(connect_timeout),
        "--max-time", str(max_time),
        "--write-out",
        "%{speed_download}\t%{time_total}\t%{size_download}\t%{http_code}"
        "\t%{time_connect}\t%{time_appconnect}\t%{time_starttransfer}",
        download_url,
    ]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=max_time + connect_timeout + 5)
    except FileNotFoundError:
        raise RuntimeError("未找到 curl。macOS 自带 curl；Windows 10/11 通常也自带 curl。")
    except subprocess.TimeoutExpired:
        return None, "curl-timeout", None, 0.0

    text = (p.stdout or "").strip()
    parts = text.split("\t")
    if len(parts) != 7:
        err = (p.stderr or "").strip().replace("\n", " ")[:120]
        return None, f"curl-{p.returncode}: {err}", None, 0.0

    try:
        speed_Bps = float(parts[0])
        size_B = float(parts[2])
        http_code = int(parts[3])
        t_connect = float(parts[4])
        t_appconnect = float(parts[5])
    except ValueError:
        return None, "parse-error", None, 0.0

    size_mb = size_B / 1_000_000
    tls_s = t_appconnect if t_appconnect > 0 else t_connect
    connect_ms: Optional[float] = round(tls_s * 1000, 1) if tls_s > 0 else None

    if p.returncode not in (0, 28):
        err = (p.stderr or "").strip().replace("\n", " ")[:120]
        return None, f"curl-{p.returncode}: {err}", connect_ms, size_mb
    if http_code != 200:
        return None, f"http-{http_code}", connect_ms, size_mb
    # 下载量太少的样本不可信（对端提前断流、劫持返回小页面等）
    if speed_Bps <= 0 or size_B < 256 * 1024:
        err = (p.stderr or "").strip().replace("\n", " ")[:120]
        return None, f"no-data: {err}" if err else "no-data", connect_ms, size_mb

    mbps = speed_Bps * 8 / 1_000_000
    return mbps, "ok", connect_ms, size_mb


WARMUP_BYTES = 1_000_000  # ~1MB 预热请求，用于估粗速度


def warmup_speed(proxy_url: str, connect_timeout: float) -> Optional[float]:
    """~1MB 轻量下载估粗速度（Mbps），供自适应样本大小参考；失败返回 None。"""
    url = (DEFAULT_DOWNLOAD_URL.format(bytes=WARMUP_BYTES)
           + f"&measId=warmup-{int(time.time()*1000)}")
    mbps, _, _, _ = curl_speed(proxy_url, url, max_time=5.0,
                               connect_timeout=connect_timeout)
    return mbps


def adaptive_sample(rough_mbps: Optional[float],
                    base_max_time: float) -> Tuple[int, float]:
    """按粗速度选自适应样本大小与单轮时限，返回 (样本 MB, max_time)。
    目标单次样本持续 2-4 秒；时限在 --max-time 基础上最多放宽到 6s
    （用户显式指定更大的 --max-time 时以用户为准）。"""
    if rough_mbps is None or rough_mbps <= 0:
        return 10, base_max_time
    if rough_mbps < 20:
        mb = 10
    elif rough_mbps < 100:
        mb = 30
    elif rough_mbps < 300:
        mb = 60
    else:
        mb = 95
    est = mb * 8 / rough_mbps  # 按粗速度预计的下载秒数
    return mb, max(base_max_time, min(6.0, est * 1.25))


def multi_stream_speed(proxy_url: str, byte_count: int, max_time: float,
                       connect_timeout: float, streams: int = 4) -> Optional[float]:
    """同一节点 streams 路并发 curl，合计带宽 Mbps（峰值参考）；全部失败返回 None。"""
    def one(i: int) -> Optional[float]:
        url = (DEFAULT_DOWNLOAD_URL.format(bytes=byte_count)
               + f"&measId=multi-{int(time.time()*1000)}-{i}")
        mbps, _, _, _ = curl_speed(proxy_url, url, max_time, connect_timeout)
        return mbps

    with ThreadPoolExecutor(max_workers=streams) as pool:
        speeds = [s for s in pool.map(one, range(streams)) if s is not None]
    return round(sum(speeds), 3) if speeds else None


def probe_latency(api: MihomoAPI, name: str, timeout_ms: int,
                  count: int = 3) -> Tuple[Optional[int], Optional[float]]:
    """多次延迟采样（首个样本兼作预热，遇失败即停止），
    返回 (中位延迟 ms, 抖动=样本标准差 ms)；完全不通返回 (None, None)。"""
    vals: List[float] = []
    for _ in range(max(1, count)):
        d = api.proxy_delay(name, DEFAULT_DELAY_URL, timeout_ms)
        if d is None:
            break
        vals.append(float(d))
    if not vals:
        return None, None
    jitter = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return int(round(statistics.median(vals))), round(jitter, 1)


def fetch_ip_info(proxy_url: str, timeout: float) -> Optional[dict]:
    """Query ip-api.com through the given proxy; returns parsed dict or None."""
    cmd = [
        "curl",
        "--proxy", proxy_url,
        "--silent", "--show-error",
        "--connect-timeout", str(min(4.0, timeout)),
        "--max-time", str(timeout),
        DEFAULT_IP_API_URL,
    ]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    if data.get("status") != "success":
        return None
    return data


def classify_ip(data: dict) -> IpInfo:
    proxy = bool(data.get("proxy"))
    hosting = bool(data.get("hosting"))
    mobile = bool(data.get("mobile"))
    isp = str(data.get("isp", ""))
    org = str(data.get("org", ""))
    if proxy:
        kind = "代理/VPN"
    elif hosting:
        kind = "机房托管"
    elif mobile:
        kind = "移动网络"
    elif isp or org:
        # 三个标记全 false 只表示"未被识别为代理/机房/移动"，
        # 可能是家庭宽带也可能是企业/校园网等，不能断言住宅，故记中性的 ISP/非托管
        kind = "ISP/非托管"
    else:
        kind = "未知"
    return IpInfo(
        exit_ip=str(data.get("query", "")),
        country=str(data.get("country", "")),
        country_code=str(data.get("countryCode", "")),
        region=str(data.get("regionName", "")),
        city=str(data.get("city", "")),
        isp=isp,
        org=org,
        asn=str(data.get("as", "")),
        asname=str(data.get("asname", "")),
        kind=kind,
        proxy=proxy,
        hosting=hosting,
        mobile=mobile,
        ok=True,
    )


def latency_score(lat: Optional[int]) -> float:
    if lat is None:
        return 0.0
    if lat <= 80:
        return 100.0
    if lat >= 800:
        return 0.0
    return (800 - lat) / 720 * 100


def bandwidth_score(mbps: Optional[float]) -> float:
    if not mbps or mbps <= 0:
        return 0.0
    # 100 Mbps 视为满分（多数机场节点的实际天花板）
    return min(mbps, 100.0) / 100 * 100


def ip_flag_score(ip: Optional[IpInfo]) -> float:
    """IP 画像分项（启发式扣分，不是真实风险库评分）：
    proxy 标记→30、hosting 标记→55、mobile 标记→80、无标记/查询失败/未知→100。
    仅反映"代理/机房出口通常更可能被风控"的经验倾向，不代表该 IP 的真实风险等级。"""
    if not ip or not ip.ok:
        return 100.0
    if ip.proxy:
        return 30.0
    if ip.hosting:
        return 55.0
    if ip.mobile:
        return 80.0
    return 100.0


def compute_score(r: Result) -> float:
    """综合评分 0-100：带宽 55% + 延迟 25% + IP 标记 20%。不通的节点为 0。"""
    bw = bandwidth_score(r.median_mbps)
    if bw == 0.0:
        return 0.0
    lat = latency_score(r.latency_ms)
    return round(0.55 * bw + 0.25 * lat + 0.20 * ip_flag_score(r.ip), 1)


def star_str(score: float) -> str:
    if score <= 0:
        return "☆☆☆☆☆"
    n = max(1, min(5, round(score / 20)))
    return "★" * n + "☆" * (5 - n)


def make_tags(r: Result) -> str:
    tags = []
    if r.median_mbps is None:
        tags.append("不通")
    else:
        if r.median_mbps < 5:
            tags.append("龟速")
        if r.median_mbps >= 50:
            tags.append("高带宽")
        if r.latency_ms is not None and r.latency_ms <= 100:
            tags.append("低延迟")
        if r.latency_ms is not None and r.latency_ms > 300:
            tags.append("高延迟")
    if r.ip and r.ip.ok:
        if r.ip.kind == "ISP/非托管":
            tags.append("ISP/非托管")
        elif r.ip.kind == "机房托管":
            tags.append("机房托管")
        elif r.ip.kind == "代理/VPN":
            tags.append("脏IP")
    return ",".join(tags)


def fmt_ms(v: Optional[int]) -> str:
    return "-" if v is None else str(v)


def fmt_speed(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.1f}"


def ip_brief(ip: IpInfo) -> str:
    """终端 IP 画像简写：国家代码·类型·AS 名（无 AS 名时退化为 ISP 简写）。"""
    cc = ip.country_code or ip.country or "?"
    net = ip.asname or ip.isp[:16]
    return f"{cc}·{ip.kind}·{net}" if net else f"{cc}·{ip.kind}"


def disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def pad_disp(s: str, width: int) -> str:
    return s + " " * max(0, width - disp_width(s))


def clip_disp(s: str, width: int) -> str:
    if disp_width(s) <= width:
        return s
    out, w = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def rank_results(results: List[Result]) -> List[Result]:
    return sorted(
        results,
        key=lambda r: (-r.score, r.latency_ms if r.latency_ms is not None else 999999),
    )


def print_speedbench(results: List[Result], top: int) -> None:
    ranked = rank_results(results)
    shown = ranked[:top] if top > 0 else ranked

    headers = ["节点", "延迟", "抖动", "建连", "带宽", "评分", "IP画像", "标签"]

    def row_of(r: Result) -> List[str]:
        ip_desc = ip_brief(r.ip) if r.ip and r.ip.ok else "-"
        if r.median_mbps is None:
            bw = "-"
        elif r.multi_mbps:
            bw = f"{r.median_mbps:.1f} / {r.multi_mbps:.0f}Mbps"
        else:
            bw = f"{r.median_mbps:.1f}Mbps"
        return [
            r.name,
            "-" if r.latency_ms is None else f"{r.latency_ms}ms",
            "-" if r.jitter_ms is None else f"{r.jitter_ms:.1f}ms",
            "-" if r.connect_ms is None else f"{r.connect_ms:.0f}ms",
            bw,
            star_str(r.score),
            ip_desc,
            r.tags or "-",
        ]

    rows = [row_of(r) for r in shown]
    n = len(headers)
    widths = [disp_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths[i], disp_width(cell)), 38)
    inner = sum(widths) + 3 * n - 1

    def hline(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def line(cells: List[str]) -> str:
        return "│" + "│".join(
            " " + pad_disp(clip_disp(cells[i], widths[i]), widths[i]) + " " for i in range(n)
        ) + "│"

    title = " Clash SpeedBench "
    print()
    print("┌" + title + "─" * max(0, inner - disp_width(title)) + "┐")
    print(line(headers))
    print(hline("├", "┼", "┤"))
    for row in rows:
        print(line(row))
    print(hline("└", "┴", "┘"))
    print("  延迟/抖动/建连单位 ms │ 带宽 = 单流 Mbps（/ 后为 --multi 4 路合计峰值） │ 评分 = 带宽55% + 延迟25% + IP标记20%（启发式扣分，非风险评分）")


def write_csv(results: List[Result], path: Path) -> None:
    ranked = rank_results(results)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["rank", "name", "provider", "protocol", "latency_ms", "jitter_ms",
                    "connect_ms", "median_mbps", "multi_mbps", "best_mbps", "sample_mb",
                    "all_samples_mbps", "score", "stars", "tags",
                    "exit_ip", "country", "asn", "asname", "isp", "org",
                    "ip_kind", "ip_flags", "status"])
        for rank, r in enumerate(ranked, 1):
            ip = r.ip if r.ip and r.ip.ok else IpInfo()
            ip_flags = "|".join(f for f, on in (("proxy", ip.proxy),
                                                ("hosting", ip.hosting),
                                                ("mobile", ip.mobile)) if on)
            w.writerow([
                rank,
                r.name,
                r.provider,
                r.proto,
                "" if r.latency_ms is None else r.latency_ms,
                "" if r.jitter_ms is None else f"{r.jitter_ms:.1f}",
                "" if r.connect_ms is None else f"{r.connect_ms:.1f}",
                "" if r.median_mbps is None else f"{r.median_mbps:.3f}",
                "" if r.multi_mbps is None else f"{r.multi_mbps:.3f}",
                "" if r.best_mbps is None else f"{r.best_mbps:.3f}",
                "" if r.sample_mb is None else r.sample_mb,
                "|".join(f"{x:.3f}" for x in r.speeds_mbps),
                f"{r.score:.1f}",
                star_str(r.score),
                r.tags,
                ip.exit_ip, ip.country, ip.asn, ip.asname, ip.isp, ip.org,
                ip.kind, ip_flags,
                r.status,
            ])


def result_to_dict(r: Result) -> dict:
    ip = r.ip if r.ip and r.ip.ok else IpInfo()
    return {
        "name": r.name,
        "provider": r.provider,
        "proto": r.proto,
        "latency_ms": r.latency_ms,
        "jitter_ms": r.jitter_ms,
        "connect_ms": r.connect_ms,
        "median_mbps": None if r.median_mbps is None else round(r.median_mbps, 3),
        "multi_mbps": None if r.multi_mbps is None else round(r.multi_mbps, 3),
        "best_mbps": None if r.best_mbps is None else round(r.best_mbps, 3),
        "sample_mb": r.sample_mb,
        "samples_mbps": [round(x, 3) for x in r.speeds_mbps],
        "score": r.score,
        "stars": star_str(r.score),
        "tags": r.tags,
        "status": r.status,
        "ip": {
            "exit_ip": ip.exit_ip, "country": ip.country, "country_code": ip.country_code,
            "region": ip.region, "city": ip.city, "isp": ip.isp, "org": ip.org,
            "asn": ip.asn, "asname": ip.asname, "kind": ip.kind,
            "proxy": ip.proxy, "hosting": ip.hosting, "mobile": ip.mobile,
            "ok": ip.ok,
        },
    }


def append_history(results: List[Result], path: Path, mb: Optional[int], rounds: int,
                   csv_path: Optional[Path]) -> None:
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "mb": mb,
        "rounds": rounds,
        "csv": str(csv_path) if csv_path else "",
        "results": [result_to_dict(r) for r in rank_results(results)],
    }
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"⚠️ 历史记录写入失败: {e}", file=sys.stderr)


def pick_switch_group(proxies: Dict[str, dict], graph: Dict[str, List[str]],
                      best_name: str, root: str) -> Optional[str]:
    """Find the Selector group to auto-switch: prefer the non-root group with the
    most members that contains the champion node (usually the main selector)."""
    cands = [g for g, children in graph.items()
             if g != root and best_name in children
             and proxies.get(g, {}).get("type") == "Selector"]
    if not cands:
        return None
    return max(cands, key=lambda g: len(graph[g]))


def auto_switch_best(api: MihomoAPI, proxies: Dict[str, dict],
                     graph: Dict[str, List[str]], root: str,
                     results: List[Result], group_override: str = "") -> None:
    ranked = rank_results(results)
    best = ranked[0] if ranked else None
    if not best or best.score <= 0:
        print("自动切换：没有测出有效节点，保持当前选择。")
        return
    group = group_override or pick_switch_group(proxies, graph, best.name, root)
    if not group:
        print(f"自动切换：找不到包含 {best.name!r} 的 Selector 组，可用 --switch-group 指定。")
        return
    try:
        current = proxies.get(group, {}).get("now")
        if current == best.name:
            print(f"自动切换：{group} 已是 {best.name}，无需变更。")
            return
        api.select(group, best.name)
        print(f"✅ 自动切换：{group} → {best.name}"
              f"（{fmt_speed(best.median_mbps)} Mbps / {fmt_ms(best.latency_ms)} ms / {star_str(best.score)}）")
    except Exception as e:
        print(f"⚠️ 自动切换失败: {e}", file=sys.stderr)


def report(results: List[Result], args, api: MihomoAPI, proxies: Dict[str, dict]) -> int:
    """Shared reporting: box table + CSV + history + optional auto-switch."""
    if not results:
        print("没有产生有效测速结果。")
        return 0
    summary = getattr(args, "mode_summary", "")
    if summary:
        print(f"\n本次模式: {summary}")
    print_speedbench(results, args.top)
    out = Path(args.output) if args.output else Path(
        f"clash-speedtest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    )
    write_csv(results, out)
    print(f"\nCSV 已保存: {out.resolve()}")
    print("排序规则：按综合评分（带宽 55% + 延迟 25% + IP 标记 20%）从高到低。")
    if any("未精测" in (r.tags or "") for r in results):
        print("注：「未精测」节点仅完成 Phase 1 粗筛（延迟/连通性/IP 画像），未参与带宽精测。")
    if not args.no_history:
        append_history(results, Path(args.history), args.mb, args.rounds, out)
    if args.auto_switch:
        graph = build_selectable_graph(proxies)
        auto_switch_best(api, proxies, graph, args.root_group, results, args.switch_group)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clash SpeedBench — Clash Verge Rev / Mihomo 节点综合测速（延迟 + 真实带宽 + IP 画像）"
    )
    parser.add_argument("--controller",
                        help="External Controller，例如 http://127.0.0.1:9097 或 unix:///tmp/verge/verge-mihomo.sock")
    parser.add_argument("--secret", default=os.environ.get("MIHOMO_SECRET", ""),
                        help="API secret；建议用环境变量 MIHOMO_SECRET，避免写进 shell history")
    parser.add_argument("--include", help="只测试名称匹配此正则的节点，例如 '香港|HK'")
    parser.add_argument("--exclude", default=r"(?i)(剩余|流量|到期|官网|套餐|公告|倍率|traffic|expire)",
                        help="排除名称匹配此正则的节点")
    parser.add_argument("--provider", help="只测试指定 provider-name（精确匹配）")
    parser.add_argument("--mb", type=int, default=None,
                        help="单轮请求数据量 MB（1~95）；不指定时先 ~1MB 预热估速，"
                             "再自适应 10/30/60/95MB（目标单样本 2-4 秒）")
    parser.add_argument("--rounds", type=int, default=1, help="每节点测速轮数，默认 1")
    parser.add_argument("--max-time", type=float, default=4.0,
                        help="每轮下载最长秒数，默认 4（自适应模式下最多放宽到 6s）")
    parser.add_argument("--settle", type=float, default=0.35,
                        help="切换节点后等待秒数，默认 0.35")
    parser.add_argument("--delay-timeout", type=int, default=5000,
                        help="延迟测试超时毫秒，默认 5000")
    parser.add_argument("--no-ip", action="store_true",
                        help="跳过出口 IP 画像查询（只测延迟和带宽）")
    parser.add_argument("--ip-timeout", type=float, default=8.0,
                        help="IP 画像查询超时秒数，默认 8")
    parser.add_argument("--top", type=int, default=0,
                        help="终端只显示前 N 名；0=全部")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多测速 N 个节点；0=全部（便于先小规模试跑）")
    parser.add_argument("--output", help="CSV 输出路径")
    parser.add_argument("--root-group", default="GLOBAL",
                        help="全局模式根策略组，默认 GLOBAL")
    parser.add_argument("--auto-switch", action="store_true",
                        help="测速结束后把主 Selector 组自动切换到综合评分第一的节点")
    parser.add_argument("--switch-group", default="",
                        help="配合 --auto-switch 使用，手动指定要切换的策略组名")
    parser.add_argument("--history",
                        default=str(Path(__file__).resolve().parent / "speedbench-history.jsonl"),
                        help="历史记录 JSONL 路径，默认在脚本目录下")
    parser.add_argument("--no-history", action="store_true",
                        help="不写入历史记录")
    parser.add_argument("--workers", type=int, default=6,
                        help="并发 worker 数（起多个临时 mihomo 实例并行做出口 IP 画像，不影响运行中的 Clash）；"
                             "1=关闭并发，回退到串行 GLOBAL 切换模式。默认 6")
    parser.add_argument("--top-n", type=int, default=15,
                        help="两阶段模式：Phase 2 只对延迟最优的前 N 个连通节点串行精测带宽，默认 15")
    parser.add_argument("--all", action="store_true",
                        help="两阶段模式：跳过 Top-N 筛选，Phase 2 对所有连通节点串行精测")
    parser.add_argument("--multi", action="store_true",
                        help="精测阶段对每节点追加 4 路并发流测峰值带宽（记入 multi_mbps，多耗 4 倍样本流量）")
    parser.add_argument("--config-file", default="",
                        help="并发模式用的完整配置文件路径（含节点凭据），默认自动找 Clash Verge 的运行配置")
    parser.add_argument("--yes", action="store_true",
                        help="不询问确认直接开始")
    args = parser.parse_args()

    if args.mb is not None and (args.mb < 1 or args.mb > 95):
        print("错误：--mb 建议范围 1~95。", file=sys.stderr)
        return 2
    if args.rounds < 1 or args.rounds > 5:
        print("错误：--rounds 建议范围 1~5。", file=sys.stderr)
        return 2
    if args.top_n < 1:
        print("错误：--top-n 至少为 1。", file=sys.stderr)
        return 2

    try:
        base, needs_secret = detect_controller(args.secret, args.controller)
        secret = get_secret_if_needed(base, args.secret, needs_secret)
        api = MihomoAPI(base, secret=secret)
        version = api.get("/version")
        config = api.get("/configs")
        proxy_data = api.get("/proxies")
    except (ApiError, Unauthorized) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    proxies: Dict[str, dict] = proxy_data.get("proxies", {})
    leaves = leaf_nodes(proxies)

    # Filters
    include_re = re.compile(args.include) if args.include else None
    exclude_re = re.compile(args.exclude) if args.exclude else None

    candidates = []
    for name, info in leaves.items():
        if include_re and not include_re.search(name):
            continue
        if exclude_re and exclude_re.search(name):
            continue
        if args.provider and info.get("provider-name", "") != args.provider:
            continue
        candidates.append(name)

    candidates.sort()
    if args.limit > 0:
        candidates = candidates[:args.limit]

    if not candidates:
        print("没有找到符合筛选条件的实际代理节点。", file=sys.stderr)
        return 1

    if args.workers > 1:
        try:
            from speedbench_workers import WorkerUnavailable, run_pool
        except ImportError:
            print("错误：缺少 speedbench_workers.py（应与 clash_speedbench.py 同目录）。",
                  file=sys.stderr)
            return 1
        proto_by_name = {n: str(info.get("type", "")) for n, info in leaves.items()}
        if args.mb:
            sample_desc = f"{args.mb} MB"
            max_mb = args.mb
        else:
            sample_desc = "自适应 10~95 MB（~1MB 预热估速）"
            max_mb = 95
        n_phase2 = len(candidates) if args.all else min(args.top_n, len(candidates))
        est_mb = max_mb * args.rounds * n_phase2 * (5 if args.multi else 1)
        print("Clash SpeedBench（两阶段并发模式，不影响正在运行的 Clash）")
        print(f"候选节点: {len(candidates)}")
        print(f"流程: Phase 1 粗筛（延迟经主实例 /delay 并发探测 + worker 出口 IP 画像，不跑带宽） → "
              f"Phase 2 {'全部' if args.all else f'Top {n_phase2}'} 串行精测带宽")
        print(f"测速参数: {sample_desc} × {args.rounds} 轮/节点，单轮最长 {args.max_time:g}s"
              + ("，追加 4 路并发峰值" if args.multi else "")
              + ("，含出口 IP 画像" if not args.no_ip else "，已跳过 IP 画像"))
        print(f"理论最大流量消耗约: {est_mb / 1024:.2f} GiB（仅 Phase 2 精测节点消耗带宽）")
        if not args.yes:
            ans = input("\n开始测速？[Y/n] ").strip().lower()
            if ans not in ("", "y", "yes"):
                print("已取消。")
                return 0
        try:
            results = run_pool(candidates, proto_by_name, args, main_api=api)
        except WorkerUnavailable as e:
            print(f"并发模式不可用：{e}\n回退到串行模式。", file=sys.stderr)
        else:
            return report(results, args, api, proxies)

    mixed_port = config.get("mixed-port") or config.get("port") or 7897
    proxy_url = f"http://127.0.0.1:{mixed_port}"
    root = args.root_group

    graph = build_selectable_graph(proxies)
    if root not in graph:
        print(f"错误：找不到可选择的根策略组 {root!r}。", file=sys.stderr)
        print("可用 Selector/URLTest/Fallback 组：")
        for g in sorted(graph):
            print("  -", g)
        print("\n如果你的全局组名称不是 GLOBAL，请用 --root-group '你的组名'。")
        return 1

    paths: Dict[str, List[str]] = {}
    skipped_no_path = []
    for name in candidates:
        p = find_path(graph, root, name)
        if p:
            paths[name] = p
        else:
            skipped_no_path.append(name)

    candidates = [n for n in candidates if n in paths]
    if not candidates:
        print(f"错误：{root!r} 无法通过可选择策略组到达任何候选节点。", file=sys.stderr)
        print("你可以尝试用 --root-group 指定一个包含所有节点的手动选择组。")
        return 1

    original_mode = str(config.get("mode", "rule")).lower()
    if args.mb:
        sample_desc = f"{args.mb} MB"
        max_mb = args.mb
    else:
        sample_desc = "自适应 10~95 MB（~1MB 预热估速）"
        max_mb = 95  # 自适应上限，用于流量估算
    data_per_node = max_mb * args.rounds * (5 if args.multi else 1)
    total_max_mb = data_per_node * len(candidates)
    args.mode_summary = (f"串行模式：单实例逐节点全测（{sample_desc} ×{args.rounds} 轮"
                         + (" + 4 路并发峰值" if args.multi else "") + "）")

    print("Clash SpeedBench")
    print(f"Mihomo: {version.get('version', version)}")
    print(f"Controller: {base}")
    print(f"Mixed port: {mixed_port}")
    print(f"Root group: {root}")
    print(f"候选节点: {len(candidates)}")
    if skipped_no_path:
        print(f"无法从 {root} 到达、将跳过: {len(skipped_no_path)} 个")
    print(f"测速参数: {sample_desc} × {args.rounds} 轮/节点，单轮最长 {args.max_time:g}s"
          + ("，追加 4 路并发峰值" if args.multi else "")
          + ("，含出口 IP 画像" if not args.no_ip else "，已跳过 IP 画像"))
    print(f"理论最大流量消耗约: {total_max_mb / 1024:.2f} GiB")
    print("注意：测速期间 Mihomo 会临时切换到 GLOBAL 模式；脚本结束或 Ctrl+C 后会尝试自动恢复。")

    if not args.yes:
        ans = input("\n开始测速？[Y/n] ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("已取消。")
            return 0

    results: List[Result] = []
    saved_groups: Dict[str, Tuple[str, Optional[str]]] = {}
    mode_changed = False

    try:
        # Force global to guarantee curl's traffic uses the tested path.
        if original_mode != "global":
            api.patch("/configs", {"mode": "global"})
            mode_changed = True

        for idx, name in enumerate(candidates, 1):
            # refresh metadata occasionally is unnecessary; original snapshot is enough for graph.
            path = paths[name]
            info = leaves[name]
            try:
                apply_path(api, path, proxies, saved_groups)
                time.sleep(args.settle)
            except Exception as e:
                print(f"[{idx}/{len(candidates)}] {name}: 切换失败: {e}")
                res = Result(
                    name=name,
                    provider=str(info.get("provider-name", "")),
                    proto=str(info.get("type", "")),
                    latency_ms=None,
                    speeds_mbps=[],
                    median_mbps=None,
                    best_mbps=None,
                    status="switch-failed",
                )
                res.tags = make_tags(res)
                results.append(res)
                continue

            latency, jitter = probe_latency(api, name, args.delay_timeout)

            # 带宽采样：--mb 未显式指定时先 ~1MB 预热估速，再自适应样本大小
            mb = args.mb
            max_time = args.max_time
            if mb is None:
                rough = warmup_speed(proxy_url, min(3.0, args.max_time))
                mb, max_time = adaptive_sample(rough, args.max_time)

            speeds = []
            statuses = []
            connect_ms = None
            for round_i in range(args.rounds):
                # Add cache-busting measId even though Cloudflare's __down is dynamic.
                byte_count = mb * 1_000_000
                url = DEFAULT_DOWNLOAD_URL.format(bytes=byte_count) + f"&measId={int(time.time()*1000)}-{idx}-{round_i}"
                speed, st, c_ms, _sz = curl_speed(
                    proxy_url=proxy_url,
                    download_url=url,
                    max_time=max_time,
                    connect_timeout=min(3.0, max_time),
                )
                statuses.append(st)
                if connect_ms is None and c_ms is not None:
                    connect_ms = c_ms
                if speed is not None:
                    speeds.append(speed)

            multi = None
            if args.multi:
                multi = multi_stream_speed(proxy_url, mb * 1_000_000, max_time,
                                           min(3.0, max_time))

            median = statistics.median(speeds) if speeds else None
            best = max(speeds) if speeds else None
            status = "ok" if speeds else ";".join(statuses)[:160]

            # Exit-IP profile through the same node (even when download failed,
            # an IP profile still tells whether the node is alive at all).
            ip = None
            if not args.no_ip:
                data = fetch_ip_info(proxy_url, args.ip_timeout)
                if data:
                    ip = classify_ip(data)

            res = Result(
                name=name,
                provider=str(info.get("provider-name", "")),
                proto=str(info.get("type", "")),
                latency_ms=latency,
                speeds_mbps=speeds,
                median_mbps=median,
                best_mbps=best,
                status=status,
                ip=ip,
                jitter_ms=jitter,
                connect_ms=connect_ms,
                multi_mbps=multi,
                sample_mb=mb,
            )
            res.score = compute_score(res)
            res.tags = make_tags(res)
            results.append(res)

            ip_txt = f" | {ip_brief(ip)}" if ip and ip.ok else ""
            multi_brief = f" / {multi:.0f}" if multi else ""
            print(
                f"[{idx:>3}/{len(candidates)}] "
                f"{name} | {fmt_ms(latency)} ms | "
                f"{fmt_speed(median)}{multi_brief} Mbps（{mb}MB）{ip_txt}"
            )

    except KeyboardInterrupt:
        print("\n\n收到 Ctrl+C，停止测速并恢复原配置……")
    finally:
        restore_groups(api, saved_groups)
        if mode_changed:
            try:
                api.patch("/configs", {"mode": original_mode})
            except Exception as e:
                print(f"⚠️ 恢复原模式失败，请手动切回 {original_mode}: {e}", file=sys.stderr)

    return report(results, args, api, proxies)


if __name__ == "__main__":
    raise SystemExit(main())
