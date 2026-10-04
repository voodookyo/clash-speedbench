import unittest
from tests.performance_fixture import compare_cases, run_case


class PerformanceFixtureTest(unittest.TestCase):
    def pair(self, scenario):
        static = run_case(10, 'legacy_static', scenario, time_scale=.05)
        dynamic = run_case(10, 'deep_dynamic', scenario, time_scale=.05)
        compare_cases(static, dynamic)
        return static, dynamic

    def test_same_coverage_cold_keeps_serial_download_and_bounded_pools(self):
        for result in self.pair('cold'):
            self.assertEqual(result['calls']['main_probe'], 100)
            self.assertEqual(result['calls']['download'], 10)
            self.assertEqual(result['synthetic_download_bytes'], 100_000_000)
            self.assertEqual(result['metrics']['download']['bytes'], 100_000_000)
            self.assertEqual(result['calls']['provider'], 16)
            self.assertLessEqual(result['peak']['main_probe'], 10)
            self.assertLessEqual(result['peak']['exit_v4'], 3)
            self.assertLessEqual(result['peak']['exit_v6'], 3)
            self.assertLessEqual(result['peak']['provider'], 2)
            self.assertEqual(result['peak']['download'], 1)
            milestones = result['milestones_ms']
            self.assertLess(milestones['first_result'], milestones['first_recommendation'])
            self.assertLess(milestones['first_recommendation'], milestones['network_complete'])
            self.assertLessEqual(milestones['network_complete'], milestones['intelligence_complete'])
            self.assertLess(milestones['intelligence_complete'], milestones['cleanup_complete'])
            self.assertGreater(result['event_count'], 100)
            self.assertTrue(result['stored'])

    def test_hot_cache_has_no_provider_transport_calls(self):
        for result in self.pair('hot'):
            self.assertEqual(result['calls'].get('provider', 0), 0)
            counters = result['metrics']['intel_cache']['counters']
            self.assertEqual(counters['cache_hits'], 16)
            self.assertEqual(counters.get('cache_misses', 0), 0)

    def test_high_failure_retains_main_and_fallback_samples_and_partial_bytes(self):
        for result in self.pair('failure'):
            self.assertEqual(result['results'], 10)
            self.assertEqual(result['calls']['main_probe'], 100)
            self.assertEqual(result['calls']['worker_probe'], 70)
            self.assertEqual(len(result['measured']), 3)
            self.assertEqual(result['failed_probes'], 70)
            self.assertEqual(result['failed_downloads'], 1)
            self.assertEqual(result['synthetic_download_bytes'], 21_250_000)
            self.assertEqual(result['metrics']['download']['bytes'], 21_250_000)
            self.assertGreater(result['metrics']['provider']['counters'].get('timeouts', 0), 0)
            sources = result['probes']['fixture-000']
            for source in ('main', 'worker'):
                self.assertEqual(sources[source]['attempts'], 10)
                self.assertEqual(sources[source]['failures'], 10)
            failed = result['outcomes']['fixture-008']
            self.assertIsNone(failed['ip_quality_score'])
            self.assertIsNone(failed['ip_grade'])
            for family in ('intel_v4', 'intel_v6'):
                self.assertEqual(failed[family]['provider_status'], {'ipqs': 'timeout'})
                self.assertIsNone(failed[family]['proxy'])
            for name in ('fixture-007', 'fixture-009'):
                self.assertIsNotNone(result['outcomes'][name]['ip_quality_score'])
                self.assertEqual(result['outcomes'][name]['intel_v4']['provider_status'], {'ipqs': 'ok'})

    def test_ipv6_unavailable_retains_successful_ipv4_and_independent_metrics(self):
        for result in self.pair('ipv6_unavailable'):
            self.assertEqual((result['ipv4'], result['ipv6']), (10, 0))
            self.assertEqual(result['metrics']['exit_v4']['successes'], 10)
            self.assertEqual(result['metrics']['exit_v6']['attempts'], 10)
            self.assertEqual(result['metrics']['exit_v6']['successes'], 0)
            self.assertEqual(result['calls']['provider'], 8)
            for value in result['outcomes'].values():
                self.assertEqual(value['exit_status'], {'ipv4': 'completed', 'ipv6': 'failed'})
                self.assertIsNone(value['intel_v6'])

    def test_mismatched_workload_is_not_a_performance_comparison(self):
        a, b = self.pair('hot')
        b['measured'] = b['measured'][:-1]
        with self.assertRaisesRegex(AssertionError, 'measured'):
            compare_cases(a, b)

    def test_all_failed_nodes_have_no_usable_recommendation_milestone(self):
        static = run_case(7, 'legacy_static', 'failure', time_scale=.05)
        dynamic = run_case(7, 'deep_dynamic', 'failure', time_scale=.05)
        comparison = compare_cases(static, dynamic)
        self.assertEqual(comparison['first_recommendation'],
                         {'legacy_static_ms': None, 'deep_dynamic_ms': None})
        self.assertEqual(dynamic['results'], 7)
        self.assertEqual(dynamic['measured'], [])
        self.assertEqual(dynamic['synthetic_download_bytes'], 0)

    def test_invalid_fixture_scope_cannot_start(self):
        for count in (0, 301, True):
            with self.assertRaises(ValueError):
                run_case(count, 'legacy_static', 'cold')


if __name__ == '__main__':
    unittest.main()
