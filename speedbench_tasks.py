"""Shared, dependency-free task contracts. Never contains connection secrets."""
import math
from dataclasses import dataclass, asdict
from typing import Optional


class TaskConfigError(ValueError):
    pass


PROFILES = ('balanced', 'daily', 'download', 'ip', 'residential')
MODES = ('legacy', 'quick', 'standard', 'deep', 'ip')
LIMITS = dict(probe_count=(1,100), top_n=(1,3000), rounds=(1,5), mb=(1,95),
              workers=(1,16), intel_workers=(1,4), delay_timeout=(50,60000),
              max_time=(0.1,60), settle=(0,5), ip_timeout=(0.1,30))
BOOLEAN_OPTIONS = ('multi', 'all', 'all_ip', 'no_ip', 'stability')


@dataclass(frozen=True)
class TaskConfig:
    mode: str = 'legacy'
    target_profile: str = 'balanced'
    probe_count: int = 3
    top_n: int = 15
    mb: Optional[int] = None
    rounds: int = 1
    max_time: float = 4.0
    settle: float = 0.35
    delay_timeout: int = 5000
    ip_timeout: float = 8.0
    workers: int = 6
    intel_workers: int = 3
    multi: bool = False
    no_ip: bool = False
    measure_all: bool = False
    bandwidth: bool = True
    ip_scope: str = 'all'

    def public(self):
        return asdict(self)

    def download_budget_mb(self, count):
        """Upper requested sample budget, excludes small exit/probe/API traffic.

        Warmup has a 1MB request. Timeout can reduce actual traffic; the budget
        is explicitly not a byte counter. Multi-stream belongs to one node.
        """
        if not self.bandwidth:
            return 0
        nodes = count if self.measure_all else min(count, self.top_n)
        return nodes * ((self.mb or 95) * self.rounds * (5 if self.multi else 1)
                        + (1 if self.mb is None else 0))


def resolve_config(params=None):
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise TaskConfigError('Task parameters must be an object')
    allowed = set(LIMITS) | set(BOOLEAN_OPTIONS) | {'mode', 'target_profile'}
    if set(params) - allowed:
        raise TaskConfigError('Unsupported task parameter')
    mode = params.get('mode', 'legacy')
    profile = params.get('target_profile', 'balanced')
    if mode not in MODES or profile not in PROFILES:
        raise TaskConfigError('Unsupported task mode or target')
    for key in BOOLEAN_OPTIONS:
        if key in params and not isinstance(params[key], bool):
            raise TaskConfigError('Task switches must be booleans')
    values = TaskConfig().public()
    values.update(mode=mode, target_profile=profile)
    if mode == 'quick':
        values.update(top_n=5, mb=10, max_time=3.0, ip_scope='selected')
    elif mode == 'standard':
        values.update(top_n=10, ip_scope='selected')
    elif mode == 'deep':
        values.update(probe_count=10, measure_all=True)
    elif mode == 'ip':
        values.update(bandwidth=False)
    if params.get('stability'):
        values['probe_count'] = 10
    for key, (low, high) in LIMITS.items():
        value = params.get(key)
        if value is None:
            continue
        integer = key not in ('max_time', 'settle', 'ip_timeout')
        if (isinstance(value, bool) or not isinstance(value, (int,float)) or
                not low <= value <= high or not math.isfinite(value) or
                (integer and not isinstance(value,int))):
            raise TaskConfigError('Task parameter is outside its supported range')
        values[key] = value
    for key in ('multi','no_ip'):
        values[key] = params.get(key, False)
    if params.get('all'):
        values['measure_all'] = True
    if params.get('all_ip'):
        values['ip_scope'] = 'all'
    if values['no_ip']:
        values['ip_scope'] = 'none'
    if mode == 'ip' and (values['no_ip'] or values['multi']):
        raise TaskConfigError('IP-only mode cannot disable IP or request bandwidth')
    return TaskConfig(**values)


def select_candidates(rows, top_n, *, measure_all=False, legacy=False, target_profile='balanced'):
    """Explainable selection over the caller's already constrained scope.

    No node-name region guess is made. ``region`` may be supplied by validated
    metadata; unknown regions add no coverage evidence. Historical bandwidth
    is an optional recent stable-ID hint, never a current test result.
    """
    reachable = sorted((r for r in rows if r.get('latency_ms') is not None),
                       key=lambda r:(r['latency_ms'],str(r.get('node_id') or r.get('name',''))))
    if measure_all:
        return reachable
    if top_n <= 0:
        return []
    if legacy:
        return reachable[:top_n]
    chosen, sources, regions = [], set(), set()
    remaining = list(reachable)
    while remaining and len(chosen) < top_n:
        def rank(row):
            sid = set(row.get('subscription_ids') or [])
            region = row.get('region')
            coverage = len(sid-sources) + int(bool(region and region not in regions))
            recent = row.get('recent_mbps')
            age = row.get('history_age_days')
            hint = (float(recent) if target_profile == 'download' and row.get('node_id')
                    and isinstance(recent,(int,float)) and math.isfinite(recent) and recent > 0
                    and isinstance(age,(int,float)) and 0 <= age <= 7 else 0)
            return (-coverage, -hint, row['latency_ms'],str(row.get('node_id') or row.get('name','')))
        selected = min(remaining, key=rank)
        remaining.remove(selected)
        chosen.append(selected)
        sources.update(selected.get('subscription_ids') or [])
        if selected.get('region'):
            regions.add(selected['region'])
    return chosen
