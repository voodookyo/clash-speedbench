"""Parent-clock milestones and bounded persisted metrics, no network."""
from contextlib import closing
import io
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
import speedbench_db as db
import speedbench_jobs as jobs
import speedbench_progress as progress
from speedbench_tasks import resolve_config
from tests import test_cli_partial_history as cli
from tests import test_phase1_probe as pool_fixtures
import clash_speedbench as core


class TaskMilestonesTest(unittest.TestCase):
    def setUp(self):
        self.now=100.0
        patch=mock.patch.object(jobs,'time',SimpleNamespace(monotonic=lambda:self.now))
        patch.start();self.addCleanup(patch.stop)
        self.store=jobs.JobStore();self.job=self.store.create(resolve_config({'mode':'quick'}))
    def publish(self,kind,result):self.store.publish(self.job,kind,node_id='n',payload={'result':result})
    def test_all_five_use_acceptance_clock_and_first_values_are_immutable(self):
        self.now=102;self.publish('node_probe',{'latency_ms':20,'probe_attempts':1})
        self.now=104;self.publish('node_measurement',{'median_mbps':30,'measurement_scope':{'bandwidth':'completed'}})
        self.now=105;self.store.publish(self.job,'milestone',payload={'milestone':'network_complete','elapsed_ms':0})
        self.now=108;self.store.publish(self.job,'milestone',payload={'milestone':'intelligence_complete'})
        self.now=109;self.store.complete_cleanup(self.job)
        self.now=120;self.store.publish(self.job,'milestone',payload={'milestone':'network_complete'})
        self.assertEqual(self.store.snapshot(self.job)['milestones'],dict(first_result=2000,first_recommendation=4000,network_complete=5000,intelligence_complete=8000,cleanup_complete=9000))
    def test_pending_zero_sample_is_not_first_result_and_partial_download_is_not_recommendation(self):
        self.publish('node_probe',{'probe_attempts':0,'status':'probe-pending'})
        self.assertEqual(self.store.snapshot(self.job)['milestones'],{})
        self.now=103;self.publish('node_measurement',{'median_mbps':30,'measurement_scope':{'bandwidth':'partial'}})
        self.assertEqual(self.store.snapshot(self.job)['milestones'],{'first_result':3000})
    def test_ip_target_requires_usable_intelligence_not_latency_or_download(self):
        self.store.transition(self.job,'failed');self.job=self.store.create(resolve_config({'mode':'ip','target_profile':'ip'}))
        self.now=101;self.publish('node_measurement',{'median_mbps':100,'latency_ms':20})
        self.now=102;self.publish('node_intelligence',{'ip_quality_score':None,'ip_grade':None})
        self.assertNotIn('first_recommendation',self.store.snapshot(self.job)['milestones'])
        self.now=104;self.publish('node_intelligence',{'ip_quality_score':50,'ip_grade':'C'})
        self.assertEqual(self.store.snapshot(self.job)['milestones']['first_recommendation'],4000)
    def test_child_cannot_forge_cleanup_or_first_result_and_private_payload_is_dropped(self):
        for value in ('cleanup_complete','first_result','CANARY'):
            with self.assertRaises(jobs.JobError):self.store.publish(self.job,'milestone',payload={'milestone':value})
        self.store.publish(self.job,'milestone',payload={'milestone':'network_complete','key':'CANARY','elapsed_ms':-1})
        self.assertNotIn('CANARY',json.dumps(self.store.snapshot(self.job)))
        self.assertNotIn('CANARY',json.dumps(self.store.read(self.job,0)))
    def test_cancelled_task_does_not_invent_unfinished_milestones(self):
        self.now=101;self.publish('node_probe',{'probe_attempts':2,'probe_successes':0})
        self.store.transition(self.job,'cancelling');self.now=105;self.store.complete_cleanup(self.job)
        self.store.transition(self.job,'cancelled')
        before=self.store.snapshot(self.job)['milestones']
        self.assertEqual(before,dict(first_result=1000,cleanup_complete=5000))
        self.assertFalse(self.store.complete_cleanup(self.job))
        self.assertEqual(self.store.snapshot(self.job)['milestones'],before)
    def test_counters_survive_emitter_store_and_db_with_strict_whitelist(self):
        stream=io.StringIO();emitter=progress.ProgressEmitter(self.job,stream)
        emitter.emit('phase_finished',payload={'metrics':{'provider':dict(duration_ms=2,attempts=1,successes=1,bytes=0,
            counters=dict(api_calls=1,cache_hits=2,timeouts=0,api_key='CANARY',bad=float('nan'),worker_count=True))}})
        record=progress.parse_record(stream.getvalue(),self.job)
        self.store.publish(self.job,record['type'],payload=record['payload'])
        self.store.publish(self.job,record['type'],payload=record['payload'])
        self.assertEqual(self.store.snapshot(self.job)['metrics']['provider']['counters'],dict(api_calls=2,cache_hits=4,timeouts=0))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'h.db';db.save_task(path,self.store.snapshot(self.job))
            self.assertEqual(db.task_snapshot(path,self.job)['metrics']['provider']['counters']['api_calls'],2)
            self.assertNotIn('CANARY',stream.getvalue())
    def test_milestones_persist_without_schema_or_raw_rewrite_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'h.db';raw='{"ts":"fixture", "results": []}'
            source=path.with_suffix('.jsonl');source.write_text(raw+'\n',encoding='utf-8');db.import_jsonl(path,source)
            self.now=101;self.publish('node_probe',{'probe_attempts':1})
            snapshot=self.store.snapshot(self.job);snapshot['milestones']['url']='CANARY'
            snapshot['milestones']['network_complete']=float('nan')
            db.save_task(path,snapshot);db.save_task(path,snapshot)
            self.assertEqual(db.task_snapshot(path,self.job)['milestones'],{'first_result':1000})
            db.interrupt_tasks(path)
            self.assertEqual(db.task_snapshot(path,self.job)['milestones'],{'first_result':1000})
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute('SELECT raw FROM runs').fetchone()[0],raw)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM task_metrics WHERE phase=?',('milestones',)).fetchone()[0],1)
                self.assertNotIn('CANARY',repr(connection.execute('SELECT * FROM task_metrics').fetchall()))

    def test_actual_serial_path_marks_network_after_restore_before_intelligence_join(self):
        fixture=cli.CliPartialHistoryTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        stream=io.StringIO();emitter=progress.ProgressEmitter(self.job,stream);order=[]
        real_milestone=core.milestone
        def mark(args,name):order.append(name);real_milestone(args,name)
        with mock.patch.object(core.ProgressEmitter,'from_environment',return_value=emitter), \
                mock.patch.object(core,'probe_latency',return_value=core.ProbeStats(20,0,3,3,0)), \
                mock.patch.object(core,'apply_path'),mock.patch.object(core,'restore_groups',side_effect=lambda *a:order.append('restore')), \
                mock.patch.object(core,'curl_speed',return_value=(30,'ok',1,10)),mock.patch.object(core.time,'sleep'), \
                mock.patch.object(core,'milestone',side_effect=mark), \
                mock.patch.object(core,'finish_intelligence_enrichment',side_effect=lambda *a:order.append('intel_join')):
            code,_,_=fixture.invoke(None,serial=True,extra=['--mb','10'])
        self.assertEqual(code,0);self.assertEqual(order,['restore','network_complete','intel_join','intelligence_complete'])
        self.assertIn('network_complete',[progress.parse_record(line,self.job)['payload'].get('milestone') for line in stream.getvalue().splitlines()])

    def test_worker_flow_emits_end_markers_and_counts_successful_readiness(self):
        args=pool_fixtures.mk_args(no_ip=True);stream=io.StringIO()
        args.progress=progress.ProgressEmitter(self.job,stream)
        pool_fixtures.RunPoolMainApiTest()._run(args,mock.Mock(),{'A':(20,1),'B':(30,1)})
        records=[progress.parse_record(line,self.job) for line in stream.getvalue().splitlines()]
        self.assertEqual([e['payload']['milestone'] for e in records if e['type']=='milestone'],['network_complete','intelligence_complete'])
        worker_metrics=[e['payload']['metrics']['worker_start'] for e in records
            if e['type']=='phase_finished' and 'worker_start' in e['payload']['metrics']]
        self.assertEqual(sum(m['attempts'] for m in worker_metrics),1)
        self.assertEqual(sum(m['counters']['worker_count'] for m in worker_metrics),1)

    def test_delayed_active_checkpoint_cannot_drop_or_change_first_milestones(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'h.db'
            self.now=101;self.publish('node_probe',{'probe_attempts':1});old=self.store.snapshot(self.job)
            self.now=103;self.store.publish(self.job,'milestone',payload={'milestone':'network_complete'})
            db.save_task(path,self.store.snapshot(self.job))
            old['milestones']['first_result']=9999;db.save_task(path,old)
            self.assertEqual(db.task_snapshot(path,self.job)['milestones'],dict(first_result=1000,network_complete=3000))


if __name__=='__main__':unittest.main()
