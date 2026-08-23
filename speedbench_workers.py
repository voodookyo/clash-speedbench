#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash SpeedBench — concurrent worker pool (two-phase).

Phase 1 粗筛：latency/connectivity is measured through the MAIN mihomo
instance's /delay API — same caliber as Clash Verge's ping (no node switching;
the main instance dials the HTTPS probe through the named proxy itself), so the
numbers match what Verge shows. The throwaway worker pool (several temporary
mihomo processes reusing the binary that ships with Clash Verge, minimal
generated config) is narrowed to exit-IP profiling plus a latency fallback for
nodes whose main-instance /delay failed — NO bandwidth download, so the user's
running Clash instance is never touched and the WAN link stays idle.

Phase 2 精测：one extra throwaway mihomo worker measures the Top-N nodes' real
download speed strictly one node at a time, so parallel downloads never fight
over the same WAN bandwidth.

Key tricks (validated against Clash Verge Rev + TUN on macOS):
- The Verge-generated clash-verge.yaml holds full proxy credentials; we extract
  the `proxies` list via a fallback chain: PyYAML (if the user installed it) ->
  macOS/Linux built-in ruby (YAML -> JSON) -> built-in mini YAML parser (zero
  dependency, covers only the serde_yaml-style subset Verge emits). No pip needed.
- Worker configs are written as JSON (valid YAML) with three test domains pinned
  in `hosts` — the main instance's TUN DNS hijack would otherwise return fake-ips.
- Selected nodes are expanded with their `dialer-proxy` dependency closure, so
  relay nodes whose entry node lives outside the shard stay dialable.
- Worker configs set a global top-level `interface-name: <physical if>` so worker
  dials bypass the main TUN; a node's own `interface-name` still takes
  precedence (mihomo only falls back to the global default when the per-proxy
  option is unset — see component/dialer), so deliberate per-node settings are
  never overridden.

Anything missing (PyYAML / ruby / 内置解析器均失败, binary / config / DoH) ->
WorkerUnavailable, and the caller falls back to the sequential in-place mode.
A default route that already lives on a virtual interface (global TUN / other VPN)
raises VirtualDefaultRoute (a WorkerUnavailable subclass) and takes the same
fallback.
"""

from __future__ import annotations

import json
import os
import re
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
from typing import Dict, List, Optional, Tuple

from clash_speedbench import (
    DEFAULT_DOWNLOAD_URL,
    IpInfo,
    MihomoAPI,
    Result,
    _no_window_kwargs,
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


if sys.platform == "win32":
    # Clash Verge Rev 的 Windows 安装位置：NSIS 每用户安装（%LOCALAPPDATA%）优先，
    # 其次机器级安装。expandvars 在环境变量不存在时原样返回（路径里残留 %），
    # 这种展开失败的路径直接过滤掉。
    MIHOMO_BIN_CANDIDATES = tuple(
        p for p in (
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Clash Verge\verge-mihomo.exe"),
            os.path.expandvars(r"%ProgramFiles%\Clash Verge\verge-mihomo.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Clash Verge\verge-mihomo.exe"),
        )
        if "%" not in p
    )
    CONFIG_CANDIDATES = tuple(
        p for p in (
            os.path.expandvars(
                r"%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml"
            ),
        )
        if "%" not in p
    )
else:
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
    # os.access(p, os.X_OK) 在 Windows 上对 .exe 基本可用（实际按文件存在性判断），保留
    for p in MIHOMO_BIN_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    if sys.platform == "win32":
        # PATH 兜底：Clash Verge 自带的内核名在前，独立安装的 mihomo 在后
        return shutil.which("verge-mihomo") or shutil.which("mihomo")
    return shutil.which("mihomo")


def find_config_file() -> Optional[str]:
    for p in CONFIG_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


# ---------------- 内置迷你 YAML 解析器 ----------------
# 零依赖，只覆盖 Clash Verge Rev 机器生成配置（serde_yaml 风格）的语法子集：
#   顶层 mapping、block sequence（元素为 block mapping 或标量）、按缩进递归的
#   嵌套 block mapping（ws-opts/reality-opts/headers 等）、与键同级的缩进对齐
#   序列（indentless sequence）、flow list [a, b]、flow dict {k: v}、空集合 []/{}、
#   标量（null/~/bool/int/float/单引号('' 转义)/双引号(常见转义)/plain）、
#   整行与行尾注释（引号感知）、--- 文档标记、带引号的键。
# anchors/aliases(& *)/多行字符串(| >)/tag(!)/多文档 等超出子集的内容一律抛
# VergeYAMLError，由 extract_proxies 的 fallback 链转成清晰报错——绝不静默解析错。
# 标量解读与 ruby Psych / PyYAML 的 YAML 1.1  resolver 对齐（yes/no/on/off 也算
# 布尔），保证三条解析路径在同一份配置上结果一致。


class VergeYAMLError(ValueError):
    """内置迷你 YAML 解析器遇到超出语法子集或 malformed 的内容。"""


class _NotYamlMappingEntry(Exception):
    """内部信号：该行不含「键: 值」分隔（按标量处理），不是解析错误。"""


def _strip_yaml_comment(line: str) -> str:
    """剥掉行尾注释，引号感知：# 在单/双引号内不算注释；引号外的 # 前面必须是
    行首或空白才算注释起始（URL、密码、节点名里的 # 不受影响）。
    单/双引号只在「值起始位置」（行首，或紧跟空白 : [ { , 之后）才被当作引号，
    plain 标量中间的撇号（如 it's）不会误判为引号。"""
    in_s = in_d = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_s:
            if c == "'":
                if i + 1 < n and line[i + 1] == "'":  # '' 是单引号内的转义
                    i += 2
                    continue
                in_s = False
        elif in_d:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_d = False
        elif c == "#":
            if i == 0 or line[i - 1] in " \t":
                return line[:i]
        elif c == "'":
            if i == 0 or line[i - 1] in " \t:[{,":
                in_s = True
        elif c == '"':
            if i == 0 or line[i - 1] in " \t:[{,":
                in_d = True
        i += 1
    return line


def _yaml_logical_lines(text: str) -> List[Tuple[int, str, int]]:
    """把 YAML 文本切成逻辑行 [(缩进, 内容, 行号)]：跳过空行/整行注释，
    剥掉行尾注释；--- 只允许出现在开头（多文档超出子集），... 表示文档结束。"""
    out: List[Tuple[int, str, int]] = []
    doc_started = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        indent_part = raw[:len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in indent_part:
            raise VergeYAMLError(f"第 {lineno} 行：缩进含有 Tab（YAML 不允许）")
        line = _strip_yaml_comment(raw).rstrip()
        if not line.strip():
            continue
        content = line.strip()
        if content == "---":
            if doc_started:
                raise VergeYAMLError(f"第 {lineno} 行：多文档 YAML（第二个 ---）超出支持范围")
            doc_started = True
            continue
        if content == "...":
            break
        doc_started = True
        indent = len(line) - len(line.lstrip(" "))
        out.append((indent, content, lineno))
    return out


_YAML_NULLS = {"~", "null", "Null", "NULL"}
_YAML_TRUE = {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
_YAML_FALSE = {"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"}
# 整数解读对齐 YAML 1.1 resolver（Psych/PyYAML）：二进制 0b、前导零八进制、
# 十六进制 0x、六十进制（12:34）、下划线分隔；0o17/089 这类是字符串
_YAML_INT_DEC_RE = re.compile(r"^[-+]?(0|[1-9][0-9_]*)$")
_YAML_INT_BIN_RE = re.compile(r"^([-+]?)0b([0-1_]+)$")
_YAML_INT_OCT_RE = re.compile(r"^([-+]?)0([0-7_]+)$")
_YAML_INT_HEX_RE = re.compile(r"^([-+]?)0x([0-9a-fA-F_]+)$")
_YAML_INT_SEX_RE = re.compile(r"^([-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$")
# 与 Psych/PyYAML 对齐：float 必须带小数点（1e5 在 YAML 1.1 里是字符串），
# 且指数必须带显式符号（1.5e2 是字符串，1.5e+2 才是 float）
_YAML_FLOAT_RE = re.compile(r"^[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+][0-9]+)?$")


def _resolve_plain_scalar(text: str):
    """plain 标量 -> Python 值；解读规则对齐 ruby Psych / PyYAML（YAML 1.1）。"""
    if text in _YAML_NULLS:
        return None
    if text in _YAML_TRUE:
        return True
    if text in _YAML_FALSE:
        return False
    if _YAML_INT_DEC_RE.match(text):
        return int(text.replace("_", ""))
    m = _YAML_INT_BIN_RE.match(text)
    if m:
        return int(m.group(1) + m.group(2).replace("_", ""), 2)
    m = _YAML_INT_OCT_RE.match(text)
    if m:
        return int(m.group(1) + m.group(2).replace("_", ""), 8)
    m = _YAML_INT_HEX_RE.match(text)
    if m:
        return int(m.group(1) + m.group(2).replace("_", ""), 16)
    m = _YAML_INT_SEX_RE.match(text)
    if m:
        parts = m.group(1).replace("_", "").split(":")
        sign = 1
        if parts[0].startswith("-"):
            sign, parts[0] = -1, parts[0][1:]
        elif parts[0].startswith("+"):
            parts[0] = parts[0][1:]
        value = 0
        for part in parts:
            value = value * 60 + int(part)
        return sign * value
    if _YAML_FLOAT_RE.match(text):
        return float(text)
    if text in (".inf", ".Inf", ".INF"):
        return float("inf")
    if text in ("-.inf", "-.Inf", "-.INF", "+.inf", "+.Inf", "+.INF"):
        return float("inf") if text[0] == "+" else float("-inf")
    if text in (".nan", ".NaN", ".NAN"):
        return float("nan")
    return text


def _parse_squote(text: str, lineno: int) -> Tuple[str, int]:
    """解析单引号字符串（'' 转义），text[0] 必须是 '；返回 (值, 结束位置)。"""
    out: List[str] = []
    i = 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            if i + 1 < n and text[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise VergeYAMLError(f"第 {lineno} 行：单引号字符串未闭合（跨行字符串超出支持范围）")


_DQUOTE_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v",
    "f": "\f", "r": "\r", "e": "\x1b", '"': '"', "/": "/", "\\": "\\",
    " ": " ", "_": "\xa0", "N": "\x85", "L": "\u2028", "P": "\u2029",
}


def _parse_dquote(text: str, lineno: int) -> Tuple[str, int]:
    r"""解析双引号字符串（\" \\ \n \xNN \uNNNN \UNNNNNNNN 等转义），
    text[0] 必须是 "；返回 (值, 结束位置)。未知转义报错，不静默放过。"""
    out: List[str] = []
    i = 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            if i + 1 >= n:
                break
            e = text[i + 1]
            if e in _DQUOTE_ESCAPES:
                out.append(_DQUOTE_ESCAPES[e])
                i += 2
                continue
            width = {"x": 2, "u": 4, "U": 8}.get(e)
            if width is not None:
                hexpart = text[i + 2:i + 2 + width]
                try:
                    out.append(chr(int(hexpart, 16)))
                except ValueError:
                    raise VergeYAMLError(
                        f"第 {lineno} 行：非法的十六进制转义 \\{e}{hexpart}")
                i += 2 + width
                continue
            raise VergeYAMLError(f"第 {lineno} 行：不支持的转义 \\{e}")
        out.append(c)
        i += 1
    raise VergeYAMLError(f"第 {lineno} 行：双引号字符串未闭合（跨行字符串超出支持范围）")


class _FlowYaml:
    """单行 flow 集合解析：[a, b]、{k: v}、空集合 []/{}、嵌套。
    机器生成配置的 flow 集合都在一行内；括号未闭合（跨行）即报错。"""

    def __init__(self, text: str, lineno: int):
        self.s = text
        self.i = 0
        self.lineno = lineno

    def at_end(self) -> bool:
        return self.i >= len(self.s)

    def skip_ws(self) -> None:
        while not self.at_end() and self.s[self.i] in " \t":
            self.i += 1

    def _err(self, msg: str) -> VergeYAMLError:
        return VergeYAMLError(f"第 {self.lineno} 行：{msg}")

    def parse_value(self):
        self.skip_ws()
        if self.at_end():
            raise self._err("flow 集合里缺少值")
        c = self.s[self.i]
        if c == "[":
            return self.parse_list()
        if c == "{":
            return self.parse_dict()
        if c == "'":
            value, pos = _parse_squote(self.s[self.i:], self.lineno)
            self.i += pos
            return value
        if c == '"':
            value, pos = _parse_dquote(self.s[self.i:], self.lineno)
            self.i += pos
            return value
        if c in "&*":
            raise self._err("anchor/alias（& *）超出支持范围")
        if c in "|>":
            raise self._err("多行字符串（| >）超出支持范围")
        if c == "!":
            raise self._err("tag（!）超出支持范围")
        start = self.i
        while not self.at_end() and self.s[self.i] not in ",]}":
            self.i += 1
        return _resolve_plain_scalar(self.s[start:self.i].strip())

    def parse_list(self) -> list:
        self.i += 1  # 跳过 [
        out = []
        self.skip_ws()
        if not self.at_end() and self.s[self.i] == "]":
            self.i += 1
            return out
        while True:
            out.append(self.parse_value())
            self.skip_ws()
            if self.at_end():
                raise self._err("flow list 未闭合（跨行集合超出支持范围）")
            c = self.s[self.i]
            if c == ",":
                self.i += 1
                continue
            if c == "]":
                self.i += 1
                return out
            raise self._err(f"flow list 里出现意外的字符 {c!r}")

    def parse_dict(self) -> dict:
        self.i += 1  # 跳过 {
        out: dict = {}
        self.skip_ws()
        if not self.at_end() and self.s[self.i] == "}":
            self.i += 1
            return out
        while True:
            self.skip_ws()
            if self.at_end():
                raise self._err("flow dict 未闭合（跨行集合超出支持范围）")
            quoted_key = False
            if self.s[self.i] == "'":
                key, pos = _parse_squote(self.s[self.i:], self.lineno)
                self.i += pos
                quoted_key = True
            elif self.s[self.i] == '"':
                key, pos = _parse_dquote(self.s[self.i:], self.lineno)
                self.i += pos
                quoted_key = True
            else:
                start = self.i
                while not self.at_end() and self.s[self.i] not in ":,}":
                    self.i += 1
                key = self.s[start:self.i].strip()
            if not key:
                raise self._err("flow dict 里键为空")
            if not quoted_key and key == "<<":
                # 与 block 级（_split_yaml_key）一致：merge key 超出支持范围，
                # 对齐 PyYAML 的 ConstructorError；带引号的 '<<' 是普通键名
                raise self._err("merge key（<<）超出支持范围")
            self.skip_ws()
            if self.at_end() or self.s[self.i] != ":":
                raise self._err("flow dict 缺少冒号")
            self.i += 1
            out[str(key)] = self.parse_value()
            self.skip_ws()
            if self.at_end():
                raise self._err("flow dict 未闭合（跨行集合超出支持范围）")
            c = self.s[self.i]
            if c == ",":
                self.i += 1
                continue
            if c == "}":
                self.i += 1
                return out
            raise self._err(f"flow dict 里出现意外的字符 {c!r}")


def _parse_yaml_scalar(text: str, lineno: int):
    """解析单个值（标量或单行 flow 集合），必须恰好消费完整个字符串。"""
    c = text[:1]
    if c in "[{":
        parser = _FlowYaml(text, lineno)
        value = parser.parse_value()
        parser.skip_ws()
        if not parser.at_end():
            raise VergeYAMLError(f"第 {lineno} 行：flow 集合后有多余内容")
        return value
    if c == "'":
        value, pos = _parse_squote(text, lineno)
        if text[pos:].strip():
            raise VergeYAMLError(f"第 {lineno} 行：引号字符串后有多余内容")
        return value
    if c == '"':
        value, pos = _parse_dquote(text, lineno)
        if text[pos:].strip():
            raise VergeYAMLError(f"第 {lineno} 行：引号字符串后有多余内容")
        return value
    if c in "|>":
        raise VergeYAMLError(f"第 {lineno} 行：多行字符串（| >）超出支持范围")
    if c in "&*":
        raise VergeYAMLError(f"第 {lineno} 行：anchor/alias（& *）超出支持范围")
    if c == "!":
        raise VergeYAMLError(f"第 {lineno} 行：tag（!）超出支持范围")
    return _resolve_plain_scalar(text)


def _split_yaml_key(text: str, lineno: int) -> Tuple[str, Optional[str]]:
    """把 'key: value' 拆成 (键, 值文本)；值文本 None 表示冒号后无内容
    （值在后续缩进块里，或就是 null）。键本身可以是带引号的字符串。
    找不到「冒号+空格/行尾」分隔时抛 _NotYamlMappingEntry（由调用方决定
    按标量处理还是报错）。"""
    in_s = in_d = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_s:
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_s = False
        elif in_d:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_d = False
        elif c == "'":
            in_s = True
        elif c == '"':
            in_d = True
        elif c == ":":
            if i + 1 >= n or text[i + 1] in " \t":
                key_text = text[:i].strip()
                if not key_text:
                    raise VergeYAMLError(f"第 {lineno} 行：键为空")
                if key_text[:1] in "'\"":
                    key = _parse_yaml_scalar(key_text, lineno)
                else:
                    key = key_text
                if key == "<<":
                    raise VergeYAMLError(f"第 {lineno} 行：merge key（<<）超出支持范围")
                value_text = text[i + 1:].strip()
                return str(key), (value_text if value_text else None)
        i += 1
    raise _NotYamlMappingEntry()


class _BlockYaml:
    """按缩进递归的 block 解析：逻辑行列表 -> 顶层 dict。"""

    def __init__(self, text: str):
        self.lines = _yaml_logical_lines(text)
        self.i = 0

    def parse(self) -> dict:
        if not self.lines:
            raise VergeYAMLError("配置内容为空")
        value = self.parse_block(self.lines[0][0])
        if self.i != len(self.lines):
            lineno = self.lines[self.i][2]
            raise VergeYAMLError(f"第 {lineno} 行：缩进层级无法归属（解析中断）")
        if not isinstance(value, dict):
            raise VergeYAMLError("顶层必须是 mapping（key: value 列表）")
        return value

    def _peek(self) -> Optional[Tuple[int, str, int]]:
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse_block(self, indent: int):
        cur = self._peek()
        assert cur is not None and cur[0] == indent
        if cur[1] == "-" or cur[1].startswith("- "):
            return self.parse_sequence(indent)
        return self.parse_mapping(indent)

    def parse_mapping(self, indent: int) -> dict:
        out: dict = {}
        while True:
            cur = self._peek()
            if cur is None or cur[0] < indent:
                break
            ind, content, lineno = cur
            if ind > indent:
                raise VergeYAMLError(f"第 {lineno} 行：缩进异常（找不到归属的键）")
            if content == "-" or content.startswith("- "):
                break  # 序列行属于外层（如 indentless sequence 结束后回到本层）
            try:
                key, value_text = _split_yaml_key(content, lineno)
            except _NotYamlMappingEntry:
                raise VergeYAMLError(f"第 {lineno} 行：不是 key: value 形式（缺少冒号分隔）")
            self.i += 1
            if value_text is None:
                out[key] = self.parse_nested(ind)
            else:
                out[key] = _parse_yaml_scalar(value_text, lineno)
        return out

    def parse_nested(self, parent_indent: int):
        """'key:' 冒号后无内容时解析后续缩进块；没有更深的块则值为 null。
        与键同级的 block sequence 仍属于该键（YAML 允许的 indentless sequence，
        如顶层 proxies: 下面的 - name: ... 与 proxies: 同在缩进 0）。"""
        cur = self._peek()
        if cur is None or cur[0] < parent_indent:
            return None
        ind, content, _ln = cur
        if ind == parent_indent:
            if content == "-" or content.startswith("- "):
                return self.parse_sequence(ind)
            return None
        return self.parse_block(ind)

    def parse_sequence(self, indent: int) -> list:
        out: list = []
        while True:
            cur = self._peek()
            if cur is None or cur[0] != indent:
                break
            ind, content, lineno = cur
            if content != "-" and not content.startswith("- "):
                break
            self.i += 1
            rest = content[1:].strip()
            if not rest:
                # 裸 "-" 项：内容只接受比 "-" 缩进更深的块；同缩进的下一个 "-"
                # 是兄弟项（本项为 null），同缩进或更浅一律交还给上层。
                # indentless sequence 规则只适用于 mapping 里 "key:" 的值位置
                # （见 parse_nested），不适用于 "-" 项的内容。
                nxt = self._peek()
                if nxt is not None and nxt[0] > ind:
                    out.append(self.parse_block(nxt[0]))
                else:
                    out.append(None)
                continue
            try:
                key_value = _split_yaml_key(rest, lineno)
            except _NotYamlMappingEntry:
                # 标量元素（rules、策略组成员列表等）
                out.append(_parse_yaml_scalar(rest, lineno))
                continue
            # block mapping 元素：第一个键在 - 行上，
            # 其虚拟缩进 = 「- 」之后内容的起始列，后续键按缩进对齐
            key, value_text = key_value
            virtual_indent = ind + (len(content) - len(content[1:].lstrip(" ")))
            item: dict = {}
            if value_text is None:
                item[key] = self.parse_nested(virtual_indent)
            else:
                item[key] = _parse_yaml_scalar(value_text, lineno)
            nxt = self._peek()
            if nxt is not None and nxt[0] > ind:
                more = self.parse_block(nxt[0])
                if not isinstance(more, dict):
                    raise VergeYAMLError(f"第 {nxt[2]} 行：序列元素的后续内容不是键值对")
                item.update(more)
            out.append(item)
        return out


def _parse_verge_yaml(text: str) -> dict:
    """内置迷你 YAML 解析器入口：返回顶层 dict（调用方取 ["proxies"]）。
    超出语法子集时抛 VergeYAMLError。"""
    return _BlockYaml(text).parse()


def _proxies_from_cfg(cfg) -> List[dict]:
    """从解析后的配置 dict 里取 proxies 列表并校验（三条解析路径共用）。"""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("proxies"), list):
        raise WorkerUnavailable("配置文件中没有可用的 proxies 列表")
    proxies = cfg["proxies"]
    if not proxies:
        raise WorkerUnavailable("配置文件 proxies 为空")
    return proxies


def _extract_proxies_ruby(config_path: str) -> List[dict]:
    """macOS/Linux 自带 ruby 路径（v0.7 以来的既有实现，行为不变）。"""
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
                           capture_output=True, text=True, timeout=30,
                           **_no_window_kwargs())
    except subprocess.TimeoutExpired:
        raise WorkerUnavailable("解析配置文件超时")
    if p.returncode != 0:
        raise WorkerUnavailable(f"解析配置文件失败: {p.stderr.strip()[:200]}")
    try:
        cfg = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise WorkerUnavailable("配置文件中没有可用的 proxies 列表")
    return _proxies_from_cfg(cfg)


def extract_proxies(config_path: str) -> List[dict]:
    """从 clash-verge.yaml 提取完整 proxies 列表（含节点凭据）。三级 fallback：
    1) PyYAML——用户装了 pyyaml 就优先用（最完整的 YAML 支持）；
    2) macOS/Linux 自带 ruby（YAML -> JSON 子进程，既有路径，行为不变）；
    3) 内置迷你 YAML 解析器 _parse_verge_yaml——零依赖，只覆盖 Verge 机器生成
       配置的语法子集（Windows 没有自带 ruby，主要靠它）。
    全部失败抛 WorkerUnavailable（main() 据此回退串行模式）。"""
    errors: List[str] = []

    try:
        import yaml  # PyYAML：可选依赖，装了就优先用；没装不影响后续 fallback
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            with open(config_path, encoding="utf-8") as f:
                return _proxies_from_cfg(yaml.safe_load(f))
        except WorkerUnavailable:
            raise  # 解析成功但内容不可用（没有 proxies / 为空），直接报
        except Exception as e:
            errors.append(f"PyYAML 解析失败: {e}")

    if sys.platform != "win32":
        try:
            return _extract_proxies_ruby(config_path)
        except WorkerUnavailable as e:
            errors.append(str(e))

    try:
        with open(config_path, encoding="utf-8") as f:
            return _proxies_from_cfg(_parse_verge_yaml(f.read()))
    except WorkerUnavailable:
        raise
    except (OSError, VergeYAMLError) as e:
        errors.append(f"内置 YAML 解析失败: {e}")

    raise WorkerUnavailable(
        "无法解析 Clash Verge 配置文件 clash-verge.yaml（"
        + "；".join(errors)
        + "）。可执行 pip install pyyaml 后重试；若配置文件尚不存在，"
          "请确认 Clash Verge 已安装并正在运行（配置才会生成）")


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
    if sys.platform == "win32":
        # Windows 没有 route get default；用 PowerShell 查默认路由所在接口的别名。
        # 中文系统上网卡名可能是「以太网」，PowerShell 默认输出编码不是 UTF-8，
        # 先把 [Console]::OutputEncoding 钉成 UTF-8，再按 utf-8/replace 解码。
        ps = shutil.which("powershell") or "powershell.exe"
        try:
            p = subprocess.run(
                [ps, "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
                 "Sort-Object RouteMetric | Select-Object -First 1).InterfaceAlias"],
                capture_output=True, timeout=10,
                encoding="utf-8", errors="replace",
                **_no_window_kwargs())
            name = (p.stdout or "").strip()
            if p.returncode == 0 and name:
                return name
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None
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

# Windows 虚拟网卡的常见接口别名前缀：Clash/mihomo 的 TUN 网卡（wintun/mihomo/
# clash）与主流 VPN 客户端（openvpn/wireguard/tailscale），以及 loopback、
# Hyper-V 的 vEthernet。Windows 接口别名大小写不固定（Wintun/vEthernet/中文名），
# 仅 win32 分支使用，比较时统一先 lower。
WIN_VIRTUAL_IFACE_PREFIXES = ("wintun", "mihomo", "clash", "openvpn", "wireguard",
                              "tailscale", "loopback", "vethernet")


def is_virtual_iface(name: Optional[str]) -> bool:
    """接口名是否为虚拟隧道接口。
    POSIX（macOS 接口名恒小写）保持原有的精确小写前缀匹配；
    Windows 接口别名大小写不固定，lower 后再做前缀匹配（含 Windows 专有前缀）。"""
    if not name:
        return False
    if sys.platform == "win32":
        return name.lower().startswith(VIRTUAL_IFACE_PREFIXES + WIN_VIRTUAL_IFACE_PREFIXES)
    return name.startswith(VIRTUAL_IFACE_PREFIXES)


def doh_resolve(domain: str) -> Optional[str]:
    for host, ip, path in DOH_SERVERS:
        try:
            # 钉 UTF-8：与 fetch_ip_info/curl_speed 保持一致，避免中文 Windows
            # 上 GBK 解码遇到非 GBK 字节在读取线程里炸 UnicodeDecodeError
            p = subprocess.run(
                ["curl", "-s", "-m", "6",
                 "--resolve", f"{host}:443:{ip}",
                 "-H", "accept: application/dns-json",
                 f"https://{host}{path}?name={domain}&type=A"],
                capture_output=True, text=True, timeout=9,
                encoding="utf-8", errors="replace",
                **_no_window_kwargs())
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
        # stop() 幂等保护：中断路径（run_pool 的 worker 注册表统一清理）与
        # shard_loop 的 finally 可能并发/重复调用，必须只真正执行一次
        self._stop_lock = threading.Lock()
        self._stopped = False

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
            **_no_window_kwargs(),
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
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
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


def probe_latency_pool(api: MihomoAPI, names: List[str], timeout_ms: int,
                       max_workers: int = 10
                       ) -> Dict[str, Tuple[Optional[int], Optional[float]]]:
    """Phase 1 第 1 步：经主实例 /delay API 并发测全部节点延迟，
    返回 {节点名: (中位延迟 ms, 抖动 ms)}；完全不通的节点值为 (None, None)。
    固定 10 路并发：/delay 是小流量 HTTPS 探测（Clash Verge 的「全部测速」
    就是这么并发打的），与临时 worker 进程数（--workers）无关。"""
    # 主实例 HTTP 超时默认 5s，慢节点的 /delay 探测可能刚好顶到 --delay-timeout
    # 才被本地 HTTP 超时砍掉（proxy_delay 对一切异常返回 None，会把「慢但通」
    # 误判成不通），探测期间放宽到 delay_timeout + 3s
    api.timeout = max(api.timeout, timeout_ms / 1000 + 3.0)
    out: Dict[str, Tuple[Optional[int], Optional[float]]] = {}
    lock = threading.Lock()
    done = {"n": 0}
    total = len(names)

    def one(name: str) -> None:
        lat, jit = probe_latency(api, name, timeout_ms)
        with lock:
            out[name] = (lat, jit)
            done["n"] += 1
            idx = done["n"]
        jitter_brief = f"±{jit:.0f}" if jit else ""
        print(f"Phase 1 粗筛 [{idx:>3}/{total}] {name} | "
              f"{fmt_ms(lat)}{jitter_brief} ms（主实例）")

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, total))) as pool:
        list(pool.map(one, names))
    return out


def _probe_node_in_worker(worker: Worker, name: str, proto: str, args,
                          latency: Optional[int] = None,
                          jitter: Optional[float] = None) -> Result:
    """Phase 1 第 2 步：worker 职责已收窄为出口 IP 画像探测（不跑带宽下载）。
    latency/jitter 来自主实例 /delay 探测结果；为 None 的节点（主实例探测失败，
    或调用方未提供主实例 api）在 worker 内兜底重测一次，worker 也失败才标「不通」。"""
    assert worker.api is not None
    if latency is None:
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


def run_pool(candidates: List[str], proto_by_name: Dict[str, str], args,
             main_api: Optional[MihomoAPI] = None) -> List[Result]:
    """两阶段测速，从不触碰用户正在运行的实例：
    Phase 1 的延迟/连通性经主实例 /delay API 并发探测（与 Clash Verge 的 ping
    同口径；main_api 缺省时退回 worker 内探测，即旧行为），worker 池只做出口
    IP 画像 + 主实例探测失败节点的延迟兜底，不跑带宽；
    Phase 2 新建单个 worker 对 Top-N 连通节点严格串行精测真实带宽，
    保证同一时刻全网只有一路测速下载。Raises WorkerUnavailable 触发回退。"""
    mihomo_bin = find_mihomo_bin()
    if not mihomo_bin:
        if sys.platform == "win32":
            raise WorkerUnavailable(
                "未找到 verge-mihomo.exe（Clash Verge 自带的内核）。"
                "请确认 Clash Verge 已安装并正在运行；默认安装在 "
                r"%LOCALAPPDATA%\Programs\Clash Verge\ 或 %ProgramFiles%\Clash Verge\ 下")
        raise WorkerUnavailable("未找到 mihomo 二进制（Clash Verge 自带的 verge-mihomo）")
    config_file = getattr(args, "config_file", "") or find_config_file()
    if not config_file:
        if sys.platform == "win32":
            raise WorkerUnavailable(
                "未找到 Clash Verge 的运行配置 clash-verge.yaml（通常在 "
                r"%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\ 下）。"
                "请确认 Clash Verge 已安装并至少运行过一次（配置才会生成），"
                "或用 --config-file 指定")
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

    started = time.time()
    total = len(selected)

    # ---- Phase 1 第 1 步：主实例 /delay 并发延迟探测 ----
    # 进度展示方案：延迟先行快速刷完一轮「Phase 1 粗筛 [N/M]」，随后 worker 的
    # IP 画像再计第二轮 [N/M]——Web 端只认最后一条 [N/M] 计数（app.js 正则），
    # 表现为进度条涨满后回退再涨，阶段标签始终是「Phase 1 粗筛」，无需改 Web。
    latency_map: Dict[str, Tuple[Optional[int], Optional[float]]] = {}
    if main_api is not None:
        print(f"Phase 1 粗筛 · 延迟探测: 经主实例 /delay 并发测 {total} 个节点"
              f"（Clash Verge ping 同口径，不切换节点）…")
        try:
            latency_map = probe_latency_pool(
                main_api, [str(p.get("name")) for p in selected], args.delay_timeout)
        except KeyboardInterrupt:
            print("\n\n收到 Ctrl+C，停止测速……")
            raise
        n_ok = sum(1 for lat, _jit in latency_map.values() if lat is not None)
        print(f"Phase 1 延迟探测完成: {n_ok}/{total} 连通"
              + (f"；主实例失败的 {total - n_ok} 个将在 worker 内兜底重测"
                 if n_ok < total else ""))

    # ---- Phase 1 第 2 步：worker 池出口 IP 画像 + 延迟兜底 ----
    # 需进 worker 的节点：未跳过 IP 画像时是全部节点；--no-ip 时只剩主实例
    # /delay 失败、需要 worker 兜底复测的节点（main_api 缺省时 latency_map 为空，
    # 全部节点都进 worker 测延迟，即旧行为）。全部连通且 --no-ip 时 worker 无活可干，
    # 直接跳过，省得起一批临时 mihomo 进程。
    ip_pending = [p for p in selected
                  if not args.no_ip
                  or latency_map.get(str(p.get("name")), (None, None))[0] is None]

    results: List[Result] = []
    results_lock = threading.Lock()
    done_counter = {"n": 0}

    if not ip_pending:
        for p in selected:
            name = str(p.get("name"))
            lat, jit = latency_map.get(name, (None, None))
            results.append(Result(name=name, provider="", proto=str(p.get("type", "")),
                                  latency_ms=lat, speeds_mbps=[], median_mbps=None,
                                  best_mbps=None,
                                  status="ok" if lat is not None else "unreachable",
                                  jitter_ms=jit))
    else:
        worker_count = max(1, min(args.workers, len(ip_pending)))
        print(f"Phase 1 粗筛 · IP 画像: 启动 {worker_count} 个并发 worker"
              f"（{len(ip_pending)} 节点，仅出口 IP 探测/延迟兜底，不下载，"
              f"不打扰正在运行的 Clash）…")

        # Round-robin 分片，尽量均匀
        shards: List[List[dict]] = [[] for _ in range(worker_count)]
        for i, p in enumerate(ip_pending):
            shards[i % worker_count].append(p)

        ip_total = len(ip_pending)

        # 中断收队机制：CTRL_BREAK/SIGINT 只会打断主线程的 pool.map，分片线程
        # 原本感知不到、会继续逐节点探测（真机验收实测：面板 5 秒兜底强杀后
        # 留下 5 个孤儿 worker 进程）。引入取消标志 + 已启动 worker 注册表：
        # 中断时置标志并立即停掉全部临时 mihomo，在途探测因出口进程消失而
        # 立刻失败返回，池 shutdown 随之秒级完成，等不到面板的强杀兜底。
        cancel_event = threading.Event()
        workers_lock = threading.Lock()
        live_workers: List[Worker] = []

        def stop_live_workers() -> None:
            with workers_lock:
                ws = list(live_workers)
            for w in ws:
                try:
                    w.stop()  # Worker.stop 幂等，与 shard_loop 的 finally 不冲突
                except Exception:
                    pass

        def shard_loop(shard: List[dict]) -> None:
            if cancel_event.is_set():
                return
            # worker 配置 = 分片节点 + 各自的 dialer-proxy 依赖闭包；
            # 探测仍只针对分片内的入选节点，依赖节点仅供链式拨号、不计入结果
            worker = Worker(mihomo_bin, with_dependencies(shard, all_proxies), hosts, iface)
            worker.start()
            with workers_lock:
                live_workers.append(worker)
            try:
                for p in shard:
                    if cancel_event.is_set():  # 节点间检查取消标志，尽快收队
                        return
                    name = str(p.get("name"))
                    proto = str(p.get("type", ""))
                    lat, jit = latency_map.get(name, (None, None))
                    try:
                        r = _probe_node_in_worker(worker, name, proto, args, lat, jit)
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
                    print(f"Phase 1 粗筛 [{idx:>3}/{ip_total}] {name} | "
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

        # 不用 with 管理池：with 的 __exit__ 会先 shutdown(wait=True) 再进
        # except，中断时白白等到面板强杀。显式 try/finally，中断分支先置
        # 取消标志、停 worker（在途探测随即快速失败），shutdown 才能秒级完成。
        pool = ThreadPoolExecutor(max_workers=worker_count)
        try:
            list(pool.map(guarded, [s for s in shards if s]))
        except KeyboardInterrupt:
            print("\n\n收到 Ctrl+C，停止测速（正在清理临时 worker）……")
            cancel_event.set()
            stop_live_workers()
            raise
        finally:
            pool.shutdown(wait=True)
    print(f"Phase 1 粗筛完成，耗时 {time.time() - started:.1f}s（{total} 节点）")

    # Phase 2 选节点：剔除不通节点后按延迟升序，取 Top N（--all 时取全部连通节点）
    chosen = select_phase2_nodes(results, getattr(args, "top_n", 15),
                                 getattr(args, "all", False))

    if not chosen:
        print("Phase 2 精测: 没有连通节点，跳过带宽精测。")
    else:
        # scope 只出「全部/Top」字样，个数由后面的 {len(chosen)} 表达，避免「Top 15 15 个节点」
        scope = "全部" if getattr(args, "all", False) else "Top"
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
