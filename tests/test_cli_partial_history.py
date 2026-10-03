"""CLI failure/cancellation retention; temporary history, no real network."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import clash_speedbench as core
import speedbench_db as db
import speedbench_progress as progress
import speedbench_workers as workers


def row(name='A', **options):
    result=core.Result(name=name,provider='',proto='ss',latency_ms=20,
                      speeds_mbps=[],median_mbps=None,best_mbps=None,status='probe-only')
    for key,value in options.items():setattr(result,key,value)
    return result


class CliPartialHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.history=self.home/'h.jsonl';self.csv=self.home/'out.csv'
        self.original=b'{"ts":"fixture-original", "results": []}\n';self.history.write_bytes(self.original)

    def invoke(self,body,*,extra=(),serial=False,csv_error=False,ip=False):
        api=mock.Mock();api.get.side_effect=lambda path: {
            '/version':{'version':'fixture'},'/configs':{'mode':'rule','mixed-port':7897},
            '/proxies':{'proxies':{'GLOBAL':{'type':'Selector','all':['A'],'now':'A'},
                                  'A':{'type':'Shadowsocks'}}}}[path]
        argv=['clash_speedbench.py','--yes','--workers','1' if serial else '2',*([] if ip else ['--no-ip']),
              '--history',str(self.history),'--output',str(self.csv),*extra]
        stdout=io.StringIO();stderr=io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ,{'SPEEDBENCH_HOME':str(self.home)},clear=True))
            stack.enter_context(mock.patch('sys.argv',argv))
            stack.enter_context(mock.patch.object(core,'connect_controller',return_value=api))
            stack.enter_context(mock.patch.object(core.signal,'signal'))
            stack.enter_context(mock.patch.object(core,'clear_cancel_request'))
            stack.enter_context(mock.patch.object(core.source_catalog,'discover_catalog',return_value={'nodes':[],'sources':[]}))
            switch=stack.enter_context(mock.patch.object(core,'auto_switch_best'))
            if not serial:stack.enter_context(mock.patch.object(workers,'run_pool',side_effect=body))
            if csv_error:stack.enter_context(mock.patch.object(core,'write_csv',side_effect=OSError('fixture output unavailable')))
            stack.enter_context(contextlib.redirect_stdout(stdout));stack.enter_context(contextlib.redirect_stderr(stderr))
            try:code=core.main()
            except KeyboardInterrupt:self.fail('CLI did not convert cancellation to a partial report')
            if code!=0:switch.assert_not_called()
        self.assertTrue(self.history.read_bytes().startswith(self.original))
        return code,stdout.getvalue()+stderr.getvalue(),api

    def records(self):
        return [json.loads(line) for line in self.history.read_text(encoding='utf-8').splitlines()][1:]

    def producer(self,error,measured=False):
        def produce(names,protos,args,**kw):
            result=row()
            if measured:result.speeds_mbps=[40.0];result.median_mbps=result.best_mbps=40.0
            progress.publish_result(args,'node_measurement' if measured else 'node_probe',result,phase_name='probing')
            progress.publish_result(args,'node_probe',result,phase_name='probing')
            raise error
        return produce

    def test_phase1_cancel_keeps_probe_once_and_marks_unfinished_not_unreachable(self):
        code,output,_=self.invoke(self.producer(KeyboardInterrupt()),extra=['--auto-switch'])
        self.assertEqual(code,130)
        record=self.records()[0];self.assertTrue(record['task']['partial']);self.assertEqual(record['task']['status'],'cancelled')
        self.assertEqual(len(record['results']),1)
        result=record['results'][0];self.assertEqual(result['latency_ms'],20)
        self.assertIsNone(result['median_mbps']);self.assertIsNone(result['ip_quality_score'])
        self.assertEqual(result['measurement_scope']['bandwidth'],'cancelled');self.assertNotIn('不通',result['tags'])

    def test_cleanup_failure_keeps_measurement_and_sqlite_raw_exact(self):
        code,output,api=self.invoke(self.producer(workers.WorkerCleanupError('CANARY private config'),True))
        self.assertEqual(code,3);self.assertNotIn('CANARY',output)
        record=self.records()[0];self.assertEqual(record['task']['status'],'failed');self.assertTrue(record['task']['partial'])
        self.assertEqual(record['results'][0]['median_mbps'],40.0)
        database=self.home/'h.db';self.assertEqual(db.import_jsonl(database,self.history),2)
        self.assertEqual(db.import_jsonl(database,self.history),0)
        with contextlib.closing(sqlite3.connect(database)) as conn:
            raws=[r[0] for r in conn.execute('SELECT raw FROM runs ORDER BY id')]
            self.assertEqual(raws[0],self.original.decode().rstrip('\n'))
            self.assertEqual(raws[1],self.history.read_text(encoding='utf-8').splitlines()[1])
        api.patch.assert_not_called()

    def test_unexpected_failure_is_redacted_and_keeps_finished_measurement(self):
        code,output,_=self.invoke(self.producer(ValueError('CANARY private transport'),True))
        self.assertEqual(code,1);self.assertNotIn('CANARY',output+json.dumps(self.records()))
        self.assertEqual(self.records()[0]['task']['status'],'failed')

    def test_csv_failure_cannot_prevent_partial_history_and_does_not_claim_saved_csv(self):
        code,output,_=self.invoke(self.producer(KeyboardInterrupt(),True),csv_error=True)
        self.assertEqual(code,130);self.assertEqual(len(self.records()),1)
        self.assertFalse(self.csv.exists());self.assertEqual(self.records()[0]['csv'],'')
        self.assertNotIn('CSV 已保存',output)

    def test_failed_exports_do_not_claim_results_were_saved(self):
        with mock.patch.object(core,'append_history',return_value=False):
            code,output,_=self.invoke(self.producer(KeyboardInterrupt(),True),csv_error=True)
        self.assertEqual(code,130);self.assertEqual(self.records(),[])
        self.assertIn('未持久化',output);self.assertNotIn('已保留',output)

    def test_normal_reporting_csv_failure_falls_back_to_single_partial_history(self):
        code,_,_=self.invoke(lambda *a,**k:[row(median_mbps=40.0,best_mbps=40.0,speeds_mbps=[40.0])],csv_error=True)
        self.assertEqual(code,1);self.assertEqual(len(self.records()),1)
        self.assertTrue(self.records()[0]['task']['partial']);self.assertEqual(self.records()[0]['task']['status'],'failed')

    def test_child_final_progress_contains_partial_scope_and_preserved_score(self):
        stream=io.StringIO();emitter=progress.ProgressEmitter('job_'+'b'*32,stream)
        with mock.patch.object(core.ProgressEmitter,'from_environment',return_value=emitter):
            code,_,_=self.invoke(self.producer(workers.WorkerCleanupError('CANARY'),True))
        records=[progress.parse_record(line,emitter.job_id) for line in stream.getvalue().splitlines()]
        final=[r for r in records if r and r['type']=='node_intelligence'][-1]['payload']['result']
        self.assertEqual(code,3);self.assertEqual(final['measurement_scope']['bandwidth'],'partial')
        self.assertEqual(final['median_mbps'],40.0);self.assertGreater(final['network_score'],0)

    def test_partial_task_metadata_rejects_arbitrary_status(self):
        core.append_history([row()],self.history,10,1,None,task={'status':'CANARY','raw':'CANARY','partial':True})
        self.assertNotIn('CANARY',json.dumps(self.records()));self.assertNotIn('status',self.records()[0]['task'])

    def test_no_history_option_and_failure_before_first_result_do_not_append(self):
        code,_,_=self.invoke(self.producer(KeyboardInterrupt()),extra=['--no-history'])
        self.assertEqual(code,130);self.assertEqual(self.records(),[])
        def fail(*a,**k):raise workers.WorkerCleanupError('CANARY')
        code,_,_=self.invoke(fail)
        self.assertEqual(code,3);self.assertEqual(self.records(),[])

    def test_partial_export_error_cannot_mask_cleanup_failure_code(self):
        with mock.patch.object(core,'save_partial_report',side_effect=OSError('CANARY private path')):
            code,output,_=self.invoke(self.producer(workers.WorkerCleanupError('CANARY'),True))
        self.assertEqual(code,3);self.assertNotIn('CANARY',output);self.assertEqual(self.records(),[])

    def test_post_commit_interrupt_does_not_duplicate_or_rewrite_history(self):
        normal_report=core.report
        def report(*args,**kwargs):normal_report(*args,**kwargs);raise KeyboardInterrupt()
        with mock.patch.object(core,'report',side_effect=report):
            code,_,_=self.invoke(lambda *a,**k:[row(median_mbps=40.0,best_mbps=40.0,speeds_mbps=[40.0])])
        self.assertEqual(code,130);self.assertEqual(len(self.records()),1)

    def test_serial_second_round_cancel_keeps_first_sample_and_restores_before_report(self):
        with mock.patch.object(core,'probe_latency',return_value=core.ProbeStats(20,0,3,3,0)), \
                mock.patch.object(core,'apply_path'),mock.patch.object(core,'restore_groups') as restore, \
                mock.patch.object(core,'curl_speed',side_effect=[(50.0,'ok',9.0,1.2),KeyboardInterrupt()]), \
                mock.patch.object(core.time,'sleep'):
            code,_,api=self.invoke(None,serial=True,extra=['--mb','10','--rounds','2','--auto-switch'])
        self.assertEqual(code,130);restore.assert_called_once()
        self.assertEqual(api.patch.call_args_list,[mock.call('/configs',{'mode':'global'}),mock.call('/configs',{'mode':'rule'})])
        record=self.records()[0];result=record['results'][0]
        self.assertEqual(result['samples_mbps'],[50.0]);self.assertEqual(result['download_bytes'],1200000)
        self.assertEqual(result['measurement_scope']['bandwidth'],'partial')
        self.assertEqual(record['task']['status'],'cancelled')

    def test_serial_export_failure_cannot_turn_cancellation_into_failure(self):
        with mock.patch.object(core,'probe_latency',return_value=core.ProbeStats(20,0,3,3,0)), \
                mock.patch.object(core,'apply_path'),mock.patch.object(core,'restore_groups') as restore, \
                mock.patch.object(core,'curl_speed',side_effect=KeyboardInterrupt()), \
                mock.patch.object(core.time,'sleep'), \
                mock.patch.object(core,'save_partial_report',side_effect=OSError('CANARY')):
            code,output,_=self.invoke(None,serial=True,extra=['--mb','10'])
        self.assertEqual(code,130);restore.assert_called_once();self.assertNotIn('CANARY',output)

    def test_serial_ipv6_cancel_keeps_completed_ipv4_and_download(self):
        def exit_ips(proxy,timeout,**options):
            options['on_result']('ipv4','192.0.2.1','completed')
            raise KeyboardInterrupt()
        with mock.patch.object(core,'probe_latency',return_value=core.ProbeStats(20,0,3,3,0)), \
                mock.patch.object(core,'apply_path'),mock.patch.object(core,'restore_groups'), \
                mock.patch.object(core,'curl_speed',return_value=(50.0,'ok',9.0,1.2)), \
                mock.patch.object(core,'fetch_exit_ips',side_effect=exit_ips),mock.patch.object(core.time,'sleep'):
            code,_,_=self.invoke(None,serial=True,ip=True,extra=['--mb','10'])
        self.assertEqual(code,130);result=self.records()[0]['results'][0]
        self.assertEqual(result['median_mbps'],50.0);self.assertEqual(result['exit_ipv4'],'192.0.2.1')
        self.assertIsNone(result['exit_ipv6']);self.assertEqual(result['exit_status'],{'ipv4':'completed','ipv6':'cancelled'})
        self.assertEqual(result['measurement_scope']['bandwidth'],'completed')

    def test_actual_pool_mid_probe_and_dns_cancel_retains_completed_probes(self):
        real_pool=workers.run_pool
        definitions=[{'name':'A','type':'ss','server':'a.example.invalid'}]
        def body(names,protos,args,**options):return real_pool(names,protos,args,**options)
        def probing(api,names,timeout,**options):
            options['on_result']('A',core.ProbeStats(20,0,3,3,0),1,1)
            raise KeyboardInterrupt()
        for stage in ('probe','dns'):
            with self.subTest(stage=stage),contextlib.ExitStack() as stack:
                for name,value in [('find_mihomo_bin','fixture'),('find_config_file','fixture'),
                                   ('extract_proxies',definitions),('physical_interface','en0')]:
                    stack.enter_context(mock.patch.object(workers,name,return_value=value))
                stack.enter_context(mock.patch.object(workers,'probe_latency_pool',side_effect=probing if stage=='probe' else None,
                    return_value={'A':core.ProbeStats(20,0,3,3,0)}))
                stack.enter_context(mock.patch.object(workers,'build_hosts',side_effect=KeyboardInterrupt()))
                code,_,_=self.invoke(body,extra=['--mode','quick'])
                self.assertEqual(code,130);result=self.records()[-1]['results'][0]
                self.assertEqual(result['latency_ms'],20);self.assertEqual(result['probe_attempts'],3)

    def test_completed_legacy_record_shape_and_single_report_remain_compatible(self):
        code,_,_=self.invoke(lambda *a,**k:[row(median_mbps=40.0,best_mbps=40.0,speeds_mbps=[40.0])])
        self.assertEqual(code,0);self.assertEqual(len(self.records()),1)
        self.assertEqual(set(self.records()[0]),{'ts','mb','rounds','csv','results'})


class ResultJournalTest(unittest.TestCase):
    def test_journal_works_without_emitter_freezes_snapshots_and_deduplicates(self):
        journal=progress.ResultJournal();args=SimpleNamespace(_result_journal=journal)
        original=row(probe_attempts=3,probe_successes=2,probe_failures=1)
        progress.publish_result(args,'node_probe',original)
        original.latency_ms=999
        self.assertEqual(journal.snapshot()[0].latency_ms,20)
        partial=row(exit_ipv4='192.0.2.1')
        progress.publish_result(args,'node_exit',partial)
        values=journal.snapshot();self.assertEqual(len(values),1)
        self.assertEqual(values[0].probe_attempts,3);self.assertEqual(values[0].exit_ipv4,'192.0.2.1')
        values[0].latency_ms=888;self.assertEqual(journal.snapshot()[0].latency_ms,20)

    def test_worker_second_round_interruption_keeps_real_first_sample(self):
        args=SimpleNamespace(mb=10,max_time=3,rounds=2,multi=False,settle=0,
                             _result_journal=progress.ResultJournal(),mode='quick')
        result=row();worker=mock.Mock(proxy_url='http://fixture.invalid')
        with mock.patch.object(workers,'cancel_requested',return_value=False), \
                mock.patch.object(workers,'curl_speed',side_effect=[(50.0,'ok',9.0,1.2),KeyboardInterrupt()]), \
                self.assertRaises(KeyboardInterrupt):workers._speed_node_in_worker(worker,result,args)
        snapshot=args._result_journal.snapshot()[0]
        self.assertEqual(snapshot.speeds_mbps,[50.0]);self.assertEqual(snapshot.download_bytes,1200000)
        self.assertEqual(snapshot.measurement_scope['bandwidth'],'partial')
