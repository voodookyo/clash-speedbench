import json
from unittest import mock
import speedbench_web as web
from tests.web_server_case import WebServerCase


class TaskApiTest(WebServerCase):
    def test_mode_contract_and_host_gate(self):
        status,body = self.request('GET','/api/task-config')
        self.assertEqual(status,200)
        quick = json.loads(body)['modes']['quick']
        self.assertEqual((quick['top_n'],quick['mb'],quick['ip_scope']),(5,10,'selected'))
        self.assertEqual(self.request('GET','/api/task-config',headers={'Host':'evil.example'})[0],403)

    def test_invalid_configs_cannot_spawn(self):
        with mock.patch.object(web,'run_benchmark') as run:
            for params in [{'mode':'bogus'},{'workers':100000},{'max_time':float('nan')},
                           {'rounds':True},{'api_key':'CANARY'},{'include':'['}]:
                status,body = self.post_authorized('/api/run',params)
                self.assertEqual(status,400)
                self.assertNotIn(b'CANARY',body)
            run.assert_not_called()

    def test_reservation_prevents_second_dispatch_even_before_thread_work(self):
        self.set_state(running=False)
        with mock.patch.object(web,'run_benchmark') as run:
            self.assertEqual(self.post_authorized('/api/run',{'mode':'quick'})[0],200)
            self.assertEqual(self.post_authorized('/api/run',{'mode':'quick'})[0],409)
            # First dispatch may still be entering its thread; no live work.

    def test_child_command_preserves_mode_defaults_and_explicit_overrides(self):
        cmd = web.benchmark_command({'mode':'quick','probe_count':4,'all_ip':True})
        self.assertIn('--mode',cmd)
        self.assertIn('--all-ip',cmd)
        self.assertIn('--probe-count',cmd)
        self.assertNotIn('--mb',cmd)
        self.assertNotIn('--auto-switch',cmd)

    def test_old_request_keeps_legacy_command(self):
        cmd = web.benchmark_command({'mb':30,'rounds':1})
        self.assertNotIn('--mode',cmd)
        self.assertIn('--mb',cmd)
