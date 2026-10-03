from types import SimpleNamespace
import unittest
from unittest import mock
from contextlib import ExitStack, redirect_stdout
import io
import threading

import clash_speedbench as core
import speedbench_workers as workers
from speedbench_tasks import resolve_config
from tests import test_phase1_probe as fixtures


class TaskWorkerModeTest(unittest.TestCase):
    def args(self, mode, **options):
        args = fixtures.mk_args()
        config = resolve_config(dict(mode=mode, **options))
        args.task_config = config
        for name in ('mb','rounds','max_time','probe_count','top_n','workers','intel_workers','multi','no_ip'):
            setattr(args,name,getattr(config,name))
        args.all = config.measure_all
        return args

    def run_mock(self, args, latency):
        return fixtures.RunPoolMainApiTest()._run(args,mock.Mock(),latency)

    def test_quick_collects_exit_only_for_selected_nodes_keeps_all_probe_results(self):
        results,calls,_worker,_stdout = self.run_mock(self.args('quick',top_n=1), {'A':(100,1),'B':(200,2)})
        self.assertEqual(sorted(r.name for r in results),['A','B'])
        self.assertEqual([c[1] for c in calls if c[0]=='probe'],['A'])
        self.assertEqual([c[1] for c in calls if c[0]=='speed'],['A'])

    def test_ip_mode_never_invokes_download(self):
        results,calls,_worker,_stdout = self.run_mock(self.args('ip'), {'A':(100,1),'B':(200,2)})
        self.assertEqual(len(results),2)
        self.assertFalse(any(c[0]=='speed' for c in calls))
        self.assertTrue(all(r.sample_mb is None for r in results))

    def test_dynamic_shard_cannot_swallow_persistent_cleanup_failure(self):
        worker=mock.Mock();worker.stop.side_effect=workers.WorkerCleanupError('CANARY config')
        with ExitStack() as stack:
            for name,value in [('find_mihomo_bin','fake'),('extract_proxies',fixtures.RunPoolMainApiTest.PROXIES),
                               ('physical_interface','en0'),('build_hosts',{}),
                               ('probe_latency_pool',{'A':(100,1),'B':(200,2)})]:
                stack.enter_context(mock.patch.object(workers,name,return_value=value))
            stack.enter_context(mock.patch.object(workers,'Worker',return_value=worker))
            stack.enter_context(mock.patch.object(workers,'_probe_node_in_worker',side_effect=lambda w,n,p,a,l,j:
                core.Result(name=n,provider='',proto=p,latency_ms=l,speeds_mbps=[],median_mbps=None,best_mbps=None,status='ok')))
            download=stack.enter_context(mock.patch.object(workers,'_speed_node_in_worker'))
            stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaises(workers.WorkerCleanupError) as caught:
                workers.run_pool(['A','B'],{},self.args('quick'),main_api=mock.Mock())
        self.assertNotIn('CANARY',str(caught.exception));download.assert_not_called()

    def test_cli_reports_cleanup_failure_without_serial_fallback_or_raw_details(self):
        import tempfile
        import os
        from pathlib import Path
        api=mock.Mock();api.get.side_effect=lambda path:{'proxies':{'node':{'type':'ss'}}} if path=='/proxies' else {}
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ,{},clear=True))
            stack.enter_context(mock.patch('sys.argv',['clash_speedbench.py','--workers','2','--yes','--history',str(Path(folder)/'h.jsonl')]))
            stack.enter_context(mock.patch.object(core,'connect_controller',return_value=api))
            stack.enter_context(mock.patch.object(core,'clear_cancel_request'))
            stack.enter_context(mock.patch.object(core.signal,'signal'))
            stack.enter_context(mock.patch.object(core.source_catalog,'discover_catalog',return_value={'nodes':[],'sources':[]}))
            stack.enter_context(mock.patch.object(workers,'run_pool',side_effect=workers.WorkerCleanupError('CANARY config')))
            output=io.StringIO();stack.enter_context(redirect_stdout(output))
            stack.enter_context(mock.patch('sys.stderr',output))
            self.assertEqual(core.main(),3)
        self.assertNotIn('CANARY',output.getvalue());self.assertNotIn('回退到串行',output.getvalue());api.patch.assert_not_called()

    def test_deep_measures_all_reachable_nodes(self):
        _results,calls,_worker,_stdout = self.run_mock(self.args('deep',top_n=1), {'A':(100,1),'B':(200,2)})
        self.assertEqual([c[1] for c in calls if c[0]=='speed'],['A','B'])

    def test_no_ip_mixed_failure_keeps_fast_successful_result(self):
        results,_calls,_worker,_stdout = self.run_mock(fixtures.mk_args(no_ip=True), {'A':(100,1),'B':(None,None)})
        self.assertEqual(sorted(r.name for r in results),['A','B'])

    def test_unmeasured_metadata_does_not_claim_bandwidth_failure(self):
        results,_calls,_worker,_stdout = self.run_mock(self.args('quick',top_n=1), {'A':(100,1),'B':(200,2)})
        b = next(r for r in results if r.name=='B')
        row = core.result_to_dict(b)
        self.assertEqual(row['measurement_scope']['bandwidth'],'not_selected')
        self.assertEqual(row['measurement_scope']['exit'],'not_selected')

    def test_new_mode_selector_covers_more_than_one_subscription(self):
        rows = [core.Result(name=n,provider='',proto='ss',latency_ms=lat,
                            speeds_mbps=[],median_mbps=None,best_mbps=None,status='ok')
                for n,lat in [('a',10),('b',11),('c',50)]]
        for r,sid in zip(rows,['x','x','y']):
            r.origin = dict(subscription_ids=[sid])
        chosen = workers.choose_task_nodes(rows,self.args('standard',top_n=2))
        self.assertEqual([r.name for r in chosen],['a','c'])

    def test_dynamic_workers_load_whole_pending_dependency_scope(self):
        _results,_calls,worker,_stdout = self.run_mock(self.args('ip',workers=2), {'A':(100,1),'B':(200,2)})
        for call in worker.call_args_list:
            self.assertEqual({p['name'] for p in call.args[1]},{'A','B'})

    def test_coverage_ranking_never_rewards_unmeasured_latency_only_node(self):
        results,_calls,_worker,_stdout = self.run_mock(self.args('quick',top_n=1), {'A':(100,1),'B':(200,2)})
        a,b = sorted(results,key=lambda r:r.name)
        a.score,b.score = 40,100
        self.assertEqual(core.rank_results(results)[0].name,'A')

    def test_dynamic_queue_slow_worker_does_not_hold_a_static_shard(self):
        proxies = [dict(name=n,type='ss',server=n+'.example') for n in 'ABCDE']
        done, claimed, loaded = threading.Event(), [], []
        lock = threading.Lock()
        slow_completed = []

        class Worker:
            def __init__(self, binary, definitions, hosts, iface):
                loaded.append({p['name'] for p in definitions})
            def start(self): pass
            def stop(self): pass

        def probe(worker,name,proto,args,lat,jit):
            with lock:
                claimed.append(name)
                if len(claimed)==5:
                    done.set()
            if name=='A':
                slow_completed.append(done.wait(1))
            return core.Result(name=name,provider='',proto=proto,latency_ms=lat,
                               speeds_mbps=[],median_mbps=None,best_mbps=None,status='ok')

        with ExitStack() as stack:
            for name,value in [('find_mihomo_bin','fake'),('extract_proxies',proxies),
                               ('physical_interface','en0'),('build_hosts',{}),
                               ('probe_latency_pool',{p['name']:(100,1) for p in proxies}),
                               ('start_intelligence_enrichment',None)]:
                stack.enter_context(mock.patch.object(workers,name,return_value=value))
            stack.enter_context(mock.patch.object(workers,'finish_intelligence_enrichment'))
            stack.enter_context(mock.patch.object(workers,'Worker',Worker))
            stack.enter_context(mock.patch.object(workers,'_probe_node_in_worker',side_effect=probe))
            stack.enter_context(redirect_stdout(io.StringIO()))
            results = workers.run_pool(list('ABCDE'),{},self.args('ip',workers=2),main_api=mock.Mock())
        self.assertEqual(slow_completed,[True])
        self.assertEqual(sorted(claimed),list('ABCDE'))
        self.assertEqual(len(results),5)
        self.assertTrue(all(names==set('ABCDE') for names in loaded))

    def test_worker_start_failure_preserves_main_probe_success(self):
        with mock.patch.object(workers.Worker,'start',side_effect=RuntimeError('fixture')):
            # Use a separate harness: the shared fixture mocks Worker itself.
            with ExitStack() as stack:
                for name,value in [('find_mihomo_bin','fake'),('extract_proxies',fixtures.RunPoolMainApiTest.PROXIES),
                                   ('physical_interface','en0'),('build_hosts',{}),
                                   ('probe_latency_pool',{'A':(100,1),'B':(200,2)}),
                                   ('start_intelligence_enrichment',None)]:
                    stack.enter_context(mock.patch.object(workers,name,return_value=value))
                stack.enter_context(mock.patch.object(workers,'finish_intelligence_enrichment'))
                stack.enter_context(redirect_stdout(io.StringIO()))
                results = workers.run_pool(['A','B'],{},self.args('ip'),main_api=mock.Mock())
        self.assertEqual(len(results),2)
        self.assertTrue(all(r.latency_ms is not None and r.probe_successes > 0 for r in results))

    def test_recovered_candidate_finishes_exit_before_download_and_is_enriched(self):
        trace = []
        args = self.args('quick',top_n=1,workers=2)
        enricher = mock.Mock()
        def probe(worker,name,proto,a,lat=None,jit=None):
            trace.append(('probe',name,a.no_ip))
            return core.Result(name=name,provider='',proto=proto,latency_ms=10 if name=='B' else 100,
                               speeds_mbps=[],median_mbps=None,best_mbps=None,status='ok',
                               exit_ipv4='1.1.1.1' if not a.no_ip else None)
        def speed(worker,r,a):
            trace.append(('speed',r.name,r.exit_ipv4))
            r.sample_mb=10
            r.median_mbps=20
        with ExitStack() as stack:
            for name,value in [('find_mihomo_bin','fake'),('extract_proxies',fixtures.RunPoolMainApiTest.PROXIES),
                               ('physical_interface','en0'),('build_hosts',{}),
                               ('probe_latency_pool',{'A':(100,1),'B':(None,None)}),
                               ('start_intelligence_enrichment',enricher)]:
                stack.enter_context(mock.patch.object(workers,name,return_value=value))
            stack.enter_context(mock.patch.object(workers,'finish_intelligence_enrichment'))
            stack.enter_context(mock.patch.object(workers,'Worker'))
            stack.enter_context(mock.patch.object(workers,'_probe_node_in_worker',side_effect=probe))
            stack.enter_context(mock.patch.object(workers,'_speed_node_in_worker',side_effect=speed))
            stack.enter_context(redirect_stdout(io.StringIO()))
            results = workers.run_pool(['A','B'],{},args,main_api=mock.Mock())
        b_events = [event for event in trace if event[1]=='B']
        self.assertEqual(b_events,[('probe','B',True),('probe','B',False),('speed','B','1.1.1.1')])
        enricher.submit_result.assert_called_once_with(next(r for r in results if r.name=='B'))
