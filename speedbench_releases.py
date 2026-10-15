"""Opt-in official release metadata checks, never a download/update mechanism.

Only public GitHub metadata is requested. Credentials, history, remote release
body/assets and arbitrary URLs are never accepted or returned. Cache is memory
only and shared across callers; a failed check never means "up to date".
"""
import json
import re
import socket
import threading
import time
from urllib import request
from urllib.error import HTTPError, URLError

CORE_VERSION = '1.0.1'
RELEASES_URL = 'https://github.com/voodookyo/clash-speedbench/releases'
API_URL = 'https://api.github.com/repos/voodookyo/clash-speedbench/releases/latest'
REQUEST_TIMEOUT = 5
MAX_BYTES = 262144
CACHE_TTL = 900
FAILURE_TTL = 60
_VERSION = re.compile(r'v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?')


class ReleaseError(ValueError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError('Invalid official release response')
        result[key] = value
    return result


def fetch_latest():
    # No auth, custom headers, request body, remote URL or redirect supplied by
    # a caller. Normal system proxy/TUN routing can still affect connectivity.
    req = request.Request(API_URL, headers={'Accept': 'application/vnd.github+json',
                          'User-Agent': 'Clash-SpeedBench-release-check',
                          'X-GitHub-Api-Version': '2026-03-10'})
    with request.build_opener(NoRedirect()).open(req, timeout=REQUEST_TIMEOUT) as response:
        if response.status != 200 or response.geturl() != API_URL:
            raise ReleaseError('Invalid official release response')
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ReleaseError('Invalid official release response')
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseError('Invalid official release response') from None


def _version(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ReleaseError('Unsupported version')
    match = _VERSION.fullmatch(value)
    if not match:
        raise ReleaseError('Unsupported version')
    pre = match[4]
    if pre and any(p.isdigit() and len(p) > 1 and p.startswith('0') for p in pre.split('.')):
        raise ReleaseError('Unsupported version')
    return tuple(int(match[i]) for i in (1, 2, 3)), pre


def local_info(current):
    try:
        _, pre = _version(current)
    except ReleaseError:
        current = 'unknown'; pre = None
    return dict(status='not_checked', current=current, current_prerelease=bool(pre),
                latest=None, comparison=None, official_url=RELEASES_URL,
                automatic_updates=False, channel='stable', cached=False)


def _normalize(value):
    if (not isinstance(value, dict) or value.get('draft') is not False or
            value.get('prerelease') is not False):
        raise ReleaseError('Invalid official release response')
    tag = value.get('tag_name')
    _, pre = _version(tag)
    if pre or value.get('html_url') != RELEASES_URL + '/tag/' + tag:
        raise ReleaseError('Invalid official release response')
    # Ignore release body/assets/author/URLs. The native opener uses its existing
    # fixed enum, not any URL returned by GitHub, even after this validation.
    return dict(status='ok', latest=tag)


class ReleaseChecker:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._cached = None
        self._expires = 0

    def check(self, current):
        info = local_info(current)
        with self._lock:
            cached = self._cached is not None and self._clock() < self._expires
            if not cached:
                try:
                    self._cached = _normalize(fetch_latest())
                except HTTPError as error:
                    self._cached = dict(status=('rate_limited' if error.code in (403, 429) else
                                                'not_published' if error.code == 404 else 'unavailable'))
                    # Python 3.9's HTTPError without a response stream has no
                    # initialized tempfile closer (e.g. a transport fixture).
                    # A real stream is still always closed without reading it.
                    if error.fp is not None:
                        error.close()
                except (TimeoutError, socket.timeout):
                    self._cached = dict(status='timeout')
                except URLError as error:
                    self._cached = dict(status='timeout' if isinstance(error.reason, (TimeoutError, socket.timeout)) else 'unavailable')
                except ReleaseError:
                    self._cached = dict(status='invalid_response')
                except Exception:
                    # Never return a traceback, response body or exception URL.
                    self._cached = dict(status='unavailable')
                self._expires = self._clock() + (CACHE_TTL if self._cached['status'] == 'ok' else FAILURE_TTL)
            info.update(self._cached)
            info['cached'] = cached
        if info['status'] == 'ok' and info['current'] != 'unknown':
            here, pre = _version(current)
            latest, _ = _version(info['latest'])
            info['comparison'] = ('update_available' if latest > here or (latest == here and pre)
                                  else 'ahead' if here > latest else 'current')
        return info
