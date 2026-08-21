#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash SpeedBench — concurrent worker pool.

Spawns several throwaway mihomo processes (reusing the binary that ships with
Clash Verge) with a minimal generated config, so bandwidth tests run truly in
parallel and the user's running Clash instance is never touched (no GLOBAL
switching, no traffic hijack).

Key tricks (validated against Clash Verge Rev + TUN on macOS):
- The Verge-generated clash-verge.yaml holds full proxy credentials; we extract
  the `proxies` list via macOS's built-in ruby (YAML -> JSON), no pip needed.
- Worker configs are written as JSON (valid YAML) with three test domains pinned
  in `hosts` — the main instance's TUN DNS hijack would otherwise return fake-ips.
- Every proxy gets `interface-name: <physical if>` so worker dials bypass the
  main TUN (verified: no double-hop through the running instance).

Anything missing (ruby / binary / config / DoH) -> WorkerUnavailable, and the
caller falls back to the sequential in-place mode.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from clash_speedbench import (
    DEFAULT_DELAY_URL,
    DEFAULT_DOWNLOAD_URL,
    IpInfo,
    MihomoAPI,
    Result,
    classify_ip,
    compute_score,
    curl_speed,
    fetch_ip_info,
    fmt_ms,
    fmt_speed,
    make_tags,
)


class WorkerUnavailable(RuntimeError):
    pass


MIHOMO_BIN_CANDIDATES = (
    "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo",
    os.path.expanduser("~/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"),
)

CONFIG_CANDIDATES = (
    os.path.expanduser(
        "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"
    ),
)

TEST_DOMAINS = ("speed.cloudflare.com", "cp.cloudflare.com", "ip-api.com")

# DoH 端点（主机名 + 固定 IP，curl --resolve 钉住，避免被本机 DNS 劫持影响）
# 注意阿里云 JSON API 在 /resolve，Cloudflare 在 /dns-query
DOH_SERVERS = (
    ("dns.alidns.com", "223.5.5.5", "/resolve"),
    ("cloudflare-dns.com", "1.1.1.1", "/dns-query"),
    ("dns.google", "8.8.8.8", "/resolve"),
)


def find_mihomo_bin() -> Optional[str]:
    for p in MIHOMO_BIN_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("mihomo")


def find_config_file() -> Optional[str]:
    for p in CONFIG_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def extract_proxies(config_path: str) -> List[dict]:
    """YAML -> JSON via macOS built-in ruby; returns the proxies list."""
    ruby = shutil.which("ruby") or "/usr/bin/ruby"
    if not os.path.isfile(ruby):
        raise WorkerUnavailable("未找到 ruby（macOS 自带，用于解析 YAML 配置）")
    script = (
        "require 'yaml'; require 'json'; "
        "cfg = YAML.load_file(ARGV[0]); "
        "puts JSON.generate({'proxies' => cfg['proxies'] || []})"
    )
    try:
        p = subprocess.run([ruby, "-e", script, config_path],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise WorkerUnavailable("解析配置文件超时")
    if p.returncode != 0:
        raise WorkerUnavailable(f"解析配置文件失败: {p.stderr.strip()[:200]}")
    try:
        proxies = json.loads(p.stdout)["proxies"]
    except (json.JSONDecodeError, KeyError):
        raise WorkerUnavailable("配置文件中没有可用的 proxies 列表")
    if not proxies:
        raise WorkerUnavailable("配置文件 proxies 为空")
    return proxies


def physical_interface() -> Optional[str]:
    try:
        p = subprocess.run(["route", "get", "default"],
                           capture_output=True, text=True, timeout=5)
        for line in p.stdout.splitlines():
            if "interface:" in line:
                return line.split()[-1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def doh_resolve(domain: str) -> Optional[str]:
    for host, ip, path in DOH_SERVERS:
        try:
            p = subprocess.run(
                ["curl", "-s", "-m", "6",
                 "--resolve", f"{host}:443:{ip}",
                 "-H", "accept: application/dns-json",
                 f"https://{host}{path}?name={domain}&type=A"],
                capture_output=True, text=True, timeout=9)
            if p.returncode == 0 and p.stdout:
                data = json.loads(p.stdout)
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        return ans.get("data")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    return None


def build_hosts(proxies: List[dict]) -> Dict[str, str]:
    """Pin test domains + every node-server domain to real IPs (bypasses the
    fake-ip answers produced by the running instance's TUN DNS hijack)."""
    domains = set(TEST_DOMAINS)
    for p in proxies:
        srv = p.get("server", "")
        if srv and not _is_ip(srv):
            domains.add(srv)
    hosts: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for domain, ip in zip(domains, pool.map(doh_resolve, domains)):
            if ip:
                hosts[domain] = ip
    missing = [d for d in TEST_DOMAINS if d not in hosts]
    if missing:
        raise WorkerUnavailable(f"无法通过 DoH 解析测试域名: {', '.join(missing)}")
    return hosts


def _is_ip(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Worker:
    """One throwaway mihomo process: mixed-port + tiny external controller."""

    def __init__(self, mihomo_bin: str, proxies: List[dict], hosts: Dict[str, str],
                 iface: Optional[str]):
        self.bin = mihomo_bin
        self.proxies = proxies
        self.hosts = hosts
        self.iface = iface
        self.proc: Optional[subprocess.Popen] = None
        self.dir: Optional[tempfile.TemporaryDirectory] = None
        self.api: Optional[MihomoAPI] = None
        self.proxy_url = ""

    def start(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="speedbench-worker-")
        mix_port = _free_port()
        ctl_port = _free_port()
        while ctl_port == mix_port:
            ctl_port = _free_port()
        proxies = []
        for p in self.proxies:
            q = dict(p)
            if self.iface:
                q["interface-name"] = self.iface
            proxies.append(q)
        cfg = {
            "mode": "global",
            "mixed-port": mix_port,
            "external-controller": f"127.0.0.1:{ctl_port}",
            "log-level": "warning",
            "allow-lan": False,
            "ipv6": False,
            "hosts": self.hosts,
            "proxies": proxies,
        }
        cfg_path = Path(self.dir.name) / "config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.proc = subprocess.Popen(
            [self.bin, "-f", str(cfg_path), "-d", self.dir.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.api = MihomoAPI(f"http://127.0.0.1:{ctl_port}", timeout=8.0)
        self.proxy_url = f"http://127.0.0.1:{mix_port}"
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise WorkerUnavailable(f"worker 进程启动后立即退出（端口 {mix_port}）")
            try:
                self.api.get("/version")
                return
            except Exception:
                time.sleep(0.25)
        raise WorkerUnavailable(f"worker 进程启动超时（端口 {mix_port}）")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.dir:
            self.dir.cleanup()

    def delay(self, name: str, timeout_ms: int) -> Optional[int]:
        """Warm-up dial once, then take the second (warm) measurement."""
        assert self.api is not None
        first = self.api.proxy_delay(name, DEFAULT_DELAY_URL, timeout_ms)
        if first is None:
            return None
        second = self.api.proxy_delay(name, DEFAULT_DELAY_URL, timeout_ms)
        return second if second is not None else first

    def select(self, name: str) -> None:
        assert self.api is not None
        self.api.select("GLOBAL", name)


def _test_node_in_worker(worker: Worker, name: str, proto: str, args) -> Result:
    latency = worker.delay(name, args.delay_timeout)
    speeds: List[float] = []
    statuses: List[str] = []
    ip: Optional[IpInfo] = None
    try:
        worker.select(name)
        time.sleep(args.settle)
    except Exception as e:
        return Result(name=name, provider="", proto=proto, latency_ms=latency,
                      speeds_mbps=[], median_mbps=None, best_mbps=None,
                      status=f"switch-failed: {e}")

    for round_i in range(args.rounds):
        byte_count = args.mb * 1_000_000
        url = (DEFAULT_DOWNLOAD_URL.format(bytes=byte_count)
               + f"&measId={int(time.time()*1000)}-w-{round_i}")
        speed, status = curl_speed(
            proxy_url=worker.proxy_url,
            download_url=url,
            max_time=args.max_time,
            connect_timeout=min(3.0, args.max_time),
        )
        statuses.append(status)
        if speed is not None:
            speeds.append(speed)

    if not args.no_ip:
        data = fetch_ip_info(worker.proxy_url, args.ip_timeout)
        if data:
            ip = classify_ip(data)

    median = statistics.median(speeds) if speeds else None
    best = max(speeds) if speeds else None
    status = "ok" if speeds else ";".join(statuses)[:160]
    return Result(name=name, provider="", proto=proto, latency_ms=latency,
                  speeds_mbps=speeds, median_mbps=median, best_mbps=best,
                  status=status, ip=ip)


def run_pool(candidates: List[str], proto_by_name: Dict[str, str], args) -> List[Result]:
    """True parallel benchmark over throwaway mihomo workers. Never touches the
    user's running instance. Raises WorkerUnavailable to trigger fallback."""
    mihomo_bin = find_mihomo_bin()
    if not mihomo_bin:
        raise WorkerUnavailable("未找到 mihomo 二进制（Clash Verge 自带的 verge-mihomo）")
    config_file = getattr(args, "config_file", "") or find_config_file()
    if not config_file:
        raise WorkerUnavailable("未找到 Clash Verge 的运行配置 clash-verge.yaml，"
                                "可用 --config-file 指定")

    print(f"Worker 模式: {mihomo_bin}")
    print(f"配置文件: {config_file}")
    all_proxies = extract_proxies(config_file)
    by_name = {p.get("name"): p for p in all_proxies if isinstance(p, dict)}
    selected = [by_name[n] for n in candidates if n in by_name]
    missing = [n for n in candidates if n not in by_name]
    if missing:
        print(f"⚠️ 配置文件中找不到 {len(missing)} 个节点的定义，跳过: {missing[:3]} ...")
    if not selected:
        raise WorkerUnavailable("配置文件中没有匹配任何候选节点")

    iface = physical_interface()
    if not iface:
        print("⚠️ 无法确定物理网卡，worker 拨号可能被主实例 TUN 截获（结果可能不准）")
    print("正在解析测试域名与节点服务器域名（DoH）…")
    hosts = build_hosts(selected)

    worker_count = max(1, min(args.workers, len(selected)))
    print(f"启动 {worker_count} 个并发 worker（不打扰正在运行的 Clash）…")

    # Round-robin 分片，尽量均匀
    shards: List[List[dict]] = [[] for _ in range(worker_count)]
    for i, p in enumerate(selected):
        shards[i % worker_count].append(p)

    results: List[Result] = []
    results_lock = threading.Lock()
    done_counter = {"n": 0}
    total = len(selected)

    def shard_loop(shard: List[dict]) -> None:
        worker = Worker(mihomo_bin, shard, hosts, iface)
        worker.start()
        try:
            for p in shard:
                name = str(p.get("name"))
                proto = str(p.get("type", ""))
                try:
                    r = _test_node_in_worker(worker, name, proto, args)
                except Exception as e:
                    r = Result(name=name, provider="", proto=proto, latency_ms=None,
                               speeds_mbps=[], median_mbps=None, best_mbps=None,
                               status=f"error: {e}"[:160])
                r.score = compute_score(r)
                r.tags = make_tags(r)
                with results_lock:
                    results.append(r)
                    done_counter["n"] += 1
                    idx = done_counter["n"]
                ip_brief = ""
                if r.ip and r.ip.ok:
                    ip_brief = f" | {r.ip.country_code or r.ip.country}·{r.ip.kind}·风险{r.ip.risk}"
                print(f"[{idx:>3}/{total}] {name} | {fmt_ms(r.latency_ms)} ms | "
                      f"{fmt_speed(r.median_mbps)} Mbps{ip_brief}")
        finally:
            worker.stop()

    def guarded(shard: List[dict]) -> None:
        try:
            shard_loop(shard)
        except Exception as e:
            # 整 shard 失败：给每个节点登记失败结果
            for p in shard:
                with results_lock:
                    results.append(Result(
                        name=str(p.get("name")), provider="",
                        proto=str(p.get("type", "")), latency_ms=None,
                        speeds_mbps=[], median_mbps=None, best_mbps=None,
                        status=f"worker-failed: {e}"[:160]))
                    done_counter["n"] += 1

    started = time.time()
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            list(pool.map(guarded, [s for s in shards if s]))
    except KeyboardInterrupt:
        print("\n\n收到 Ctrl+C，停止测速（worker 均为临时进程，正在清理）……")
        raise
    elapsed = time.time() - started
    print(f"并发测速完成，耗时 {elapsed:.1f}s（{total} 节点 × {worker_count} workers）")
    return results
