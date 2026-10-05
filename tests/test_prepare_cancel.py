"""Preparation uses the real owned loopback transport, never user controllers."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import clash_speedbench as core
from speedbench_progress import ProgressEmitter
from tests.test_transport_cancel import stalled_http


class PrepareCancellationTest(unittest.TestCase):
    def run_stalled_phase(self,discovery):
        cancelled=threading.Event();stream=io.StringIO()
        args=SimpleNamespace(secret=None,controller=None,non_interactive=True,
            history='fixture-unused.jsonl',config_file='',progress=ProgressEmitter('job_'+'a'*32,stream))
        with stalled_http(True) as (base,started,release,requests),ThreadPoolExecutor(1) as pool:
            transport=core.MihomoAPI(base)
            class Api:
                def get(self,path):
                    if path=='/providers/proxies':return transport.get(path)
                    return {'/version':{},'/configs':{},'/proxies':{'proxies':{}}}[path]
            def catalog(api,*a,**kw):
                api.get('/providers/proxies')
                return {'nodes':[],'sources':[]}
            api=Api() if discovery else transport
            with mock.patch.object(core.signal,'signal'), \
                 mock.patch.object(core,'clear_cancel_request'), \
                 mock.patch.object(core,'cancel_requested',cancelled.is_set), \
                 mock.patch.object(core,'connect_controller',return_value=api), \
                 mock.patch.object(core.source_catalog,'discover_catalog',side_effect=catalog):
                future=pool.submit(core._execute_benchmark,args,None)
                try:
                    self.assertTrue(started.wait(1));cancelled.set()
                    with self.assertRaises(KeyboardInterrupt):future.result(timeout=1)
                    self.assertEqual(requests,['/providers/proxies' if discovery else '/version'])
                finally:release.set()
        records=[json.loads(line.split(' ',1)[1]) for line in stream.getvalue().splitlines()]
        metrics={k:v for record in records for k,v in record.get('payload',{}).get('metrics',{}).items()}
        self.assertIn('discovery' if discovery else 'connection',metrics)
        if not discovery:self.assertEqual(metrics['connection']['successes'],0)

    def test_connection_body_cancels_before_worker_or_configuration_write(self):
        self.run_stalled_phase(False)

    def test_catalog_provider_body_cancels_inside_preparation(self):
        self.run_stalled_phase(True)
