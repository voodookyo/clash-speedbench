"""Per-request probe retention, independent paths, no external requests."""
import contextlib
import io
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import clash_speedbench as core
import speedbench_db as db
import speedbench_jobs as jobs
import speedbench_progress as progress
import speedbench_workers as workers
from speedbench_tasks import resolve_config
from tests import test_cli_partial_history as cli
from tests import test_phase1_probe as pool_fixtures


class PartialProbeTest(unittest.TestCase):
    def setUp(self):
        self.fixture=cli.CliPartialHistoryTest();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.stream=io.StringIO();self.job='job_'+'a'*32
        self.args=SimpleNamespace(probe_count=3,delay_timeout=5000,no_ip=True,
            progress=progress.ProgressEmitter(self.job,self.stream),_result_journal=progress.ResultJournal())

    def metric(self,name):
        spans=[progress.parse_record(line,self.job) for line in self.stream.getvalue().splitlines()]
        values=[v['payload']['metrics'][name] for v in spans if v and v['type']=='phase_finished'
                and name in v['payload'].get('metrics',{})]
        return {key:sum(v[key] for v in values) for key in ('attempts','successes','bytes')}

    def test_primitive_notifies_each_completed_sample_before_interrupt(self):
        api=mock.Mock();api.proxy_delay.side_effect=[20,None,KeyboardInterrupt()]
        samples=[];started=[]
        with mock.patch.object(core,'cancel_requested',return_value=False),self.assertRaises(KeyboardInterrupt):
            core.probe_latency(api,'A',5000,count=3,
                on_sample=lambda stats,complete:samples.append((stats.to_dict(),complete)),
                on_attempt=lambda:started.append(1))
        self.assertEqual(len(started),3)
        self.assertEqual([s[0]['attempts'] for s in samples],[1,2,2])
        self.assertEqual(samples[-1][0]['failures'],1);self.assertFalse(samples[-1][1])

    def test_cancel_between_samples_does_not_invent_unstarted_attempt(self):
        api=mock.Mock();api.proxy_delay.side_effect=[20,None]
        with mock.patch.object(core,'cancel_requested',side_effect=[False,False,True]),self.assertRaises(KeyboardInterrupt):
            core.observed_probe(api,'A',5000,self.args,source='serial',metric='delay')
        snapshot=self.args._result_journal.snapshot()[0]
        self.assertEqual(snapshot.probe_attempts,2);self.assertEqual(snapshot.probe_loss_pct,50)
        self.assertEqual(snapshot.probe_sources['serial']['started'],2)
        self.assertEqual(self.metric('delay')['attempts'],2)

    def test_pre_cancel_has_no_result_no_request_and_zero_metric(self):
        api=mock.Mock()
        with mock.patch.object(core,'cancel_requested',return_value=True),self.assertRaises(KeyboardInterrupt):
            core.observed_probe(api,'A',5000,self.args,source='serial',metric='delay')
        api.proxy_delay.assert_not_called();self.assertEqual(self.args._result_journal.snapshot(),[])
        self.assertEqual(self.metric('delay')['attempts'],0)

    def test_serial_cli_keeps_completed_samples_restores_and_marks_partial(self):
        api=mock.Mock();api.proxy_delay.side_effect=[20,None,KeyboardInterrupt()]
        real_probe=core.probe_latency
        def probe(_api,*args,**kwargs):return real_probe(api,*args,**kwargs)
        with mock.patch.object(core,'probe_latency',side_effect=probe), \
                mock.patch.object(core.ProgressEmitter,'from_environment',return_value=self.args.progress), \
                mock.patch.object(core,'apply_path'),mock.patch.object(core,'restore_groups') as restore, \
                mock.patch.object(core.time,'sleep'),mock.patch.object(core,'cancel_requested',return_value=False):
            code,_,_=self.fixture.invoke(None,serial=True,extra=['--mb','10'])
        self.assertEqual(code,130);restore.assert_called_once()
        result=self.fixture.records()[0]['results'][0]
        self.assertEqual(result['probe_attempts'],2);self.assertEqual(result['probe_failures'],1)
        self.assertEqual(result['probe_loss_pct'],50);self.assertEqual(result['latency_ms'],20)
        self.assertEqual(result['probe_sources']['serial']['started'],3)
        self.assertEqual(result['measurement_scope']['probe'],'partial')
        self.assertIsNone(result['ip_quality_score']);self.assertEqual(self.metric('delay')['attempts'],3)

    def test_main_pool_interrupt_publishes_completed_samples_not_fake_full_node(self):
        api=mock.Mock(timeout=5);api.proxy_delay.side_effect=[20,None,KeyboardInterrupt()]
        with mock.patch.object(core,'cancel_requested',return_value=False), \
                contextlib.redirect_stdout(io.StringIO()),self.assertRaises(KeyboardInterrupt):
            workers.probe_latency_pool(api,['A'],5000,progress_args=self.args)
        result=self.args._result_journal.snapshot()[0]
        self.assertEqual(result.probe_attempts,2);self.assertEqual(result.probe_sources['main']['started'],3)
        self.assertEqual(result.probe_sources['main']['status'],'partial')
        self.assertEqual(result.measurement_scope['probe'],'partial')

    def test_worker_interrupt_preserves_main_failure_and_partial_worker_counts(self):
        row=cli.row(probe_attempts=3,probe_successes=0,probe_failures=3)
        row.probe_sources={'main':dict(attempts=3,successes=0,failures=3,started=3,requested=3,status='completed')}
        progress.publish_result(self.args,'node_probe',row)
        api=mock.Mock();api.proxy_delay.side_effect=[None,20,KeyboardInterrupt()]
        with mock.patch.object(core,'cancel_requested',return_value=False),self.assertRaises(KeyboardInterrupt):
            workers._probe_node_in_worker(SimpleNamespace(api=api),'A','ss',self.args)
        result=self.args._result_journal.snapshot()[0]
        self.assertEqual(result.probe_sources['main']['failures'],3)
        self.assertEqual(result.probe_sources['worker']['failures'],1)
        self.assertEqual(result.probe_sources['worker']['started'],3)
        self.assertEqual(result.probe_loss_pct,50)
        self.assertEqual(self.metric('probe')['attempts'],3)

    def test_finished_worker_fallback_keeps_two_independent_paths_in_final_result(self):
        args=pool_fixtures.mk_args(no_ip=False)
        args.task_config=resolve_config({'mode':'ip'});args._result_journal=progress.ResultJournal()
        args.progress=self.args.progress
        main=mock.Mock(timeout=5);main.proxy_delay.return_value=None
        fallback=mock.Mock();fallback.proxy_delay.return_value=20
        worker=SimpleNamespace(api=fallback,proxy_url='http://fixture.invalid',
            start=lambda:None,stop=lambda:None,select=lambda name:None)
        with contextlib.ExitStack() as stack:
            for name,value in [('find_mihomo_bin','fixture'),('extract_proxies',[{'name':'A','type':'ss','server':'a.invalid'}]),
                               ('physical_interface','en0'),('build_hosts',{}),('Worker',worker),
                               ('fetch_exit_ips',(None,None,None))]:
                stack.enter_context(mock.patch.object(workers,name,return_value=value))
            stack.enter_context(mock.patch.object(core,'cancel_requested',return_value=False))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result=workers.run_pool(['A'],{'A':'ss'},args,main_api=main)[0]
        payload=core.result_to_dict(result)
        self.assertEqual(payload['probe_sources']['main']['loss_pct'],100)
        self.assertEqual(payload['probe_sources']['worker']['loss_pct'],0)
        self.assertEqual(payload['probe_attempts'],3);self.assertEqual(payload['probe_successes'],3)
        self.assertEqual(self.metric('delay')['attempts'],3);self.assertEqual(self.metric('probe')['attempts'],3)

    def test_observer_failure_cannot_repeat_real_network_attempt(self):
        api=mock.Mock();api.proxy_delay.return_value=20
        with mock.patch.object(core,'cancel_requested',return_value=False), \
                mock.patch.object(progress,'publish_result',side_effect=TypeError('CANARY')), \
                self.assertRaises(TypeError):
            core.observed_probe(api,'A',5000,self.args,source='serial',metric='delay')
        api.proxy_delay.assert_called_once()

    def test_legacy_three_argument_probe_adapter_stays_compatible(self):
        calls=[]
        def probe(api,name,timeout):calls.append(name);return 20,0
        stats=core.observed_probe(mock.Mock(),'A',5000,self.args,source='worker',metric='probe',probe_function=probe)
        self.assertEqual(stats.attempts,3);self.assertEqual(calls,['A'])

    def test_nonfinite_or_invalid_delays_are_failures_not_nan_results(self):
        api=mock.Mock();api.proxy_delay.side_effect=[float('nan'),float('inf'),-1,'bad',20]
        with mock.patch.object(core,'cancel_requested',return_value=False):
            stats=core.probe_latency(api,'A',5000,count=5)
        self.assertEqual(stats.attempts,5);self.assertEqual(stats.successes,1);self.assertEqual(stats.latency_ms,20)

    def test_sources_protocol_and_sqlite_are_whitelisted_and_do_not_change_raw(self):
        store=jobs.JobStore();job=store.create(resolve_config())
        sources={'main':dict(attempts=3,successes=0,failures=3,started=3,requested=3,status='completed',api_key='CANARY'),
                 'worker':dict(attempts=2,successes=1,failures=1,started=3,requested=3,status='partial',url='CANARY'),
                 'CANARY':{'password':'CANARY'}}
        row=cli.row();row.probe_sources=sources
        payload=core.result_to_dict(row)
        self.assertNotIn('CANARY',json.dumps(payload))
        store.publish(job,'node_probe',node_id='A',payload={'result':payload})
        store.transition(job,'failed')
        database=self.fixture.home/'h.db'
        db.import_jsonl(database,self.fixture.history);db.save_task(database,store.snapshot(job))
        saved=db.task_snapshot(database,job)['results'][0]['probe_sources']
        self.assertEqual(saved['main']['failures'],3);self.assertEqual(saved['worker']['started'],3)
        with contextlib.closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('SELECT raw FROM runs').fetchone()[0],self.fixture.original.decode().rstrip('\n'))

    def test_proto_spelling_changes_do_not_duplicate_one_runtime_node_in_job(self):
        store=jobs.JobStore();job=store.create(resolve_config())
        for proto in ('Shadowsocks','ss'):
            progress.publish_result(self.args,'node_probe',cli.row(proto=proto))
        for line in self.stream.getvalue().splitlines():
            event=progress.parse_record(line,self.job)
            store.publish(job,event['type'],node_id=event['node_id'],payload=event['payload'])
        self.assertEqual(len(store.snapshot(job)['results']),1)

    def test_closed_progress_transport_does_not_discard_completed_probe_samples(self):
        self.stream.close();api=mock.Mock();api.proxy_delay.side_effect=[20,None,KeyboardInterrupt()]
        with mock.patch.object(core,'cancel_requested',return_value=False),self.assertRaises(KeyboardInterrupt):
            core.observed_probe(api,'A',5000,self.args,source='serial',metric='delay')
        self.assertEqual(self.args._result_journal.snapshot()[0].probe_attempts,2)

    def test_pool_interrupt_stops_next_probes_and_queued_nodes_then_joins_active_calls(self):
        real_observed=core.observed_probe;cancel_functions={};calls=[];lock=threading.Lock()
        b_started=threading.Event()
        def observed(api,name,timeout,args,**options):
            cancel_functions[name]=options.get('cancel',lambda:False)
            return real_observed(api,name,timeout,args,**options)
        class Api:
            timeout=5
            def proxy_delay(self,name,url,timeout):
                with lock:
                    calls.append(name);number=calls.count(name)
                if name=='A':
                    if not b_started.wait(2):raise RuntimeError('fixture B did not start')
                    if number==2:raise KeyboardInterrupt()
                elif name=='B':
                    b_started.set();deadline=time.monotonic()+2
                    while not cancel_functions[name]() and time.monotonic()<deadline:time.sleep(.001)
                return 20
        with mock.patch.object(core,'cancel_requested',return_value=False), \
                mock.patch.object(workers,'observed_probe',side_effect=observed), \
                contextlib.redirect_stdout(io.StringIO()),self.assertRaises(KeyboardInterrupt):
            workers.probe_latency_pool(Api(),['A','B','C','D'],5000,max_workers=2,progress_args=self.args)
        self.assertEqual(calls.count('A'),2);self.assertEqual(calls.count('B'),1)
        self.assertNotIn('C',calls);self.assertNotIn('D',calls)
        values={r.name:r for r in self.args._result_journal.snapshot()}
        self.assertEqual(values['B'].probe_sources['main']['attempts'],1)


if __name__=='__main__':unittest.main()
