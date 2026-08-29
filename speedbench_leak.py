#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local, best-effort environment leak audit helpers.

This module deliberately contains no network client.  The browser gathers
WebRTC candidates and the localhost Web UI submits the small, normalized
candidate list here for evaluation.  DNS is intentionally a guided audit;
there is no trustworthy way to infer the resolver that answered a browser
query by reading the operating system configuration.

The functions are stdlib-only and are also useful from unit tests without
starting a web server or contacting a third party.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


PUBLIC_LEAK_TYPES = frozenset(("host", "srflx", "prflx"))
KNOWN_CANDIDATE_TYPES = frozenset(("host", "srflx", "prflx", "relay"))


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _candidate_line(value: Any) -> str:
    """Return an ICE candidate a-line without the optional ``candidate:`` prefix."""
    line = _text(value)
    if line.lower().startswith("a="):
        line = line[2:]
    if line.lower().startswith("candidate:"):
        line = line[len("candidate:"):]
    return line.strip()


def _parse_line(line: str) -> Dict[str, Any]:
    """Parse the RFC 8445 candidate grammar used by browser a-lines.

    Browser implementations have used both ``raddr`` and ``raddr``-free
    forms.  We only need the foundation/transport/address/port/type fields;
    unknown extensions are ignored.  A malformed line is returned as an
    empty mapping rather than being treated as a leak.
    """
    source = _candidate_line(line)
    if not source or source.lower() == "end-of-candidates":
        return {}
    parts = source.split()
    # foundation component transport priority address port typ type
    if len(parts) < 6:
        return {}
    try:
        port = int(parts[5])
    except (TypeError, ValueError):
        port = None
    result: Dict[str, Any] = {
        "address": parts[4],
        "port": port,
        "protocol": parts[2].lower(),
        "foundation": parts[0],
    }
    for index in range(5, len(parts) - 1):
        if parts[index].lower() == "typ":
            result["type"] = parts[index + 1].lower()
            break
    # A few engines expose a host candidate with no explicit typ extension.
    if "type" not in result and parts[3].lower().endswith(".local"):
        result["type"] = "host"
    return result


@dataclass
class IceCandidate:
    """Safe, normalized subset of a browser ICE candidate."""

    type: str
    address: str
    port: Optional[int] = None
    protocol: Optional[str] = None
    source: str = "browser"
    mdns: bool = False
    raw_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "address": self.address,
            "port": self.port,
            "protocol": self.protocol,
            "source": self.source,
            "mdns": self.mdns,
        }


def normalize_candidate(value: Any) -> Optional[IceCandidate]:
    """Normalize a browser candidate object or standard a-line.

    ``RTCIceCandidate.type`` and ``RTCIceCandidate.address`` are preferred.
    The a-line is only a compatibility fallback for older browser wrappers.
    ``.local`` mDNS names are retained as ``mdns=True`` but are never resolved
    or submitted to an external service.
    """
    if isinstance(value, IceCandidate):
        return value
    mapping: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
    raw_line = mapping.get("candidate") if mapping else value
    parsed = _parse_line(raw_line)
    candidate_type = _text(
        mapping.get("type") or mapping.get("candidateType") or parsed.get("type")
    ).lower()
    address = _text(
        mapping.get("address") or mapping.get("ip") or parsed.get("address")
    )
    if not candidate_type or candidate_type not in KNOWN_CANDIDATE_TYPES:
        return None
    if not address:
        return None
    # Strip a URI-style IPv6 zone identifier; it is not part of an IP address
    # and must not make a valid candidate look malformed.
    if ":" in address and "%" in address:
        address = address.split("%", 1)[0]
    mdns = address.lower().endswith(".local")
    port_value = mapping.get("port", parsed.get("port"))
    try:
        port = int(port_value) if port_value is not None else None
    except (TypeError, ValueError):
        port = None
    protocol = _text(mapping.get("protocol") or parsed.get("protocol")).lower() or None
    return IceCandidate(
        type=candidate_type,
        address=address,
        port=port,
        protocol=protocol,
        source=_text(mapping.get("source")) or "browser",
        mdns=mdns,
        raw_type=candidate_type,
    )


def normalize_candidates(values: Any) -> List[IceCandidate]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, Mapping, IceCandidate)):
        values = [values]
    out: List[IceCandidate] = []
    seen = set()
    for value in values:
        candidate = normalize_candidate(value)
        if candidate is None:
            continue
        key = (candidate.type, candidate.address, candidate.port, candidate.protocol)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def parse_candidate(value: Any) -> Optional[IceCandidate]:
    """Compatibility alias used by callers and tests."""
    return normalize_candidate(value)


def is_public_address(value: Any) -> bool:
    """Return True only for a globally routable literal IP address.

    Private, loopback, link-local, multicast, reserved, unspecified and
    documentation addresses are not public leaks.  mDNS names are never
    resolved and therefore return False.
    """
    address = _text(value)
    if not address or address.lower().endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(ip.is_global)


def _ip_version(value: str) -> int:
    try:
        return ipaddress.ip_address(value).version
    except ValueError:
        return 0


def _lookup_basic(address: str, lookup: Any) -> Mapping[str, Any]:
    if lookup is None:
        return {}
    try:
        value = lookup(address) if callable(lookup) else lookup.get(address, {})
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def _lookup_text(info: Mapping[str, Any]) -> str:
    values = []
    for key in ("country", "country_code", "countryCode", "isp", "org",
                "organization", "asname", "as_name", "asn"):
        if info.get(key) is not None:
            values.append(_text(info.get(key)))
    return " ".join(values).lower()


def _is_china_or_unicom(info: Mapping[str, Any]) -> bool:
    text = _lookup_text(info)
    country = " ".join(_text(info.get(k)) for k in ("country", "country_code", "countryCode")).lower()
    return country.strip() in {"cn", "china", "中国"} or any(
        term in text for term in ("china unicom", "中国联通", "unicom")
    )


@dataclass
class LeakEvaluation:
    """Result of comparing public ICE candidates with browser exits."""

    status: str = "unknown"  # clear, warning, unknown
    status_text: str = "无法确认"
    complete: bool = False
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    public_candidates: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    compared: bool = False
    exit_ipv4: Optional[str] = None
    exit_ipv6: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "clear"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "status_text": self.status_text,
            "complete": self.complete,
            "candidates": list(self.candidates),
            "public_candidates": list(self.public_candidates),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "compared": self.compared,
            "exit_ipv4": self.exit_ipv4,
            "exit_ipv6": self.exit_ipv6,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def _valid_exit(value: Any, version: int) -> Optional[str]:
    value = _text(value)
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if ip.version != version or not ip.is_global:
        return None
    return str(ip)


def evaluate_webrtc(
    candidates: Any,
    exit_ipv4: Any = None,
    exit_ipv6: Any = None,
    basic_lookup: Any = None,
    collection_complete: bool = True,
    collection_error: Any = None,
    policy_blocked: bool = False,
) -> LeakEvaluation:
    """Evaluate normalized or raw WebRTC candidates.

    ``basic_lookup`` is intentionally injectable and is expected to be a
    local/basic lookup only.  It must not be an IP reputation provider.  The
    evaluator never performs network I/O.
    """
    normalized = normalize_candidates(candidates)
    v4 = _valid_exit(exit_ipv4, 4)
    v6 = _valid_exit(exit_ipv6, 6)
    out = LeakEvaluation(
        complete=bool(collection_complete),
        candidates=[item.to_dict() for item in normalized],
        exit_ipv4=v4,
        exit_ipv6=v6,
    )

    if policy_blocked or collection_error:
        out.notes.append("浏览器策略或 STUN 错误，未能完成候选采集")
        out.status_text = "无法确认"
        return out
    mdns_count = 0
    public: List[IceCandidate] = []
    for candidate in normalized:
        if candidate.mdns:
            mdns_count += 1
            continue
        if is_public_address(candidate.address):
            public.append(candidate)
    out.public_candidates = [item.to_dict() for item in public]

    # A collection with no usable public candidate cannot prove either leak
    # or safety.  This covers mDNS-only and STUN-failed browser behavior.
    if not normalized or (not public and mdns_count == len(normalized)):
        out.notes.append("没有可比较的公网 ICE candidate（可能为 mDNS 或浏览器隐私策略）")
        out.status_text = "无法确认"
        return out

    warnings: List[str] = []
    compared = False
    for candidate in public:
        version = _ip_version(candidate.address)
        # TURN relay addresses are server-side relay endpoints, not the
        # browser's direct interface.  Retain them for display but do not
        # classify them as a direct leak.
        if candidate.type == "relay":
            continue
        lookup_info = _lookup_basic(candidate.address, basic_lookup)
        if _is_china_or_unicom(lookup_info):
            warnings.append(
                "候选暴露中国/中国联通公网地址: " + candidate.address
            )
        if version == 6:
            if v6 is None:
                warnings.append("发现未经代理或无法对应出口的公网 IPv6: " + candidate.address)
                compared = True
            else:
                compared = True
                if candidate.address != v6:
                    warnings.append("发现与当前 IPv6 出口不一致的公网 candidate: " + candidate.address)
        elif version == 4:
            if v4 is None:
                # Without a browser-side exit comparison, it is not safe to
                # call a public candidate clean merely from its type.
                continue
            compared = True
            if candidate.address != v4:
                warnings.append("发现与当前 IPv4 出口不一致的公网 candidate: " + candidate.address)

    out.compared = compared
    out.warnings = list(dict.fromkeys(warnings))
    if out.warnings:
        out.status = "warning"
        out.status_text = "发现潜在泄漏"
        return out
    # All public candidates were relay endpoints, or there was no matching
    # exit family; in both cases a clean verdict would overclaim certainty.
    if not compared:
        out.notes.append("缺少同地址出口信息，无法完成公网 candidate 比较")
        out.status_text = "无法确认"
        return out
    if not out.complete:
        out.notes.append("候选采集未明确完成，不能据此断言无泄漏")
        out.status_text = "无法确认"
        return out
    out.status = "clear"
    out.status_text = "未发现明显泄漏"
    return out


evaluate_candidates = evaluate_webrtc
evaluate_leak = evaluate_webrtc


class DnsLeakProvider:
    """Interface placeholder for a future authoritative DNS observer.

    Public web pages cannot provide a dependable machine-readable observer
    API for this purpose.  The Web UI therefore offers a guided audit and
    never calls these methods automatically.
    """

    name = "guided"

    def start_audit(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "guided", "automated": False}

    def poll_result(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "unavailable", "automated": False}


def make_audit_record(evaluation: LeakEvaluation, dns_status: Optional[str] = None) -> Dict[str, Any]:
    """Create the JSON-safe shape accepted by ``speedbench_db`` adapters."""
    return {
        "exit_ipv4": evaluation.exit_ipv4,
        "exit_ipv6": evaluation.exit_ipv6,
        "webrtc_status": evaluation.status,
        "candidates": list(evaluation.candidates),
        "dns_mode": "guided",
        "dns_status": dns_status,
        "details": {
            "status_text": evaluation.status_text,
            "public_candidates": list(evaluation.public_candidates),
            "warnings": list(evaluation.warnings),
            "notes": list(evaluation.notes),
            "complete": evaluation.complete,
            "compared": evaluation.compared,
        },
    }


__all__ = [
    "PUBLIC_LEAK_TYPES", "KNOWN_CANDIDATE_TYPES", "IceCandidate",
    "LeakEvaluation", "DnsLeakProvider", "normalize_candidate",
    "normalize_candidates", "parse_candidate", "is_public_address",
    "evaluate_webrtc", "evaluate_candidates", "evaluate_leak",
    "make_audit_record",
]
