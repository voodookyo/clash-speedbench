import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import clash_speedbench as core
import speedbench_db as db
import speedbench_workers as workers
from speedbench_tasks import resolve_config
from tests.test_phase1_probe import mk_args


class CandidateHistoryTest(unittest.TestCase):
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    a = 'node_v2_' + 'a' * 32
    b = 'node_v2_' + 'b' * 32

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '中文 history.db'
        self.jsonl = self.path.with_suffix('.jsonl')

    def insert(self, *, node=None, age=1, strength='strong', speed=80,
               name='改名前', status='ok', region='US', ts=None):
        row = dict(name=name, node_id=node or self.a, identity_version=2,
                   identity_strength=strength, source_status='unknown',
                   median_mbps=speed, status=status,
                   ip=dict(ok=True, country_code=region, exit_ip='203.0.113.1'))
        record = dict(ts=ts or (self.now - timedelta(days=age)).isoformat(), results=[row])
        with self.jsonl.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
        db.import_jsonl(self.path, self.jsonl)

    def hints(self, ids=None):
        return db.candidate_history_hints(self.path, ids or [self.a], now=self.now)

    def test_read_only_latest_successful_hint_and_country_are_whitelisted(self):
        self.insert(age=2, speed=40)
        self.insert(age=1, speed=90, name='改名后')
        before = self.path.read_bytes()
        hints = self.hints()
        self.assertEqual(hints[self.a], dict(recent_mbps=90, history_age_days=1, region='US'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn('改名', json.dumps(hints))

    def test_weak_legacy_or_changed_identity_cannot_borrow_hint(self):
        self.insert(strength='weak')
        self.insert(node=self.b, age=0.5, speed=999)
        self.assertEqual(self.hints(), {})

    def test_expired_and_future_hint_are_not_current_evidence(self):
        for age in (8, -1):
            with self.subTest(age=age):
                self.insert(age=age)
                self.assertEqual(self.hints(), {})

    def test_invalid_time_and_region_do_not_escape_parser(self):
        self.insert(ts='CANARY-invalid-date', region='CANARY-network-name')
        self.assertEqual(self.hints(), {})

    def test_offset_and_naive_times_use_real_age(self):
        self.insert(ts='2026-10-03T00:00:00+08:00')
        self.assertAlmostEqual(self.hints()[self.a]['history_age_days'], 20 / 24)
        local = (self.now - timedelta(hours=4)).astimezone().replace(tzinfo=None)
        self.insert(ts=local.isoformat(), speed=70)
        self.assertAlmostEqual(self.hints()[self.a]['history_age_days'], 4 / 24)

    def test_unmeasured_or_failed_rows_do_not_replace_successful_bandwidth(self):
        self.insert(age=3, speed=35)
        self.insert(age=2, speed=None)
        self.insert(age=1, speed=900, status='error')
        self.assertEqual(self.hints()[self.a]['recent_mbps'], 35)

    def test_missing_or_old_or_corrupt_database_never_created_or_migrated(self):
        self.assertEqual(self.hints(), {})
        self.assertFalse(self.path.exists())
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('CREATE TABLE runs (raw TEXT)')
            c.commit()
        before = self.path.read_bytes()
        self.assertEqual(self.hints(), {})
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_ids_and_locked_database_are_safe_fallback(self):
        self.insert()
        self.assertEqual(db.candidate_history_hints(self.path, ['x', self.a + "'"], now=self.now), {})
        with mock.patch.object(db.sqlite3, 'connect', side_effect=sqlite3.OperationalError('CANARY-secret')):
            self.assertEqual(self.hints(), {})

    def test_corrupt_database_and_non_country_text_degrade_safely(self):
        self.path.write_bytes(b'CANARY-broken-database')
        before = self.path.read_bytes()
        self.assertEqual(self.hints(), {})
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()  # Exact temporary fixture, never a user's database.
        self.insert(region='CANARY-network-name')
        self.assertNotIn('region', self.hints()[self.a])

    def test_budget_exhaustion_does_not_bias_selection_with_partial_hints(self):
        self.insert()
        self.insert(node=self.b, age=0.5)
        with mock.patch.object(db.time, 'monotonic', side_effect=[0, 0, 1]):
            self.assertEqual(self.hints([self.a, self.b]), {})

    def result(self, name, node_id, latency, strength='strong'):
        r = core.Result(name=name, provider='', proto='ss', latency_ms=latency,
                        speeds_mbps=[], median_mbps=None, best_mbps=None, status='ok')
        r.origin = dict(node_id=node_id, identity_strength=strength, subscription_ids=['s1'])
        return r

    def test_production_selector_loads_once_and_never_attaches_old_measurement(self):
        self.insert(node=self.b, age=1, speed=100)
        args = mk_args(history=str(self.jsonl), task_config=resolve_config(
            dict(mode='quick', top_n=1, target_profile='download')))
        rows = [self.result('a', self.a, 10), self.result('b', self.b, 20)]
        with mock.patch.object(db, 'candidate_history_hints', wraps=lambda p, ids:
                               db_loader(p, ids, now=self.now)) as loader:
            self.assertEqual([r.name for r in workers.choose_task_nodes(rows, args)], ['b'])
            self.assertEqual([r.name for r in workers.choose_task_nodes(rows, args)], ['b'])
            self.assertEqual(loader.call_count, 1)
        self.assertTrue(all(r.median_mbps is None and r.ip is None for r in rows))

    def test_selector_obeys_scope_and_weak_identity_and_legacy_default(self):
        args = mk_args(history=str(self.jsonl), task_config=resolve_config(
            dict(mode='quick', top_n=1, target_profile='download')))
        hints = {self.b:dict(recent_mbps=999, history_age_days=1)}
        rows = [self.result('a', self.a, 10), self.result('b', self.b, 20, 'weak')]
        with mock.patch.object(db, 'candidate_history_hints', return_value=hints):
            self.assertEqual(workers.choose_task_nodes(rows, args)[0].name, 'a')
            self.assertEqual(workers.choose_task_nodes(rows[:1], args)[0].name, 'a')
        args.task_config = resolve_config()
        with mock.patch.object(db, 'candidate_history_hints') as loader:
            self.assertEqual(workers.choose_task_nodes(rows, args)[0].name, 'a')
            loader.assert_not_called()


# Keep the real function when the selector test installs its wrapper.
db_loader = getattr(db, 'candidate_history_hints', None)
