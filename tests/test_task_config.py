import unittest
from speedbench_tasks import TaskConfigError, resolve_config, select_candidates


class TaskConfigTest(unittest.TestCase):
    def test_legacy_defaults_remain_unchanged(self):
        c = resolve_config()
        self.assertEqual((c.mode, c.top_n, c.probe_count, c.mb, c.max_time),
                         ('legacy', 15, 3, None, 4.0))
        self.assertEqual(c.ip_scope, 'all')

    def test_modes_have_shared_explicit_defaults(self):
        quick = resolve_config({'mode':'quick'})
        self.assertEqual((quick.top_n, quick.mb, quick.max_time, quick.probe_count), (5,10,3.0,3))
        self.assertEqual(quick.ip_scope, 'selected')
        standard = resolve_config({'mode':'standard'})
        self.assertEqual((standard.top_n, standard.mb, standard.probe_count), (10,None,3))
        deep = resolve_config({'mode':'deep'})
        self.assertEqual((deep.measure_all, deep.probe_count, deep.multi), (True,10,False))
        ip = resolve_config({'mode':'ip'})
        self.assertFalse(ip.bandwidth)
        self.assertEqual(ip.ip_scope, 'all')
        self.assertEqual(ip.download_budget_mb(100), 0)

    def test_explicit_advanced_options_override_mode(self):
        c = resolve_config({'mode':'quick', 'probe_count':10, 'mb':30, 'top_n':2, 'max_time':5})
        self.assertEqual((c.probe_count,c.mb,c.top_n,c.max_time),(10,30,2,5))

    def test_stability_only_changes_default_probe_count(self):
        self.assertEqual(resolve_config({'stability':True}).probe_count,10)
        self.assertEqual(resolve_config({'stability':True,'probe_count':4}).probe_count,4)

    def test_invalid_values_are_rejected_without_echoing(self):
        values = [{'mode':'CANARY'}, {'rounds':True}, {'rounds':0}, {'workers':1000},
                  {'probe_count':101}, {'max_time':float('nan')}, {'ip_timeout':float('inf')},
                  {'multi':'true'}, {'top_n':-1}, {'mb':96}, {'target_profile':'CANARY'},
                  {'unknown_key':'CANARY'}, {'mode':'ip','multi':True}, {'mode':'ip','no_ip':True},
                  {'workers':10**1000}, {'mode':None}]
        for params in values:
            with self.subTest(params=params), self.assertRaises(TaskConfigError) as caught:
                resolve_config(params)
            self.assertNotIn('CANARY', str(caught.exception))

    def test_budget_includes_warmup_and_multistream_but_is_not_actual_bytes(self):
        self.assertEqual(resolve_config({'mode':'quick'}).download_budget_mb(100),50)
        self.assertEqual(resolve_config({'mode':'standard'}).download_budget_mb(100),960)
        self.assertEqual(resolve_config({'mode':'quick','multi':True}).download_budget_mb(100),250)

    def test_legacy_serial_budget_counts_every_scoped_node(self):
        config=resolve_config({'mode':'legacy','workers':1})
        self.assertTrue(config.measure_all)
        self.assertEqual(config.download_budget_mb(100),9600)

    def test_public_config_is_whitelisted_and_json_safe(self):
        import json
        self.assertEqual(json.loads(json.dumps(resolve_config({'mode':'quick'}).public()))['mode'],'quick')
        with self.assertRaises(TaskConfigError):
            resolve_config({'api_key':'CANARY'})


class CandidateSelectionTest(unittest.TestCase):
    def rows(self):
        return [dict(name='a',node_id='1',identity_strength='strong',latency_ms=10,subscription_ids=['s1'],region='US'),
                dict(name='b',node_id='2',identity_strength='strong',latency_ms=11,subscription_ids=['s1'],region='US'),
                dict(name='c',node_id='3',identity_strength='strong',latency_ms=20,subscription_ids=['s2'],region='JP'),
                dict(name='d',node_id='4',identity_strength='strong',latency_ms=None,subscription_ids=['s3'],region='HK')]

    def test_representative_coverage_before_latency_fill(self):
        self.assertEqual([r['name'] for r in select_candidates(self.rows(),2)],['a','c'])

    def test_legacy_remains_latency_only(self):
        self.assertEqual([r['name'] for r in select_candidates(self.rows(),2,legacy=True)],['a','b'])

    def test_deterministic_ties_no_input_mutation(self):
        rows = self.rows()
        before = repr(rows)
        self.assertEqual(select_candidates(rows,2),select_candidates(list(reversed(rows)),2))
        self.assertEqual(repr(rows),before)

    def test_ip_selects_no_download_candidates_deep_selects_all_reachable(self):
        self.assertEqual(select_candidates(self.rows(),0),[])
        self.assertEqual(len(select_candidates(self.rows(),0,measure_all=True)),3)

    def test_fresh_download_history_is_hint_not_current_measurement(self):
        rows = self.rows()[:2]
        rows[1]['recent_mbps'] = 200
        rows[1]['history_age_days'] = 2
        self.assertEqual(select_candidates(rows,1,target_profile='download')[0]['name'],'b')
        rows[1]['history_age_days'] = 30
        self.assertEqual(select_candidates(rows,1,target_profile='download')[0]['name'],'a')

    def test_weak_identity_cannot_borrow_auth_history(self):
        rows = self.rows()[:2]
        rows[1].update(node_id='',recent_mbps=200,history_age_days=0)
        self.assertEqual(select_candidates(rows,1,target_profile='download')[0]['name'],'a')

    def test_weak_id_with_valid_text_still_cannot_borrow_history(self):
        rows = self.rows()[:2]
        rows[1].update(identity_strength='weak', recent_mbps=200, history_age_days=0)
        self.assertEqual(select_candidates(rows,1,target_profile='download')[0]['name'],'a')

    def test_daily_uses_current_jitter_and_completed_probe_loss(self):
        rows=self.rows()[:2]
        rows[0].update(jitter_ms=150,probe_loss_pct=50)
        rows[1].update(jitter_ms=3,probe_loss_pct=0)
        self.assertEqual(select_candidates(rows,1,target_profile='daily')[0]['name'],'b')
        self.assertEqual(select_candidates(rows,1,legacy=True,target_profile='daily')[0]['name'],'a')

    def test_ip_and_residential_use_different_recent_evidence(self):
        rows=self.rows()[:2]
        rows[0].update(history_age_days=1,recent_ip_quality=85,recent_ip_category='datacenter',recent_ip_confidence=90)
        rows[1].update(history_age_days=1,recent_ip_quality=70,recent_ip_category='residential',recent_ip_confidence=90)
        self.assertEqual(select_candidates(rows,1,target_profile='ip')[0]['name'],'a')
        self.assertEqual(select_candidates(rows,1,target_profile='residential')[0]['name'],'b')

    def test_unknown_or_high_risk_history_is_not_a_residential_bonus(self):
        rows=self.rows()[:2]
        rows[0].update(history_age_days=1,recent_ip_quality=10,recent_ip_category='residential',recent_ip_confidence=90)
        rows[1].update(history_age_days=1,recent_ip_quality=60,recent_ip_category='datacenter',recent_ip_confidence=90)
        self.assertEqual(select_candidates(rows,1,target_profile='residential')[0]['name'],'b')

    def test_every_historical_target_rejects_weak_future_stale_or_nonfinite_hints(self):
        for profile in ('balanced','daily','download','ip','residential'):
            for invalid in ({'identity_strength':'weak'},{'history_age_days':8},{'history_age_days':-1},
                            {'history_age_days':True},{'history_age_days':float('nan')}):
                rows=self.rows()[:2]
                rows[1].update(history_age_days=1,recent_mbps=999,recent_ip_quality=99,
                               recent_ip_category='residential',recent_ip_confidence=99)
                rows[1].update(invalid)
                with self.subTest(profile=profile,invalid=invalid):
                    self.assertEqual(select_candidates(rows,1,target_profile=profile)[0]['name'],'a')

    def test_target_hint_does_not_override_subscription_or_known_region_coverage(self):
        rows=self.rows()[:3]
        rows[1].update(history_age_days=1,recent_mbps=999,recent_ip_quality=99,
                       recent_ip_category='residential',recent_ip_confidence=99)
        for profile in ('download','ip','residential'):
            with self.subTest(profile=profile):
                self.assertIn('c',[r['name'] for r in select_candidates(rows,2,target_profile=profile)])

    def test_nan_bool_and_infinite_history_cannot_rank_or_change_results(self):
        rows=self.rows()[:2]
        for value in (True,float('nan'),float('inf'),10**1000):
            rows[1].update(recent_mbps=value,history_age_days=1)
            self.assertEqual(select_candidates(rows,1,target_profile='download')[0]['name'],'a')
