import json
import threading
import unittest
from speedbench_jobs import JobStore, JobError, PhaseTimer
from speedbench_tasks import resolve_config


class JobStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(event_limit=3)
        self.job = self.store.create(resolve_config({'mode':'quick'}))

    def test_single_owner_atomic_under_concurrent_create(self):
        store = JobStore()
        answers = []
        def create():
            try:
                answers.append(store.create(resolve_config()))
            except JobError:
                answers.append(None)
        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(a is not None for a in answers),1)

    def test_parallel_publication_has_unique_monotonic_sequence(self):
        store = JobStore(event_limit=1000)
        job = store.create(resolve_config())
        threads = [threading.Thread(target=lambda:[store.publish(job,'node_probe') for _ in range(10)])
                   for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual([e['seq'] for e in store.read(job,0)['events']],list(range(1,82)))

    def test_sequence_increments_and_old_cursor_requires_resync(self):
        for i in range(5):
            self.store.publish(self.job,'node_probe',node_id='n',payload={'result':{'latency_ms':i}})
        response = self.store.read(self.job,0)
        self.assertTrue(response['resync'])
        self.assertEqual(response['snapshot']['results'][0]['latency_ms'],4)
        delta = self.store.read(self.job,4)
        self.assertFalse(delta['resync'])
        self.assertEqual([e['seq'] for e in delta['events']],[5,6])

    def test_node_updates_merge_once_not_duplicate_rows(self):
        self.store.publish(self.job,'node_probe',node_id='n',payload={'result':{'name':'node','latency_ms':10}})
        self.store.publish(self.job,'node_measurement',node_id='n',payload={'result':{'median_mbps':30}})
        rows = self.store.snapshot(self.job)['results']
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['latency_ms'],rows[0]['median_mbps']),(10,30))

    def test_terminal_unique_and_late_updates_ignored(self):
        self.store.transition(self.job,'preparing')
        self.store.transition(self.job,'probing')
        self.store.transition(self.job,'finalizing')
        self.assertTrue(self.store.transition(self.job,'completed'))
        self.assertFalse(self.store.transition(self.job,'failed'))
        self.assertFalse(self.store.publish(self.job,'node_probe',node_id='n',payload={'result':{'latency_ms':1}}))
        state = self.store.snapshot(self.job)
        self.assertEqual(state['status'],'completed')
        self.assertIsNotNone(state['finished_at'])
        self.assertEqual(sum(e['type']=='job_finished' for e in self.store.read(self.job,state['seq']-2)['events']),1)

    def test_cancel_preserves_partial_results_and_releases_owner(self):
        self.store.publish(self.job,'node_probe',node_id='n',payload={'result':{'latency_ms':1}})
        self.store.transition(self.job,'cancelling')
        self.store.transition(self.job,'cancelled')
        self.assertTrue(self.store.snapshot(self.job)['partial'])
        self.assertEqual(len(self.store.snapshot(self.job)['results']),1)
        self.assertNotEqual(self.store.create(resolve_config()),self.job)

    def test_safe_whitelist_omits_nested_secrets_and_paths(self):
        self.store.publish(self.job,'node_probe',node_id='n',payload={
            'raw':'CANARY','password':'CANARY','result':{'name':'node','password':'CANARY',
            'measurement_scope':{'mode':'quick','secret':'CANARY'}}})
        self.assertNotIn('CANARY',json.dumps(self.store.snapshot(self.job)))
        self.assertNotIn('CANARY',json.dumps(self.store.read(self.job,0)))

    def test_invalid_events_nan_and_cursor_rejected(self):
        for callback in [lambda:self.store.publish(self.job,'CANARY'),
                         lambda:self.store.publish(self.job,'node_probe',payload={'result':{'latency_ms':float('nan')}}),
                         lambda:self.store.read(self.job,-1),
                         lambda:self.store.read('CANARY',0),
                         lambda:self.store.transition(self.job,'completed')]:
            with self.assertRaises(JobError) as caught:
                callback()
            self.assertNotIn('CANARY',str(caught.exception))

    def test_snapshots_are_detached_from_mutable_store(self):
        snapshot = self.store.snapshot(self.job)
        snapshot['config']['mode'] = 'changed'
        self.assertEqual(self.store.snapshot(self.job)['config']['mode'],'quick')

    def test_new_process_has_no_ghost_active_job(self):
        self.assertIsNone(JobStore().active_id())

    def test_bounded_retention_evicts_old_terminal_jobs_not_active(self):
        store = JobStore(job_limit=2)
        a = store.create(resolve_config())
        store.transition(a,'failed')
        b = store.create(resolve_config())
        store.transition(b,'failed')
        c = store.create(resolve_config())
        with self.assertRaises(JobError): store.snapshot(a)
        self.assertEqual(store.active_id(),c)
        self.assertEqual(store.snapshot(b)['status'],'failed')


class PhaseTimerTest(unittest.TestCase):
    def test_monotonic_timings_include_cleanup_and_actual_counters(self):
        clock = iter([100,100.25,102,103.5])
        timer = PhaseTimer(clock=lambda:next(clock))
        timer.start('download')
        timer.finish('download',attempts=1,successes=1,bytes=12345)
        timer.start('cleanup')
        timer.finish('cleanup')
        metrics = timer.snapshot()
        self.assertEqual(metrics['download']['duration_ms'],250)
        self.assertEqual(metrics['download']['bytes'],12345)
        self.assertEqual(metrics['cleanup']['duration_ms'],1500)

    def test_double_finish_and_non_actual_byte_counts_rejected(self):
        timer = PhaseTimer()
        timer.start('probe')
        timer.finish('probe')
        with self.assertRaises(JobError): timer.finish('probe')
        timer.start('download')
        with self.assertRaises(JobError): timer.finish('download',bytes=1.5)
