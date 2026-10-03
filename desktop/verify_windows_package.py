"""Independent portable-artifact acceptance, temporary data/no live benchmark.

Verifies the produced ZIP, not the development resource stage. Never replaces
an installation or queries a real controller/provider. Native window/tray
manual acceptance is a separate gate, not claimed by this headless check.
"""
import argparse
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
        if key.upper().startswith('PYTHON') or key.startswith(('SPEEDBENCH_IP','SPEEDBENCH_SCAMALYTICS')):env.pop(key)
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
        if history.read_bytes()!=raw:raise ValueError('Original fixture raw changed during packaged lifecycle')
        # An attacker-updated side manifest cannot authorize modified source.
        source=root/'app/speedbench_desktop.py';source.write_bytes(source.read_bytes()+b'\n# fixture tamper\n')
        manifest['files']['app/speedbench_desktop.py']=digest(source)
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        if native_check()==0:raise ValueError('Mutable side manifest bypassed native integrity anchor')
    print('Windows artifact acceptance OK: native integrity/tamper, bundled Python without PATH, private bootstrap, Origin, restart/preferences, original raw retained. Native window/tray acceptance not included.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('package',nargs='?')
    value=parser.parse_args().package
    if value is None:value=package_path(json.loads((STAGE/'manifest.json').read_text(encoding='utf-8')))
    verify(value)
