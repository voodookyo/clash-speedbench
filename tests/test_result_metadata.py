"""Result metadata: display timestamps, independent states, measured count.

No real network, providers or global state.  Only actual observation
boundaries may acquire a timestamp; disabled/pending/cancelled work never
infers success from an event name or a fabricated value.
"""
import io
import json
from types import SimpleNamespace
import unittest
from unittest import mock

import clash_speedbench as core
import speedbench_jobs as jobs
import speedbench_progress as progress
import speedbench_workers as workers
from tests import test_cli_partial_history as fixtures


def result(**options):
    row = core.Result(name='A', provider='', proto='ss', latency_ms=None,
                      speeds_mbps=[], median_mbps=None, best_mbps=None, status='ok')
    for key, value in options.items():
        setattr(row, key, value)
    return row


class MetricStampTest(unittest.TestCase):
    def patch_clock(self, seconds):
        return mock.patch.object(core.time, 'time', return_value=seconds)

    def test_probe_observation_stamps_probe_and_network_in_epoch_ms(self):
        row = result()
        with self.patch_clock(1000.0):
            core._apply_probe_stats(row, core.ProbeStats(20, 0, 3, 2, 1))
        self.assertEqual(row.metric_updated_at, {'probe': 1_000_000, 'network': 1_000_000})

    def test_reapplying_probe_stats_does_not_regress_a_stale_boundary(self):
        row = result()
        with self.patch_clock(1000.0):
            core._apply_probe_stats(row, core.ProbeStats(20, 0, 3, 3, 0))
        with self.patch_clock(5000.0):
            core._apply_probe_stats(row, core.ProbeStats(20, 0, 3, 3, 0))
        self.assertEqual(row.metric_updated_at, {'probe': 1_000_000, 'network': 1_000_000})

    def test_zero_attempt_probe_is_not_an_observed_boundary(self):
        row = result()
        with self.patch_clock(1000.0):
            core._apply_probe_stats(row, core.ProbeStats(None, None, 0, 0, 0))
        self.assertIsNone(row.metric_updated_at)

    def test_enrichment_and_scoring_do_not_advance_network(self):
        row = result(exit_ipv4='192.0.2.1',
                     metric_updated_at={'probe': 1000, 'network': 2000, 'bandwidth': 2000})
        with self.patch_clock(9000.0):
            core._apply_intelligence(row, {})
            core.compute_score(row)
        self.assertEqual(row.metric_updated_at['network'], 2000)

    def test_intelligence_completion_stamps_only_real_attached_results(self):
        intel = core.IpIntelligence(ip_quality_score=80.0, ip_grade='A')
        row = result(exit_ipv4='192.0.2.1')
        with self.patch_clock(1000.0):
            core._apply_intelligence(row, {})
            core.compute_score(row)
        self.assertNotIn('intel', row.metric_updated_at or {})
        with self.patch_clock(2000.0):
            core._apply_intelligence(row, {'192.0.2.1': intel})
        self.assertEqual(row.metric_updated_at['intel'], 2_000_000)
        self.assertEqual(row.metric_updated_at['ip_grade'], 2_000_000)

    def test_intel_without_a_usable_grade_does_not_fabricate_grade_time(self):
        intel = core.IpIntelligence(ip_quality_score=80.0, ip_grade=None,provider_status={'fixture':'ok'})
        row = result(exit_ipv4='192.0.2.1')
        with self.patch_clock(3000.0):
            core._apply_intelligence(row, {'192.0.2.1': intel})
        self.assertEqual(row.metric_updated_at['intel'], 3_000_000)
        self.assertNotIn('ip_grade', row.metric_updated_at)

    def test_completed_probe_time_is_not_replaced_while_building_a_new_row(self):
        stats=core.ProbeStats(20,0,3,3,0,updated_at_ms=1234)
        with self.patch_clock(9000):
            first=result();core._apply_probe_stats(first,stats)
            copied=result();core._apply_probe_stats(copied,stats)
        self.assertEqual(copied.metric_updated_at,{'probe':1234,'network':1234})
        self.assertEqual(copied.metric_updated_at,first.metric_updated_at)

    def test_disabled_intelligence_has_no_completion_or_grade_timestamp(self):
        intel=core.IpIntelligence(provider_status={'ip_api':'disabled','ipqs':'key_missing'})
        row=result(exit_ipv4='192.0.2.1',metric_updated_at={'network':1000})
        core._apply_intelligence(row,{'192.0.2.1':intel})
        self.assertEqual(row.measurement_scope['intel'],'not_requested')
        self.assertEqual(row.metric_updated_at,{'network':1000})

    def test_failed_provider_is_failed_with_its_own_completion_time(self):
        row=result(exit_ipv4='192.0.2.1',metric_updated_at={'network':1000})
        with self.patch_clock(2):
            core._apply_intelligence(row,{'192.0.2.1':core.IpIntelligence(provider_status={'ip_api':'timeout'})})
        self.assertEqual(row.measurement_scope['intel'],'failed')
        self.assertEqual(row.metric_updated_at,{'network':1000,'intel':2000})

    def test_missing_finished_query_and_partial_dual_stack_have_explicit_states(self):
        row=result(exit_ipv4='192.0.2.1',exit_ipv6='2001:db8::1',measurement_scope={'intel':'pending'},
            metric_updated_at={'network':1000})
        core._apply_intelligence(row,{})
        self.assertEqual(row.measurement_scope['intel'],'failed')
        self.assertEqual(row.metric_updated_at,{'network':1000})
        core._apply_intelligence(row,{'192.0.2.1':core.IpIntelligence(ip_quality_score=80,ip_grade='A')})
        self.assertEqual(row.measurement_scope['intel'],'partial')
        self.assertEqual(row.metric_updated_at['network'],1000)

    def test_probe_pending_states_and_no_ip_are_persisted_without_event_inference(self):
        args=SimpleNamespace(no_ip=True,_result_journal=progress.ResultJournal())
        observer=progress.ProbeObserver(args,'A','ss','','main',3)
        observer.start();observer.sample(core.ProbeStats(20,0,1,1,0,updated_at_ms=1234),False)
        row=args._result_journal.snapshot()[0]
        self.assertEqual(row.measurement_scope,{'probe':'partial','bandwidth':'pending','intel':'not_requested'})
        core.finish_intelligence_enrichment(None,[row])
        self.assertEqual(row.measurement_scope['intel'],'not_requested')
        self.assertEqual(row.metric_updated_at,{'probe':1234,'network':1234})

    def test_all_completed_probe_failures_do_not_display_success(self):
        args=SimpleNamespace(no_ip=True,_result_journal=progress.ResultJournal())
        observer=progress.ProbeObserver(args,'A','ss','','main',3)
        observer.sample(core.ProbeStats(None,None,3,0,3,updated_at_ms=1234),True)
        row=args._result_journal.snapshot()[0]
        self.assertEqual(row.measurement_scope['probe'],'failed')
        self.assertEqual(row.probe_sources['main']['status'],'completed')
        self.assertEqual(row.probe_loss_pct,100)


class ResultDictMetadataTest(unittest.TestCase):
    def test_new_unstamped_result_counts_observations_without_fabricated_times(self):
        payload = core.result_to_dict(result(latency_ms=10, median_mbps=20.0))
        self.assertEqual(payload['metric_updated_at'],{})
        self.assertEqual(payload['measured_metric_count'],2)
        self.assertEqual(payload['measurement_scope'],{'probe':'unknown','bandwidth':'unknown','intel':'unknown'})

    def test_reading_legacy_history_does_not_fabricate_metadata(self):
        original={'name':'old','latency_ms':10,'median_mbps':20}
        public=jobs._result(original)
        self.assertNotIn('metric_updated_at',public)
        self.assertNotIn('measured_metric_count',public)

    def test_stamped_result_serializes_whitelisted_metadata_and_count(self):
        row = result(latency_ms=10, jitter_ms=1.0, connect_ms=2.0, median_mbps=20.0,
                     metric_updated_at={'probe': 1, 'network': 2, 'exit': 3})
        payload = core.result_to_dict(row)
        self.assertEqual(payload['metric_updated_at'], {'probe': 1, 'network': 2, 'exit': 3})
        self.assertEqual(payload['measured_metric_count'], 4)

    def test_measured_count_excludes_derived_scores_and_needs_real_intelligence(self):
        row = result(network_score=99.0, score=99.0, ip_grade='S', ip_quality_score=99.0,
                     metric_updated_at={'network': 1})
        payload = core.result_to_dict(row)
        self.assertEqual(payload['measured_metric_count'], 0)


class JobSanitizeTest(unittest.TestCase):
    def test_cancelled_family_states_survive_the_same_result_projection(self):
        value={'name':'A','exit_status':{'ipv4':'cancelled','ipv6':'interrupted','basic':'CANARY'}}
        self.assertEqual(jobs._result(value)['exit_status'],{'ipv4':'cancelled','ipv6':'interrupted'})

    def test_unknown_and_malformed_timestamps_are_dropped(self):
        value = {'name': 'A', 'latency_ms': 10,
                 'metric_updated_at': {'probe': 1, 'bogus': 5, 'bandwidth': True,
                                       'exit': -3, 'intel': float('inf'),
                                       'network': 'now'}}
        public = jobs._result(value)
        self.assertEqual(public['metric_updated_at'], {'probe': 1})

    def test_large_timestamp_integer_and_wrong_ip_family_do_not_claim_measurement(self):
        public=jobs._result({'name':'A','metric_updated_at':{'probe':10**400,'network':0,'exit':1.5},
            'exit_ipv4':'not-an-ip','exit_ipv6':'192.0.2.1','probe_attempts':3,'probe_loss_pct':200,
            'measured_metric_count':True})
        self.assertEqual(public['metric_updated_at'],{'network':0})
        self.assertEqual(public['measured_metric_count'],0)

    def test_counts_are_clamped_and_invalid_counts_are_omitted(self):
        huge = jobs._result({'name': 'A', 'latency_ms': 10, 'measured_metric_count': 9999})
        self.assertEqual(huge['measured_metric_count'], jobs.MAX_MEASURED_METRICS)
        negative = jobs._result({'name': 'A', 'latency_ms': 10, 'measured_metric_count': -4})
        self.assertEqual(negative['measured_metric_count'], 0)
        for bad in (True, 'many', float('nan'), float('inf')):
            with self.subTest(bad=bad):
                public = jobs._result({'name': 'A', 'latency_ms': 10,
                                       'measured_metric_count': bad})
                self.assertNotIn('measured_metric_count', public)

    def test_derived_count_only_describes_real_observed_dimensions(self):
        public = jobs._result({
            'name': 'A', 'latency_ms': 10, 'jitter_ms': 0.5, 'connect_ms': 3.0,
            'median_mbps': 20.0, 'multi_mbps': 40.0, 'probe_attempts': 3,
            'probe_loss_pct': 0.0, 'exit_ipv4': '192.0.2.1', 'exit_ipv6': '2001:db8::1',
            'ip_quality_score': 80.0, 'intel_v4': {'ip_quality_score': 80.0},
            'network_score': 50.0, 'ip_grade': 'A', 'metric_updated_at': {'probe': 1}})
        self.assertEqual(public['measured_metric_count'], 9)


class ResultJournalMetadataTest(unittest.TestCase):
    def test_final_result_inherits_earlier_partial_timestamps(self):
        journal = progress.ResultJournal()
        partial = result(metric_updated_at={'probe': 1000, 'network': 1000})
        journal.remember(partial)
        final = result(metric_updated_at={'bandwidth': 2000})
        journal.remember(final)
        snapshot = journal.snapshot()[0].metric_updated_at
        self.assertEqual(snapshot, {'probe': 1000, 'network': 1000, 'bandwidth': 2000})
        self.assertEqual(final.metric_updated_at, snapshot)

    def test_final_row_inherits_independent_scope_and_probe_counts_for_serialization(self):
        journal=progress.ResultJournal()
        journal.remember(result(probe_attempts=3,probe_successes=2,probe_failures=1,probe_loss_pct=33.3,
            metric_updated_at={'probe':1000},measurement_scope={'probe':'completed','intel':'not_requested'}))
        final=result(measurement_scope={'bandwidth':'partial'});journal.remember(final)
        public=core.result_to_dict(final)
        self.assertEqual(public['probe_attempts'],3)
        self.assertEqual(public['measurement_scope'],{'probe':'completed','intel':'not_requested','bandwidth':'partial'})
        self.assertEqual(public['measured_metric_count'],1)

    def test_a_stale_final_row_never_regresses_an_earlier_boundary(self):
        journal = progress.ResultJournal()
        journal.remember(result(metric_updated_at={'probe': 5000}))
        journal.remember(result(metric_updated_at={'probe': 1000}))
        self.assertEqual(journal.snapshot()[0].metric_updated_at, {'probe': 5000})


class ProductionPathTest(unittest.TestCase):
    def test_worker_bandwidth_publish_advances_bandwidth_and_network_only(self):
        row = result(metric_updated_at={'probe': 1000, 'network': 1000})
        args = SimpleNamespace(mb=10, max_time=3, rounds=1, multi=False, settle=0,
                               mode='quick', _result_journal=progress.ResultJournal(), progress=None)
        worker = SimpleNamespace(select=lambda name: None, proxy_url='http://fixture.invalid')
        with mock.patch.object(workers, 'cancel_requested', return_value=False), \
                mock.patch.object(workers, 'curl_speed', return_value=(50.0, 'ok', 9.0, 1.2)), \
                mock.patch.object(core.time, 'time', return_value=2000.0), \
                mock.patch.object(core.time, 'sleep'):
            workers._speed_node_in_worker(worker, row, args)
        self.assertEqual(row.metric_updated_at['probe'], 1000)
        self.assertEqual(row.metric_updated_at['bandwidth'], 2_000_000)
        self.assertEqual(row.metric_updated_at['network'], 2_000_000)

    def test_no_progress_serial_cli_history_keeps_real_timestamps(self):
        fixture = fixtures.CliPartialHistoryTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with mock.patch.object(core, 'probe_latency', return_value=core.ProbeStats(20, 0, 3, 3, 0)), \
                mock.patch.object(core, 'apply_path'), \
                mock.patch.object(core, 'restore_groups'), \
                mock.patch.object(core, 'curl_speed', return_value=(50.0, 'ok', 9.0, 1.2)), \
                mock.patch.object(core.time, 'sleep'):
            code, _, _ = fixture.invoke(None, serial=True, extra=['--mb', '10'])
        self.assertEqual(code, 0)
        record = fixture.records()[0]['results'][0]
        self.assertIn('metric_updated_at', record)
        self.assertIn('network', record['metric_updated_at'])
        self.assertIn('bandwidth', record['metric_updated_at'])
        self.assertEqual(set(record['metric_updated_at']) - {
            'probe', 'bandwidth', 'exit', 'intel', 'network', 'ip_grade'}, set())
        self.assertEqual(record['measured_metric_count'], 5)


class ProgressTransportTest(unittest.TestCase):
    def test_emitted_result_carries_sanitized_metadata_and_count(self):
        stream = io.StringIO()
        emitter = progress.ProgressEmitter('job_' + 'a' * 32, stream)
        args = SimpleNamespace(progress=emitter, _result_journal=progress.ResultJournal())
        row = result(latency_ms=10, median_mbps=20.0,
                     metric_updated_at={'probe': 1, 'network': 2, 'bogus': 9})
        progress.publish_result(args, 'node_measurement', row, phase_name='measuring')
        event = progress.parse_record(stream.getvalue(), emitter.job_id)
        payload = event['payload']['result']
        self.assertEqual(payload['metric_updated_at'], {'probe': 1, 'network': 2})
        self.assertEqual(payload['measured_metric_count'], 2)
        self.assertNotIn('bogus', json.dumps(payload))


if __name__ == '__main__':
    unittest.main()
