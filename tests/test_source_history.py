import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import clash_speedbench as c
import speedbench_db as db
from speedbench_sources import apply_origin, build_catalog
from tests.test_source_catalog import profile, proxy, SEED


class SourceHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jsonl = Path(self.temp.name) / 'history.jsonl'
        self.database = self.jsonl.with_suffix('.db')

    def result(self, name='节点', source_name='订阅甲', password='CANARY-password'):
        origin = build_catalog([proxy(name, password)], [profile(name=source_name, nodes=[proxy(password=password)])],
                               seed=SEED, namespace='fixture')['nodes'][0]
        r = c.Result(name=name, provider='', proto='ss', latency_ms=50,
                     speeds_mbps=[], median_mbps=None, best_mbps=None, status='ok')
        apply_origin(r, origin)
        return c.result_to_dict(r)

    def import_rows(self, rows):
        lines = [json.dumps({'ts': (datetime.now() - timedelta(days=len(rows)-i)).isoformat(),
                             'results': values}, ensure_ascii=False) for i, values in enumerate(rows)]
        self.jsonl.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        db.import_jsonl(self.database, self.jsonl)
        return lines

    def test_sources_and_identity_persist_without_credentials(self):
        r = self.result()
        self.import_rows([[r]])
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM subscription_sources').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT node_id FROM node_identities').fetchone()[0], r['node_id'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM node_origins').fetchone()[0], 1)
            tables = ['runs','node_results','subscription_sources','node_identities','node_origins']
            for table in tables:
                self.assertNotIn('CANARY-password', str(conn.execute('SELECT * FROM '+table).fetchall()))

    def test_rename_groups_by_source_id_and_preserves_snapshots(self):
        self.import_rows([[self.result()], [self.result('改名节点', '改名订阅')]])
        summary = db.source_summary(self.database)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]['name'], '改名订阅')
        self.assertEqual(summary[0]['node_count'], 1)
        self.assertEqual(summary[0]['run_count'], 2)
        with closing(sqlite3.connect(self.database)) as conn:
            names = [r[0] for r in conn.execute('SELECT name_snapshot FROM node_origins ORDER BY node_result_id')]
        self.assertEqual(names, ['订阅甲', '改名订阅'])

    def test_unmeasured_online_is_not_bandwidth_failure_or_offline(self):
        self.import_rows([[self.result()]])
        summary = db.source_summary(self.database)[0]
        self.assertEqual(summary['probe_online_ratio'], 1)
        self.assertEqual(summary['bandwidth_coverage'], 0)
        self.assertIsNone(summary['bandwidth_success_ratio'])

    def test_old_raw_and_unknown_origin_are_not_backfilled(self):
        lines = self.import_rows([[{'name':'旧节点', 'status':'ok','provider':''}], [self.result()]])
        self.assertEqual(db.import_jsonl(self.database, self.jsonl), 0)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual([x[0] for x in conn.execute('SELECT raw FROM runs ORDER BY id')], lines)
            self.assertIsNone(conn.execute('SELECT node_id FROM node_results ORDER BY id LIMIT 1').fetchone()[0])
        unknown = next(x for x in db.source_summary(self.database) if not x['subscription_id'])
        self.assertEqual(unknown['source_status'], 'legacy_unknown')

    def test_missing_probe_data_is_unknown_not_offline(self):
        self.import_rows([[{'name':'旧节点', 'status':'ok','provider':''}]])
        summary = db.source_summary(self.database)[0]
        self.assertIsNone(summary['probe_online_ratio'])
        self.assertEqual(summary['probe_coverage'], 0)

    def test_failed_probe_is_known_offline(self):
        r = self.result()
        r.update(latency_ms=None, probe_attempts=3, probe_successes=0)
        self.import_rows([[r]])
        summary = db.source_summary(self.database)[0]
        self.assertEqual(summary['probe_online_ratio'], 0)
        self.assertEqual(summary['probe_coverage'], 1)

    def test_node_id_history_does_not_merge_different_credentials(self):
        a, b = self.result(), self.result(password='other')
        self.import_rows([[a, b]])
        self.assertEqual(len(db.node_series(self.database, '', node_id=a['node_id'])), 1)
        self.assertEqual(len(db.node_series(self.database, '')), 0)

    def test_summary_is_json_serializable(self):
        self.import_rows([[self.result()]])
        self.assertIsInstance(json.dumps(db.source_summary(self.database)), str)


if __name__ == '__main__':
    unittest.main()
