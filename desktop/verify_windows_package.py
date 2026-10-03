"""Independent portable-artifact acceptance, temporary data/no live benchmark.

Verifies the produced ZIP, not the development resource stage. Never replaces
an installation or queries a real controller/provider. Native window/tray
manual acceptance is a separate gate, not claimed by this headless check.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import tempfile
import threading

try:from .prepare_resources import extract_zip, digest
except ImportError:from prepare_resources import extract_zip, digest
try:from .package_windows import package_path, STAGE
except ImportError:from package_windows import package_path, STAGE


def bootstrap(root,data,manifest):
    env=dict(os.environ,SPEEDBENCH_HOME=str(data),PATH=str(Path(os.environ['SystemRoot'])/'System32'))
    for key in list(env):
        if key.upper().startswith('PYTHON') or key.startswith(('SPEEDBENCH_IP','SPEEDBENCH_SCAMALYTICS')) or key=='SPEEDBENCH_VERGE_ROOT':env.pop(key)
    python=root/'runtime/python.exe'
    process=subprocess.Popen([str(python),'-B','-E','-s','-u',str(root/'app/speedbench_desktop.py')],
        cwd=data,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    token=None;nonce=secrets.token_hex(32)
    try:
        process.stdin.write(json.dumps({'protocol':1,'parent_pid':os.getpid(),'nonce':nonce}).encode()+b'\n');process.stdin.flush()
        received=queue.Queue(maxsize=1)
        threading.Thread(target=lambda:received.put(process.stdout.readline(4097)),daemon=True).start()
        frame=json.loads(received.get(timeout=10));token=frame['token']
        if frame['nonce']!=nonce or frame['pid']!=process.pid or frame['version']!=manifest['version']:
            raise ValueError('Packaged handshake identity mismatch')
        connection=http.client.HTTPConnection('127.0.0.1',frame['port'],timeout=5)
        try:
            connection.request('GET','/api/desktop/identity')
            response=connection.getresponse();response.read()
            if response.status!=403:raise ValueError('Packaged private identity accepted unauthenticated request')
            auth={'X-SpeedBench-Token':token}
            connection.request('GET','/api/desktop/identity',headers=auth)
            response=connection.getresponse();body=response.read()
            if response.status!=200 or token.encode() in body or nonce.encode() in body:
                raise ValueError('Packaged public identity disclosure or authentication failure')
            connection.request('GET','/api/releases',headers=auth)
            response=connection.getresponse();local_version=json.loads(response.read())
            if response.status!=200 or local_version.get('status')!='not_checked' or local_version.get('current')!=manifest['version']:
                raise ValueError('Packaged local version lookup failed or queried updates automatically')
            connection.request('POST','/api/releases/check',json.dumps({'url':'https://evil.example/fixture'}),
                headers={**auth,'Content-Type':'application/json'})
            response=connection.getresponse();response.read()
            if response.status!=400:raise ValueError('Packaged release check accepted arbitrary parameters')
            connection.request('GET','/api/data-status',headers=auth)
            response=connection.getresponse();data_status=json.loads(response.read())
            if response.status!=200 or data_status.get('automatic_import') is not False or Path(data_status.get('data_home','')).resolve()!=data.resolve():
                raise ValueError('Packaged data guidance mismatch')
            connection.request('GET','/api/config-root',headers=auth)
            response=connection.getresponse();root_info=json.loads(response.read())
            if response.status!=200 or root_info.get('mode')!='auto' or root_info.get('persistence')!='session':
                raise ValueError('Packaged configuration root leaked across sessions')
            connection.request('POST','/api/config-root/preview',json.dumps({'root':'../CANARY-config'}),
                headers={**auth,'Content-Type':'application/json'})
            response=connection.getresponse();body=response.read()
            if response.status!=400 or b'CANARY-config' in body:raise ValueError('Packaged configuration root accepted unsafe path')
            for module in ('preferences.js','releases.js','config-root.js'):
                connection.request('GET','/static/'+module)
                response=connection.getresponse();body=response.read()
                if (response.status!=200 or 'application/javascript' not in response.getheader('Content-Type','') or
                        hashlib.sha256(body).hexdigest()!=manifest['files'].get('app/web/'+module)):
                    raise ValueError('Packaged shared settings module missing or altered')
            connection.request('POST','/api/preferences',json.dumps({'sb_theme':'dark'}),
                headers={**auth,'Content-Type':'application/json','Origin':'https://evil.example'})
            response=connection.getresponse();response.read()
            if response.status!=403:raise ValueError('Packaged Origin validation failed')
            connection.request('POST','/api/preferences',json.dumps({'sb_theme':'dark'}),
                headers={**auth,'Content-Type':'application/json'})
            response=connection.getresponse();response.read()
            if response.status!=200:raise ValueError('Packaged preferences cannot persist')
            connection.request('GET','/api/preferences',headers=auth)
            response=connection.getresponse();saved=json.loads(response.read())
            if response.status!=200 or saved['values'].get('sb_theme')!='dark':raise ValueError('Packaged preferences not restored')
        finally:connection.close()
        process.stdin.close();process.stdin=None
        out,err=process.communicate(timeout=10)
        if process.returncode!=0 or nonce.encode() in out+err or token.encode() in out+err:
            raise ValueError('Packaged cleanup or private output boundary failed')
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)
        for stream in (process.stdin,process.stdout,process.stderr):
            if stream:stream.close()


def verify_worker_cleanup(root,data):
    """Exercise actual bundled cleanup code, using only fixture Python children."""
    program='''
import subprocess,sys
import speedbench_workers as workers
from clash_speedbench import CLEANUP_FAILED_EXIT
assert CLEANUP_FAILED_EXIT==3
group=[];other=None
def spawn():
    return subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
try:
    other=spawn()
    for i in range(2):
        worker=workers.Worker('fixture',[],{},None)
        worker.dir=workers._WorkerDirectory();group.append(worker);worker.proc=spawn()
    workers.stop_workers(group)
    assert all(w.proc.poll() is not None for w in group)
    assert other.poll() is None
finally:
    for process in [w.proc for w in group]+[other]:
        if process is not None and process.poll() is None:process.kill();process.wait(timeout=3)
    for worker in group:worker.dir.cleanup()
'''
    env=dict(os.environ,SPEEDBENCH_HOME=str(data),PATH=str(Path(os.environ['SystemRoot'])/'System32'))
    env.pop('SPEEDBENCH_VERGE_ROOT',None)
    result=subprocess.run([str(root/'runtime/python.exe'),'-B','-E','-s','-c',program],
        cwd=root/'app',env=env,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode!=0:raise ValueError('Bundled worker group cleanup fixture failed')


def verify_cli_ownership(root,data):
    """Actual bundled backend-to-CLI pipe with a fixture measurement body only."""
    child='''
import sys
sys.path.insert(0,sys.argv.pop(1))
import clash_speedbench as core
import speedbench_owner as owner
def execute(args,config):
    assert owner.delegated_active()
    from pathlib import Path
    try:owner.BackendLease(Path(args.history).parent).acquire()
    except owner.LeaseError:pass
    else:raise AssertionError('Delegated child did not retain directory ownership')
    print('fixture-owned-cli',flush=True)
    return 0
core._execute_benchmark=execute
assert core.main()==0
'''
    program='''
import io,os,sys
from pathlib import Path
from contextlib import redirect_stderr
import speedbench_web as web
import clash_speedbench as core
from speedbench_owner import BackendLease
history=Path(os.environ['SPEEDBENCH_HOME'])/'speedbench-history.jsonl'
original=history.read_bytes()
with BackendLease(history.parent) as lease:
    web.DATA_OWNER=lease;web.HISTORY=history
    web.connect_controller=lambda *a,**k:None
    web.sync_db=lambda:None
    web.benchmark_command=lambda params:[sys.executable,'-B','-E','-s','-u','-c',sys.argv[1],str(Path(web.SCRIPT).parent),'--yes','--history',str(history)]
    web.run_benchmark({})
    assert web.STATE['exit_code']==0 and not web.STATE['running']
    assert 'fixture-owned-cli' in web.STATE['lines']
    assert lease.instance_id not in str(web.STATE['lines'])
    core.connect_controller=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Must not connect'))
    sys.argv=['clash_speedbench.py','--yes','--history',str(history)]
    with redirect_stderr(io.StringIO()):assert core.main()==2
web.DATA_OWNER=None
assert history.read_bytes()==original
with BackendLease(history.parent):pass
'''
    env=dict(os.environ,SPEEDBENCH_HOME=str(data),PATH=str(Path(os.environ['SystemRoot'])/'System32'))
    env.pop('SPEEDBENCH_VERGE_ROOT',None)
    result=subprocess.run([str(root/'runtime/python.exe'),'-B','-E','-s','-c',program,child],
        cwd=root/'app',env=env,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode!=0:raise ValueError('Bundled private CLI ownership fixture failed')


def verify_cli_partial(root,data):
    """Bundled runner -> CLI failure -> JSONL/SQLite, fixture metrics only."""
    partial=data/'partial-history-fixture';partial.mkdir()
    child='''
import sys
sys.path.insert(0,sys.argv.pop(1))
import clash_speedbench as core
from speedbench_progress import publish_result
def execute(args,config):
    row=core.Result(name='fixture node',provider='',proto='ss',latency_ms=20,
        speeds_mbps=[30.0],median_mbps=30.0,best_mbps=30.0,status='ok')
    with core.measure(args,'download') as counts:
        counter=core.DownloadCounter(args,row,counts)
        counter.start();counter.finish(30.0,.25)
    publish_result(args,'node_measurement',row,phase_name='measuring')
    return 3
core._execute_benchmark=execute
raise SystemExit(core.main())
'''
    program='''
import os,sys,json,sqlite3
from pathlib import Path
from contextlib import closing
import speedbench_web as web
from speedbench_owner import BackendLease
from speedbench_tasks import resolve_config
history=Path(os.environ['SPEEDBENCH_HOME'])/'speedbench-history.jsonl'
original=b'{"ts":"fixture-original", "results": []}\\n'
history.write_bytes(original)
job=web.JOBS.create(resolve_config())
with BackendLease(history.parent) as lease:
    web.DATA_OWNER=lease;web.HISTORY=history
    web.connect_controller=lambda *a,**k:None
    web.benchmark_command=lambda params:[sys.executable,'-B','-E','-s','-u','-c',sys.argv[1],str(Path(web.SCRIPT).parent),
        '--yes','--no-ip','--history',str(history),'--output',str(history.parent/'partial.csv')]
    web.run_benchmark({'_job_id':job})
    assert web.STATE['exit_code']==3 and not web.STATE['running'] and web.STATE['cleanup_incomplete']
    assert lease.instance_id not in str(web.STATE['lines'])
    assert history.read_bytes().startswith(original)
    rows=history.read_text(encoding='utf-8').splitlines();assert len(rows)==2
    record=json.loads(rows[1]);assert record['task']['partial'] and record['task']['status']=='failed'
    assert record['results'][0]['median_mbps']==30.0 and record['results'][0]['ip_quality_score'] is None
    task=web.speedbench_db.task_snapshot(web.db_path(),job)
    assert task['status']=='failed' and task['partial'] and task['results'][0]['median_mbps']==30.0
    assert task['results'][0]['download_bytes']==250000
    assert task['metrics']['download']['attempts']==1 and task['metrics']['download']['successes']==1
    assert task['metrics']['download']['bytes']==250000 and task['metrics']['summary']['duration_ms']>=0
    assert task['run_id'] is not None
    with closing(sqlite3.connect(web.db_path())) as connection:
        assert [r[0] for r in connection.execute('SELECT raw FROM runs ORDER BY id')]==rows
web.DATA_OWNER=None
with BackendLease(history.parent):pass
'''
    env=dict(os.environ,SPEEDBENCH_HOME=str(partial),PATH=str(Path(os.environ['SystemRoot'])/'System32'))
    env.pop('SPEEDBENCH_VERGE_ROOT',None)
    result=subprocess.run([str(root/'runtime/python.exe'),'-B','-E','-s','-c',program,child],
        cwd=root/'app',env=env,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode!=0:raise ValueError('Bundled CLI partial JSONL/SQLite fixture failed')


def verify(package):
    if os.name!='nt':raise ValueError('Windows artifact verification requires Windows')
    package=Path(package).resolve()
    expected=package.with_suffix('.zip.sha256').read_text(encoding='utf-8').split()[0]
    if digest(package)!=expected:raise ValueError('Portable ZIP checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='speedbench artifact 中文 空格 🧪-') as folder:
        temporary=Path(folder);extract_zip(package,temporary)
        root=temporary/'Clash-SpeedBench';manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        if manifest['target']!='windows-x86_64':raise ValueError('Unexpected artifact platform')
        executable=root/'clash-speedbench-desktop.exe'
        def native_check():
            return subprocess.run([str(executable),'--verify-package'],cwd=temporary,timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW).returncode
        if native_check()!=0:raise ValueError('Native embedded manifest rejected package')
        data=temporary/'独立数据 中文 空格 🧪';data.mkdir()
        history=data/'speedbench-history.jsonl';raw=b'{"ts":"fixture-only","results":[]}\n';history.write_bytes(raw)
        bootstrap(root,data,manifest);bootstrap(root,data,manifest)
        verify_worker_cleanup(root,data)
        verify_cli_ownership(root,data)
        verify_cli_partial(root,data)
        if history.read_bytes()!=raw:raise ValueError('Original fixture raw changed during packaged lifecycle')
        # An attacker-updated side manifest cannot authorize modified source.
        source=root/'app/speedbench_desktop.py';source.write_bytes(source.read_bytes()+b'\n# fixture tamper\n')
        manifest['files']['app/speedbench_desktop.py']=digest(source)
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        if native_check()==0:raise ValueError('Mutable side manifest bypassed native integrity anchor')
    print('Windows artifact acceptance OK: native integrity/tamper, bundled Python without PATH, private bootstrap, Origin, restart/preferences, local version/data guidance and shared settings assets, bundled worker cleanup group leaves unrelated fixture process alive, private backend-to-CLI delegation and direct CLI exclusion, failed partial JSONL/SQLite/task retention and observed metrics, original raw retained. Native window/tray acceptance not included.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('package',nargs='?')
    value=parser.parse_args().package
    if value is None:value=package_path(json.loads((STAGE/'manifest.json').read_text(encoding='utf-8')))
    verify(value)
