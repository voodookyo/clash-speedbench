#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provider based IP intelligence for Clash SpeedBench.

This module deliberately has no dependency on the speed test or web server.
It provides a small, standard-library-only boundary that can be used by both
the CLI and the web application:

* providers return the same :class:`ProviderResult` shape;
* a SQLite cache is keyed by ``(provider, ip)``;
* classification is evidence based (``hosting=False`` is never residential);
* the SpeedBench grade is explicitly heuristic and is kept separate from the
  vendor fraud scores.

The request function is injectable.  Tests can therefore supply a transport
without making a real request, and callers can decide whether a provider
request should use the local network or another controlled route.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# Public status values.  Keeping these in one place makes the UI and tests
# robust to a provider being unavailable without coupling the speed test to
# any one provider.
PROVIDER_STATUSES = {
    "ok",
    "cache_hit",
    "key_missing",
    "configuration_incomplete",
    "timeout",
    "rate_limited",
    "quota_unavailable",
    "unsupported_tier",
    "invalid_response",
    "error",
}

IP_CATEGORIES = {
    "residential",
    "residential_proxy",
    "corporate",
    "mobile",
    "datacenter",
    "vpn_proxy",
    "unknown",
}

BASIC_TTL_SECONDS = 7 * 24 * 60 * 60
RISK_TTL_SECONDS = 24 * 60 * 60
DEFAULT_COOLDOWN_SECONDS = 5.0
MAX_COOLDOWN_SECONDS = 60 * 60

IPINFO_TOKEN_ENV = "SPEEDBENCH_IPINFO_TOKEN"
IPQS_KEY_ENV = "SPEEDBENCH_IPQS_KEY"
SCAMALYTICS_USERNAME_ENV = "SPEEDBENCH_SCAMALYTICS_USERNAME"
SCAMALYTICS_KEY_ENV = "SPEEDBENCH_SCAMALYTICS_KEY"
SCAMALYTICS_REGION_ENV = "SPEEDBENCH_SCAMALYTICS_REGION"


def _now() -> float:
    return time.time()


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_or_none(value: Any) -> Optional[bool]:
    """Convert API booleans without treating arbitrary strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1", "on"}:
            return True
        if lowered in {"false", "no", "n", "0", "off"}:
            return False
    return None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _nested(mapping: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    """Return the first value found at one of the supplied nested paths."""
    for path in paths:
        value: Any = mapping
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                value = None
                break
            value = value[key]
        if value is not None:
            return value
    return None


def _safe_json(value: Any) -> Any:
    """Make arbitrary transport data JSON serializable without executing it."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item) for item in value]
    return str(value)


_SECRET_KEY_RE = re.compile(
    r"(?:token|api[_-]?key|access[_-]?key|secret|password|authorization|credential|"
    r"key|username|user[_-]?name)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _sanitize_payload(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact credential-shaped fields and known secret values.

    Provider responses normally do not echo credentials, but doing this at the
    result boundary protects cache/raw JSON if a mock or upstream error does.
    """
    secret_values = [str(item) for item in secrets if item]

    def scrub(item: Any, key: Optional[str] = None) -> Any:
        if key and _SECRET_KEY_RE.search(key):
            return "[redacted]"
        if isinstance(item, Mapping):
            return {str(k): scrub(v, str(k)) for k, v in item.items()}
        if isinstance(item, (list, tuple, set)):
            return [scrub(v) for v in item]
        if isinstance(item, str):
            text = item
            for secret in secret_values:
                if secret:
                    text = text.replace(secret, "[redacted]")
            # A URL is not useful in a persisted raw response and may contain
            # a credential in a query/path segment (IPQS/Scamalytics do).
            return _URL_RE.sub("[redacted-url]", text)
        return _safe_json(item)

    return scrub(_safe_json(value))


def _sanitize_error(error: Any, secrets: Iterable[str] = ()) -> str:
    """Return a short, non-secret error code/message for persistence/UI."""
    text = str(error or "error").replace("\r", " ").replace("\n", " ")
    text = _URL_RE.sub("redacted-url", text)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "redacted")
    # Avoid accidentally persisting a long server response or stack trace.
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180] or "error"


def _header_value(headers: Mapping[str, Any], name: str) -> Optional[str]:
    """Look up an HTTP header case-insensitively without retaining headers."""
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return _clean_text(value)
    return None


def _header_seconds(headers: Mapping[str, Any], name: str) -> Optional[float]:
    value = _header_value(headers, name)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return 0.0
    return min(seconds, float(MAX_COOLDOWN_SECONDS))


def _valid_ip(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def ip_version(value: Any) -> int:
    try:
        return ipaddress.ip_address(str(value)).version
    except ValueError:
        return 0


@dataclass
class ProviderResult:
    """Normalized response from one provider.

    ``raw`` and ``normalized`` are always serialized through the redaction
    boundary before they are returned or cached.  ``error`` is intentionally a
    short status detail, never a request URL.
    """

    provider: str
    ip: str
    status: str
    fetched_at: float = field(default_factory=_now)
    expires_at: Optional[float] = None
    raw: Any = field(default_factory=dict)
    normalized: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    http_status: Optional[int] = None

    def __post_init__(self) -> None:
        if self.status not in PROVIDER_STATUSES:
            self.status = "error"
        self.raw = _sanitize_payload(self.raw)
        self.normalized = _sanitize_payload(self.normalized)
        if not isinstance(self.normalized, dict):
            self.normalized = {}
        if self.error:
            self.error = _sanitize_error(self.error)

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "cache_hit"}

    @property
    def cacheable(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "ip": self.ip,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "raw": _sanitize_payload(self.raw),
            "normalized": _sanitize_payload(self.normalized),
            "error": _sanitize_error(self.error) if self.error else None,
            "http_status": self.http_status,
        }

    as_dict = to_dict

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProviderResult":
        return cls(
            provider=str(data.get("provider", "")),
            ip=str(data.get("ip", "")),
            status=str(data.get("status", "error")),
            fetched_at=float(data.get("fetched_at") or _now()),
            expires_at=_float_or_none(data.get("expires_at")),
            raw=data.get("raw", {}),
            normalized=data.get("normalized", {}),
            error=data.get("error"),
            http_status=_int_or_none(data.get("http_status")),
        )


@dataclass
class IpClassification:
    category: str = "unknown"
    confidence: int = 0
    evidence: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.category not in IP_CATEGORIES:
            self.category = "unknown"
        self.confidence = max(0, min(100, int(self.confidence)))
        self.evidence = [str(item) for item in self.evidence]
        self.conflicts = [str(item) for item in self.conflicts]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "conflicts": list(self.conflicts),
        }

    as_dict = to_dict


@dataclass
class IpIntelligence:
    """Aggregated and serializable intelligence for one exit IP."""

    ip: str = ""
    ip_version: int = 0
    country: Optional[str] = None
    asn: Optional[str] = None
    as_name: Optional[str] = None
    isp: Optional[str] = None
    organization: Optional[str] = None
    hosting: Optional[bool] = None
    proxy: Optional[bool] = None
    vpn: Optional[bool] = None
    tor: Optional[bool] = None
    mobile: Optional[bool] = None
    residential_proxy: Optional[bool] = None
    connection_type: Optional[str] = None
    ipqs_fraud_score: Optional[int] = None
    ipqs_recent_abuse: Optional[bool] = None
    ipqs_abuse_velocity: Optional[str] = None
    scamalytics_score: Optional[int] = None
    scamalytics_risk: Optional[str] = None
    scamalytics_datacenter: Optional[bool] = None
    scamalytics_blacklisted: Optional[bool] = None
    classification: IpClassification = field(default_factory=IpClassification)
    ip_quality_score: Optional[float] = None
    ip_grade: Optional[str] = None
    provider_status: Dict[str, str] = field(default_factory=dict)
    provider_data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    provider_results: Dict[str, ProviderResult] = field(
        default_factory=dict, repr=False, compare=False
    )

    # Compatibility aliases used by the existing ip-api-shaped result model.
    @property
    def asname(self) -> Optional[str]:
        return self.as_name

    @property
    def org(self) -> Optional[str]:
        return self.organization

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "ip": self.ip,
            "ip_version": self.ip_version,
            "country": self.country,
            "asn": self.asn,
            "as_name": self.as_name,
            "isp": self.isp,
            "organization": self.organization,
            "hosting": self.hosting,
            "proxy": self.proxy,
            "vpn": self.vpn,
            "tor": self.tor,
            "mobile": self.mobile,
            "residential_proxy": self.residential_proxy,
            "connection_type": self.connection_type,
            "ipqs_fraud_score": self.ipqs_fraud_score,
            "ipqs_recent_abuse": self.ipqs_recent_abuse,
            "ipqs_abuse_velocity": self.ipqs_abuse_velocity,
            "scamalytics_score": self.scamalytics_score,
            "scamalytics_risk": self.scamalytics_risk,
            "scamalytics_datacenter": self.scamalytics_datacenter,
            "scamalytics_blacklisted": self.scamalytics_blacklisted,
            "classification": self.classification.to_dict(),
            "ip_quality_score": self.ip_quality_score,
            "ip_grade": self.ip_grade,
            "provider_status": dict(self.provider_status),
            "provider_data": _sanitize_payload(self.provider_data),
        }
        return _sanitize_payload(data)

    as_dict = to_dict


@dataclass(frozen=True)
class ProviderConfig:
    """Credential/configuration snapshot loaded from environment or mapping."""

    ipinfo_token: Optional[str] = None
    ipqs_key: Optional[str] = None
    scamalytics_username: Optional[str] = None
    scamalytics_key: Optional[str] = None
    scamalytics_region: Optional[str] = None

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "ProviderConfig":
        env = os.environ if environ is None else environ
        region = _clean_text(env.get(SCAMALYTICS_REGION_ENV))
        return cls(
            ipinfo_token=_clean_text(env.get(IPINFO_TOKEN_ENV)),
            ipqs_key=_clean_text(env.get(IPQS_KEY_ENV)),
            scamalytics_username=_clean_text(env.get(SCAMALYTICS_USERNAME_ENV)),
            scamalytics_key=_clean_text(env.get(SCAMALYTICS_KEY_ENV)),
            scamalytics_region=region.lower() if region else None,
        )


def load_provider_config(environ: Optional[Mapping[str, str]] = None) -> ProviderConfig:
    return ProviderConfig.from_env(environ)


provider_config_from_env = load_provider_config


@dataclass
class _TransportResponse:
    status_code: int
    body: Any
    headers: Mapping[str, Any] = field(default_factory=dict)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward credential-bearing provider URLs to another host."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        return None


class _ProviderFailure(Exception):
    def __init__(self, status: str, detail: str = "error", http_status: Optional[int] = None):
        super().__init__(detail)
        self.status = status if status in PROVIDER_STATUSES else "error"
        self.detail = detail
        self.http_status = http_status


def _decode_body(body: Any, secrets: Iterable[str] = ()) -> Any:
    if isinstance(body, (Mapping, list)):
        return body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            raise _ProviderFailure("invalid_response", "invalid_json")
    # Support simple mock response objects exposing json().
    json_method = getattr(body, "json", None)
    if callable(json_method):
        try:
            return json_method()
        except Exception as exc:
            raise _ProviderFailure("invalid_response", _sanitize_error(exc, secrets))
    return body


def _coerce_transport_response(response: Any) -> _TransportResponse:
    if isinstance(response, _TransportResponse):
        return response
    if isinstance(response, tuple) and len(response) >= 2:
        return _TransportResponse(int(response[0]), response[1],
                                  response[2] if len(response) >= 3 else {})
    if isinstance(response, Mapping) or isinstance(response, list) or isinstance(response, str):
        return _TransportResponse(200, response)
    status = getattr(response, "status_code", getattr(response, "status", 200))
    body = getattr(response, "body", None)
    if body is None and callable(getattr(response, "json", None)):
        body = response
    if body is None and callable(getattr(response, "read", None)):
        body = response.read()
    headers = getattr(response, "headers", {}) or {}
    return _TransportResponse(int(status), body, headers)


def _urllib_transport(url: str, timeout: float = 8.0,
                      headers: Optional[Mapping[str, str]] = None) -> _TransportResponse:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Clash-SpeedBench/ip-intel" , **dict(headers or {})},
        method="GET",
    )
    try:
        # Passing an explicit empty ProxyHandler is important: build_opener's
        # default ProxyHandler otherwise inherits HTTP(S)_PROXY from the
        # environment.  Provider credentials must never be sent through a
        # tested node or an ambient proxy.
        opener = urllib.request.build_opener(
            _NoRedirectHandler, urllib.request.ProxyHandler({})
        )
        with opener.open(request, timeout=timeout) as response:
            return _TransportResponse(int(response.getcode()), response.read(), response.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return _TransportResponse(int(exc.code), body, getattr(exc, "headers", {}))


Transport = Callable[..., Any]


class IpIntelProvider:
    """Small provider interface; subclasses only parse their official API."""

    name = "provider"
    ttl_seconds = RISK_TTL_SECONDS

    def __init__(self, transport: Optional[Transport] = None,
                 timeout: float = 8.0,
                 clock: Callable[[], float] = _now) -> None:
        self.transport = transport or _urllib_transport
        self.timeout = max(0.1, float(timeout))
        self._clock = clock
        self._secrets: Tuple[str, ...] = ()
        self._cooldown_lock = threading.RLock()
        self._cooldown_until = 0.0

    def configured_status(self) -> str:
        return "ok"

    def _url(self, ip: str) -> str:
        raise NotImplementedError

    def _parse(self, ip: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def cooldown_remaining(self, now: Optional[float] = None) -> float:
        """Return provider-wide cooldown seconds without exposing credentials."""
        current = self._clock() if now is None else float(now)
        with self._cooldown_lock:
            return max(0.0, float(self._cooldown_until) - current)

    def _set_cooldown(self, seconds: Optional[float]) -> None:
        if seconds is None:
            return
        try:
            duration = max(0.0, min(float(seconds), float(MAX_COOLDOWN_SECONDS)))
        except (TypeError, ValueError):
            return
        until = self._clock() + duration
        with self._cooldown_lock:
            # Do not shorten an existing pause while concurrent responses are
            # unwinding.
            self._cooldown_until = max(float(self._cooldown_until), until)

    def _set_rate_limit_cooldown(self, headers: Mapping[str, Any]) -> None:
        seconds = _header_seconds(headers, "Retry-After")
        if seconds is None:
            seconds = _header_seconds(headers, "X-Ttl")
        if seconds is None:
            seconds = DEFAULT_COOLDOWN_SECONDS
        self._set_cooldown(seconds)

    def _set_ip_api_cooldown(self, headers: Mapping[str, Any]) -> None:
        # ip-api communicates remaining quota through X-Rl/X-Ttl.  Header
        # names are case-insensitive, and only the explicit zero condition
        # should pause an otherwise successful response.
        remaining = _header_value(headers, "X-Rl")
        ttl = _header_seconds(headers, "X-Ttl")
        if remaining is None or ttl is None:
            return
        try:
            exhausted = int(float(remaining)) == 0
        except (TypeError, ValueError):
            exhausted = False
        if exhausted:
            self._set_cooldown(ttl)

    def _cooldown_result(self, ip: str) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            ip=ip,
            status="rate_limited",
            error="provider_cooldown",
            http_status=429,
        )

    def _request(self, url: str) -> _TransportResponse:
        try:
            # The common keyword form is convenient for test fakes.  The two
            # fallbacks keep compatibility with tiny ``lambda url`` mocks.
            try:
                response = self.transport(url, timeout=self.timeout,
                                           headers={"Accept": "application/json"})
            except TypeError:
                try:
                    response = self.transport(url, self.timeout,
                                              {"Accept": "application/json"})
                except TypeError:
                    response = self.transport(url)
            return _coerce_transport_response(response)
        except _ProviderFailure:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise _ProviderFailure("timeout", _sanitize_error(exc, self._secrets))
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise _ProviderFailure("timeout", "timeout")
            raise _ProviderFailure("error", _sanitize_error(reason, self._secrets))
        except Exception as exc:
            # subprocess.TimeoutExpired is deliberately handled by class name
            # so importing subprocess is not needed in this dependency-free
            # core module.
            if exc.__class__.__name__ in {"TimeoutExpired", "ReadTimeout"}:
                raise _ProviderFailure("timeout", "timeout")
            raise _ProviderFailure("error", _sanitize_error(exc, self._secrets), None)

    def _failure(self, ip: str, status: str, detail: str = "error",
                 http_status: Optional[int] = None, raw: Any = None) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            ip=ip,
            status=status,
            raw=_sanitize_payload(raw or {}, self._secrets),
            normalized={},
            error=_sanitize_error(detail, self._secrets),
            http_status=http_status,
        )

    def query(self, ip: str) -> ProviderResult:
        normalized_ip = _valid_ip(ip)
        if not normalized_ip:
            return self._failure(str(ip or ""), "invalid_response", "invalid_ip")
        if self.cooldown_remaining() > 0:
            return self._cooldown_result(normalized_ip)
        config_status = self.configured_status()
        if config_status != "ok":
            return self._failure(normalized_ip, config_status, config_status)
        try:
            response = self._request(self._url(normalized_ip))
            # A rate-limit/auth/quota response is often plain text or HTML.
            # Classify it from the HTTP status before requiring a JSON body;
            # only successful 2xx responses need a strict JSON Mapping.
            if response.status_code in (429, 402, 403, 401, 407):
                if response.status_code == 429:
                    self._set_rate_limit_cooldown(response.headers)
                try:
                    error_payload = _decode_body(response.body, self._secrets)
                except _ProviderFailure:
                    error_payload = {}
                if response.status_code == 429:
                    return self._failure(normalized_ip, "rate_limited", "rate_limited",
                                         response.status_code, error_payload)
                if response.status_code in (402, 403):
                    return self._failure(normalized_ip, "quota_unavailable", "quota_unavailable",
                                         response.status_code, error_payload)
                return self._failure(normalized_ip, "key_missing", "authentication_failed",
                                     response.status_code, error_payload)
            if response.status_code < 200 or response.status_code >= 300:
                try:
                    error_payload = _decode_body(response.body, self._secrets)
                except _ProviderFailure:
                    error_payload = {}
                return self._failure(normalized_ip, "error", "http_error",
                                     response.status_code, error_payload)
            payload = _decode_body(response.body, self._secrets)
            if not isinstance(payload, Mapping):
                return self._failure(normalized_ip, "invalid_response", "invalid_json",
                                     response.status_code, payload)
            normalized = self._parse(normalized_ip, payload)
            if not isinstance(normalized, Mapping):
                return self._failure(normalized_ip, "invalid_response", "invalid_fields",
                                     response.status_code, payload)
            result = ProviderResult(
                provider=self.name,
                ip=normalized_ip,
                status="ok",
                raw=_sanitize_payload(payload, self._secrets),
                normalized=_sanitize_payload(dict(normalized), self._secrets),
                http_status=response.status_code,
            )
            self._set_ip_api_cooldown(response.headers)
            return result
        except _ProviderFailure as exc:
            if exc.status == "rate_limited":
                self._set_cooldown(DEFAULT_COOLDOWN_SECONDS)
            return self._failure(normalized_ip, exc.status, exc.detail,
                                 exc.http_status, {})
        except (TypeError, ValueError, KeyError) as exc:
            return self._failure(normalized_ip, "invalid_response", _sanitize_error(exc))
        except Exception as exc:
            return self._failure(normalized_ip, "error", _sanitize_error(exc))


class IpApiProvider(IpIntelProvider):
    name = "ip-api"
    ttl_seconds = BASIC_TTL_SECONDS
    endpoint = "http://ip-api.com/json/{ip}"

    def _url(self, ip: str) -> str:
        fields = (
            "status,message,query,country,countryCode,regionName,city,isp,org,as,asname,"
            "mobile,proxy,hosting"
        )
        return self.endpoint.format(ip=urllib.parse.quote(ip, safe="")) + "?fields=" + fields

    def _parse(self, ip: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if str(payload.get("status", "")).lower() != "success":
            raise _ProviderFailure("invalid_response", "provider_rejected")
        return {
            "ip": _clean_text(payload.get("query")) or ip,
            "country": _clean_text(payload.get("country")),
            "country_code": _clean_text(payload.get("countryCode")),
            "region": _clean_text(payload.get("regionName")),
            "city": _clean_text(payload.get("city")),
            "isp": _clean_text(payload.get("isp")),
            "organization": _clean_text(payload.get("org")),
            "asn": _clean_text(payload.get("as")),
            "as_name": _clean_text(payload.get("asname")),
            "mobile": _bool_or_none(payload.get("mobile")),
            "proxy": _bool_or_none(payload.get("proxy")),
            "hosting": _bool_or_none(payload.get("hosting")),
        }


class IpInfoProvider(IpIntelProvider):
    name = "ipinfo"
    ttl_seconds = RISK_TTL_SECONDS
    endpoint = "https://api.ipinfo.io/lookup/{ip}"

    def __init__(self, token: Optional[str] = None,
                 transport: Optional[Transport] = None,
                 timeout: float = 8.0,
                 clock: Callable[[], float] = _now) -> None:
        super().__init__(transport=transport, timeout=timeout, clock=clock)
        self.token = _clean_text(token)
        self._secrets = (self.token or "",)

    def configured_status(self) -> str:
        return "ok" if self.token else "key_missing"

    def _url(self, ip: str) -> str:
        # IPinfo supports token query authentication for the official API.
        return self.endpoint.format(ip=urllib.parse.quote(ip, safe="")) + "?token=" + urllib.parse.quote(self.token or "", safe="")

    def _parse(self, ip: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not any(key in payload for key in (
            "ip", "asn", "geo", "company", "anonymous", "privacy",
            "is_hosting", "is_mobile", "country", "country_code",
        )):
            raise _ProviderFailure("invalid_response", "missing_fields")
        asn = payload.get("asn") if isinstance(payload.get("asn"), Mapping) else {}
        geo = payload.get("geo") if isinstance(payload.get("geo"), Mapping) else {}
        company = payload.get("company") if isinstance(payload.get("company"), Mapping) else {}
        anonymous = payload.get("anonymous") if isinstance(payload.get("anonymous"), Mapping) else {}
        privacy = payload.get("privacy") if isinstance(payload.get("privacy"), Mapping) else {}
        asn_owner = _clean_text(_first(asn, "name"))

        # Max responses expose anonymous.is_* and top-level is_hosting/is_mobile;
        # older official privacy responses use privacy.*.  Missing fields stay
        # None so an unavailable tier is never interpreted as False.
        return {
            "ip": _clean_text(payload.get("ip")) or ip,
            "country": _clean_text(_first(geo, "country", "country_code"))
            or _clean_text(payload.get("country")),
            "country_code": _clean_text(_first(geo, "country_code", "countryCode"))
            or _clean_text(payload.get("country_code")),
            "asn": _clean_text(_first(asn, "asn")) or _clean_text(payload.get("asn"))
            if not isinstance(payload.get("asn"), Mapping) else _clean_text(asn.get("asn")),
            "as_name": _clean_text(_first(asn, "name", "as_name"))
            or _clean_text(payload.get("as_name"))
            or _clean_text(payload.get("asname")),
            "isp": _clean_text(_first(company, "name"))
            or _clean_text(payload.get("isp"))
            or asn_owner,
            "organization": _clean_text(_first(company, "name"))
            or _clean_text(payload.get("organization"))
            or _clean_text(payload.get("org"))
            or asn_owner,
            "asn_type": _clean_text(_first(asn, "type")),
            "hosting": _bool_or_none(_first(payload, "is_hosting", "hosting"))
            if _first(payload, "is_hosting", "hosting") is not None
            else _bool_or_none(_first(privacy, "hosting", "is_hosting")),
            "mobile": _bool_or_none(_first(payload, "is_mobile", "mobile"))
            if _first(payload, "is_mobile", "mobile") is not None
            else _bool_or_none(_first(privacy, "mobile", "is_mobile")),
            "proxy": _bool_or_none(_first(anonymous, "is_proxy", "proxy"))
            if _first(anonymous, "is_proxy", "proxy") is not None
            else _bool_or_none(_first(privacy, "proxy", "is_proxy")),
            "vpn": _bool_or_none(_first(anonymous, "is_vpn", "vpn"))
            if _first(anonymous, "is_vpn", "vpn") is not None
            else _bool_or_none(_first(privacy, "vpn", "is_vpn")),
            "tor": _bool_or_none(_first(anonymous, "is_tor", "tor"))
            if _first(anonymous, "is_tor", "tor") is not None
            else _bool_or_none(_first(privacy, "tor", "is_tor")),
            "relay": _bool_or_none(_first(anonymous, "is_relay", "relay"))
            if _first(anonymous, "is_relay", "relay") is not None
            else _bool_or_none(_first(privacy, "relay", "is_relay")),
            "residential_proxy": _bool_or_none(
                _first(anonymous, "is_res_proxy", "residential_proxy", "is_residential_proxy")
            ) if _first(anonymous, "is_res_proxy", "residential_proxy", "is_residential_proxy") is not None
            else _bool_or_none(_first(privacy, "residential_proxy", "is_res_proxy")),
            "is_anonymous": _bool_or_none(payload.get("is_anonymous")),
        }


class IpqsProvider(IpIntelProvider):
    name = "ipqs"
    ttl_seconds = RISK_TTL_SECONDS
    endpoint = "https://ipqualityscore.com/api/json/ip/{key}/{ip}"

    def __init__(self, key: Optional[str] = None,
                 transport: Optional[Transport] = None,
                 timeout: float = 8.0,
                 clock: Callable[[], float] = _now) -> None:
        super().__init__(transport=transport, timeout=timeout, clock=clock)
        self.key = _clean_text(key)
        self._secrets = (self.key or "",)

    def configured_status(self) -> str:
        return "ok" if self.key else "key_missing"

    def _url(self, ip: str) -> str:
        return self.endpoint.format(
            key=urllib.parse.quote(self.key or "", safe=""),
            ip=urllib.parse.quote(ip, safe=""),
        )

    def _parse(self, ip: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        success = _bool_or_none(payload.get("success"))
        if success is False:
            message = str(payload.get("message", "provider_rejected"))
            lowered = message.lower()
            if any(word in lowered for word in ("quota", "credit", "limit", "balance")):
                raise _ProviderFailure("quota_unavailable", "quota_unavailable")
            if "rate" in lowered or "too many" in lowered:
                raise _ProviderFailure("rate_limited", "rate_limited")
            raise _ProviderFailure("error", "provider_rejected")
        if success is None and not any(key in payload for key in (
            "IP", "ip", "ISP", "organization", "ASN", "connection_type",
            "proxy", "vpn", "tor", "fraud_score", "recent_abuse",
        )):
            raise _ProviderFailure("invalid_response", "missing_fields")
        return {
            "ip": _clean_text(payload.get("IP")) or _clean_text(payload.get("ip")) or ip,
            "isp": _clean_text(payload.get("ISP")),
            "organization": _clean_text(payload.get("organization")),
            "asn": _clean_text(payload.get("ASN")),
            "connection_type": _clean_text(payload.get("connection_type")),
            "proxy": _bool_or_none(payload.get("proxy")),
            "vpn": _bool_or_none(payload.get("vpn")),
            "tor": _bool_or_none(payload.get("tor")),
            "mobile": _bool_or_none(payload.get("mobile")),
            "fraud_score": _int_or_none(payload.get("fraud_score")),
            "recent_abuse": _bool_or_none(payload.get("recent_abuse")),
            "abuse_velocity": _clean_text(payload.get("abuse_velocity")),
            "bot_status": _bool_or_none(payload.get("bot_status")),
            "frequent_abuser": _bool_or_none(payload.get("frequent_abuser")),
        }


class ScamalyticsProvider(IpIntelProvider):
    name = "scamalytics"
    ttl_seconds = RISK_TTL_SECONDS
    endpoints = {
        "us": "https://api11.scamalytics.com/v3/{username}",
        "eu": "https://api12.scamalytics.com/v3/{username}",
    }

    def __init__(self, username: Optional[str] = None,
                 key: Optional[str] = None,
                 region: Optional[str] = None,
                 transport: Optional[Transport] = None,
                 timeout: float = 8.0,
                 clock: Callable[[], float] = _now) -> None:
        super().__init__(transport=transport, timeout=timeout, clock=clock)
        self.username = _clean_text(username)
        self.key = _clean_text(key)
        self.region = _clean_text(region).lower() if _clean_text(region) else None
        self._secrets = tuple(item for item in (self.username, self.key) if item)

    def configured_status(self) -> str:
        # The account is tied to one API node.  Never silently choose a node.
        if not self.region:
            return "configuration_incomplete"
        if self.region not in self.endpoints:
            return "configuration_incomplete"
        if not self.username or not self.key:
            return "key_missing"
        return "ok"

    def _url(self, ip: str) -> str:
        base = self.endpoints[self.region].format(
            username=urllib.parse.quote(self.username or "", safe="")
        )
        return base + "?key=" + urllib.parse.quote(self.key or "", safe="") + "&ip=" + urllib.parse.quote(ip, safe="")

    def _parse(self, ip: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        # v3 nests the risk response and proxy flags under ``scamalytics``.
        # The top-level fallbacks keep compatibility with older fixtures and
        # minor response envelope changes, without scraping.
        risk = payload.get("scamalytics") if isinstance(payload.get("scamalytics"), Mapping) else payload
        proxy = {}
        if isinstance(risk, Mapping) and isinstance(risk.get("scamalytics_proxy"), Mapping):
            proxy = risk.get("scamalytics_proxy")
        elif isinstance(payload.get("scamalytics_proxy"), Mapping):
            proxy = payload.get("scamalytics_proxy")
        external = {}
        if isinstance(risk, Mapping) and isinstance(risk.get("external_datasources"), Mapping):
            external = risk.get("external_datasources")
        elif isinstance(payload.get("external_datasources"), Mapping):
            external = payload.get("external_datasources")
        response_status = _first(risk, "status") or payload.get("status")
        if str(response_status or "").lower() in {"error", "failed", "failure"}:
            message = (
                _first(risk, "message", "error")
                or _first(payload, "message", "error")
                or "provider_rejected"
            )
            lowered = str(message).lower()
            if any(word in lowered for word in ("credit", "quota", "limit", "balance")):
                raise _ProviderFailure("quota_unavailable", "quota_unavailable")
            raise _ProviderFailure("error", "provider_rejected")
        if not any(key in payload for key in (
            "status", "scamalytics", "scamalytics_proxy", "external_datasources",
            "scamalytics_score", "scamalytics_risk", "fraud_score", "score",
        )):
            raise _ProviderFailure("invalid_response", "missing_fields")

        score = _first(risk, "scamalytics_score", "fraud_score", "score")
        risk_level = _first(risk, "scamalytics_risk", "risk")
        datacenter = _first(proxy, "is_datacenter", "datacenter")
        if datacenter is None:
            datacenter = _first(risk, "is_datacenter", "datacenter")
        vpn = _first(proxy, "is_vpn", "vpn")
        if vpn is None:
            vpn = _first(risk, "is_vpn", "vpn")
        tor = _first(proxy, "is_tor", "tor")
        if tor is None:
            tor = _first(risk, "is_tor", "tor")
        server = _first(proxy, "is_server", "server")
        if server is None:
            server = _first(risk, "is_server", "server")
        is_proxy = _first(proxy, "is_proxy", "proxy")
        if is_proxy is None:
            is_proxy = _first(risk, "is_proxy", "proxy")
        blacklist = _first(
            proxy,
            "is_blacklisted_external",
            "blacklisted_external",
            "blacklisted",
        )
        if blacklist is None:
            blacklist = _first(
                risk,
                "is_blacklisted_external",
                "blacklisted_external",
                "blacklisted",
            )
        if blacklist is None:
            blacklist = _first(
                external,
                "is_blacklisted_external",
                "blacklisted_external",
                "blacklisted",
            )
        return {
            "ip": _clean_text(_first(risk, "ip")) or ip,
            "scamalytics_score": _int_or_none(score),
            "scamalytics_risk": _clean_text(risk_level),
            "scamalytics_isp_score": _int_or_none(_first(risk, "scamalytics_isp_score", "isp_score")),
            "scamalytics_isp_risk": _clean_text(_first(risk, "scamalytics_isp_risk", "isp_risk")),
            "datacenter": _bool_or_none(datacenter),
            "vpn": _bool_or_none(vpn),
            "tor": _bool_or_none(tor),
            "proxy": _bool_or_none(is_proxy),
            "server": _bool_or_none(server),
            "blacklisted": _bool_or_none(blacklist),
            "residential_proxy": _bool_or_none(_first(
                proxy, "is_residential_proxy", "residential_proxy"
            )),
        }


def make_default_providers(
    config: Optional[ProviderConfig] = None,
    transport: Optional[Transport] = None,
    timeout: float = 8.0,
) -> List[IpIntelProvider]:
    cfg = config or load_provider_config()
    return [
        IpApiProvider(transport=transport, timeout=timeout),
        IpInfoProvider(token=cfg.ipinfo_token, transport=transport, timeout=timeout),
        IpqsProvider(key=cfg.ipqs_key, transport=transport, timeout=timeout),
        ScamalyticsProvider(
            username=cfg.scamalytics_username,
            key=cfg.scamalytics_key,
            region=cfg.scamalytics_region,
            transport=transport,
            timeout=timeout,
        ),
    ]


class IpIntelCache:
    """SQLite cache with TTL and per-key single-flight de-duplication."""

    TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS ip_intel_cache (
            provider TEXT NOT NULL,
            ip TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            raw_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            PRIMARY KEY (provider, ip)
        )
    """

    def __init__(self, db_path: Any,
                 basic_ttl: int = BASIC_TTL_SECONDS,
                 risk_ttl: int = RISK_TTL_SECONDS,
                 clock: Callable[[], float] = _now,
                 secrets: Optional[Iterable[str]] = None) -> None:
        self.db_path = str(db_path)
        self.basic_ttl = max(1, int(basic_ttl))
        self.risk_ttl = max(1, int(risk_ttl))
        self.clock = clock
        # Passing secrets explicitly is useful for a web process that keeps
        # credentials only in memory.  The environment fallback also protects
        # callers that construct the cache directly; values are never written
        # to SQLite or returned by this module.
        if secrets is None:
            cfg = load_provider_config()
            secrets = (
                cfg.ipinfo_token,
                cfg.ipqs_key,
                cfg.scamalytics_username,
                cfg.scamalytics_key,
            )
        self.secrets = tuple(str(item) for item in secrets if item)
        self._db_lock = threading.RLock()
        self._flight_lock = threading.RLock()
        self._flights: Dict[Tuple[str, str], "_Flight"] = {}
        self._memory_connection: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._memory_connection = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        conn = sqlite3.connect(self.db_path, timeout=30)
        return conn

    def _ensure_table(self) -> None:
        with self._db_lock:
            conn = self._connect()
            try:
                conn.execute(self.TABLE_SQL)
                conn.commit()
            finally:
                if conn is not self._memory_connection:
                    conn.close()

    def ttl_seconds(self, provider: Any) -> int:
        name = getattr(provider, "name", provider)
        return self.basic_ttl if str(name).lower() == "ip-api" else self.risk_ttl

    def get(self, provider: Any, ip: str,
            now: Optional[float] = None) -> Optional[ProviderResult]:
        name = str(getattr(provider, "name", provider))
        normalized_ip = _valid_ip(ip) or str(ip)
        current = self.clock() if now is None else float(now)
        with self._db_lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT fetched_at, expires_at, raw_json, normalized_json "
                    "FROM ip_intel_cache WHERE provider = ? AND ip = ?",
                    (name, normalized_ip),
                ).fetchone()
            finally:
                if conn is not self._memory_connection:
                    conn.close()
        if not row:
            return None
        fetched_at, expires_at, raw_json, normalized_json = row
        if float(expires_at) <= current:
            return None
        try:
            raw = json.loads(raw_json)
            normalized = json.loads(normalized_json)
        except (TypeError, ValueError):
            return None
        return ProviderResult(
            provider=name,
            ip=normalized_ip,
            status="cache_hit",
            fetched_at=float(fetched_at),
            expires_at=float(expires_at),
            raw=raw,
            normalized=normalized if isinstance(normalized, dict) else {},
        )

    def put(self, result: ProviderResult, ttl: Optional[int] = None,
            now: Optional[float] = None) -> ProviderResult:
        current = self.clock() if now is None else float(now)
        ttl_value = self.ttl_seconds(result.provider) if ttl is None else max(1, int(ttl))
        fetched = float(result.fetched_at or current)
        expires = current + ttl_value
        # Persist only sanitized JSON, never ProviderResult internals that may
        # contain a transport error object or URL.
        safe_raw = _sanitize_payload(result.raw, self.secrets)
        safe_normalized = _sanitize_payload(result.normalized, self.secrets)
        safe_error = _sanitize_error(result.error, self.secrets) if result.error else None
        raw_json = json.dumps(safe_raw, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"))
        normalized_json = json.dumps(safe_normalized, ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":"))
        with self._db_lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO ip_intel_cache "
                    "(provider, ip, fetched_at, expires_at, raw_json, normalized_json) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(provider, ip) DO UPDATE SET "
                    "fetched_at=excluded.fetched_at, expires_at=excluded.expires_at, "
                    "raw_json=excluded.raw_json, normalized_json=excluded.normalized_json",
                    (result.provider, result.ip, fetched, expires, raw_json, normalized_json),
                )
                conn.commit()
            finally:
                if conn is not self._memory_connection:
                    conn.close()
        return ProviderResult(
            provider=result.provider,
            ip=result.ip,
            status=result.status,
            fetched_at=fetched,
            expires_at=expires,
            raw=safe_raw,
            normalized=safe_normalized,
            error=safe_error,
            http_status=result.http_status,
        )

    def delete_expired(self, now: Optional[float] = None) -> int:
        current = self.clock() if now is None else float(now)
        with self._db_lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM ip_intel_cache WHERE expires_at <= ?", (current,)
                )
                conn.commit()
                return int(cursor.rowcount)
            finally:
                if conn is not self._memory_connection:
                    conn.close()

    def get_or_query(self, provider: Any, ip: str,
                     query: Optional[Callable[..., ProviderResult]] = None,
                     now: Optional[float] = None) -> ProviderResult:
        """Get a valid cached result or run one query per provider/IP.

        Concurrent callers waiting on the same key receive the owner result,
        including a non-cacheable error, without issuing duplicate requests.
        Different keys remain concurrent.
        """
        name = str(getattr(provider, "name", provider))
        normalized_ip = _valid_ip(ip) or str(ip)
        cached = self.get(name, normalized_ip, now=now)
        if cached is not None:
            return cached

        key = (name, normalized_ip)
        with self._flight_lock:
            flight = self._flights.get(key)
            owner = flight is None
            if owner:
                flight = _Flight()
                self._flights[key] = flight
        assert flight is not None
        if not owner:
            flight.event.wait()
            if flight.result is not None:
                return flight.result
            # Owner should always publish a result, but a defensive retry is
            # preferable to silently turning a provider outage into success.
            return self._error_result(name, normalized_ip, "error", "singleflight_failed")

        try:
            # A second lookup closes the race where another process/thread
            # populated SQLite after the first lookup.
            cached = self.get(name, normalized_ip, now=now)
            if cached is not None:
                result = cached
            else:
                callable_query = query
                if callable_query is None and callable(getattr(provider, "query", None)):
                    callable_query = provider.query
                if callable_query is None:
                    result = self._error_result(name, normalized_ip, "error", "missing_query")
                else:
                    try:
                        try:
                            result = callable_query(normalized_ip)
                        except TypeError:
                            result = callable_query()
                    except Exception as exc:
                        result = self._error_result(name, normalized_ip, "error",
                                                    _sanitize_error(exc, self.secrets))
                    if not isinstance(result, ProviderResult):
                        result = self._error_result(name, normalized_ip, "invalid_response",
                                                    "query_not_provider_result")
                    # A custom provider may have constructed its result
                    # without knowing the cache's in-memory credentials.  Do
                    # one final redaction before handing data back to the
                    # coordinator or persisting it.
                    result = ProviderResult(
                        provider=result.provider,
                        ip=result.ip,
                        status=result.status,
                        fetched_at=result.fetched_at,
                        expires_at=result.expires_at,
                        raw=_sanitize_payload(result.raw, self.secrets),
                        normalized=_sanitize_payload(result.normalized, self.secrets),
                        error=_sanitize_error(result.error, self.secrets) if result.error else None,
                        http_status=result.http_status,
                    )
                    if result.provider != name:
                        result.provider = name
                    if result.ip != normalized_ip:
                        result.ip = normalized_ip
                    if result.cacheable:
                        result = self.put(result, now=now)
            flight.result = result
            return result
        finally:
            flight.event.set()
            with self._flight_lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]

    get_or_fetch = get_or_query

    def query_many(self, ip: str, providers: Sequence[Any],
                   max_workers: int = 4,
                   now: Optional[float] = None) -> Dict[str, ProviderResult]:
        """Query each provider once for one IP, with cache/single-flight."""
        unique: Dict[str, Any] = {}
        for provider in providers:
            unique[str(getattr(provider, "name", provider))] = provider
        if not unique:
            return {}
        workers = max(1, min(int(max_workers), len(unique)))
        if workers == 1:
            return {name: self.get_or_query(provider, ip, now=now)
                    for name, provider in unique.items()}
        out: Dict[str, ProviderResult] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {
                pool.submit(self.get_or_query, provider, ip, None, now): name
                for name, provider in unique.items()
            }
            for future in as_completed(pending):
                name = pending[future]
                try:
                    out[name] = future.result()
                except Exception as exc:
                    out[name] = self._error_result(
                        name, ip, "error", _sanitize_error(exc, self.secrets)
                    )
        return out

    @staticmethod
    def _error_result(provider: str, ip: str, status: str, detail: str) -> ProviderResult:
        return ProviderResult(provider=provider, ip=ip,
                              status=status if status in PROVIDER_STATUSES else "error",
                              error=_sanitize_error(detail))


@dataclass
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[ProviderResult] = None


def _provider_payloads(results: Any) -> Dict[str, Dict[str, Any]]:
    """Coerce ProviderResult/dict fixtures into provider -> normalized map."""
    if isinstance(results, IpIntelligence):
        if results.provider_results:
            results = results.provider_results
        elif results.provider_data:
            results = results.provider_data
        else:
            results = {}
    if isinstance(results, ProviderResult):
        return {results.provider: dict(results.normalized)}
    if isinstance(results, (list, tuple)):
        items = results
        out: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if isinstance(item, ProviderResult):
                out[item.provider] = dict(item.normalized)
            elif isinstance(item, Mapping):
                provider = _clean_text(item.get("provider"))
                if provider:
                    normalized = item.get("normalized", item)
                    out[provider] = dict(normalized) if isinstance(normalized, Mapping) else {}
        return out
    if not isinstance(results, Mapping):
        return {}
    # A single normalized response is accepted as an ip-api-like fixture.
    provider_hint = _clean_text(results.get("provider"))
    if provider_hint and any(key in results for key in ("normalized", "status")):
        normalized = results.get("normalized", results)
        return {provider_hint: dict(normalized) if isinstance(normalized, Mapping) else {}}
    out = {}
    for name, value in results.items():
        key = str(name)
        if isinstance(value, ProviderResult):
            out[key] = dict(value.normalized)
        elif isinstance(value, Mapping):
            normalized = value.get("normalized")
            if isinstance(normalized, Mapping):
                out[key] = dict(normalized)
            else:
                out[key] = dict(value)
    return out


def _provider_statuses(results: Any) -> Dict[str, str]:
    if isinstance(results, IpIntelligence):
        return dict(results.provider_status)
    if isinstance(results, ProviderResult):
        return {results.provider: results.status}
    if isinstance(results, Mapping):
        out = {}
        for name, value in results.items():
            if isinstance(value, ProviderResult):
                out[str(name)] = value.status
            elif isinstance(value, Mapping) and value.get("status") in PROVIDER_STATUSES:
                out[str(name)] = str(value["status"])
        return out
    return {}


def _display_provider(name: str) -> str:
    return {"ip-api": "ip-api", "ipinfo": "IPinfo", "ipqs": "IPQS",
            "scamalytics": "Scamalytics"}.get(name, name)


def _value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def classify_ip(results: Any = None, **provider_results: Any) -> IpClassification:
    """Classify an IP using independent evidence, never one boolean alone.

    The function accepts a provider mapping, a list of ProviderResult objects,
    or keyword fixtures such as ``classify_ip(ipqs=..., ipinfo=...)``.  This
    keeps the classifier easy to use from the coordinator and from unit tests.
    """
    if provider_results:
        merged: Dict[str, Any] = {}
        if isinstance(results, Mapping):
            merged.update(results)
        merged.update(provider_results)
        payloads = _provider_payloads(merged)
    else:
        payloads = _provider_payloads(results or {})

    evidence: List[str] = []
    conflicts: List[str] = []
    res_sources: set = set()
    dc_sources: set = set()
    mobile_sources: set = set()
    corporate_sources: set = set()
    res_proxy_sources: set = set()
    proxy_sources: set = set()
    negative_dc_sources: set = set()
    negative_host_sources: set = set()

    for raw_name, payload in payloads.items():
        name = str(raw_name).lower().replace("_", "-")
        display = _display_provider(name)
        if not isinstance(payload, Mapping):
            continue
        conn = str(_value(payload, "connection_type") or "").strip().lower()
        if conn in {"residential", "residential broadband", "home"}:
            res_sources.add(name)
            evidence.append(display + " Residential")
        elif conn in {"data center", "datacenter", "hosting", "server"}:
            dc_sources.add(name)
            evidence.append(display + " Data Center")
        elif conn in {"corporate", "business", "business broadband", "education"}:
            corporate_sources.add(name)
            evidence.append(display + " Corporate/Business")
        elif conn in {"mobile", "cellular"}:
            mobile_sources.add(name)
            evidence.append(display + " Mobile")

        hosting = _bool_or_none(_value(payload, "hosting", "is_hosting"))
        datacenter = _bool_or_none(_value(payload, "datacenter", "is_datacenter"))
        mobile = _bool_or_none(_value(payload, "mobile", "is_mobile"))
        proxy = _bool_or_none(_value(payload, "proxy", "is_proxy"))
        vpn = _bool_or_none(_value(payload, "vpn", "is_vpn"))
        tor = _bool_or_none(_value(payload, "tor", "is_tor"))
        server = _bool_or_none(_value(payload, "server", "is_server"))
        res_proxy = _bool_or_none(_value(
            payload, "residential_proxy", "is_residential_proxy", "is_res_proxy"
        ))

        if hosting is True:
            dc_sources.add(name)
            evidence.append(display + " Hosting=True")
        elif hosting is False:
            negative_host_sources.add(name)
            evidence.append(display + " Hosting=False")
        if datacenter is True:
            dc_sources.add(name)
            evidence.append(display + " Datacenter=True")
        elif datacenter is False:
            negative_dc_sources.add(name)
            evidence.append(display + " Datacenter=False")
        if mobile is True:
            mobile_sources.add(name)
            evidence.append(display + " Mobile=True")
        if proxy is True:
            proxy_sources.add(name)
            evidence.append(display + " Proxy=True")
        if vpn is True:
            proxy_sources.add(name)
            evidence.append(display + " VPN=True")
        if tor is True:
            proxy_sources.add(name)
            evidence.append(display + " Tor=True")
        if server is True:
            proxy_sources.add(name)
            evidence.append(display + " Server=True")
        if res_proxy is True:
            res_proxy_sources.add(name)
            evidence.append(display + " Residential Proxy=True")

        # IPQS has no separate official residential_proxy response field.  A
        # Residential connection plus its explicit proxy flag is useful
        # evidence, but clearly label the inference.
        if name == "ipqs" and conn in {"residential", "residential broadband"} and proxy is True:
            res_proxy_sources.add(name)
            evidence.append("IPQS Residential + Proxy (inferred residential proxy)")

    # Explicit residential and explicit hosting/datacenter are incompatible;
    # preserve both sides rather than silently choosing a flattering label.
    if res_sources and dc_sources:
        for source in sorted(res_sources):
            conflicts.append(_display_provider(source) + " says Residential")
        for source in sorted(dc_sources):
            conflicts.append(_display_provider(source) + " says Hosting/Datacenter")
        return IpClassification("unknown", 20, evidence, conflicts)

    if res_proxy_sources:
        support = len(res_proxy_sources)
        confidence = 94 if support >= 2 else 84
        if res_sources and negative_dc_sources:
            confidence = min(92, confidence + 3)
        return IpClassification("residential_proxy", confidence, evidence, conflicts)

    # A generic explicit proxy/VPN signal has priority over a neutral ISP
    # description, but does not turn into residential_proxy without evidence.
    if proxy_sources:
        confidence = 90 if len(proxy_sources) >= 2 else 78
        return IpClassification("vpn_proxy", confidence, evidence, conflicts)

    if dc_sources:
        confidence = 95 if len(dc_sources) >= 3 else 86 if len(dc_sources) >= 2 else 70
        return IpClassification("datacenter", confidence, evidence, conflicts)

    if mobile_sources:
        confidence = 88 if len(mobile_sources) >= 2 else 72
        return IpClassification("mobile", confidence, evidence, conflicts)

    if res_sources:
        # One Residential response plus independent negative hosting/DC
        # evidence is useful, but still not absolute proof of a home user.
        confidence = 90 if len(res_sources) >= 2 else 86 if (
            negative_dc_sources and negative_host_sources
        ) else 70
        return IpClassification("residential", confidence, evidence, conflicts)

    if corporate_sources:
        confidence = 86 if len(corporate_sources) >= 2 else 70
        return IpClassification("corporate", confidence, evidence, conflicts)

    return IpClassification("unknown", 0, evidence, conflicts)


def _has_substantive_risk_data(payloads: Mapping[str, Mapping[str, Any]]) -> bool:
    for name, payload in payloads.items():
        if name == "ip-api":
            continue
        if not isinstance(payload, Mapping):
            continue
        # IPinfo's lookup/privacy flags are useful evidence when they identify
        # a hosting/anonymous/residential-proxy condition.  A false-only
        # basic response, however, is not a vendor reputation verdict and
        # must not manufacture a clean IP Grade.
        if name == "ipinfo":
            if any(
                _bool_or_none(payload.get(key)) is True
                for key in (
                    "hosting", "is_hosting", "proxy", "vpn", "tor",
                    "residential_proxy", "is_res_proxy", "is_residential_proxy",
                )
            ):
                return True
            continue
        for key in (
            "fraud_score", "scamalytics_score", "scamalytics_risk",
            "recent_abuse", "abuse_velocity", "bot_status", "blacklisted",
            "residential_proxy", "is_res_proxy", "proxy", "vpn", "tor",
        ):
            if payload.get(key) is not None:
                return True
    return False


def _risk_bool(payloads: Mapping[str, Mapping[str, Any]], *keys: str) -> bool:
    return any(_bool_or_none(payload.get(key)) is True
               for payload in payloads.values() if isinstance(payload, Mapping)
               for key in keys)


def _risk_text(payloads: Mapping[str, Mapping[str, Any]], *keys: str) -> List[str]:
    return [str(payload.get(key)).lower()
            for payload in payloads.values() if isinstance(payload, Mapping)
            for key in keys if payload.get(key) is not None]


def compute_ip_quality_score(results: Any) -> Optional[float]:
    """Return SpeedBench's heuristic IP quality score, or ``None``.

    Vendor fraud scores are not averaged.  The worst score/risk fact is used,
    while duplicate flags reported by several providers are deducted once.
    """
    payloads = _provider_payloads(results)
    if not _has_substantive_risk_data(payloads):
        return None

    score = 100.0
    fraud_scores = []
    for payload in payloads.values():
        if not isinstance(payload, Mapping):
            continue
        for key in ("fraud_score", "ipqs_fraud_score", "scamalytics_score"):
            number = _int_or_none(payload.get(key))
            if number is not None and 0 <= number <= 100:
                fraud_scores.append(number)
    highest = max(fraud_scores) if fraud_scores else None
    if highest is not None:
        if highest >= 90:
            score -= 68
        elif highest >= 75:
            score -= 48
        elif highest >= 60:
            score -= 30
        elif highest >= 40:
            score -= 16
        elif highest >= 20:
            score -= 7

    risk_levels = _risk_text(payloads, "scamalytics_risk", "risk")
    very_high_risk = any(
        level in {"very high", "very_high", "critical"} for level in risk_levels
    )
    if very_high_risk:
        score -= 18
    elif any(level == "high" for level in risk_levels):
        score -= 12
    elif any(level == "medium" for level in risk_levels):
        score -= 5

    blacklisted = _risk_bool(
        payloads, "blacklisted", "scamalytics_blacklisted", "is_blacklisted_external"
    )
    if blacklisted:
        score -= 55
    recent_abuse = _risk_bool(payloads, "recent_abuse", "frequent_abuser")
    if recent_abuse:
        score -= 28
    abuse_velocity = _risk_text(payloads, "abuse_velocity")
    if any(any(word in value for word in ("high", "rapid", "very")) for value in abuse_velocity):
        score -= 15
    bot_status = _risk_text(payloads, "bot_status")
    if any(value not in {"false", "clean", "low", "0", "none"} for value in bot_status):
        score -= 12
    if _risk_bool(payloads, "proxy", "vpn", "tor", "server", "is_server"):
        score -= 20
    if _risk_bool(payloads, "residential_proxy", "is_res_proxy", "is_residential_proxy"):
        score -= 15
    if _risk_bool(payloads, "hosting", "datacenter", "is_datacenter"):
        score -= 5

    classification = classify_ip(results)
    if classification.conflicts:
        score -= 10
    score = max(0.0, min(100.0, score))

    # These are safety caps, not vendor-score conversions.  A blacklist or a
    # very-high risk signal must never appear as a clean-looking S/A/B/C
    # recommendation merely because no other penalty happened to be present.
    # Recent abuse and a high fraud score are capped at C; a more severe
    # fraud score (>=90) already falls into D through the penalty above.
    if blacklisted or very_high_risk:
        score = min(score, 39.0)
    elif recent_abuse or (highest is not None and highest >= 75):
        score = min(score, 59.0)
    return round(score, 1)


def ip_quality_grade(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 90:
        return "S"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    return "D"


compute_ip_grade = ip_quality_grade


def aggregate_ip_intelligence(ip: str, results: Any) -> IpIntelligence:
    """Build the unified model consumed by the coordinator/UI."""
    payloads = _provider_payloads(results)
    statuses = _provider_statuses(results)

    def first_field(*keys: str) -> Optional[str]:
        for preferred in ("ipinfo", "ip-api", "ipqs", "scamalytics"):
            payload = payloads.get(preferred)
            if isinstance(payload, Mapping):
                value = _clean_text(_value(payload, *keys))
                if value:
                    return value
        return None

    def merge_bool(*keys: str) -> Optional[bool]:
        values = []
        for payload in payloads.values():
            if isinstance(payload, Mapping):
                for key in keys:
                    value = _bool_or_none(payload.get(key))
                    if value is not None:
                        values.append(value)
        if True in values:
            return True
        if values and all(value is False for value in values):
            return False
        return None

    def first_int(*keys: str) -> Optional[int]:
        for payload in payloads.values():
            if isinstance(payload, Mapping):
                for key in keys:
                    value = _int_or_none(payload.get(key))
                    if value is not None:
                        return value
        return None

    def provider_int(provider: str, *keys: str) -> Optional[int]:
        payload = payloads.get(provider)
        if not isinstance(payload, Mapping):
            return None
        for key in keys:
            value = _int_or_none(payload.get(key))
            if value is not None:
                return value
        return None

    def provider_bool(provider: str, *keys: str) -> Optional[bool]:
        payload = payloads.get(provider)
        if not isinstance(payload, Mapping):
            return None
        values = [_bool_or_none(payload.get(key)) for key in keys]
        values = [value for value in values if value is not None]
        if True in values:
            return True
        if values:
            return False
        return None

    classification = classify_ip(results)
    quality = compute_ip_quality_score(results)
    model = IpIntelligence(
        ip=_valid_ip(ip) or str(ip),
        ip_version=ip_version(ip),
        country=first_field("country"),
        asn=first_field("asn"),
        as_name=first_field("as_name", "asname"),
        isp=first_field("isp"),
        organization=first_field("organization", "org"),
        hosting=merge_bool("hosting", "is_hosting"),
        proxy=merge_bool("proxy", "is_proxy"),
        vpn=merge_bool("vpn", "is_vpn"),
        tor=merge_bool("tor", "is_tor"),
        mobile=merge_bool("mobile", "is_mobile"),
        residential_proxy=merge_bool(
            "residential_proxy", "is_residential_proxy", "is_res_proxy"
        ),
        connection_type=first_field("connection_type"),
        ipqs_fraud_score=provider_int("ipqs", "ipqs_fraud_score", "fraud_score"),
        ipqs_recent_abuse=provider_bool("ipqs", "recent_abuse"),
        ipqs_abuse_velocity=_clean_text(
            _value(payloads.get("ipqs", {}), "abuse_velocity")
            if isinstance(payloads.get("ipqs"), Mapping) else None
        ),
        scamalytics_score=provider_int(
            "scamalytics", "scamalytics_score", "fraud_score", "score"
        ),
        scamalytics_risk=_clean_text(
            _value(payloads.get("scamalytics", {}), "scamalytics_risk", "risk")
            if isinstance(payloads.get("scamalytics"), Mapping) else None
        ),
        scamalytics_datacenter=provider_bool("scamalytics", "datacenter", "is_datacenter"),
        scamalytics_blacklisted=provider_bool(
            "scamalytics", "blacklisted", "scamalytics_blacklisted", "is_blacklisted_external"
        ),
        classification=classification,
        ip_quality_score=quality,
        ip_grade=ip_quality_grade(quality),
        provider_status=statuses,
        provider_data={k: dict(v) for k, v in payloads.items()},
        provider_results={
            k: v for k, v in (results.items() if isinstance(results, Mapping) else [])
            if isinstance(v, ProviderResult)
        },
    )
    return model


def provider_status_snapshot(providers: Sequence[IpIntelProvider]) -> Dict[str, str]:
    """Return configuration status without exposing credentials."""
    out = {}
    for provider in providers:
        try:
            status = provider.configured_status()
        except Exception:
            status = "error"
        out[provider.name] = status if status in PROVIDER_STATUSES else "error"
    return out


__all__ = [
    "BASIC_TTL_SECONDS", "RISK_TTL_SECONDS", "DEFAULT_COOLDOWN_SECONDS",
    "MAX_COOLDOWN_SECONDS", "PROVIDER_STATUSES", "IP_CATEGORIES",
    "ProviderConfig", "ProviderResult", "IpClassification", "IpIntelligence",
    "IpIntelProvider", "IpApiProvider", "IpInfoProvider", "IpqsProvider",
    "ScamalyticsProvider", "IpIntelCache", "load_provider_config",
    "provider_config_from_env", "make_default_providers", "classify_ip",
    "compute_ip_quality_score", "ip_quality_grade", "compute_ip_grade",
    "aggregate_ip_intelligence", "provider_status_snapshot", "ip_version",
]
