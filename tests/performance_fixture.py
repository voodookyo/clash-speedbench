"""Owned, offline same-coverage scheduling fixture; not a network benchmark.

Run: python3 -m tests.performance_fixture
Production probe/exit pools, node measurements, enrichment/cache, progress,
reporting and history remain real. Controller, DNS, Mihomo lifecycle and all
network transports are synthetic. Reported download bytes are injected curl
return values, never measured traffic. Wall times include real fixture sleeps.
"""
import argparse
from collections import Counter
from contextlib import closing, ExitStack, redirect_stdout
import io
import ipaddress
import json
from pathlib import Path
import platform
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import clash_speedbench as core
import speedbench_db as database
from speedbench_ip_intel import IpIntelCache, IpqsProvider, ProviderConfig
from speedbench_jobs import JobStore, MILESTONES
from speedbench_progress import ProgressEmitter, ResultJournal, measure, parse_record
from speedbench_tasks import resolve_config
import speedbench_workers as workers

SCENARIOS = ('cold', 'hot', 'failure', 'ipv6_unavailable')
STRATEGIES = ('legacy_static', 'deep_dynamic')
PARAMETERS = dict(probe_count=10, workers=3, intel_workers=2, mb=10,
                  rounds=1, max_time=3.0, settle=0, delay_timeout=1000,
                  ip_timeout=1.0, multi=False, no_ip=False, all=True)


def _outcome(row):
    evidence = {key: row.get(key) for key in ('exit_status', 'ip_quality_score', 'ip_grade')}
    for key in ('intel_v4', 'intel_v6'):
        value = row.get(key)
        evidence[key] = ({field: value.get(field) for field in
                         ('provider_status', 'proxy', 'vpn', 'tor', 'ip_quality_score', 'ip_grade')}
                        if value is not None else None)
    return evidence


class _ProgressSink:
    """Use the real parent parser/state machine without starting a web server."""
    def __init__(self, store, job):
        self.store, self.job = store, job
        self.source_seq = 0

    def write(self, line):
        event = parse_record(line, self.job)
        assert event is not None, 'Invalid production progress record'
        assert event['source_seq'] == self.source_seq + 1, 'Non-monotonic child seq'
        self.source_seq = event['source_seq']
        if event['type'] == 'phase_started':
            self.store.transition(self.job, event['phase'])
        else:
            self.store.publish(self.job, event['type'], phase=event['phase'],
                               node_id=event['node_id'], payload=event['payload'])
        return len(line)

    def flush(self):
        pass


class _Transports:
    def __init__(self, count, scenario, time_scale):
        self.names = [f'fixture-{i:03}' for i in range(count)]
        self.indices = {name: i for i, name in enumerate(self.names)}
        self.scenario, self.time_scale = scenario, time_scale
        self.lock = threading.RLock()
        self.instances = []
        self.by_proxy = {}
        self.calls = Counter()
        self.active = Counter()
        self.peak = Counter()
        self.downloaded = Counter()
        self.violations = []

    def pause(self, seconds):
        time.sleep(seconds * self.time_scale)

    def unreachable(self, name):
        return self.scenario == 'failure' and self.indices[name] % 10 < 7

    def ips(self, name):
        index = self.indices[name] % 8 + 1
        return f'192.0.2.{index}', f'2001:db8::{index}'

    def enter(self, kind, worker=None):
        with self.lock:
            self.active[kind] += 1
            self.peak[kind] = max(self.peak[kind], self.active[kind])
            self.calls[kind] += 1
            if worker is not None:
                worker.in_flight += 1

    def leave(self, kind, worker=None):
        with self.lock:
            self.active[kind] -= 1
            if worker is not None:
                worker.in_flight -= 1

    def api(self, worker=None):
        owner = self

        class API:
            timeout = 1.0

            def get(self, path):
                assert path in ('/version', '/proxies')
                owner.pause(.001)
                return {'version': 'fixture'} if path == '/version' else {
                    'proxies': {n: {'type': 'ss'} for n in owner.names}}

            def proxy_delay(self, name, url, timeout_ms):
                if worker is not None:
                    assert name in worker.loaded
                kind = 'worker_probe' if worker is not None else 'main_probe'
                owner.enter(kind, worker)
                try:
                    owner.pause(.0002)
                    return None if owner.unreachable(name) else 20 + owner.indices[name]
                finally:
                    owner.leave(kind, worker)

        return API()

    def worker_type(self):
        owner = self

        class Worker:
            def __init__(self, binary, definitions, hosts, iface):
                self.loaded = {p['name'] for p in definitions}
                self.in_flight = 0
                self.selected = None
                self.stopped = False
                with owner.lock:
                    self.proxy_url = f'fixture://worker/{len(owner.instances)}'
                    owner.instances.append(self)
                    owner.by_proxy[self.proxy_url] = self
                self.api = owner.api(self)

            def start(self):
                owner.pause(.001)

            def select(self, name):
                with owner.lock:
                    if self.in_flight or self.stopped or name not in self.loaded:
                        owner.violations.append('worker switched outside its loaded/idle scope')
                        raise AssertionError(owner.violations[-1])
                    self.selected = name

            def stop(self):
                with owner.lock:
                    if self.in_flight:
                        owner.violations.append('worker stopped with a fixture request in flight')
                    self.stopped = True

        return Worker

    def exit_ip(self, proxy_url, timeout, ipv6=False):
        worker = self.by_proxy[proxy_url]
        name = worker.selected
        kind = 'exit_v6' if ipv6 else 'exit_v4'
        self.enter(kind, worker)
        try:
            # The static round-robin shard 0 gets all slow nodes. Dynamic
            # scheduling may share them; both strategies see identical work.
            slow = self.indices[name] % 3 == 0
            self.pause((.016 if ipv6 else .004) if slow else .002)
            if ipv6 and self.scenario == 'ipv6_unavailable':
                self.pause(.004)
                return None
            return self.ips(name)[int(ipv6)]
        finally:
            self.leave(kind, worker)

    def download(self, *, proxy_url, download_url, max_time, connect_timeout):
        worker = self.by_proxy[proxy_url]
        name = worker.selected
        assert parse_qs(urlsplit(download_url).query)['bytes'] == ['10000000']
        assert max_time == PARAMETERS['max_time']
        self.enter('download', worker)
        try:
            with self.lock:
                if self.active['download'] > 1:
                    self.violations.append('cross-node bandwidth concurrency')
                self.downloaded[name] += 1
            self.pause(.002)
            if self.scenario == 'failure' and self.indices[name] % 10 == 7:
                return None, 'http-500', 2.0, 1.25
            return 80.0 + self.indices[name], 'ok', 2.0, 10.0
        finally:
            self.leave('download', worker)

    def provider(self, url, **options):
        ip = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
        assert ipaddress.ip_address(ip) in (ipaddress.ip_network('192.0.2.0/24')
            if ':' not in ip else ipaddress.ip_network('2001:db8::/32'))
        self.enter('provider')
        try:
            self.pause(.01)
            if self.scenario == 'failure' and int(ipaddress.ip_address(ip)) % 2:
                raise TimeoutError('fixture timeout')
            return {'success': True, 'IP': ip, 'ISP': 'Fixture ISP',
                    'ASN': 64500, 'country_code': 'JP', 'proxy': False,
                    'vpn': False, 'tor': False, 'fraud_score': 5,
                    'recent_abuse': False, 'connection_type': 'Residential'}
        finally:
            self.leave('provider')


def run_case(count, strategy, scenario, *, time_scale=1.0):
    """One fresh owned data directory; no external processes or sockets."""
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 300:
        raise ValueError('Fixture node count must be between 1 and 300')
    if strategy not in STRATEGIES or scenario not in SCENARIOS or time_scale < 0:
        raise ValueError('Unsupported fixture case')
    transport = _Transports(count, scenario, time_scale)
    config = resolve_config(dict(PARAMETERS, mode='deep' if strategy == 'deep_dynamic' else 'legacy'))
    definitions = [dict(name=n, type='ss', server='192.0.2.1', port=i+1000)
                   for i, n in enumerate(transport.names)]
    with tempfile.TemporaryDirectory(prefix='speedbench-perf-') as folder, ExitStack() as stack:
        # Fail if a future implementation accidentally crosses the fixture's
        # declared boundary, rather than falling through to user resources.
        stack.enter_context(mock.patch('socket.socket', side_effect=AssertionError('Fixture network is disabled')))
        stack.enter_context(mock.patch('subprocess.Popen', side_effect=AssertionError('Fixture processes are disabled')))
        history = Path(folder) / 'speedbench-history.jsonl'
        db_path = history.with_suffix('.db')
        # Warm the same unique successful exits in each strategy's own cache,
        # outside accepted-task elapsed time; no previous fixture is reused.
        if scenario == 'hot':
            cache = IpIntelCache(db_path)
            provider = IpqsProvider(key='fixture-key', transport=transport.provider)
            for ip in sorted({ip for n in transport.names for ip in transport.ips(n)}):
                assert cache.get_or_query(provider, ip).status == 'ok'
            transport.calls.clear()
            transport.peak.clear()
        store = JobStore()
        job = store.create(config)
        store.transition(job, 'preparing')
        sink = _ProgressSink(store, job)
        args = SimpleNamespace(**{k: getattr(config, k) for k in PARAMETERS if k != 'all'},
            all=True, task_config=config if strategy == 'deep_dynamic' else None,
            mode='deep' if strategy == 'deep_dynamic' else None,
            target_profile='balanced', top_n=count, config_file=str(Path(folder)/'fixture.yaml'),
            history=str(history), output=str(Path(folder)/'fixture.csv'), top=1,
            no_history=False, auto_switch=False, cancelled=False,
            progress=ProgressEmitter(job, sink), _result_journal=ResultJournal(),
            _owned_intel_pools=[])
        for name, value in [('find_mihomo_bin', 'fixture-mihomo'),
                            ('extract_proxies', definitions), ('physical_interface', 'fixture-physical'),
                            ('build_hosts', {})]:
            stack.enter_context(mock.patch.object(workers, name, return_value=value))
        stack.enter_context(mock.patch.object(workers, 'Worker', transport.worker_type()))
        stack.enter_context(mock.patch.object(core, 'fetch_exit_ip', transport.exit_ip))
        stack.enter_context(mock.patch.object(core, 'load_provider_config',
                                              return_value=ProviderConfig(ip_api_enabled=False)))
        stack.enter_context(mock.patch.object(workers, 'curl_speed', transport.download))
        stack.enter_context(mock.patch.object(core, 'make_default_providers',
            side_effect=lambda **kw: [IpqsProvider(key='fixture-key', transport=transport.provider)]))
        stack.enter_context(mock.patch.object(core, 'cancel_requested', return_value=False))
        stack.enter_context(mock.patch.object(workers, 'cancel_requested', return_value=False))
        stack.enter_context(redirect_stdout(io.StringIO()))
        main_api = transport.api()
        with measure(args, 'connection') as metric:
            main_api.get('/version')
            metric.update(attempts=1, successes=1)
        with measure(args, 'discovery') as metric:
            main_api.get('/proxies')
            metric.update(attempts=1, successes=1, counters={'nodes': count})
        results = workers.run_pool(transport.names, {n: 'ss' for n in transport.names}, args,
                                  main_api=main_api)
        with measure(args, 'summary'):
            assert core._report(results, args, main_api, {}) == 0
            assert database.import_jsonl(db_path, history) == 1
        # The fixture has no child OS process. Cleanup here proves only that
        # its owned Worker objects and production enrichment futures ended.
        assert not transport.violations, transport.violations
        assert transport.instances and all(w.stopped and not w.in_flight for w in transport.instances)
        assert not any(transport.active.values())
        assert all(f.done() for pool in args._owned_intel_pools for f in pool.futures.values())
        store.complete_cleanup(job)
        store.transition(job, 'completed')
        snapshot = store.snapshot(job)
        assert not args.progress.transport_failed
        database.save_task(db_path, snapshot)
        saved = database.task_snapshot(db_path, job)
        assert saved['milestones'] == snapshot['milestones']
        assert saved['metrics'] == {name: dict(metric, counters=metric.get('counters', {}))
                                    for name, metric in snapshot['metrics'].items()}
        raw = history.read_text(encoding='utf-8').rstrip('\n')
        with closing(sqlite3.connect(db_path)) as conn:
            assert conn.execute('SELECT raw FROM runs').fetchone()[0] == raw
        expected_milestones = set(MILESTONES)
        if not any(r.median_mbps is not None and r.median_mbps > 0 for r in results):
            expected_milestones.remove('first_recommendation')
        assert set(snapshot['milestones']) == expected_milestones
        assert len(results) == len(snapshot['results']) == len(args._result_journal.snapshot()) == count
        probes = {r.name: {'main': (r.probe_sources or {}).get('main'),
                           'worker': (r.probe_sources or {}).get('worker')}
                  for r in args._result_journal.snapshot()}
        measured = sorted(r.name for r in results if r.sample_mb is not None)
        reachable = sorted(n for n in transport.names if not transport.unreachable(n))
        assert measured == reachable, 'Fixture detail coverage changed'
        assert transport.calls['main_probe'] == count * config.probe_count
        assert transport.calls['worker_probe'] == (count - len(reachable)) * config.probe_count
        assert dict(transport.downloaded) == {name: config.rounds for name in reachable}
        outcomes = {r.name: _outcome(core.result_to_dict(r)) for r in results}
        # Validate terminal values at every retention boundary, not just
        # callback/transport counts. Missing enrichment cannot masquerade as
        # a successful fixture or a clean IP when providers timed out.
        for rows in (snapshot['results'], saved['results'], json.loads(raw)['results']):
            assert {row['name']: _outcome(row) for row in rows} == outcomes
        for name in reachable:
            value = outcomes[name]
            assert value['exit_status'] == dict(ipv4='completed',
                ipv6='failed' if scenario == 'ipv6_unavailable' else 'completed')
            timed_out = scenario == 'failure' and int(ipaddress.ip_address(transport.ips(name)[0])) % 2
            status = 'timeout' if timed_out else 'cache_hit' if scenario == 'hot' else 'ok'
            for family in ('intel_v4', 'intel_v6'):
                intel = value[family]
                if family == 'intel_v6' and scenario == 'ipv6_unavailable':
                    assert intel is None
                    continue
                assert intel is not None and intel['provider_status'] == {'ipqs': status}
                if timed_out:
                    assert intel['ip_quality_score'] is None and intel['ip_grade'] is None
                    assert all(intel[field] is None for field in ('proxy', 'vpn', 'tor'))
            if timed_out:
                assert value['ip_quality_score'] is None and value['ip_grade'] is None
            else:
                assert value['ip_quality_score'] is not None and 0 <= value['ip_quality_score'] <= 100
                assert value['ip_grade'] in ('S', 'A', 'B', 'C', 'D')
        for name, sources in probes.items():
            assert sources['main']['attempts'] == config.probe_count
            assert sources['main']['successes'] == (0 if transport.unreachable(name) else config.probe_count)
            if transport.unreachable(name):
                assert sources['worker']['attempts'] == config.probe_count
                assert sources['worker']['failures'] == config.probe_count
        assert sum(bool(r.exit_ipv4) for r in results) == len(reachable)
        assert sum(bool(r.exit_ipv6) for r in results) == (0 if scenario == 'ipv6_unavailable' else len(reachable))
        assert transport.peak['main_probe'] <= 10
        assert transport.peak['worker_probe'] <= config.workers
        assert max(transport.peak['exit_v4'], transport.peak['exit_v6']) <= config.workers
        assert transport.peak['provider'] <= config.intel_workers
        assert transport.peak['download'] <= 1
        return dict(nodes=count, strategy=strategy, scenario=scenario,
            parameters={k: getattr(config, k) for k in PARAMETERS if k != 'all'},
            measure_all=True, results=len(results), measured=measured,
            ipv4=sum(bool(r.exit_ipv4) for r in results), ipv6=sum(bool(r.exit_ipv6) for r in results),
            failed_probes=sum(r.probe_failures or 0 for r in results),
            failed_downloads=sum(r.sample_mb is not None and r.median_mbps is None for r in results),
            synthetic_download_bytes=sum(r.download_bytes or 0 for r in results),
            probes=probes, outcomes=outcomes, calls=dict(transport.calls), peak=dict(transport.peak),
            milestones_ms=snapshot['milestones'], elapsed_ms=snapshot['elapsed_ms'],
            metrics=snapshot['metrics'], resources_stopped=len(transport.instances),
            event_count=sink.source_seq, stored=True)


def compare_cases(static, dynamic):
    """Refuse timing comparisons unless the coverage/workload actually agree."""
    for key in ('nodes', 'scenario', 'parameters', 'measure_all', 'results', 'measured',
                'ipv4', 'ipv6', 'failed_probes', 'failed_downloads', 'synthetic_download_bytes',
                'probes', 'outcomes', 'calls'):
        assert static[key] == dynamic[key], f'Different comparison workload: {key}'
    return {name: {'legacy_static_ms': (round(static['milestones_ms'][name], 2)
                                       if name in static['milestones_ms'] else None),
                   'deep_dynamic_ms': (round(dynamic['milestones_ms'][name], 2)
                                      if name in dynamic['milestones_ms'] else None)}
            for name in MILESTONES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', nargs='+', type=int, default=[30, 100, 300])
    options = parser.parse_args()
    pairs = []
    for count in options.nodes:
        for scenario in SCENARIOS:
            static, dynamic = [run_case(count, strategy, scenario) for strategy in STRATEGIES]
            comparison = compare_cases(static, dynamic)
            # Raw synthetic node samples are assertions, not useful bulk output.
            for row in (static, dynamic):
                row.pop('probes')
                row.pop('outcomes')
                row['measured'] = len(row['measured'])
            pairs.append(dict(nodes=count, scenario=scenario, comparison=comparison,
                              runs=[static, dynamic]))
    print(json.dumps(dict(evidence='offline synthetic scheduling fixture',
        python=platform.python_version(), platform=platform.platform(),
        time_scale=1.0, network_bytes_measured=False, native_workers=False,
        cases=pairs), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
