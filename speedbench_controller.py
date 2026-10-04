#!/usr/bin/env python3
"""Read local Verge controller metadata without loading subscription YAML.

This module has no dependency on the API/worker modules. Credentials stay in
memory, paired with the endpoint from the same file, and never enter reprs.
Only the small scalar subset emitted for controller settings is supported.
"""
from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
import stat
import sys
import threading
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple
from speedbench_config import ENV as ROOT_ENV, validate_root

APP_ID = "io.github.clash-verge-rev.clash-verge-rev"
FIELDS = {"external-controller", "external-controller-pipe", "external-controller-unix", "secret"}
MAX_CONFIG_BYTES = 16 * 1024 * 1024
_KNOWN_SECRETS = deque(maxlen=64)
_SECRET_LOCK = threading.Lock()


class ControllerConfigError(ValueError):
    """Deliberately never includes configuration contents or parser input."""


@dataclass(frozen=True)
class ControllerTarget:
    base: str
    secret: str = field(default="", repr=False)
    source: str = "default"


def remember_secret(secret: str) -> None:
    if secret:
        with _SECRET_LOCK:
            if secret not in _KNOWN_SECRETS:
                _KNOWN_SECRETS.append(secret)


def redact_text(value: object, secrets=()) -> str:
    text = str(value)
    with _SECRET_LOCK:
        known = list(_KNOWN_SECRETS)
    known.extend(secrets)
    known.append(os.environ.get("MIHOMO_SECRET", ""))
    forms = set()
    for secret in known:
        if secret:
            forms.update((secret, json.dumps(secret, ensure_ascii=False)[1:-1],
                          json.dumps(secret, ensure_ascii=True)[1:-1], repr(secret)[1:-1]))
    for secret in sorted(forms, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def redact_payload(value):
    """Redact strings before JSON encoding; never modify JSON structural tokens."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_payload(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact_payload(v) for v in value]
    return value


def config_paths(platform: Optional[str] = None, environ: Optional[Mapping[str, str]] = None,
                 home: Optional[str] = None, config_root: Optional[str] = None) -> List[Path]:
    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    selected=env.get(ROOT_ENV,'') if config_root is None else config_root
    if selected:
        return [Path(validate_root(selected))/'clash-verge.yaml']
    home = os.path.expanduser("~") if home is None else home
    if platform == "win32":
        roaming = env.get("APPDATA")
        roots = [Path(roaming) / APP_ID] if roaming else []
    elif platform == "darwin":
        roots = [Path(home) / "Library" / "Application Support" / APP_ID]
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        roots = [Path(xdg) / APP_ID] if xdg and posixpath.isabs(xdg) else [Path(home) / ".config" / APP_ID]
    return [root / "clash-verge.yaml" for root in roots]


# Clash Verge's service mode (macOS) exposes mihomo's control API on a per-user
# IPC socket. The path layout is public upstream interoperability metadata; no
# directory, user, port or process enumeration is performed here.
SERVICE_SOCKET_ROOT = Path("/var/run/clash-verge-service")


def service_controller_socket(uid: Optional[int] = None) -> Optional[Path]:
    """Documented current-user service IPC socket, or None when unavailable."""
    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is None:
        return None
    return SERVICE_SOCKET_ROOT / "users" / str(uid) / "verge-mihomo.sock"


def _owned_socket(path: Path) -> bool:
    """True only for a real, non-symlink Unix socket owned by the current uid."""
    try:
        if path.is_symlink():
            return False
        metadata = path.stat()
    except OSError:
        return False
    if not stat.S_ISSOCK(metadata.st_mode):
        return False
    return not hasattr(os, "getuid") or metadata.st_uid == os.getuid()


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "'\"":
        quote = raw[0]
        out = []
        pos = 1
        closed = False
        escapes = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v",
                   "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
                   "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}
        while pos < len(raw):
            ch = raw[pos]
            if ch == quote:
                if quote == "'" and pos + 1 < len(raw) and raw[pos + 1] == "'":
                    out.append("'")
                    pos += 2
                    continue
                pos += 1
                closed = True
                break
            if ch == "\\" and quote == '"':
                pos += 1
                if pos >= len(raw):
                    raise ControllerConfigError("控制器配置的标量语法不支持")
                escape = raw[pos]
                if escape in escapes:
                    out.append(escapes[escape])
                elif escape in "xuU":
                    count = {"x": 2, "u": 4, "U": 8}[escape]
                    digits = raw[pos + 1:pos + 1 + count]
                    if len(digits) != count or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                        raise ControllerConfigError("控制器配置的标量语法不支持")
                    try:
                        out.append(chr(int(digits, 16)))
                    except ValueError:
                        raise ControllerConfigError("控制器配置的标量语法不支持") from None
                    pos += count
                else:
                    raise ControllerConfigError("控制器配置的标量语法不支持")
            else:
                out.append(ch)
            pos += 1
        # A YAML comment requires separation from the closing quote.
        tail = raw[pos:]
        if not closed or (tail.strip() and not (tail[0].isspace() and tail.lstrip().startswith("#"))):
            raise ControllerConfigError("控制器配置的标量语法不支持")
        value = "".join(out)
    else:
        value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        if value.startswith("#"):
            value = ""
        if value.startswith(("|", ">", "&", "*", "[", "{", "!", "`", "@")) or re.search(r":\s", value):
            raise ControllerConfigError("控制器配置的标量语法不支持")
        if value in ("null", "Null", "NULL", "~"):
            value = ""
    # Control characters cannot appear in the scalar subset. Header encoding
    # is validated separately for secret; quoted spaces remain intact.
    if any(ch in value for ch in ("\r", "\n", "\0")) or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise ControllerConfigError("控制器配置包含非法控制字符")
    return value


def parse_controller_fields(text: str) -> Dict[str, str]:
    values = {}
    documents = 0
    ended = False
    selected_scalar = False
    content_seen = False
    for line in text.lstrip("\ufeff").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            if selected_scalar:
                raise ControllerConfigError("不支持跨行控制器标量")
            continue
        selected_scalar = False
        if re.fullmatch(r"---(?:\s+#.*)?\s*", line):
            documents += 1
            if documents > 1 or ended or content_seen:
                raise ControllerConfigError("不支持多文档控制器配置")
            continue
        if re.fullmatch(r"\.\.\.(?:\s+#.*)?\s*", line):
            ended = True
            continue
        if ended:
            raise ControllerConfigError("不支持多文档控制器配置")
        content_seen = True
        if re.match(r"^(?:<<|'<<'|\"<<\"):", line):
            raise ControllerConfigError("不支持控制器配置顶层 merge")
        match = re.match(r"^(?:([\w-]+)|'([\w-]+)'|\"([\w-]+)\"):\s*(.*)$", line)
        if not match:
            continue
        key = next((s for s in match.groups()[:3] if s is not None), "")
        if key not in FIELDS:
            continue
        if key in values:
            raise ControllerConfigError("控制器配置字段重复")
        values[key] = _scalar(match.group(4))
        selected_scalar = True
    return values


def valid_secret(value: str) -> bool:
    return isinstance(value, str) and not any(ord(ch) < 32 or ord(ch) == 127 or ord(ch) > 255 for ch in value)


def local_tcp_base(value: str) -> Optional[str]:
    try:
        parsed = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
        if (parsed.scheme not in ("http", "https") or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/") or not parsed.port):
            return None
        host = parsed.hostname or ""
        if "%" in host:
            return None
        address = ipaddress.ip_address(host)
        if address.is_unspecified:
            address = ipaddress.ip_address("::1" if address.version == 6 else "127.0.0.1")
        if not address.is_loopback:
            return None
        host = "[{}]".format(address) if address.version == 6 else str(address)
        return "{}://{}:{}".format(parsed.scheme, host, parsed.port)
    except (ValueError, TypeError):
        return None


def _local_pipe(value: str) -> Optional[str]:
    prefix = "\\\\.\\pipe\\"
    if not value.lower().startswith(prefix):
        return None
    name = value[len(prefix):]
    if not name or any(c in name for c in "\\/:?#") or any(ord(c) < 32 for c in name):
        return None
    return "pipe://" + name


def canonical_explicit(value: str) -> str:
    """Canonical comparison only. Never turn a remote explicit target into local."""
    if value.startswith("pipe://"):
        pipe = _local_pipe(value[7:])
        return pipe if pipe else value.rstrip("/")
    local = local_tcp_base(value)
    return local if local else value.rstrip("/")


def discover_targets(platform: Optional[str] = None, config_root: Optional[str] = None) -> Tuple[List[ControllerTarget], List[str]]:
    platform = platform or sys.platform
    targets = []
    warnings = []
    # The service socket belongs to the machine, not to a chosen config root, so
    # it is only a fallback for the implicit default root. An explicit or
    # environment-selected root must never silently reach another root's service.
    default_root = not (config_root if config_root is not None else os.environ.get(ROOT_ENV))
    paths=(config_paths(platform=platform) if config_root is None else
           config_paths(platform=platform,config_root=config_root))
    for runtime in paths:
        chosen = runtime
        try:
            if not runtime.exists():
                if (os.environ.get(ROOT_ENV) if config_root is None else config_root):
                    warnings.append('自定义配置目录已失效；不会使用其他目录')
                    continue
                chosen = runtime.with_name("config.yaml")
                if not chosen.exists():
                    continue
            with chosen.open("rb") as stream:
                data = stream.read(MAX_CONFIG_BYTES + 1)
            if len(data) > MAX_CONFIG_BYTES:
                raise ControllerConfigError("控制器配置过大")
            fields = parse_controller_fields(data.decode("utf-8-sig"))
            secret = fields.get("secret", "")
            if not valid_secret(secret):
                raise ControllerConfigError("控制器密钥不适用于 HTTP header，请手动检查本机配置")
            bases = []
            if platform == "win32":
                pipe = _local_pipe(fields.get("external-controller-pipe", ""))
                if pipe:
                    bases.append(pipe)
            else:
                unix = fields.get("external-controller-unix", "")
                if unix and posixpath.isabs(unix):
                    bases.append("unix://" + unix)
            tcp = local_tcp_base(fields.get("external-controller", ""))
            if tcp:
                bases.append(tcp)
            if default_root and platform == "darwin":
                service = service_controller_socket()
                if service is not None and _owned_socket(service):
                    service_base = "unix://" + str(service)
                    if service_base not in bases:
                        bases.append(service_base)
            if not bases:
                warnings.append("本机 Clash Verge 配置未声明可用的本地控制器地址")
            for base in bases:
                remember_secret(secret)
                targets.append(ControllerTarget(base, secret, "verge_config"))
        except (OSError, UnicodeError, ControllerConfigError):
            warnings.append("本机 Clash Verge 控制器配置不可读或格式不支持，请检查配置或设置 MIHOMO_SECRET")
    return targets, warnings
