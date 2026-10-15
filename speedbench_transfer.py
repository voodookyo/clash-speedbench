"""Explicit history transfer, private backup and guarded rollback (stdlib).

Only fixed SpeedBench data names are read. Preview never writes the chosen
directories. A private scratch copy lets SQLite consume WAL without creating
or updating the original directory's shared-memory files. No source cache,
preferences, seed or credentials are installed into the destination.
"""
from contextlib import closing, contextmanager, ExitStack
from dataclasses import fields
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import time

import speedbench_db as db
from speedbench_identity import IdentityError, _secure_new_file, _windows_sid, _windows_private
from speedbench_jobs import TRANSITIONS, MAX_RESULTS, _result, safe_metrics, safe_milestones
from speedbench_owner import BackendLease, LeaseError, _private_owner_stream
from speedbench_tasks import TaskConfig

FILES = ('speedbench-history.jsonl', 'speedbench-history.db', 'ui-preferences.json', 'identity-seed')
SIDE = ('speedbench-history.db-wal', 'speedbench-history.db-shm')
PENDING = 'history-import-pending.json'
LAST = 'history-import-last.json'
MAX_BYTES = 64 * 1024 * 1024
MAX_RUNS = 10000
BACKUP_ID = re.compile(r'import_[0-9a-f]{32}')


class TransferError(RuntimeError):
    pass


def _root(value):
    if not isinstance(value, (str, Path)) or len(str(value)) > 4096:
        raise TransferError('请选择本机 SpeedBench 数据目录的绝对路径')
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise TransferError('目录必须为绝对路径，不能使用目录符号链接')
    try:
        path = path.resolve(strict=True)
        info = path.stat()
    except (OSError, ValueError):
        raise TransferError('数据目录不存在或无法安全访问') from None
    if not path.is_dir() or (os.name != 'nt' and info.st_uid != os.getuid()):
        raise TransferError('数据目录不存在或不属于当前用户')
    return path


def _info(path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode) or value.st_size > MAX_BYTES:
        raise TransferError('数据文件不是普通文件、含符号链接或超过 64MB 限制')
    if os.name != 'nt' and value.st_uid != os.getuid():
        raise TransferError('数据文件不属于当前用户')
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode, value.st_uid]


def _stamp(root):
    return {name: _info(root/name) for name in FILES + SIDE}


def _read(path):
    before = _info(path)
    if before is None:
        return None
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        actual = os.fstat(stream.fileno())
        if [actual.st_dev, actual.st_ino] != before[:2]:
            raise TransferError('文件在读取期间被替换，请重新预览')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or _info(path) != before:
        raise TransferError('文件在读取期间改变或超过限制，请重新预览')
    return data


def _private_write(path, data):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        _secure_new_file(path, _windows_sid() if os.name == 'nt' else None)
        stream.write(data)
        stream.flush(); os.fsync(stream.fileno())


def _atomic(path, data):
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.history-import-', dir=str(path.parent))
        with os.fdopen(descriptor, 'wb') as stream:
            _secure_new_file(temporary, _windows_sid() if os.name == 'nt' else None)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        _info(path)
        os.replace(temporary, path); temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _json_write(path, value):
    _atomic(path, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _json_read(path):
    data = _read(path)
    if data is None:
        return None
    if len(data) > 32768:
        raise TransferError('导入事务记录无效，请保留私有备份')
    if os.name == 'nt':
        _windows_private(path, _windows_sid())
    elif path.stat().st_mode & 0o077:
        raise TransferError('导入事务记录权限不安全，请保留私有备份')
    return json.loads(data)


@contextmanager
def _guard(root, names=('backend-owner.lock', 'benchmark-writer.lock')):
    """Hold existing leases only; never create or rewrite source metadata."""
    with ExitStack() as stack:
        for name in names:
            path = root/name
            if _info(path) is None:
                continue
            stream = stack.enter_context(_private_owner_stream(path))
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TransferError('目录有活跃写入者；请先关闭使用该目录的新旧 SpeedBench') from None
        yield


@contextmanager
def _sqlite_reader(root):
    with tempfile.TemporaryDirectory(prefix='speedbench-history-read-') as folder:
        private = Path(folder)
        for name in ('speedbench-history.db', 'speedbench-history.db-wal'):
            data = _read(root/name)
            if data is not None:
                _private_write(private/name, data)
        path = private/'speedbench-history.db'
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + 5
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            conn.execute('BEGIN')
            yield conn


def _sqlite_copy(root, path):
    _private_write(path, b'')
    with _sqlite_reader(root) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)


def _records(lines):
    records = {}
    for text in lines:
        raw = text.rstrip('\r\n')
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('ts'), str) or not 0 < len(value['ts']) <= 64:
            raise TransferError('历史中有无效时间戳或记录，请修复副本后重新预览')
        ts = value['ts']
        if ts in records and records[ts] != raw:
            raise TransferError('源历史内部存在同一时间戳的不同 raw，不能静默合并')
        records[ts] = raw
        if len(records) > MAX_RUNS:
            raise TransferError('历史超过 10000 轮限制，请分批导入')
    return records


def _tasks(conn, tables):
    if 'task_runs' not in tables:
        return {}
    tasks = {}
    for row in conn.execute('SELECT * FROM task_runs LIMIT 10001'):
        value = dict(row); job = value.get('job_id')
        if not isinstance(job, str) or not re.fullmatch(r'job_[0-9a-f]{32}', job):
            raise TransferError('任务身份无效，不能导入')
        status = value.get('status')
        if status not in set(TRANSITIONS) | {'completed', 'cancelled', 'failed', 'interrupted'}:
            raise TransferError('任务状态无效，不能导入')
        status = status if status in ('completed', 'cancelled', 'failed', 'interrupted') else 'interrupted'
        config = json.loads(value.get('config_json', '{}'))
        results = json.loads(value.get('results_json', '[]'))
        if not isinstance(config, dict) or not isinstance(results, list) or len(results) > MAX_RESULTS:
            raise TransferError('任务快照无效或超过限制')
        config = {f.name: config[f.name] for f in fields(TaskConfig) if f.name in config}
        metrics = {}; milestones = {}
        if 'task_metrics' in tables:
            for metric in conn.execute('SELECT * FROM task_metrics WHERE job_id=?', (job,)):
                metric = dict(metric); phase = metric['phase']
                counters = json.loads(metric['counters_json'])
                if phase == 'milestones':
                    milestones = safe_milestones(counters)
                else:
                    metrics.update(safe_metrics({phase: dict(metric, counters=counters)}))
        tasks[job] = dict(version=1, job_id=job, status=status, partial=status != 'completed',
            started_at=value.get('started_at'), finished_at=value.get('finished_at'), config=config,
            results=[_result(r) for r in results], elapsed_ms=value.get('elapsed_ms', 0),
            metrics=metrics, milestones=milestones)
        if len(tasks) > MAX_RUNS:
            raise TransferError('任务历史超过 10000 项限制')
    return tasks


def _snapshot(root, *, destination=False):
    before = _stamp(root)
    ledger = _read(root/FILES[0])
    records = _records(ledger.decode('utf-8').split('\n')) if ledger is not None else {}
    tasks = {}; database_records = {}
    if before[FILES[1]] is not None:
        with _sqlite_reader(root) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'runs' in tables:
                database_records = _records([r[0] for r in conn.execute('SELECT raw FROM runs LIMIT 10001')])
            tasks = _tasks(conn, tables)
    if ledger is None or destination:
        # Existing destination DB-only records must survive ledger creation.
        # JSONL wins exact duplicate timestamps; existing DB raw is never rewritten.
        records = dict(database_records, **records)
    if _stamp(root) != before:
        raise TransferError('目录内容在预览期间变化，请重新预览')
    return dict(stamp=before, ledger=ledger, records=records, tasks=tasks,
                format='jsonl' if ledger is not None else 'sqlite' if before[FILES[1]] else 'empty',
                database_only=sum(ts not in records for ts in database_records))


def _plan(source, destination):
    new = {ts: raw for ts, raw in source['records'].items() if ts not in destination['records']}
    new_tasks = {job: row for job, row in source['tasks'].items() if job not in destination['tasks']}
    if len(destination['records']) + len(new) > MAX_RUNS:
        raise TransferError('合并后的历史将超过 10000 轮限制，请先清理或拆分来源副本')
    if len(destination['tasks']) + len(new_tasks) > MAX_RUNS:
        raise TransferError('合并后的任务历史将超过 10000 项限制')
    conflicts = sum(raw != destination['records'][ts] for ts, raw in source['records'].items()
                    if ts in destination['records'])
    conflicts += sum(row != destination['tasks'][job] for job, row in source['tasks'].items()
                     if job in destination['tasks'])
    return new, new_tasks, conflicts


def _append(ledger, rows):
    data = ledger or b''
    for raw in rows:
        if data and not data.endswith((b'\n', b'\r')):
            data += b'\n'
        data += raw.encode('utf-8') + b'\n'
    if len(data) > MAX_BYTES:
        raise TransferError('合并后的历史超过 64MB 限制')
    return data


class HistoryTransfer:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.lock = threading.RLock()
        self.previews = {}

    def _owner(self, owner):
        if (not isinstance(owner, BackendLease) or owner._task or owner.path.parent != self.home or
                owner.stream is None or owner.stream.closed):
            raise TransferError('需要本实例已经持有的数据目录所有权')
        _root(self.home)

    def _backup_path(self, backup_id):
        if not isinstance(backup_id, str) or not BACKUP_ID.fullmatch(backup_id):
            raise TransferError('备份身份无效')
        parent = self.home/'history-import-backups'
        if parent.is_symlink():
            raise TransferError('备份目录不能是符号链接')
        path = parent/backup_id
        if path.is_symlink():
            raise TransferError('备份目录不能是符号链接')
        return path

    def preview(self, directory, owner):
        try:
            with self.lock:
                self._owner(owner)
                if _read(self.home/PENDING) is not None:
                    raise TransferError('此前导入尚未恢复，请先恢复该私有备份')
                source_root = _root(directory)
                if source_root == self.home:
                    raise TransferError('源目录与本实例目录相同，无需导入')
                with _guard(source_root), _guard(self.home, ('benchmark-writer.lock',)):
                    source = _snapshot(source_root)
                    destination = _snapshot(self.home, destination=True)
                if source['format'] == 'empty':
                    raise TransferError('该目录没有固定名称的 JSONL 或 SQLite 历史文件')
                new, tasks, conflicts = _plan(source, destination)
                token = secrets.token_hex(16)
                self.previews = {key: row for key, row in self.previews.items() if row['expires'] > time.monotonic()}
                while len(self.previews) >= 2:
                    del self.previews[next(iter(self.previews))]
                self.previews[token] = dict(source_root=source_root, source=source, destination=destination,
                    expires=time.monotonic()+600)
                return dict(ok=True, token=token, source=str(source_root), destination=str(self.home),
                    source_format=source['format'], new_runs=len(new), new_tasks=len(tasks),
                    duplicate_runs=len(source['records'])-len(new)-sum(ts in destination['records'] and
                        raw != destination['records'][ts] for ts, raw in source['records'].items()),
                    conflicts=conflicts, can_apply=not conflicts,
                    ignored_database_runs=source['database_only'], private_files_transferred=False,
                    source_must_be_closed=True, timestamp_policy='无时区旧记录按本机时区解释')
        except TransferError:
            raise
        except (OSError, ValueError, sqlite3.Error, IdentityError, LeaseError):
            raise TransferError('无法安全预览该数据目录；未修改原目录或文件') from None

    def _receipt(self, backup_id):
        path = self._backup_path(backup_id)
        _root(path)
        value = _json_read(path/'receipt.json')
        if (not isinstance(value, dict) or value.get('version') != 1 or value.get('backup_id') != backup_id or
                value.get('state') not in ('prepared', 'applied', 'rolling_back', 'rolled_back') or
                not isinstance(value.get('original_stamp'), dict) or set(value['original_stamp']) != set(FILES+SIDE) or
                not isinstance(value.get('existed'), list) or set(value['existed'])-set(FILES) or
                not isinstance(value.get('next_files'), list) or set(value['next_files'])-set(FILES[:2])):
            raise TransferError('私有备份事务记录无效；请保留文件')
        return path, value

    def _clear_sidecars(self):
        for name in SIDE:
            if _info(self.home/name) is not None:
                (self.home/name).unlink()

    def _restore(self, path, receipt):
        # Validate every required private backup before replacing any file.
        original = {}
        for name in receipt['existed']:
            original[name] = _read(path/name)
            if original[name] is None:
                raise TransferError('私有备份缺少原文件；停止恢复')
            if os.name == 'nt':
                _windows_private(path/name, _windows_sid())
            elif (path/name).stat().st_mode & 0o077:
                raise TransferError('私有备份文件权限不安全；停止恢复')
        self._clear_sidecars()
        for name in FILES:
            if name in receipt['existed']:
                _atomic(self.home/name, original[name])
            elif _info(self.home/name) is not None:
                (self.home/name).unlink()
        receipt['state'] = 'rolled_back'
        receipt['restored_stamp'] = _stamp(self.home)
        _json_write(path/'receipt.json', receipt)
        (self.home/PENDING).unlink(missing_ok=True)

    def _pending_known(self, path, receipt):
        current = _stamp(self.home)
        initial = receipt.get('rollback_stamp', receipt['original_stamp'])
        for name in FILES:
            if current[name] == initial.get(name):
                continue
            data = _read(self.home/name)
            original = _read(path/name) if name in receipt['existed'] else None
            staged = (_read(path/('next-'+name)) if name in receipt['next_files']
                      and receipt['state'] == 'prepared' else original)
            if data != original and data != staged:
                raise TransferError('导入中断后出现其他数据更改；停止自动恢复，保留私有备份')
        for name in SIDE:
            if current[name] is not None and current[name] != initial.get(name):
                raise TransferError('SQLite 辅助文件已有其他更改；停止自动恢复，保留私有备份')

    def apply(self, token, owner):
        backup = None; receipt = None
        try:
            with self.lock:
                self._owner(owner)
                preview = self.previews.pop(token, None) if isinstance(token, str) else None
                if preview is None or preview['expires'] <= time.monotonic():
                    raise TransferError('预览已失效，请重新预览')
                if _read(self.home/PENDING) is not None:
                    raise TransferError('此前导入尚未恢复')
                with _guard(preview['source_root']):
                    # Re-read exact raw/task content as well as metadata. No digests.
                    source = _snapshot(preview['source_root'])
                    destination = _snapshot(self.home, destination=True)
                    if source != preview['source'] or destination != preview['destination']:
                        raise TransferError('源目录或本实例数据已改变，请重新预览')
                    new, tasks, conflicts = _plan(source, destination)
                    if conflicts:
                        raise TransferError('同一时间戳或任务身份包含不同数据，拒绝覆盖；请检查源副本')
                    if not new and not tasks:
                        return dict(ok=True, imported_runs=0, imported_tasks=0, backup_id=None)
                    with BackendLease(self.home, _task=True):
                        parent = self.home/'history-import-backups'
                        if parent.is_symlink():
                            raise TransferError('备份目录不能是符号链接')
                        parent.mkdir(mode=0o700, exist_ok=True)
                        backup_id = 'import_'+secrets.token_hex(16)
                        backup = self._backup_path(backup_id); backup.mkdir(mode=0o700)
                        if os.name == 'nt':
                            _secure_new_file(backup, _windows_sid())
                        existed = []
                        for name in FILES:
                            if destination['stamp'][name] is not None:
                                if name == FILES[1]:
                                    _sqlite_copy(self.home, backup/name)
                                else:
                                    _private_write(backup/name, _read(self.home/name))
                                existed.append(name)
                        # Stage complete new files alongside the original backups
                        # so a crash can identify only this transaction's writes.
                        base_records = _records(destination['ledger'].decode('utf-8').split('\n')) if destination['ledger'] is not None else {}
                        ledger = _append(destination['ledger'],
                            [raw for ts, raw in destination['records'].items() if ts not in base_records] + list(new.values()))
                        next_files = [FILES[1]]
                        next_ledger = backup/('next-'+FILES[0])
                        if destination['ledger'] is not None or ledger:
                            _private_write(next_ledger, ledger); next_files.append(FILES[0])
                        next_db = backup/('next-'+FILES[1])
                        if destination['stamp'][FILES[1]] is not None:
                            _sqlite_copy(self.home, next_db)
                        else:
                            _private_write(next_db, b'')
                        if next_ledger.exists():
                            db.import_jsonl(next_db, next_ledger)
                        for task in tasks.values():
                            db.save_task(next_db, task)
                        # An empty new SQLite-only task/history still needs schema.
                        with closing(db._open(next_db)) as conn:
                            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        if _stamp(self.home) != destination['stamp'] or _stamp(preview['source_root']) != source['stamp']:
                            raise TransferError('目录内容在准备期间改变，请重新预览；原文件未替换')
                        receipt = dict(version=1, backup_id=backup_id, state='prepared',
                            original_stamp=destination['stamp'], existed=existed, next_files=next_files)
                        _json_write(backup/'receipt.json', receipt)
                        _json_write(self.home/PENDING, {'version':1, 'backup_id':backup_id})
                        if FILES[0] in next_files:
                            _atomic(self.home/FILES[0], _read(next_ledger))
                        self._clear_sidecars()
                        _atomic(self.home/FILES[1], _read(next_db))
                        receipt.update(state='applied', applied_stamp=_stamp(self.home))
                        _json_write(backup/'receipt.json', receipt)
                        _json_write(self.home/LAST, {'version':1, 'backup_id':backup_id})
                        (self.home/PENDING).unlink()
                        for name in next_files:
                            (backup/('next-'+name)).unlink(missing_ok=True)
                        return dict(ok=True, imported_runs=len(new), imported_tasks=len(tasks), backup_id=backup_id)
        except Exception as error:
            if receipt is not None and receipt['state'] == 'applied':
                raise TransferError('历史已写入，但导入收尾未完成；请保留私有备份并重启核验，恢复前停止新写入') from None
            if receipt is not None and receipt['state'] == 'prepared':
                try:
                    # Reacquire the writer lease and use the durable receipt;
                    # exceptions here have already unwound the commit lease.
                    self.recover(owner)
                except Exception:
                    raise TransferError('导入失败且回退未完成；保留私有备份，停止新任务并恢复该事务') from None
            if isinstance(error, TransferError):
                raise
            raise TransferError('导入失败；已回退原文件或尚未替换，请保留私有备份') from None

    def recover(self, owner):
        """Before any startup DB/seed write, reconcile a durable pending import."""
        with self.lock:
            self._owner(owner)
            pending = _json_read(self.home/PENDING)
            if pending is None:
                return False
            if not isinstance(pending, dict) or set(pending) != {'version','backup_id'} or pending['version'] != 1:
                raise TransferError('导入恢复记录无效，请保留私有备份')
            path, receipt = self._receipt(pending['backup_id'])
            with BackendLease(self.home, _task=True):
                if receipt['state'] in ('prepared', 'rolling_back'):
                    self._pending_known(path, receipt)
                    self._restore(path, receipt)
                elif receipt['state'] == 'applied':
                    if _stamp(self.home) != receipt.get('applied_stamp'):
                        raise TransferError('导入后已有其他更改，停止自动恢复并保留私有备份')
                    _json_write(self.home/LAST, {'version':1, 'backup_id':pending['backup_id']})
                    (self.home/PENDING).unlink(missing_ok=True)
                elif receipt['state'] == 'rolled_back' and _stamp(self.home) != receipt.get('restored_stamp'):
                    raise TransferError('恢复后已有其他更改，停止自动恢复并保留私有备份')
                else:
                    (self.home/PENDING).unlink(missing_ok=True)
            return True

    def rollback(self, backup_id, owner):
        with self.lock:
            self._owner(owner)
            path, receipt = self._receipt(backup_id)
            if receipt['state'] != 'applied' or _stamp(self.home) != receipt.get('applied_stamp'):
                raise TransferError('本实例已有其他更改或备份已恢复；拒绝删除新数据')
            with BackendLease(self.home, _task=True):
                # A writer can finish between the optimistic check and this
                # acquisition. Never restore over that newly committed data.
                if _stamp(self.home) != receipt.get('applied_stamp'):
                    raise TransferError('取得写入所有权前数据已改变；拒绝删除新数据')
                _json_write(self.home/PENDING, {'version':1, 'backup_id':backup_id})
                receipt.update(state='rolling_back', rollback_stamp=_stamp(self.home))
                _json_write(path/'receipt.json', receipt)
                self._restore(path, receipt)
            return dict(ok=True, backup_id=backup_id)

    def status(self):
        pending = _json_read(self.home/PENDING)
        last = _json_read(self.home/LAST)
        backup_id = last.get('backup_id') if isinstance(last, dict) else None
        if backup_id is not None and (not isinstance(backup_id,str) or not BACKUP_ID.fullmatch(backup_id)):
            raise TransferError('导入备份记录无效')
        can_rollback=False
        if backup_id:
            _path, receipt=self._receipt(backup_id)
            can_rollback=not pending and receipt['state']=='applied' and _stamp(self.home)==receipt.get('applied_stamp')
        return dict(pending=bool(pending), backup_id=backup_id, can_rollback=can_rollback)
