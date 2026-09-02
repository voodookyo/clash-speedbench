#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash SpeedBench — Clash Verge Rev / Mihomo 节点综合测速
- Reads the running Mihomo controller API (TCP, Unix socket or Windows named pipe)
- Measures per-node latency with Mihomo's /delay API and repeated
  HTTP/HTTPS application-level probes
- Temporarily switches Mihomo to GLOBAL mode for real download tests
- Downloads through the running mixed-port with curl
- Discovers IPv4/IPv6 exits through the tested proxy and keeps the legacy
  ip-api profile; optional multi-source Intelligence is queried after exits
  are deduplicated
- Restores the original mode and proxy selections on exit
- Renders a star-rated box table and writes a CSV report

No third-party Python packages required.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import http.client
import ipaddress
import io
import json
import os
import re
import signal
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

from speedbench_ip_intel import (
    IpIntelCache,
    IpIntelligence,
    aggregate_ip_intelligence,
    load_provider_config,
    make_default_providers,
)


# Clash Verge Rev (recent versions) launches mihomo with -ext-ctl-unix instead of
# a TCP external-controller, so the Unix socket is probed first.
# Windows 版 Verge 对应的默认通道是命名管道 external-controller-pipe——真机实测
# 生成的运行配置里 external-controller 为空（9097/9090 根本不监听），管道常常
# 是 controller 的唯一入口；故 win32 候选为 pipe:// 优先 + TCP 兜底，unix:// 剔除。
# 用户仍可用 --controller 显式指定任意地址（含 pipe://<名字>）。
_ALL_CONTROLLERS = (
    "unix:///tmp/verge/verge-mihomo.sock",
    "pipe://verge-mihomo",
    "http://127.0.0.1:9097",
    "http://127.0.0.1:9090",
)
DEFAULT_CONTROLLERS = tuple(
    c for c in _ALL_CONTROLLERS
    if c.startswith("http://")
    or (c.startswith("unix://") and sys.platform != "win32")
    or (c.startswith("pipe://") and sys.platform == "win32")
)
DEFAULT_DELAY_URL = "https://cp.cloudflare.com/generate_204"
DEFAULT_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
DEFAULT_IPIFY4_URL = "https://api.ipify.org?format=json"
DEFAULT_IPIFY6_URL = "https://api6.ipify.org?format=json"
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


# ---------------- Windows 命名管道传输（external-controller-pipe） ----------------
# Clash Verge Rev 的 Windows 版默认（服务模式时甚至是唯一）把 mihomo 的完整
# HTTP API 挂在 \\.\pipe\verge-mihomo 上。用 ctypes 调系统 DLL 实现，保持零第三方
# 依赖。ctypes.windll 只在真实 Windows 上存在，故一律在函数内惰性取用；
# 测试（含 posix 上 mock 平台分支）通过 patch 下面四个 plumbing 函数进行。
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT),
                    ("MaximumLength", wintypes.USHORT),
                    ("Buffer", wintypes.LPWSTR)]

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                    ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
                    ("Attributes", wintypes.ULONG),
                    ("SecurityDescriptor", wintypes.LPVOID),
                    ("SecurityQualityOfService", wintypes.LPVOID)]

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_ssize_t), ("Information", ctypes.c_size_t)]

    _PIPE_GENERIC_RW = 0x80000000 | 0x40000000  # GENERIC_READ | GENERIC_WRITE
    _PIPE_SYNCHRONIZE = 0x00100000
    _PIPE_SHARE_RW = 0x1 | 0x2                  # FILE_SHARE_READ | FILE_SHARE_WRITE
    _PIPE_OPEN_EXISTING = 3                     # OPEN_EXISTING
    _PIPE_FILE_OPEN = 1                         # FILE_OPEN
    _PIPE_CASE_INSENSITIVE = 0x40               # OBJ_CASE_INSENSITIVE
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    # ReadFile 的对端关闭/未连接错误码：视同 EOF
    _PIPE_EOF_ERRORS = (109, 233)               # ERROR_BROKEN_PIPE / PIPE_NOT_CONNECTED


def _pipe_kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _open_pipe_handle(pipe_name: str) -> int:
    """打开命名管道，返回 Win32 句柄；失败抛 OSError。

    先试标准的 CreateFileW(\\\\.\\pipe\\<name>)；真机验收实测：mihomo 以服务
    模式（SYSTEM、会话 0）运行的机器上，Win32 路径解析对这条管道报
    ERROR_PATH_NOT_FOUND，而原生 NT 路径能开——故回退 ntdll.NtCreateFile
    直开 \\Device\\NamedPipe\\<name>。
    """
    if sys.platform != "win32":
        raise OSError("命名管道 controller 仅支持 Windows")
    kernel32 = _pipe_kernel32()
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    handle = kernel32.CreateFileW("\\\\.\\pipe\\" + pipe_name, _PIPE_GENERIC_RW,
                                  _PIPE_SHARE_RW, None, _PIPE_OPEN_EXISTING, 0, None)
    if handle != _INVALID_HANDLE_VALUE:
        return handle
    # Win32 路径解析失败（真机实测 err 3）：回退原生 NT 路径
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtCreateFile.restype = wintypes.LONG
    ntdll.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                   ctypes.POINTER(_OBJECT_ATTRIBUTES),
                                   ctypes.POINTER(_IO_STATUS_BLOCK),
                                   wintypes.LPVOID, wintypes.ULONG, wintypes.ULONG,
                                   wintypes.ULONG, wintypes.ULONG,
                                   wintypes.LPVOID, wintypes.ULONG]
    nt_path = "\\Device\\NamedPipe\\" + pipe_name
    name_buf = ctypes.create_unicode_buffer(nt_path)
    ustr = _UNICODE_STRING(len(nt_path) * 2, len(nt_path) * 2,
                           ctypes.cast(name_buf, wintypes.LPWSTR))
    attrs = _OBJECT_ATTRIBUTES()
    attrs.Length = ctypes.sizeof(_OBJECT_ATTRIBUTES)
    attrs.ObjectName = ctypes.pointer(ustr)
    attrs.Attributes = _PIPE_CASE_INSENSITIVE
    out_handle = wintypes.HANDLE()
    iosb = _IO_STATUS_BLOCK()
    status = ntdll.NtCreateFile(ctypes.byref(out_handle),
                                _PIPE_GENERIC_RW | _PIPE_SYNCHRONIZE,
                                ctypes.byref(attrs), ctypes.byref(iosb),
                                None, 0, _PIPE_SHARE_RW, _PIPE_FILE_OPEN, 0, None, 0)
    if status != 0:
        raise OSError(f"命名管道 {pipe_name} 打开失败，NTSTATUS 0x{status & 0xFFFFFFFF:08X}"
                      "（请确认 Clash Verge Rev 正在运行）")
    return out_handle.value


def _pipe_write_all(handle: int, data: bytes) -> None:
    kernel32 = _pipe_kernel32()
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    view = memoryview(data)
    while view:
        written = wintypes.DWORD()
        # c_void_p 形参不吃 memoryview（真机实测 TypeError），逐段转 bytes 再传
        chunk = view.tobytes()
        if not kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "WriteFile 命名管道失败")
        view = view[written.value:]


def _pipe_read(handle: int, size: int) -> bytes:
    """读管道；对端关闭或读到 0 字节视同 EOF 返回 b""。"""
    kernel32 = _pipe_kernel32()
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD()
    ok = kernel32.ReadFile(handle, buf, size, ctypes.byref(read), None)
    if read.value:
        return buf.raw[: read.value]  # 含消息模式下的 ERROR_MORE_DATA 部分数据
    if not ok:
        err = ctypes.get_last_error()
        if err in _PIPE_EOF_ERRORS:
            return b""
        raise OSError(err, "ReadFile 命名管道失败")
    return b""


def _pipe_close(handle: int) -> None:
    _pipe_kernel32().CloseHandle(wintypes.HANDLE(handle))


class _PipeRawReader(io.RawIOBase):
    """把管道句柄适配成 io 读接口，供 BufferedReader 包装。

    句柄所有权随 makefile() 移交到这里：HTTPConnection 在 Connection: close
    响应上 begin() 后立刻 conn.close()（→ sock.close()），而响应体还没从
    fp 读完——对齐 socket.makefile 的语义（sock 先关、fp 继续可读），
    由本对象的 close() 在响应读完时真正关句柄。
    """

    def __init__(self, handle: int):
        super().__init__()
        self._handle: Optional[int] = handle

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = _pipe_read(self._handle, len(b))
        b[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if self._handle is not None:
            _pipe_close(self._handle)
            self._handle = None
        super().close()


class _PipeSock:
    """http.client 所需的最小 socket 外观（命名管道实现）。

    http.client 只调用 sendall()/makefile("rb")/close()。makefile("rb") 把句柄
    所有权移交给返回的文件对象（见 _PipeRawReader），此后 close() 不再管句柄。
    管道读写是阻塞式 Win32 调用、没有 socket 意义上的超时（settimeout 收下来
    但不生效）；/delay 等 API 的服务端 timeout 参数兜底，mihomo 总会在有限
    时间内应答。
    """

    def __init__(self, handle: int):
        self._handle: Optional[int] = handle

    def sendall(self, data) -> None:
        if self._handle is None:
            raise OSError("管道句柄已移交或关闭")
        _pipe_write_all(self._handle, bytes(data))

    def makefile(self, mode, buffering=None):
        if mode != "rb":
            raise ValueError("管道 socket 外观只支持 makefile(\"rb\")")
        if self._handle is None:
            raise OSError("管道句柄已移交或关闭")
        rfile = io.BufferedReader(_PipeRawReader(self._handle), buffer_size=65536)
        self._handle = None  # 句柄所有权随读侧文件移交
        return rfile

    def settimeout(self, _timeout) -> None:
        pass  # 命名管道不支持，见类注释

    def close(self) -> None:
        if self._handle is not None:
            _pipe_close(self._handle)
            self._handle = None


class WinPipeHTTPConnection(http.client.HTTPConnection):
    """HTTP-over-Windows-命名管道连接（mihomo 的 external-controller-pipe）。"""

    def __init__(self, pipe_name: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.pipe_name = pipe_name

    def connect(self) -> None:
        self.sock = _PipeSock(_open_pipe_handle(self.pipe_name))


class MihomoAPI:
    def __init__(self, base: str, secret: str = "", timeout: float = 5.0):
        base = base.rstrip("/")
        self.unix_path: Optional[str] = None
        self.pipe_name: Optional[str] = None
        if base.startswith("unix://"):
            self.unix_path = base[len("unix://"):]
            self.base = "http://localhost"
        elif base.startswith("pipe://"):
            # Windows 命名管道 controller（Clash Verge Rev Windows 版默认通道）
            self.pipe_name = base[len("pipe://"):]
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
        elif self.pipe_name is not None:
            conn = WinPipeHTTPConnection(self.pipe_name, timeout=self.timeout)
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
class ProbeStats:
    """Application-level HTTP/HTTPS probe statistics.

    ``__iter__`` intentionally yields only the legacy ``(latency, jitter)``
    pair.  Existing callers can therefore continue to unpack the return value
    while new callers retain the attempt/success/failure accounting required
    for a meaningful application-level failure rate.
    """

    latency_ms: Optional[int] = None
    jitter_ms: Optional[float] = None
    attempts: int = 0
    successes: int = 0
    failures: int = 0

    def __post_init__(self) -> None:
        self.attempts = max(0, int(self.attempts))
        self.successes = max(0, int(self.successes))
        self.failures = max(0, int(self.failures))
        if not self.attempts:
            self.successes = 0
            self.failures = 0
        if self.attempts:
            self.successes = min(self.successes, self.attempts)
            self.failures = min(self.failures, self.attempts - self.successes)
        # A caller may provide attempts and successes but omit failures.  Keep
        # the three counters internally consistent without inventing attempts.
        if self.attempts and self.successes + self.failures != self.attempts:
            self.failures = max(0, self.attempts - self.successes)

    @property
    def success_rate(self) -> Optional[float]:
        if self.attempts <= 0:
            return None
        return round(self.successes / self.attempts * 100.0, 1)

    @property
    def loss_pct(self) -> Optional[float]:
        if self.attempts <= 0:
            return None
        return round(self.failures / self.attempts * 100.0, 1)

    # Names used in persisted Result/history records.
    @property
    def probe_success_rate(self) -> Optional[float]:
        return self.success_rate

    @property
    def probe_loss_pct(self) -> Optional[float]:
        return self.loss_pct

    def __iter__(self):
        yield self.latency_ms
        yield self.jitter_ms

    def __len__(self) -> int:
        # Keep the small tuple-like surface used by older callers/tests.
        return 2

    def __getitem__(self, index: int):
        if index == 0:
            return self.latency_ms
        if index == 1:
            return self.jitter_ms
        raise IndexError(index)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, tuple) and len(other) == 2:
            return (self.latency_ms, self.jitter_ms) == other
        if not isinstance(other, ProbeStats):
            return False
        return (
            self.latency_ms, self.jitter_ms, self.attempts,
            self.successes, self.failures,
        ) == (
            other.latency_ms, other.jitter_ms, other.attempts,
            other.successes, other.failures,
        )

    def to_dict(self) -> dict:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.success_rate,
            "loss_pct": self.loss_pct,
            "latency_ms": self.latency_ms,
            "jitter_ms": self.jitter_ms,
        }


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
    node_key: str = ""                   # 节点稳定身份（proto|server|port 哈希），改名不断链
    # Application-level probe counters; these are deliberately separate from
    # ICMP/physical packet loss (the latter is not measured by SpeedBench).
    probe_attempts: int = 0
    probe_successes: int = 0
    probe_failures: int = 0
    probe_success_rate: Optional[float] = None
    probe_loss_pct: Optional[float] = None
    # Exit identities and multi-source intelligence.  ``ip`` remains the
    # legacy ip-api-shaped IPv4 profile for old JSONL/UI consumers.
    exit_ipv4: Optional[str] = None
    exit_ipv6: Optional[str] = None
    intel_v4: Optional[IpIntelligence] = None
    intel_v6: Optional[IpIntelligence] = None
    network_score: Optional[float] = None
    ip_quality_score: Optional[float] = None
    ip_grade: Optional[str] = None
    dual_stack_inconsistent: bool = False


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


# 无控制台取消通道：面板以 pythonw 无窗口运行时，测速子进程没有可依附的
# 控制台，CTRL_BREAK_EVENT 无处可投。改由面板把哨兵文件路径放进环境变量，
# 测速循环在节点/轮次间隙发现文件出现即走与 Ctrl+C 相同的优雅中断路径
# （KeyboardInterrupt → finally 恢复 Clash 配置）。
_CANCEL_FILE = os.environ.get("SPEEDBENCH_CANCEL_FILE", "")


def cancel_requested() -> bool:
    """面板是否请求中断（哨兵文件出现）。未设置环境变量时恒为 False。"""
    return bool(_CANCEL_FILE) and os.path.exists(_CANCEL_FILE)


def clear_cancel_request() -> None:
    """启动时清掉上一轮残留的哨兵文件，否则一开场就会被误判为已取消。"""
    if not _CANCEL_FILE:
        return
    try:
        os.unlink(_CANCEL_FILE)
    except OSError:
        pass


def _no_window_kwargs() -> dict:
    """Windows 下给子进程（curl/mihomo/powershell 等）加 CREATE_NO_WINDOW，
    防止面板以 pythonw 无控制台方式启动时每跑一个子进程就弹黑色控制台窗口；
    这些子进程的输出全部走管道/DEVNULL，不依赖控制台。POSIX 返回空 dict，
    调用处用 ** 展开，对既有调用签名零影响。"""
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            return {"creationflags": flags}
    return {}


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
        # 钉 UTF-8：中文 Windows 的默认 GBK 解码遇到 curl 输出里的非 GBK 字节
        # 会在 subprocess 读取线程里炸 UnicodeDecodeError（真机实测）
        p = subprocess.run(cmd, text=True, capture_output=True,
                           encoding="utf-8", errors="replace",
                           timeout=max_time + connect_timeout + 5,
                           **_no_window_kwargs())
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


def _probe_count_from_args(args: Any) -> int:
    """Resolve the CLI probe mode while keeping old test namespaces valid."""
    explicit = getattr(args, "probe_count", None)
    if explicit is not None:
        try:
            return max(1, int(explicit))
        except (TypeError, ValueError):
            return 3
    return 10 if bool(getattr(args, "stability", False)) else 3


def _coerce_probe_stats(value: Any, attempts: Optional[int] = None) -> ProbeStats:
    """Accept new ProbeStats and old two-item tuple test doubles."""
    if isinstance(value, ProbeStats):
        return value
    try:
        latency, jitter = value
    except (TypeError, ValueError):
        latency, jitter = None, None
    # A two-value legacy result carries no counter metadata.  Use the requested
    # attempt count when available, which accurately reflects the normal path;
    # test doubles can still return a tuple without breaking callers.
    n = max(0, int(attempts or 0))
    successes = n if latency is not None else 0
    failures = max(0, n - successes)
    return ProbeStats(latency, jitter, n, successes, failures)


def probe_latency(api: MihomoAPI, name: str, timeout_ms: int,
                  count: int = 3) -> ProbeStats:
    """Run independent application-level probes and retain failure counters.

    A failed HTTP/HTTPS probe does not stop the remaining attempts.  The
    returned object remains unpackable as the legacy ``(latency, jitter)``
    pair, while exposing attempts/successes/failures and percentages for new
    callers.  This is *not* ICMP packet loss measurement.
    """
    attempts = max(1, int(count))
    vals: List[float] = []
    failures = 0
    for _ in range(attempts):
        try:
            d = api.proxy_delay(name, DEFAULT_DELAY_URL, timeout_ms)
        except Exception:
            d = None
        if d is None:
            failures += 1
            continue
        try:
            vals.append(float(d))
        except (TypeError, ValueError):
            failures += 1
    if not vals:
        return ProbeStats(None, None, attempts, 0, failures)
    jitter = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return ProbeStats(
        int(round(statistics.median(vals))),
        round(jitter, 1),
        attempts,
        len(vals),
        failures,
    )


def _apply_probe_stats(result: Result, stats: Any,
                       fallback_attempts: Optional[int] = None) -> Result:
    """Copy probe counters onto a Result without changing its legacy fields."""
    probe = _coerce_probe_stats(stats, attempts=fallback_attempts)
    result.probe_attempts = probe.attempts
    result.probe_successes = probe.successes
    result.probe_failures = probe.failures
    result.probe_success_rate = probe.success_rate
    result.probe_loss_pct = probe.loss_pct
    # ProbeStats is authoritative for latency/jitter when a caller supplied it;
    # a tuple fallback is intentionally also accepted for compatibility.
    if result.latency_ms is None and probe.latency_ms is not None:
        result.latency_ms = probe.latency_ms
    if result.jitter_ms is None and probe.jitter_ms is not None:
        result.jitter_ms = probe.jitter_ms
    return result


def fetch_ip_info(proxy_url: str, timeout: float) -> Optional[dict]:
    """Query ip-api.com through the given proxy; returns parsed dict or None."""
    # Keep the legacy HTTP sink itself opt-out aware so direct callers cannot
    # bypass the provider-level SPEEDBENCH_DISABLE_IP_API guard.
    if not load_provider_config().ip_api_enabled:
        return None
    cmd = [
        "curl",
        "--proxy", proxy_url,
        "--silent", "--show-error",
        "--connect-timeout", str(min(4.0, timeout)),
        "--max-time", str(timeout),
        DEFAULT_IP_API_URL,
    ]
    try:
        # 钉 UTF-8：ip-api 返回体是 UTF-8 JSON（lang=zh-CN 时含中文地名），
        # 中文 Windows 按 GBK 解码必炸 UnicodeDecodeError（真机实测）
        p = subprocess.run(cmd, text=True, capture_output=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout + 5,
                           **_no_window_kwargs())
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("status") != "success":
        return None
    return data


def fetch_exit_ip(proxy_url: str, timeout: float,
                  ipv6: bool = False) -> Optional[str]:
    """Discover one exit address through the tested proxy.

    ``api6.ipify.org`` is IPv6-only, so it naturally reports ``None`` when a
    node or its worker cannot reach IPv6.  We do not pass ``--ipv6`` to curl:
    that flag would force the localhost HTTP proxy connection itself onto an
    IPv6 socket on systems where the proxy only listens on 127.0.0.1.  Mihomo
    resolves the IPv6-only destination according to its worker IPv6 setting.
    """
    url = DEFAULT_IPIFY6_URL if ipv6 else DEFAULT_IPIFY4_URL
    cmd = [
        "curl", "--proxy", proxy_url, "--silent", "--show-error",
        "--connect-timeout", str(min(4.0, timeout)),
        "--max-time", str(timeout), url,
    ]
    try:
        p = subprocess.run(
            cmd, text=True, capture_output=True, encoding="utf-8",
            errors="replace", timeout=timeout + 5, **_no_window_kwargs()
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    try:
        payload = json.loads(p.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    value = payload.get("ip") if isinstance(payload, dict) else payload
    try:
        parsed = ipaddress.ip_address(str(value).strip())
    except (TypeError, ValueError):
        return None
    if ipv6 and parsed.version != 6:
        return None
    if not ipv6 and parsed.version != 4:
        return None
    return str(parsed)


def _coerce_exit_family(value: Any, version: int) -> Optional[str]:
    """Validate an exit address at the aggregate boundary as well.

    ``fetch_exit_ip`` already validates its subprocess response, but keeping
    this second guard makes ``fetch_exit_ips`` safe when a transport/test
    adapter is replaced or returns a value from the wrong address family.
    """
    try:
        parsed = ipaddress.ip_address(str(value).strip())
    except (TypeError, ValueError):
        return None
    return str(parsed) if parsed.version == version else None


def fetch_exit_ips(proxy_url: str, timeout: float) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """Return ``(IPv4, IPv6, legacy ip-api payload)`` for one tested node.

    The independent exit requests run together so a slow IPv6-only endpoint
    cannot add a second or third full timeout to every node.  IPv4 uses ipify
    first and, when the legacy provider is enabled, falls back to the existing
    ip-api self lookup after all requests have completed.  The third return
    value keeps the old ``IpInfo`` mapping available to the rest of the
    application.  IPv6 failure is a normal ``None`` outcome; disabling
    ip-api leaves both ipify probes active and returns a ``None`` legacy value.
    """
    ip_api_enabled = load_provider_config().ip_api_enabled
    # Keep the legacy ip-api self lookup alongside both ipify calls only when
    # enabled: it remains the documented IPv4 fallback, while a slow/failed
    # source cannot multiply the per-node timeout.  Each worker is independent
    # and failures are treated as unavailable data rather than aborting the
    # node measurement.
    with ThreadPoolExecutor(max_workers=3 if ip_api_enabled else 2) as pool:
        futures = {
            "ipv4": pool.submit(fetch_exit_ip, proxy_url, timeout, False),
            "ipv6": pool.submit(fetch_exit_ip, proxy_url, timeout, True),
        }
        if ip_api_enabled:
            futures["legacy"] = pool.submit(fetch_ip_info, proxy_url, timeout)

        def read(name: str):
            try:
                return futures[name].result()
            except Exception:
                return None

    ipv4 = _coerce_exit_family(read("ipv4"), 4)
    legacy = read("legacy") if ip_api_enabled else None
    ipv6 = _coerce_exit_family(read("ipv6"), 6)

    # Keep the fallback after all requests are joined.  Do not trust an
    # arbitrary ip-api query value: validate that it really is IPv4 so a
    # malformed/dual-stack response cannot populate the IPv4 field.
    if ipv4 is None and legacy:
        candidate = legacy.get("query")
        try:
            parsed = ipaddress.ip_address(str(candidate).strip())
            if parsed.version == 4:
                ipv4 = str(parsed)
        except (TypeError, ValueError):
            pass
    return ipv4, ipv6, legacy


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


def multi_bandwidth_score(mbps: Optional[float]) -> float:
    """Score the optional four-stream aggregate on a 400 Mbps scale."""
    if not mbps or mbps <= 0:
        return 0.0
    return min(float(mbps), 400.0) / 400.0 * 100.0


def jitter_score(jitter_ms: Optional[float]) -> Optional[float]:
    """Map jitter to a simple, testable 0-100 score.

    Up to 5 ms is excellent; 200 ms or more is unusable.  Missing jitter is
    omitted from the weighted network score rather than treated as a bonus or
    penalty.
    """
    if jitter_ms is None:
        return None
    try:
        value = float(jitter_ms)
    except (TypeError, ValueError):
        return None
    if value <= 5:
        return 100.0
    if value >= 200:
        return 0.0
    return (200.0 - value) / 195.0 * 100.0


def connect_score(connect_ms: Optional[float]) -> Optional[float]:
    """Map TCP/TLS connect time to a simple, testable 0-100 score."""
    if connect_ms is None:
        return None
    try:
        value = float(connect_ms)
    except (TypeError, ValueError):
        return None
    if value <= 100:
        return 100.0
    if value >= 2000:
        return 0.0
    return (2000.0 - value) / 1900.0 * 100.0


def probe_success_score(result: Result) -> Optional[float]:
    """Return application-level probe success percentage when measured."""
    value = result.probe_success_rate
    if value is None and result.probe_attempts > 0:
        value = result.probe_successes / result.probe_attempts * 100.0
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def compute_network_score(r: Result) -> float:
    """Compute the network-only score with valid-dimension re-normalization.

    A valid single-stream bandwidth sample is a prerequisite.  Optional
    multi-stream, latency, jitter, connect and application-level probe
    dimensions are omitted when unavailable and the remaining weights are
    normalized to 100.  This score contains no IP/reputation signal.
    """
    if r.median_mbps is None or r.median_mbps <= 0:
        return 0.0
    dimensions: List[Tuple[float, float]] = [(35.0, bandwidth_score(r.median_mbps))]
    if r.multi_mbps is not None and r.multi_mbps > 0:
        dimensions.append((15.0, multi_bandwidth_score(r.multi_mbps)))
    if r.latency_ms is not None:
        dimensions.append((20.0, latency_score(r.latency_ms)))
    jitter = jitter_score(r.jitter_ms)
    if jitter is not None:
        dimensions.append((10.0, jitter))
    connect = connect_score(r.connect_ms)
    if connect is not None:
        dimensions.append((10.0, connect))
    probe = probe_success_score(r)
    if probe is not None:
        dimensions.append((10.0, probe))
    total_weight = sum(weight for weight, _value in dimensions)
    if total_weight <= 0:
        return 0.0
    return round(sum(weight * value for weight, value in dimensions) / total_weight, 1)


def ip_flag_score(ip: Optional[IpInfo]) -> Optional[float]:
    """Legacy IP marker helper.

    It is retained for callers that display the old ip-api flags, but an
    unavailable/unknown profile returns ``None``.  Unknown is never a clean
    100-point IP result, and ``compute_score`` no longer uses this heuristic.
    """
    if not ip or not ip.ok:
        return None
    if ip.proxy:
        return 30.0
    if ip.hosting:
        return 55.0
    if ip.mobile:
        return 80.0
    return 100.0


def compute_score(r: Result) -> float:
    """Compute Overall score from Network and optional IP quality.

    IP quality is supplied only by the multi-source intelligence layer.  When
    it is unavailable, Overall equals Network; there is no implicit 100-point
    reward for an unknown/failed IP query.
    """
    network = compute_network_score(r)
    r.network_score = network
    # Accept callers that attach unified v4/v6 objects directly without first
    # calling _apply_intelligence; this keeps the public scoring helper useful
    # in tests and for future integrations.
    if r.ip_quality_score is None:
        quality_values = [intel.ip_quality_score for intel in (r.intel_v4, r.intel_v6)
                          if intel is not None and intel.ip_quality_score is not None]
        if quality_values:
            r.ip_quality_score = min(quality_values)
            worst = min(
                (intel for intel in (r.intel_v4, r.intel_v6)
                 if intel is not None and intel.ip_quality_score is not None),
                key=_intel_risk_key,
            )
            r.ip_grade = worst.ip_grade
    if r.ip_quality_score is None:
        r.score = network
    elif network <= 0.0:
        # A node without a valid single-stream sample cannot earn an Overall
        # score from reputation alone.  This preserves the bandwidth
        # prerequisite even when an exit-IP provider happened to respond.
        r.score = 0.0
    else:
        quality = max(0.0, min(100.0, float(r.ip_quality_score)))
        r.ip_quality_score = round(quality, 1)
        r.score = round(network * 0.80 + quality * 0.20, 1)
    return r.score


def _intel_dict(intel: Optional[IpIntelligence]) -> Optional[dict]:
    """Serialize only normalized Intelligence fields (never provider raw data)."""
    if intel is None:
        return None
    try:
        data = intel.to_dict()
    except Exception:
        return None
    # ``IpIntelligence.to_dict`` intentionally excludes ProviderResult.raw;
    # keep this boundary explicit in case that model gains internal fields.
    if isinstance(data, dict):
        data.pop("provider_results", None)
        data.pop("raw", None)
    return data if isinstance(data, dict) else None


def _intel_risk_key(intel: IpIntelligence) -> Tuple[int, float]:
    """Sort usable IP quality results from worse to better."""
    score = intel.ip_quality_score
    if score is None:
        return (1, 101.0)
    return (0, float(score))


def _apply_intelligence(result: Result, intel_by_ip: Dict[str, IpIntelligence]) -> Result:
    """Attach v4/v6 Intelligence and derive node-level worst-IP summary."""
    # Resolve the legacy ip-api identity before looking up intelligence.  Some
    # callers only have the old ``Result.ip`` profile (for compatibility with
    # v0.x history/test records), so doing the lookup first would silently
    # discard an already-fetched normalized intelligence record.
    if not result.exit_ipv4 and result.ip and result.ip.ok and result.ip.exit_ip:
        result.exit_ipv4 = result.ip.exit_ip
    if result.exit_ipv4:
        result.intel_v4 = intel_by_ip.get(result.exit_ipv4)
    if result.exit_ipv6:
        result.intel_v6 = intel_by_ip.get(result.exit_ipv6)

    usable = [intel for intel in (result.intel_v4, result.intel_v6)
              if intel is not None and intel.ip_quality_score is not None]
    worst = min(usable, key=_intel_risk_key) if usable else None
    result.ip_quality_score = worst.ip_quality_score if worst else None
    result.ip_grade = worst.ip_grade if worst else None

    comparable: Dict[str, set] = {"country": set(), "asn": set(), "category": set()}
    for intel in (result.intel_v4, result.intel_v6):
        if intel is None:
            continue
        for key, value in (
            ("country", intel.country),
            ("asn", intel.asn),
            ("category", intel.classification.category),
        ):
            if value and (key != "category" or value != "unknown"):
                comparable[key].add(str(value).strip().lower())
    result.dual_stack_inconsistent = any(len(values) > 1 for values in comparable.values())
    return result


class _IntelEnrichment:
    """Asynchronous, deduplicated IP Intelligence coordinator.

    One outer worker handles one unique exit IP.  Provider calls inside that
    job are serial, keeping total external concurrency bounded by the outer
    pool (2-4) instead of accidentally multiplying nested pools.
    """

    def __init__(self, args: Any):
        history_value = getattr(args, "history", None)
        history = Path(history_value) if history_value else Path(__file__).with_name(
            "speedbench-history.jsonl"
        )
        self.cache = IpIntelCache(history.with_suffix(".db"))
        timeout = float(getattr(args, "ip_timeout", 8.0) or 8.0)
        self.providers = make_default_providers(timeout=timeout)
        configured = getattr(args, "intel_workers", 3)
        try:
            workers = max(2, min(4, int(configured)))
        except (TypeError, ValueError):
            workers = 3
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.futures: Dict[str, Any] = {}
        self.values: Dict[str, IpIntelligence] = {}

    def _query_one(self, ip: str) -> IpIntelligence:
        provider_results = self.cache.query_many(
            ip, self.providers, max_workers=1
        )
        return aggregate_ip_intelligence(ip, provider_results)

    def submit_ip(self, ip: Optional[str]) -> None:
        if not ip or ip in self.futures:
            return
        try:
            parsed = ipaddress.ip_address(str(ip))
            normalized = str(parsed)
        except (TypeError, ValueError):
            return
        if normalized in self.futures:
            return
        self.futures[normalized] = self.pool.submit(self._query_one, normalized)

    def submit_result(self, result: Result) -> None:
        if not result.exit_ipv4 and result.ip and result.ip.ok:
            result.exit_ipv4 = result.ip.exit_ip or None
        self.submit_ip(result.exit_ipv4)
        self.submit_ip(result.exit_ipv6)

    def finish(self) -> None:
        try:
            for ip, future in list(self.futures.items()):
                try:
                    value = future.result()
                except Exception:
                    # Intelligence is optional; retain the network result if a
                    # provider/cache/database operation unexpectedly fails.
                    continue
                if isinstance(value, IpIntelligence):
                    self.values[ip] = value
        finally:
            self.pool.shutdown(wait=True)

    def apply(self, results: List[Result]) -> None:
        for result in results:
            _apply_intelligence(result, self.values)
            compute_score(result)
            result.tags = make_tags(result)


def start_intelligence_enrichment(results: List[Result], args: Any) -> Optional[_IntelEnrichment]:
    """Create an enrichment pool and submit all already-known exit IPs."""
    if bool(getattr(args, "no_ip", False)):
        return None
    # Avoid creating the cache database at all when Phase 1 found no exits
    # (important for --no-ip-like test doubles and failed nodes).
    has_ip = any(
        (result.exit_ipv4 or result.exit_ipv6 or
         (result.ip and result.ip.ok and result.ip.exit_ip))
        for result in results
    )
    if not has_ip:
        return None
    try:
        enricher = _IntelEnrichment(args)
    except Exception:
        # Cache/provider setup is optional and must never abort network tests.
        return None
    for result in results:
        enricher.submit_result(result)
    if not enricher.futures:
        enricher.pool.shutdown(wait=False)
        return None
    return enricher


def finish_intelligence_enrichment(enricher: Optional[_IntelEnrichment],
                                   results: List[Result]) -> None:
    if enricher is None:
        for result in results:
            compute_score(result)
            result.tags = make_tags(result)
        return
    enricher.finish()
    enricher.apply(results)


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
    if r.dual_stack_inconsistent:
        # This describes the node's two observed proxy exits.  It is not a
        # client-side IPv6 leak verdict (that belongs to the Leak Audit page).
        tags.append("双栈出口不一致")
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

    headers = ["节点", "延迟", "抖动", "建连", "带宽", "Network", "Overall",
               "IP Grade", "IP画像", "应用层失败率", "标签"]

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
            "-" if r.network_score is None else f"{r.network_score:.1f}",
            star_str(r.score),
            r.ip_grade or "N/A",
            ip_desc,
            "-" if r.probe_loss_pct is None else f"{r.probe_loss_pct:.1f}%",
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
    print("  延迟/抖动/建连单位 ms │ 带宽 = 单流 Mbps（/ 后为 --multi 4 路合计峰值） │ 应用层失败率 = HTTP/HTTPS application-level probe failure rate（非 ICMP packet loss）")


def write_csv(results: List[Result], path: Path) -> None:
    ranked = rank_results(results)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["rank", "name", "provider", "protocol", "latency_ms", "jitter_ms",
                    "connect_ms", "median_mbps", "multi_mbps", "best_mbps", "sample_mb",
                    "all_samples_mbps", "network_score", "ip_quality_score", "ip_grade",
                    "score", "stars", "tags", "probe_attempts", "probe_successes",
                    "probe_failures", "probe_success_rate", "probe_loss_pct",
                    "exit_ipv4", "exit_ipv6",
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
                "" if r.network_score is None else f"{r.network_score:.1f}",
                "" if r.ip_quality_score is None else f"{r.ip_quality_score:.1f}",
                r.ip_grade or "",
                f"{r.score:.1f}",
                star_str(r.score),
                r.tags,
                r.probe_attempts,
                r.probe_successes,
                r.probe_failures,
                "" if r.probe_success_rate is None else f"{r.probe_success_rate:.1f}",
                "" if r.probe_loss_pct is None else f"{r.probe_loss_pct:.1f}",
                r.exit_ipv4 or "",
                r.exit_ipv6 or "",
                ip.exit_ip, ip.country, ip.asn, ip.asname, ip.isp, ip.org,
                ip.kind, ip_flags,
                r.status,
            ])


def node_key_of(proto: str, server: str, port, name: str) -> str:
    """节点稳定身份：有 server/port 时取 sha1(proto|server|port) 前 12 位，
    订阅方改名不会断链；拿不到凭据（串行模式的 /proxies 快照没有 server/port）
    时退化为 sha1(proto|name)——退化路径下节点改名即换 key，趋势断链属已知取舍。"""
    if server and port:
        raw = f"{proto}|{server}|{port}"
    else:
        raw = f"{proto}|{name}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def classify_failure(status: str) -> str:
    """把 Result.status 编码的失败语义归类成固定类别，供订阅维度统计。

    成功（"ok"/空）归 ""；status 可能是多轮状态以 ";" 拼接的串，按整串里的
    关键词判定。未识别的非 ok 一律 "other"。
    """
    s = (status or "").strip().lower()
    if not s or s == "ok":
        return ""
    if "timeout" in s:
        return "timeout"          # curl-timeout（含多轮拼接）
    if s.startswith("no-data"):
        return "no_data"          # 连上了但零字节
    if s.startswith("http-"):
        return "http_error"       # 下载端返回异常状态码（http-403 等）
    if s.startswith("switch-failed") or s.startswith("switch_failed"):
        return "switch_failed"    # 切换到该节点失败
    if s.startswith("curl-") or s == "unreachable":
        return "connect_error"    # curl 退出码非 0（curl-7 等）/ 延迟探测不通
    return "other"                # parse-error / error: / worker-failed: 等


def result_to_dict(r: Result) -> dict:
    ip = r.ip if r.ip and r.ip.ok else IpInfo()
    return {
        "name": r.name,
        "provider": r.provider,
        "node_key": r.node_key,
        "proto": r.proto,
        "latency_ms": r.latency_ms,
        "jitter_ms": r.jitter_ms,
        "connect_ms": r.connect_ms,
        "probe_attempts": r.probe_attempts,
        "probe_successes": r.probe_successes,
        "probe_failures": r.probe_failures,
        "probe_success_rate": r.probe_success_rate,
        "probe_loss_pct": r.probe_loss_pct,
        "median_mbps": None if r.median_mbps is None else round(r.median_mbps, 3),
        "multi_mbps": None if r.multi_mbps is None else round(r.multi_mbps, 3),
        "best_mbps": None if r.best_mbps is None else round(r.best_mbps, 3),
        "sample_mb": r.sample_mb,
        "samples_mbps": [round(x, 3) for x in r.speeds_mbps],
        "network_score": r.network_score,
        "ip_quality_score": r.ip_quality_score,
        "ip_grade": r.ip_grade,
        "score": r.score,
        "stars": star_str(r.score),
        "tags": r.tags,
        "status": r.status,
        "fail_reason": classify_failure(r.status),
        "exit_ipv4": r.exit_ipv4 or (ip.exit_ip if ip.ok else None),
        "exit_ipv6": r.exit_ipv6,
        "dual_stack_inconsistent": bool(r.dual_stack_inconsistent),
        # These are normalized provider fields only.  ProviderResult.raw and
        # all credentials remain strictly internal to the cache/provider layer.
        "intel_v4": _intel_dict(r.intel_v4),
        "intel_v6": _intel_dict(r.intel_v6),
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
    print("排序规则：按 Overall（Network + 可用 IP Quality，未知 IP 不加分）从高到低。")
    if any("未精测" in (r.tags or "") for r in results):
        print("注：「未精测」节点仅完成 Phase 1 粗筛（延迟/连通性/IP 画像），未参与带宽精测。")
    if not args.no_history:
        append_history(results, Path(args.history), args.mb, args.rounds, out)
    if args.auto_switch:
        graph = build_selectable_graph(proxies)
        auto_switch_best(api, proxies, graph, args.root_group, results, args.switch_group)
    return 0


def _reconfigure_stdio_for_console() -> None:
    """让 stdout/stderr 遇到控制台编码无法表示的字符时替换而非崩溃。

    中文 Windows 控制台（GBK/cp936）与西欧 cp1252 都无法编码节点名里的
    emoji（如 🇭🇰 区域指示符、⚠️ 的 variation selector），直接 print 会
    UnicodeEncodeError 中断整个测速（v1.0.1 用户实测）。与 Web 面板同款
    策略：errors="replace"——GBK 能表示的中文不受影响，emoji 退化为 ?。
    面板子进程走管道时同样继承本设置，覆盖所有启动路径。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass  # 非 TextIOWrapper 环境（IDLE/嵌入式/自定义捕获）跳过即可


def main() -> int:
    _reconfigure_stdio_for_console()
    parser = argparse.ArgumentParser(
        description="Clash SpeedBench — Clash Verge Rev / Mihomo 节点综合测速（网络性能 + IP Intelligence）"
    )
    parser.add_argument("--controller",
                        help="External Controller，例如 http://127.0.0.1:9097、"
                             "unix:///tmp/verge/verge-mihomo.sock 或 pipe://verge-mihomo（Windows）")
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
    parser.add_argument("--probe-count", type=int, default=None,
                        help="HTTP/HTTPS application-level probe 次数，默认 3；与 --stability 同时指定时以本参数为准")
    parser.add_argument("--stability", action="store_true",
                        help="稳定性模式：未指定 --probe-count 时执行 10 次 application-level probe")
    parser.add_argument("--no-ip", action="store_true",
                        help="跳过出口 IP 画像查询（只测延迟和带宽）")
    parser.add_argument("--ip-timeout", type=float, default=8.0,
                        help="IP 画像查询超时秒数，默认 8")
    parser.add_argument("--intel-workers", type=int, default=3,
                        help="不同出口 IP 的 Intelligence 查询并发数（2~4，默认 3）")
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
    if args.probe_count is not None and args.probe_count < 1:
        print("错误：--probe-count 至少为 1。", file=sys.stderr)
        return 2
    if args.intel_workers < 1:
        print("错误：--intel-workers 至少为 1。", file=sys.stderr)
        return 2
    if args.top_n < 1:
        print("错误：--top-n 至少为 1。", file=sys.stderr)
        return 2

    # Windows：命令行场景下用户可在控制台按 Ctrl+Break（Python 映射为
    # SIGBREAK）。本文件没有显式 SIGINT handler——Ctrl+C 靠解释器默认把 SIGINT
    # 转成 KeyboardInterrupt；这里给 SIGBREAK 注册同样的转换，让 CTRL_BREAK_EVENT
    # 走与 Ctrl+C 完全相同的优雅中断路径（except KeyboardInterrupt + finally
    # 恢复策略组/原模式）。terminate=TerminateProcess 不会跑 finally，不能用它。
    # Web 面板的「停止测速」不走这里：面板无控制台，改发哨兵文件
    # （见 cancel_requested / SPEEDBENCH_CANCEL_FILE）。
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        def _on_sigbreak(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, _on_sigbreak)

    clear_cancel_request()

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
        # 订阅来源（/proxies 快照的 provider-name）传给 workers：凭据表里没有该信息
        provider_by_name = {n: str(info.get("provider-name", ""))
                            for n, info in leaves.items()}
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
            results = run_pool(candidates, proto_by_name, args, main_api=api,
                               provider_by_name=provider_by_name)
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
    intel_enricher: Optional[_IntelEnrichment] = None
    saved_groups: Dict[str, Tuple[str, Optional[str]]] = {}
    mode_changed = False

    try:
        # Force global to guarantee curl's traffic uses the tested path.
        if original_mode != "global":
            api.patch("/configs", {"mode": "global"})
            mode_changed = True

        for idx, name in enumerate(candidates, 1):
            if cancel_requested():  # 面板哨兵文件：节点间检查，走优雅中断
                raise KeyboardInterrupt
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
                    # /proxies 快照没有 server/port，node_key 走 proto|name 退化路径
                    node_key=node_key_of(str(info.get("type", "")), "", "", name),
                )
                res.tags = make_tags(res)
                results.append(res)
                continue

            probe = probe_latency(
                api, name, args.delay_timeout,
                count=_probe_count_from_args(args),
            )
            latency, jitter = probe

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
                if cancel_requested():  # 轮次间隙检查哨兵，尽快收队
                    raise KeyboardInterrupt
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
            exit_ipv4 = None
            exit_ipv6 = None
            if not args.no_ip:
                exit_ipv4, exit_ipv6, data = fetch_exit_ips(proxy_url, args.ip_timeout)
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
                # /proxies 快照没有 server/port，node_key 走 proto|name 退化路径
                node_key=node_key_of(str(info.get("type", "")), "", "", name),
                exit_ipv4=exit_ipv4,
                exit_ipv6=exit_ipv6,
            )
            _apply_probe_stats(res, probe, fallback_attempts=_probe_count_from_args(args))
            if not args.no_ip:
                if (intel_enricher is None and
                        (res.exit_ipv4 or res.exit_ipv6 or
                         (res.ip and res.ip.ok and res.ip.exit_ip))):
                    try:
                        intel_enricher = _IntelEnrichment(args)
                    except Exception:
                        intel_enricher = None
                if intel_enricher is not None:
                    intel_enricher.submit_result(res)
            res.score = compute_score(res)
            res.tags = make_tags(res)
            results.append(res)

            ip_txt = f" | {ip_brief(ip)}" if ip and ip.ok else ""
            multi_brief = f" / {multi:.0f}" if multi else ""
            print(
                f"[{idx:>3}/{len(candidates)}] "
                f"{name} | {fmt_ms(latency)} ms | "
                f"{fmt_speed(median)}{multi_brief} Mbps（{mb}MB）{ip_txt}"
                + (f" | application-level probe failure {res.probe_loss_pct:.1f}%"
                   if res.probe_loss_pct is not None else "")
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

    # Enrichment was submitted while the serial network work was in progress;
    # only now wait for the deduplicated provider jobs and recompute Overall.
    finish_intelligence_enrichment(intel_enricher, results)

    return report(results, args, api, proxies)


if __name__ == "__main__":
    raise SystemExit(main())
