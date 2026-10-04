from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import speedbench_db as db
from speedbench_owner import BackendLease
from speedbench_preferences import Preferences
from speedbench_transfer import HistoryTransfer, TransferError


def ledger(root, ts, speed=12, *, whitespace=False):
    raw = json.dumps(dict(ts=ts, mb=10, rounds=1, results=[dict(
        name='fixture', proto='ss', latency_ms=20, median_mbps=speed, status='ok')]))
    if whitespace:
        raw = '  ' + raw + '  '
    path = root/'speedbench-history.jsonl'
    with path.open('a', encoding='utf-8') as stream:
        stream.write(raw+'\n')
    return raw


class HistoryTransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)/'source'
        self.home = Path(self.temp.name)/'home'
        self.source.mkdir(); self.home.mkdir()
        self.owner = BackendLease(self.home).acquire()
        self.addCleanup(self.owner.close)
        self.service = HistoryTransfer(self.home)

    def preview(self):
        return self.service.preview(str(self.source), self.owner)

    def test_preview_does_not_create_or_change_source_or_destination_files(self):
        raw=ledger(self.source,'2026-10-01T01:00:00',whitespace=True)
        before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.source.iterdir()}
        names={p.name for p in self.home.iterdir()}
        result=self.preview()
        self.assertEqual((result['new_runs'],result['duplicate_runs'],result['conflicts']),(1,0,0))
        self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.source.iterdir()})
        self.assertEqual(names,{p.name for p in self.home.iterdir()})
        self.assertNotIn(raw,json.dumps(result))

    def test_merge_preserves_raw_ids_seed_and_preferences_and_is_idempotent(self):
        old=ledger(self.home,'2026-10-03T01:00:00',speed=50,whitespace=True)
        incoming=ledger(self.source,'2026-10-01T01:00:00',whitespace=True)
        database=self.home/'speedbench-history.db'
        db.import_jsonl(database,self.home/'speedbench-history.jsonl')
        seed=self.home/'identity-seed'; seed.write_bytes(b'owned-fixture-seed');seed.chmod(0o600)
        Preferences(self.home).patch({'sb_theme':'dark'})
        prefix=(self.home/'speedbench-history.jsonl').read_bytes()
        result=self.service.apply(self.preview()['token'],self.owner)
        self.assertEqual(result['imported_runs'],1)
        self.assertTrue((self.home/'speedbench-history.jsonl').read_bytes().startswith(prefix))
        with closing(sqlite3.connect(database)) as conn:
            self.assertEqual(conn.execute('SELECT raw FROM runs WHERE id=1').fetchone()[0],old)
            self.assertEqual(conn.execute('SELECT raw FROM runs WHERE ts=?',('2026-10-01T01:00:00',)).fetchone()[0],incoming)
        self.assertEqual(seed.read_bytes(),b'owned-fixture-seed')
        self.assertEqual(Preferences(self.home).read()['sb_theme'],'dark')
        self.assertEqual(db.latest_run(database)['ts'],'2026-10-03T01:00:00')
        same=(self.home/'speedbench-history.jsonl').read_bytes()
        repeated=self.service.apply(self.preview()['token'],self.owner)
        self.assertEqual(repeated['imported_runs'],0)
        self.assertIsNone(repeated['backup_id'])
        self.assertEqual((self.home/'speedbench-history.jsonl').read_bytes(),same)

    def test_conflicting_timestamp_rejected_before_backup_or_write(self):
        ledger(self.source,'2026-10-01T01:00:00',speed=1)
        ledger(self.home,'2026-10-01T01:00:00',speed=2)
        before=(self.home/'speedbench-history.jsonl').read_bytes()
        preview=self.preview()
        self.assertEqual(preview['conflicts'],1)
        self.assertFalse(preview['can_apply'])
        with self.assertRaises(TransferError):self.service.apply(preview['token'],self.owner)
        self.assertEqual((self.home/'speedbench-history.jsonl').read_bytes(),before)
        self.assertFalse((self.home/'history-import-backups').exists())

    def test_source_or_destination_changes_invalidate_preview(self):
        for root in (self.source,self.home):
            with self.subTest(root=root.name):
                ledger(self.source,'2026-10-01T01:00:00')
                preview=self.preview()
                ledger(root,'2026-10-02T01:00:00')
                before={p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()}
                with self.assertRaises(TransferError):self.service.apply(preview['token'],self.owner)
                self.assertEqual(before,{p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()})
                (root/'speedbench-history.jsonl').unlink()

    def test_active_source_owner_is_rejected_without_changing_lock_metadata(self):
        ledger(self.source,'2026-10-01T01:00:00')
        with BackendLease(self.source) as source_owner:
            lock=source_owner.path
            before=(lock.read_bytes(),lock.stat().st_mtime_ns,lock.stat().st_ino)
            with self.assertRaises(TransferError):self.preview()
            self.assertEqual((lock.read_bytes(),lock.stat().st_mtime_ns,lock.stat().st_ino),before)

    def test_sqlite_only_wal_source_is_read_without_modifying_original(self):
        path=self.source/'speedbench-history.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA wal_autocheckpoint=0')
            conn.execute('CREATE TABLE runs(ts TEXT,raw TEXT)')
            raw=json.dumps({'ts':'2026-10-01T01:00:00','results':[]})
            conn.execute('INSERT INTO runs VALUES (?,?)',('2026-10-01T01:00:00',raw));conn.commit()
            before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.source.iterdir()}
            preview=self.preview()
            self.assertEqual(preview['source_format'],'sqlite')
            self.assertEqual(preview['new_runs'],1)
            self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.source.iterdir()})
            self.service.apply(preview['token'],self.owner)
        self.assertEqual(db.latest_run(self.home/'speedbench-history.db')['ts'],'2026-10-01T01:00:00')

    def test_source_cache_and_private_preferences_are_not_transferred(self):
        ledger(self.source,'2026-10-01T01:00:00')
        source_db=self.source/'speedbench-history.db'
        db.import_jsonl(source_db,self.source/'speedbench-history.jsonl')
        with closing(sqlite3.connect(source_db)) as conn:
            conn.execute('CREATE TABLE fixture_private_cache(value TEXT)')
            conn.execute('INSERT INTO fixture_private_cache VALUES (?)',('CANARY-PRIVATE',));conn.commit()
        Preferences(self.source).patch({'sb_theme':'dark'})
        self.service.apply(self.preview()['token'],self.owner)
        with closing(sqlite3.connect(self.home/'speedbench-history.db')) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='fixture_private_cache'").fetchone())
        self.assertFalse((self.home/'ui-preferences.json').exists())

    def test_interrupted_source_task_retains_partial_and_raw_and_becomes_interrupted(self):
        from speedbench_jobs import JobStore
        from speedbench_tasks import resolve_config
        jobs=JobStore();job=jobs.create(resolve_config({'mode':'quick'}))
        jobs.publish(job,'node_probe',node_id='fixture',payload={'result':{'name':'fixture','latency_ms':12}})
        db.save_task(self.source/'speedbench-history.db',jobs.snapshot(job))
        preview=self.preview();self.assertEqual(preview['new_tasks'],1)
        self.service.apply(preview['token'],self.owner)
        saved=db.task_snapshot(self.home/'speedbench-history.db',job)
        self.assertEqual(saved['status'],'interrupted')
        self.assertTrue(saved['partial'])
        self.assertEqual(saved['results'][0]['latency_ms'],12)
        self.assertEqual(db.all_runs(self.home/'speedbench-history.db'),[])

    def test_backup_is_private_consistent_and_rollback_refuses_new_history(self):
        old=ledger(self.home,'2026-10-01T01:00:00')
        ledger(self.source,'2026-10-02T01:00:00')
        db.import_jsonl(self.home/'speedbench-history.db',self.home/'speedbench-history.jsonl')
        Preferences(self.home).patch({'sb_theme':'light'})
        seed=self.home/'identity-seed';seed.write_bytes(b'fixture-seed');seed.chmod(0o600)
        result=self.service.apply(self.preview()['token'],self.owner)
        backup=self.home/'history-import-backups'/result['backup_id']
        self.assertEqual((backup/'speedbench-history.jsonl').read_text().rstrip('\n'),old)
        self.assertEqual(db.latest_run(backup/'speedbench-history.db')['ts'],'2026-10-01T01:00:00')
        self.assertEqual((backup/'identity-seed').read_bytes(),b'fixture-seed')
        if os.name!='nt':
            self.assertEqual(backup.stat().st_mode&0o077,0)
            self.assertTrue(all(p.stat().st_mode&0o077==0 for p in backup.iterdir() if p.is_file()))
        self.service.rollback(result['backup_id'],self.owner)
        self.assertEqual((self.home/'speedbench-history.jsonl').read_text().rstrip('\n'),old)
        second=self.service.apply(self.preview()['token'],self.owner)
        ledger(self.home,'2026-10-03T01:00:00')
        with self.assertRaises(TransferError):self.service.rollback(second['backup_id'],self.owner)
        self.assertIn('2026-10-03',(self.home/'speedbench-history.jsonl').read_text())

    def test_unsafe_paths_and_missing_destination_owner_are_rejected(self):
        ledger(self.source,'2026-10-01T01:00:00')
        with self.assertRaises(TransferError):self.service.preview('relative',self.owner)
        with self.assertRaises(TransferError):self.service.preview(str(self.home),self.owner)
        with self.assertRaises(TransferError):self.service.preview(str(self.source),None)
        other=self.source/'other';other.write_text('fixture')
        history=self.source/'speedbench-history.jsonl';history.unlink();history.symlink_to(other)
        with self.assertRaises(TransferError):self.preview()

    def test_source_internal_conflict_is_not_silently_deduplicated(self):
        ledger(self.source,'2026-10-01T01:00:00',speed=1)
        ledger(self.source,'2026-10-01T01:00:00',speed=2)
        with self.assertRaises(TransferError):self.preview()
        self.assertFalse((self.home/'history-import-backups').exists())

    def test_destination_database_only_history_survives_ledger_creation(self):
        raw=ledger(self.home,'2026-10-01T01:00:00',whitespace=True)
        db.import_jsonl(self.home/'speedbench-history.db',self.home/'speedbench-history.jsonl')
        (self.home/'speedbench-history.jsonl').unlink()
        ledger(self.source,'2026-10-02T01:00:00')
        self.service.apply(self.preview()['token'],self.owner)
        self.assertIn(raw,(self.home/'speedbench-history.jsonl').read_text())
        self.assertEqual(len(db.all_runs(self.home/'speedbench-history.db')),2)

    def test_source_ledger_is_authority_when_database_contains_other_runs(self):
        ledger(self.source,'2026-10-01T01:00:00')
        db.import_jsonl(self.source/'speedbench-history.db',self.source/'speedbench-history.jsonl')
        (self.source/'speedbench-history.jsonl').unlink()
        ledger(self.source,'2026-10-02T01:00:00')
        preview=self.preview()
        self.assertEqual((preview['new_runs'],preview['ignored_database_runs']),(1,1))
        self.service.apply(preview['token'],self.owner)
        self.assertEqual([r['ts'] for r in db.all_runs(self.home/'speedbench-history.db')],['2026-10-02T01:00:00'])

    def test_crash_between_ledger_and_database_is_recovered_before_new_writes(self):
        import speedbench_transfer as transfer
        old=ledger(self.home,'2026-10-01T01:00:00',whitespace=True)
        ledger(self.source,'2026-10-02T01:00:00')
        original=transfer._atomic
        def crash(path,data):
            if path==self.home.resolve()/'speedbench-history.db':raise SystemExit('fixture crash')
            return original(path,data)
        with mock.patch.object(transfer,'_atomic',side_effect=crash), self.assertRaises(SystemExit):
            self.service.apply(self.preview()['token'],self.owner)
        self.assertTrue((self.home/transfer.PENDING).exists())
        self.assertTrue(HistoryTransfer(self.home).recover(self.owner))
        self.assertEqual((self.home/'speedbench-history.jsonl').read_text().rstrip('\n'),old)
        self.assertFalse((self.home/'speedbench-history.db').exists())
        self.assertFalse((self.home/transfer.PENDING).exists())

    def test_changed_data_after_crash_stops_recovery(self):
        import speedbench_transfer as transfer
        ledger(self.source,'2026-10-02T01:00:00')
        original=transfer._atomic
        def crash(path,data):
            if path==self.home.resolve()/'speedbench-history.db':raise SystemExit('fixture crash')
            return original(path,data)
        with mock.patch.object(transfer,'_atomic',side_effect=crash), self.assertRaises(SystemExit):
            self.service.apply(self.preview()['token'],self.owner)
        ledger(self.home,'2026-10-03T01:00:00')
        before=(self.home/'speedbench-history.jsonl').read_bytes()
        with self.assertRaises(TransferError):HistoryTransfer(self.home).recover(self.owner)
        self.assertEqual((self.home/'speedbench-history.jsonl').read_bytes(),before)
        self.assertTrue((self.home/transfer.PENDING).exists())

    def test_rollback_crash_mid_restore_is_recoverable(self):
        import speedbench_transfer as transfer
        old=ledger(self.home,'2026-10-01T01:00:00',whitespace=True)
        db.import_jsonl(self.home/'speedbench-history.db',self.home/'speedbench-history.jsonl')
        ledger(self.source,'2026-10-02T01:00:00')
        result=self.service.apply(self.preview()['token'],self.owner)
        original=transfer._atomic
        def crash(path,data):
            if path==self.home.resolve()/'speedbench-history.db':raise SystemExit('fixture crash')
            return original(path,data)
        with mock.patch.object(transfer,'_atomic',side_effect=crash), self.assertRaises(SystemExit):
            self.service.rollback(result['backup_id'],self.owner)
        self.assertTrue(HistoryTransfer(self.home).recover(self.owner))
        self.assertEqual((self.home/'speedbench-history.jsonl').read_text().rstrip('\n'),old)
        self.assertEqual(db.latest_run(self.home/'speedbench-history.db')['ts'],'2026-10-01T01:00:00')

    def test_missing_backup_file_rejects_before_any_restore(self):
        ledger(self.home,'2026-10-01T01:00:00')
        Preferences(self.home).patch({'sb_theme':'light'})
        ledger(self.source,'2026-10-02T01:00:00')
        result=self.service.apply(self.preview()['token'],self.owner)
        backup=self.home/'history-import-backups'/result['backup_id']
        (backup/'ui-preferences.json').unlink()
        before=(self.home/'speedbench-history.jsonl').read_bytes()
        with self.assertRaises(TransferError):self.service.rollback(result['backup_id'],self.owner)
        self.assertEqual((self.home/'speedbench-history.jsonl').read_bytes(),before)

    def test_writer_finishing_before_rollback_lease_cannot_lose_new_history(self):
        import speedbench_transfer as transfer
        ledger(self.source,'2026-10-02T01:00:00')
        result=self.service.apply(self.preview()['token'],self.owner)
        real_lease=BackendLease
        def intervening_writer(home,**kwargs):
            with real_lease(home,_task=True):
                ledger(Path(home),'2026-10-03T01:00:00')
                db.import_jsonl(Path(home)/'speedbench-history.db',Path(home)/'speedbench-history.jsonl')
            return real_lease(home,**kwargs)
        with mock.patch.object(transfer,'BackendLease',side_effect=intervening_writer), \
                mock.patch.object(self.service,'_owner'), self.assertRaises(TransferError):
            self.service.rollback(result['backup_id'],self.owner)
        self.assertEqual(db.latest_run(self.home/'speedbench-history.db')['ts'],'2026-10-03T01:00:00')
        self.assertIn('2026-10-03',(self.home/'speedbench-history.jsonl').read_text())
        self.assertFalse((self.home/transfer.PENDING).exists())

    def test_unicode_line_separators_inside_json_strings_are_not_record_boundaries(self):
        raw=json.dumps({'ts':'2026-10-01T01:00:00','results':[{'name':'中文\u2028节点\u2029与\u0085文本'}]},ensure_ascii=False)
        (self.source/'speedbench-history.jsonl').write_text(raw+'\n',encoding='utf-8')
        self.service.apply(self.preview()['token'],self.owner)
        with closing(sqlite3.connect(self.home/'speedbench-history.db')) as conn:
            self.assertEqual(conn.execute('SELECT raw FROM runs').fetchone()[0],raw)
        self.assertEqual((self.home/'speedbench-history.jsonl').read_text(),raw+'\n')

    def test_crash_after_applied_receipt_recovers_backup_pointer_and_rollback(self):
        import speedbench_transfer as transfer
        ledger(self.source,'2026-10-01T01:00:00')
        original=transfer._json_write
        def crash(path,value):
            if path==self.home.resolve()/transfer.LAST:raise SystemExit('fixture crash')
            return original(path,value)
        with mock.patch.object(transfer,'_json_write',side_effect=crash),self.assertRaises(SystemExit):
            self.service.apply(self.preview()['token'],self.owner)
        restarted=HistoryTransfer(self.home)
        self.assertTrue(restarted.recover(self.owner))
        status=restarted.status()
        self.assertIsNotNone(status['backup_id']);self.assertTrue(status['can_rollback'])
        restarted.rollback(status['backup_id'],self.owner)
        self.assertFalse((self.home/'speedbench-history.jsonl').exists())
