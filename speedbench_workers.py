#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash SpeedBench — concurrent worker pool (two-phase).

Phase 1 粗筛：spawns several throwaway mihomo processes (reusing the binary that
ships with Clash Verge) with a minimal generated config, probing nodes in
parallel — latency/jitter, connectivity and exit-IP profile only, NO bandwidth
download, so the user's running Clash instance is never touched and the WAN
link stays idle.

Phase 2 精测：one extra throwaway mihomo worker measures the Top-N nodes' real
download speed strictly one node at a time, so parallel downloads never fight
over the same WAN bandwidth.

Key tricks (validated against Clash Verge Rev + TUN on macOS):
- The Verge-generated clash-verge.yaml holds full proxy credentials; we extract
  the `proxies` list via macOS's built-in ruby (YAML -> JSON), no pip needed.
- Worker configs are written as JSON (valid YAML) with three test domains pinned
  in `hosts` — the main instance's TUN DNS hijack would otherwise return fake-ips.
- Selected nodes are expanded with their `dialer-proxy` dependency closure, so
  relay nodes whose entry node lives outside the shard stay dialable.
- Worker configs set a global top-level `interface-name: <physical if>` so worker
  dials bypass the main TUN; a node's own `interface-name` still takes
  precedence (mihomo only falls back to the global default when the per-proxy
  option is unset — see component/dialer), so deliberate per-node settings are
  never overridden.

Anything missing (ruby / binary / config / DoH) -> WorkerUnavailable, and the
caller falls back to the sequential in-place mode. A default route that already
lives on a virtual interface (global TUN / other VPN) raises VirtualDefaultRoute
(a WorkerUnavailable subclass) and takes the same fallback.
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
    DEFAULT_DOWNLOAD_URL,
    IpInfo,
    MihomoAPI,
    Result,
    adaptive_sample,
    classify_ip,
    compute_score,
    curl_speed,
    fetch_ip_info,
    fmt_ms,
    fmt_speed,
    ip_brief,
    make_tags,
    multi_stream_speed,
    probe_latency,
    warmup_speed,
)


class WorkerUnavailable(RuntimeError):
    pass


class VirtualDefaultRoute(WorkerUnavailable):
    """默认路由落在虚拟隧道接口（全局 TUN/其他 VPN）时抛出：worker 模式拒绝启动。
    继承 WorkerUnavailable，main() 走同一条「并发不可用 -> 回退串行」路径。"""


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


def with_dependencies(selected: List[dict], all_proxies: List[dict]) -> List[dict]:
    """把入选节点的 dialer-proxy 链式依赖闭包并进来。
    mihomo 的链式代理（dialer-proxy: 前置节点名）要求前置节点出现在同一份
    配置的 proxies 里，否则该节点无法拨号（主 Clash 能用、这里误报不通）。
    返回新列表：入选节点保持原顺序在前，依赖节点按发现顺序追加在后；
    visited 集合负责去重并防御循环依赖（A -> B -> A）。不改动入参。"""
    by_name = {p.get("name"): p for p in all_proxies
               if isinstance(p, dict) and p.get("name")}
    merged: List[dict] = []
    visited: set = set()
    # 第一遍先收齐入选节点（保持原顺序，去重）
    for p in selected:
        if isinstance(p, dict) and p.get("name") not in visited:
            visited.add(p.get("name"))
            merged.append(p)
    # 第二遍沿 merged 逐节点补 dialer-proxy 链；新追加的依赖也会被继续解析，
    # 循环依赖（A -> B -> A）在 visited 处停下
    i = 0
    while i < len(merged):
        dep = by_name.get(merged[i].get("dialer-proxy") or "")
        if dep is not None and dep.get("name") not in visited:
            visited.add(dep.get("name"))
            merged.append(dep)
        i += 1
    return merged


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


# 虚拟隧道接口前缀：默认路由落在这些接口上，说明全局 TUN/其他 VPN
# 已接管系统流量，worker 绑它拨号等于在别人的隧道里测速，结果无意义
VIRTUAL_IFACE_PREFIXES = ("utun", "ipsec", "ppp", "tun", "tap")


def is_virtual_iface(name: Optional[str]) -> bool:
    """接口名是否为虚拟隧道接口（utun*/ipsec*/ppp*/tun*/tap*）。"""
    return bool(name) and name.startswith(VIRTUAL_IFACE_PREFIXES)


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
        # 复制节点 dict，避免改动调用方共享的节点定义
        proxies = [dict(p) for p in self.proxies]
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
        if self.iface:
            # 绑接口用配置顶层的全局 interface-name，而不是逐节点改写：
            # mihomo 拨号时节点自身的 interface-name 优先，全局值仅作缺省
            # （见 component/dialer/dialer.go），因此节点原本故意设置的接口
            # 绝不覆盖，未设置的节点才绑物理网卡绕过主实例 TUN
            cfg["interface-name"] = self.iface
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

    def select(self, name: str) -> None:
        assert self.api is not None
        self.api.select("GLOBAL", name)


def _probe_node_in_worker(worker: Worker, name: str, proto: str, args) -> Result:
    """Phase 1 粗筛：只做延迟/抖动 + 连通性判定 + 出口 IP 画像，不跑带宽下载。"""
    assert worker.api is not None
    latency, jitter = probe_latency(worker.api, name, args.delay_timeout)
    if latency is None:
        return Result(name=name, provider="", proto=proto, latency_ms=None,
                      speeds_mbps=[], median_mbps=None, best_mbps=None,
                      status="unreachable")

    ip: Optional[IpInfo] = None
    if not args.no_ip:
        try:
            worker.select(name)
            time.sleep(args.settle)
            data = fetch_ip_info(worker.proxy_url, args.ip_timeout)
            if data:
                ip = classify_ip(data)
        except Exception:
            pass  # IP 画像失败不影响粗筛结论
    return Result(name=name, provider="", proto=proto, latency_ms=latency,
                  speeds_mbps=[], median_mbps=None, best_mbps=None,
                  status="ok", ip=ip, jitter_ms=jitter)


def _speed_node_in_worker(worker: Worker, r: Result, args) -> None:
    """Phase 2 精测：对单个节点测真实带宽（严格串行调用），结果合并进 Phase 1 的 r。"""
    try:
        worker.select(r.name)
        time.sleep(args.settle)
    except Exception as e:
        r.status = f"switch-failed: {e}"[:160]
        return

    # --mb 未显式指定时先 ~1MB 预热估速，再自适应样本大小
    mb = args.mb
    max_time = args.max_time
    if mb is None:
        rough = warmup_speed(worker.proxy_url, min(3.0, args.max_time))
        mb, max_time = adaptive_sample(rough, args.max_time)
    r.sample_mb = mb

    speeds: List[float] = []
    statuses: List[str] = []
    for round_i in range(args.rounds):
        byte_count = mb * 1_000_000
        url = (DEFAULT_DOWNLOAD_URL.format(bytes=byte_count)
               + f"&measId={int(time.time()*1000)}-p2-{round_i}")
        speed, status, c_ms, _sz = curl_speed(
            proxy_url=worker.proxy_url,
            download_url=url,
            max_time=max_time,
            connect_timeout=min(3.0, max_time),
        )
        statuses.append(status)
        if r.connect_ms is None and c_ms is not None:
            r.connect_ms = c_ms
        if speed is not None:
            speeds.append(speed)

    if getattr(args, "multi", False):
        r.multi_mbps = multi_stream_speed(worker.proxy_url, mb * 1_000_000,
                                          max_time, min(3.0, max_time))

    r.speeds_mbps = speeds
    r.median_mbps = statistics.median(speeds) if speeds else None
    r.best_mbps = max(speeds) if speeds else None
    r.status = "ok" if speeds else ";".join(statuses)[:160]


def select_phase2_nodes(results: List[Result], top_n: int,
                        measure_all: bool = False) -> List[Result]:
    """Phase 2 选节点：剔除不通节点后按延迟升序，取 Top N（measure_all 时取全部连通节点）。"""
    reachable = sorted((r for r in results if r.latency_ms is not None),
                       key=lambda r: r.latency_ms or 0)
    top_n = max(1, top_n)
    return reachable if measure_all else reachable[:top_n]


def relabel_unmeasured(r: Result) -> None:
    """连通但未进 Phase 2 精测的节点：把「不通」标签改标为「未精测」。"""
    if r.latency_ms is not None and "不通" in r.tags.split(","):
        r.tags = ",".join("未精测" if t == "不通" else t
                          for t in r.tags.split(","))


def run_pool(candidates: List[str], proto_by_name: Dict[str, str], args) -> List[Result]:
    """两阶段测速，从不触碰用户正在运行的实例：
    Phase 1 用并发 worker 池粗筛（延迟/抖动/连通性/IP 画像，不跑带宽），
    Phase 2 新建单个 worker 对 Top-N 连通节点严格串行精测真实带宽，
    保证同一时刻全网只有一路测速下载。Raises WorkerUnavailable 触发回退。"""
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
    if iface and is_virtual_iface(iface):
        # 默认路由已被全局 TUN/其他 VPN 接管：绑虚拟接口拨号的测速无意义。
        # 抛专用异常（WorkerUnavailable 子类），main() 按既有机制自动回退串行模式
        raise VirtualDefaultRoute(
            f"默认路由接口 {iface} 是虚拟隧道接口（全局 TUN/其他 VPN 接管了默认路由），"
            f"worker 模式测速无意义")
    if not iface:
        print("⚠️ 无法确定物理网卡，worker 拨号可能被主实例 TUN 截获（结果可能不准）")
    print("正在解析测试域名与节点服务器域名（DoH）…")
    # 依赖闭包内前置节点的 server 域名也要钉住，否则前置节点拨号会拿到 fake-ip
    hosts = build_hosts(with_dependencies(selected, all_proxies))

    worker_count = max(1, min(args.workers, len(selected)))
    print(f"Phase 1 粗筛: 启动 {worker_count} 个并发 worker"
          f"（仅延迟/连通性/IP 画像，不下载，不打扰正在运行的 Clash）…")

    # Round-robin 分片，尽量均匀
    shards: List[List[dict]] = [[] for _ in range(worker_count)]
    for i, p in enumerate(selected):
        shards[i % worker_count].append(p)

    results: List[Result] = []
    results_lock = threading.Lock()
    done_counter = {"n": 0}
    total = len(selected)

    def shard_loop(shard: List[dict]) -> None:
        # worker 配置 = 分片节点 + 各自的 dialer-proxy 依赖闭包；
        # 探测仍只针对分片内的入选节点，依赖节点仅供链式拨号、不计入结果
        worker = Worker(mihomo_bin, with_dependencies(shard, all_proxies), hosts, iface)
        worker.start()
        try:
            for p in shard:
                name = str(p.get("name"))
                proto = str(p.get("type", ""))
                try:
                    r = _probe_node_in_worker(worker, name, proto, args)
                except Exception as e:
                    r = Result(name=name, provider="", proto=proto, latency_ms=None,
                               speeds_mbps=[], median_mbps=None, best_mbps=None,
                               status=f"error: {e}"[:160])
                with results_lock:
                    results.append(r)
                    done_counter["n"] += 1
                    idx = done_counter["n"]
                ip_txt = f" | {ip_brief(r.ip)}" if r.ip and r.ip.ok else ""
                jitter_brief = f"±{r.jitter_ms:.0f}" if r.jitter_ms else ""
                print(f"Phase 1 粗筛 [{idx:>3}/{total}] {name} | "
                      f"{fmt_ms(r.latency_ms)}{jitter_brief} ms{ip_txt}")
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
    print(f"Phase 1 粗筛完成，耗时 {time.time() - started:.1f}s"
          f"（{total} 节点 × {worker_count} workers）")

    # Phase 2 选节点：剔除不通节点后按延迟升序，取 Top N（--all 时取全部连通节点）
    chosen = select_phase2_nodes(results, getattr(args, "top_n", 15),
                                 getattr(args, "all", False))

    if not chosen:
        print("Phase 2 精测: 没有连通节点，跳过带宽精测。")
    else:
        scope = "全部" if getattr(args, "all", False) else f"Top {len(chosen)}"
        print(f"Phase 2 精测: {scope} {len(chosen)} 个节点，单 worker 严格串行"
              f"（同一时刻只有一路测速下载）…")
        chosen_proxies = [by_name[r.name] for r in chosen if r.name in by_name]
        worker2: Optional[Worker] = None
        try:
            # Phase 2 单 worker 同样并入 dialer-proxy 依赖闭包
            worker2 = Worker(mihomo_bin, with_dependencies(chosen_proxies, all_proxies),
                             hosts, iface)
            worker2.start()
        except Exception as e:
            print(f"⚠️ Phase 2 worker 启动失败，保留 Phase 1 粗筛结果: {e}",
                  file=sys.stderr)
            worker2 = None
        if worker2 is not None:
            try:
                for i, r in enumerate(chosen, 1):
                    try:
                        _speed_node_in_worker(worker2, r, args)
                    except Exception as e:
                        r.status = f"error: {e}"[:160]
                    r.score = compute_score(r)
                    r.tags = make_tags(r)
                    ip_txt = f" | {ip_brief(r.ip)}" if r.ip and r.ip.ok else ""
                    multi_brief = f" / {r.multi_mbps:.0f}" if r.multi_mbps else ""
                    mb_brief = f"（{r.sample_mb}MB 样本）" if r.sample_mb else ""
                    print(f"Phase 2 精测 [{i:>3}/{len(chosen)}] {r.name} | "
                          f"{fmt_ms(r.latency_ms)} ms | "
                          f"{fmt_speed(r.median_mbps)}{multi_brief} Mbps"
                          f"{mb_brief}{ip_txt}")
            except KeyboardInterrupt:
                # 保留已完成的精测结果，继续走报告
                print("\n\n收到 Ctrl+C，停止精测（保留已完成结果，正在清理 worker）……")
            finally:
                worker2.stop()

    # 汇总打分/标签：Phase 2 节点已算分；连通但未精测的标「未精测」而非「不通」
    measured = {r.name for r in chosen}
    for r in results:
        if r.name in measured:
            continue
        r.score = compute_score(r)
        r.tags = make_tags(r)
        relabel_unmeasured(r)

    sample_desc = f"{args.mb}MB" if args.mb else "自适应10~95MB"
    if getattr(args, "multi", False):
        sample_desc += " + 4路峰值"
    scope = f"全部 {len(chosen)}" if getattr(args, "all", False) else f"Top {len(chosen)}"
    args.mode_summary = (f"两阶段：Phase1 {total} 节点并发粗筛 → "
                         f"Phase2 {scope} 节点串行精测（{sample_desc} ×{args.rounds} 轮）")
    print(f"两阶段测速完成，总耗时 {time.time() - started:.1f}s")
    return results
