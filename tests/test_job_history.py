from contextlib import closing
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
import speedbench_db as db
from speedbench_jobs import JobStore
from speedbench_tasks import resolve_config


class JobHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name)/'history.db'
        self.store = JobStore()
        self.job = self.store.create(resolve_config({'mode':'quick'}))

    def test_partial_results_persist_independently_of_legacy_raw(self):
        self.store.publish(self.job,'node_probe',node_id='n',payload={'result':{'name':'node','latency_ms':12}})
        self.store.transition(self.job,'cancelling')
        self.store.transition(self.job,'cancelled')
        db.save_task(self.database,self.store.snapshot(self.job))
        task = db.task_history(self.database)[0]
        self.assertTrue(task['partial'])
        self.assertEqual(db.task_snapshot(self.database,self.job)['results'][0]['latency_ms'],12)
        self.assertEqual(db.all_runs(self.database),[])

    def test_repeat_checkpoint_does_not_duplicate_task_or_results(self):
        snapshot = self.store.snapshot(self.job)
        for _ in range(3): db.save_task(self.database,snapshot)
        self.assertEqual(len(db.task_history(self.database)),1)
        self.assertEqual(db.task_snapshot(self.database,self.job)['results'],[])

    def test_terminal_status_cannot_be_regressed_by_delayed_checkpoint(self):
        queued = self.store.snapshot(self.job)
        self.store.transition(self.job,'failed')
        db.save_task(self.database,self.store.snapshot(self.job))
        db.save_task(self.database,queued)
        self.assertEqual(db.task_snapshot(self.database,self.job)['status'],'failed')

    def test_metrics_are_actual_counts_whitelisted_and_idempotent(self):
        snapshot = self.store.snapshot(self.job)
        snapshot['metrics'] = {'download':{'duration_ms':250,'attempts':2,'successes':1,
                              'bytes':1234,'counters':{'cache_hits':1,'api_key':'CANARY'}}}
        db.save_task(self.database,snapshot)
        db.save_task(self.database,snapshot)
        task = db.task_snapshot(self.database,self.job)
        self.assertEqual(task['metrics']['download']['bytes'],1234)
        self.assertNotIn('CANARY',json.dumps(task))
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM task_metrics').fetchone()[0],1)

    def test_old_raw_stays_exact_after_task_schema_migration(self):
        jsonl = self.database.with_suffix('.jsonl')
        raw = '{"ts":"2026-01-01T01:01:01", "results": []}'
        jsonl.write_text(raw+'\n',encoding='utf-8')
        db.import_jsonl(self.database,jsonl)
        db.save_task(self.database,self.store.snapshot(self.job))
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT raw FROM runs').fetchone()[0],raw)

    def test_secrets_are_absent_from_all_task_columns(self):
        snapshot = self.store.snapshot(self.job)
        snapshot['config']['api_key'] = 'CANARY'
        snapshot['config']['config_file'] = 'CANARY'
        snapshot['results'] = [dict(name='node',latency_ms=10,password='CANARY',raw='CANARY')]
        db.save_task(self.database,snapshot)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertNotIn('CANARY',repr(conn.execute('SELECT * FROM task_runs').fetchall()))

    def test_server_restart_marks_active_persisted_jobs_interrupted_only(self):
        db.save_task(self.database,self.store.snapshot(self.job))
        self.store.transition(self.job,'failed')
        done = self.store.snapshot(self.job)
        other = self.store.create(resolve_config())
        db.save_task(self.database,done)
        db.save_task(self.database,self.store.snapshot(other))
        self.assertEqual(db.interrupt_tasks(self.database),1)
        self.assertEqual(db.task_snapshot(self.database,other)['status'],'interrupted')
        self.assertEqual(db.task_snapshot(self.database,self.job)['status'],'failed')
