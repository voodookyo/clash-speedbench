"""Observed cache/request accounting; all transports are in-memory fakes."""
import io
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import clash_speedbench as core
import speedbench_ip_intel as intel
import speedbench_jobs as jobs
import speedbench_progress as progress
from speedbench_tasks import resolve_config


class IntelMetricsTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.database=Path(temporary.name)/'history.db';self.events=[];self.lock=threading.Lock()
    def observe(self,phase,metric):
        with self.lock:self.events.append((phase,metric))
    def metric(self,phase):
        out=dict(attempts=0,successes=0,bytes=0,duration_ms=0,counters={})
        for name,value in self.events:
            if name!=phase:continue
            for key in ('attempts','successes','bytes','duration_ms'):out[key]+=value.get(key,0)
            for key,count in value.get('counters',{}).items():out['counters'][key]=out['counters'].get(key,0)+count
        return out
    def provider(self,transport=None,key='CANARY-key'):
        provider=intel.IpqsProvider(key=key,transport=transport or (lambda *a,**k:{'success':True,'fraud_score':4}),clock=lambda:1000)
        provider.observer=self.observe;return provider
    def cache(self):return intel.IpIntelCache(self.database,observer=self.observe)
    def test_cold_then_hot_counts_sql_checks_and_only_one_request(self):
        provider=self.provider();cache=self.cache()
        self.assertEqual(cache.get_or_query(provider,'192.0.2.1').status,'ok')
        self.assertEqual(cache.get_or_query(provider,'192.0.2.1').status,'cache_hit')
        self.assertEqual(self.metric('provider')['counters']['api_calls'],1)
        self.assertEqual(self.metric('provider')['attempts'],1)
        self.assertEqual(self.metric('intel_cache')['counters']['cache_hits'],1)
        self.assertEqual(self.metric('intel_cache')['counters']['cache_misses'],2) # initial and race recheck
        self.assertEqual(self.metric('intel_cache')['counters']['cache_writes'],1)
        self.assertNotIn('CANARY-key',json.dumps(self.events))
    def test_missing_key_does_not_count_as_request(self):
        result=self.cache().get_or_query(self.provider(key=''),'192.0.2.2')
        self.assertEqual(result.status,'key_missing');self.assertEqual(self.metric('provider')['attempts'],0)
        self.assertEqual(self.metric('provider')['counters']['key_missing'],1)
    def test_cooldown_is_not_an_extra_api_request(self):
        provider=self.provider(lambda *a,**k:(429,{}, {'Retry-After':'60'}));cache=self.cache()
        self.assertEqual(cache.get_or_query(provider,'192.0.2.3').status,'rate_limited')
        self.assertEqual(cache.get_or_query(provider,'192.0.2.4').status,'rate_limited')
        self.assertEqual(self.metric('provider')['attempts'],1)
        self.assertEqual(self.metric('provider')['counters']['cooldown_skips'],1)
    def test_invalid_json_http_success_is_not_a_usable_result(self):
        self.cache().get_or_query(self.provider(lambda *a,**k:'not JSON'),'192.0.2.5')
        self.assertEqual(self.metric('provider')['successes'],1) # transport 2xx, not reputation
        self.assertEqual(self.metric('provider')['counters']['invalid_responses'],1)
        self.assertEqual(self.metric('provider')['counters'].get('usable_results',0),0)
    def test_timeout_counts_an_invocation_not_success(self):
        def transport(*a,**k):raise TimeoutError('CANARY-key')
        self.assertEqual(self.cache().get_or_query(self.provider(transport),'192.0.2.6').status,'timeout')
        self.assertEqual(self.metric('provider')['attempts'],1);self.assertEqual(self.metric('provider')['successes'],0)
        self.assertEqual(self.metric('provider')['counters']['timeouts'],1)
        self.assertNotIn('CANARY-key',json.dumps(self.events))
    def test_interrupt_still_retains_started_request_counter(self):
        def transport(*a,**k):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.cache().get_or_query(self.provider(transport),'192.0.2.7')
        self.assertEqual(self.metric('provider')['attempts'],1);self.assertEqual(self.metric('provider')['successes'],0)
    def test_transport_body_typeerror_never_retries_a_paid_call(self):
        calls=[]
        def transport(*a,**k):calls.append(1);raise TypeError('fixture body failure')
        self.assertEqual(self.provider(transport).query('192.0.2.8').status,'error')
        self.assertEqual(calls,[1])
    def test_unsupported_signature_never_counts_an_uninvoked_transport(self):
        calls=[]
        def transport():calls.append(1)
        self.assertEqual(self.provider(transport).query('192.0.2.15').status,'error')
        self.assertEqual(calls,[])
        self.assertEqual(self.metric('provider')['attempts'],0)
        self.assertEqual(self.metric('provider')['counters'].get('api_calls',0),0)
    def test_query_body_typeerror_never_retries_custom_query(self):
        calls=[]
        def query(ip=None):calls.append(ip);raise TypeError('fixture body failure')
        self.assertEqual(intel.IpIntelCache(self.database).get_or_query('ipqs','192.0.2.9',query).status,'error')
        self.assertEqual(calls,['192.0.2.9'])
    def test_legacy_tiny_transport_and_no_argument_query_are_supported(self):
        self.assertEqual(self.provider(lambda url:{'success':True,'fraud_score':4}).query('192.0.2.10').status,'ok')
        result=self.cache().get_or_query('ipqs','192.0.2.11',lambda:intel.ProviderResult('ipqs','192.0.2.11','ok'))
        self.assertEqual(result.status,'ok')
    def test_observation_failure_cannot_abort_optional_provider_or_cache(self):
        def broken(*a):raise RuntimeError('CANARY')
        provider=self.provider();provider.observer=broken
        cache=intel.IpIntelCache(self.database,observer=broken)
        self.assertEqual(cache.get_or_query(provider,'192.0.2.12').status,'ok')
        self.assertEqual(cache.get_or_query(provider,'192.0.2.12').status,'cache_hit')
    def test_singleflight_waits_are_separate_from_sql_cache_hits(self):
        gate=threading.Event();waiting=threading.Event();wait_count=[0]
        class WaitEvent:
            def __init__(self):self.event=threading.Event()
            def wait(self,*a,**k):
                with self_test.lock:
                    wait_count[0]+=1
                    if wait_count[0]==3:waiting.set()
                return self.event.wait(*a,**k)
            def set(self):self.event.set()
        self_test=self
        def transport(*a,**k):
            if not gate.wait(3):raise RuntimeError('fixture gate timeout')
            return {'success':True,'fraud_score':4}
        provider=self.provider(transport);cache=self.cache()
        with mock.patch.object(intel,'_Flight',side_effect=lambda:SimpleNamespace(event=WaitEvent(),result=None)),ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(cache.get_or_query,provider,'192.0.2.13') for _ in range(4)]
            try:self.assertTrue(waiting.wait(2))
            finally:gate.set()
            self.assertTrue(all(f.result().status=='ok' for f in futures))
        self.assertEqual(self.metric('provider')['counters']['api_calls'],1)
        self.assertEqual(self.metric('intel_cache_wait')['counters']['singleflight_reuses'],3)
        self.assertEqual(self.metric('intel_cache')['counters'].get('cache_hits',0),0)
    def test_coordinator_dedup_and_hot_cache_metrics_reach_job_without_keys(self):
        args=SimpleNamespace(history=str(self.database.with_suffix('.jsonl')),no_ip=False,intel_workers=2)
        store=jobs.JobStore();job=store.create(resolve_config());stream=io.StringIO()
        args.progress=progress.ProgressEmitter(job,stream)
        provider=self.provider()
        rows=[core.Result(name=name,provider='',proto='ss',latency_ms=20,speeds_mbps=[],median_mbps=None,best_mbps=None,status='probe-only',exit_ipv4='192.0.2.14') for name in ('A','B')]
        with mock.patch.object(core,'make_default_providers',return_value=[provider]):
            for _ in range(2):
                enricher=core.start_intelligence_enrichment(rows,args);core.finish_intelligence_enrichment(enricher,rows)
        for line in stream.getvalue().splitlines():
            event=progress.parse_record(line,job);store.publish(job,event['type'],payload=event['payload'])
        metrics=store.snapshot(job)['metrics']
        self.assertEqual(metrics['provider']['counters']['api_calls'],1)
        self.assertEqual(metrics['intel_cache']['counters']['unique_ips'],2)
        self.assertEqual(metrics['intel_cache']['counters']['cache_hits'],1)
        self.assertNotIn('CANARY-key',stream.getvalue())


if __name__=='__main__':unittest.main()
