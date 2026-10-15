import io
import json
from types import SimpleNamespace
import unittest
from unittest import mock
from speedbench_jobs import JobStore
from speedbench_tasks import resolve_config
from speedbench_progress import ProgressEmitter, measure
import clash_speedbench as core
import speedbench_workers as workers


class ProgressMetricsTest(unittest.TestCase):
    def test_metrics_cross_protocol_as_actual_counts(self):
        store = JobStore()
        job = store.create(resolve_config())
        stream = io.StringIO()
        args = SimpleNamespace(progress=ProgressEmitter(job,stream))
        with measure(args,'download') as counts:
            counts.update(attempts=2,successes=1,bytes=1234)
        record = json.loads(stream.getvalue().split(' ',1)[1])
        store.publish(job,record['type'],payload=record['payload'])
        metric = store.snapshot(job)['metrics']['download']
        self.assertEqual(metric['bytes'],1234)
        self.assertEqual(metric['attempts'],2)
        self.assertGreaterEqual(metric['duration_ms'],0)

    def test_metrics_whitelist_nonfinite_negative_and_secrets(self):
        store = JobStore()
        job = store.create(resolve_config())
        store.publish(job,'phase_finished',payload={'metrics':{
            'download':{'duration_ms':float('nan'),'bytes':-1,'attempts':1,'successes':9},
            'secret':{'api_key':'CANARY'}}})
        value = store.snapshot(job)
        self.assertNotIn('CANARY',json.dumps(value))
        self.assertEqual(value['metrics']['download']['bytes'],0)
        self.assertEqual(value['metrics']['download']['successes'],1)

    def test_node_download_counter_uses_observed_size_not_requested_budget(self):
        r = core.Result('node','','ss',12,[],None,None,'probe-only')
        args = SimpleNamespace(settle=0,mb=10,max_time=3,rounds=2,multi=False)
        worker = SimpleNamespace(select=lambda name:None,proxy_url='http://127.0.0.1:1')
        with mock.patch.object(workers,'curl_speed',side_effect=[(8,'ok',2,.5),(None,'http-500',None,.125)]):
            workers._speed_node_in_worker(worker,r,args)
        self.assertEqual(r.download_bytes,625000)
        self.assertEqual(core.result_to_dict(r)['download_bytes'],625000)

    def test_unmeasured_tags_survive_final_intelligence_recomputation(self):
        r = core.Result('node','','ss',12,[],None,None,'probe-only')
        r.measurement_scope = {'bandwidth':'not_selected'}
        core.finish_intelligence_enrichment(None,[r])
        self.assertIn('未精测',r.tags)
        self.assertNotIn('不通',r.tags)

    def test_application_probes_stop_before_next_request_after_cancel(self):
        api = mock.Mock()
        with mock.patch.object(core,'cancel_requested',return_value=True):
            with self.assertRaises(KeyboardInterrupt):
                core.probe_latency(api,'node',100,count=10)
        api.proxy_delay.assert_not_called()

    def test_completed_round_survives_cancellation_of_next_round(self):
        r = core.Result('node','','ss',12,[],None,None,'probe-only')
        args = SimpleNamespace(settle=0,mb=10,max_time=3,rounds=2,multi=False)
        worker = SimpleNamespace(select=lambda name:None,proxy_url='http://127.0.0.1:1')
        with mock.patch.object(workers,'curl_speed',side_effect=[(8,'ok',2,.5),KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                workers._speed_node_in_worker(worker,r,args)
        self.assertEqual(r.median_mbps,8)
        self.assertEqual(r.download_bytes,500000)
