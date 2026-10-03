"""Build-only resource staging: pinned runtime + explicit public source list.

No pip, user history, environment snapshots or private config in the bundle.
Only build artifacts under desktop/src-tauri/resources and dist are written.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
try:from .license_notices import notices
except ImportError:from license_notices import notices

ROOT=Path(__file__).resolve().parents[1]
STAGE=ROOT/'desktop'/'src-tauri'/'resources'
MODULES=('clash_speedbench','speedbench_controller','speedbench_identity','speedbench_sources',
         'speedbench_tasks','speedbench_jobs','speedbench_process','speedbench_progress',
         'speedbench_owner','speedbench_desktop','speedbench_preferences','speedbench_db','speedbench_ip_intel',
         'speedbench_leak','speedbench_web','speedbench_switch','speedbench_workers','speedbench_tray')
WEB=('index.html','app.js','tasks.js','view.js','style.css')


def digest(path):
    checksum=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1_048_576),b''):checksum.update(chunk)
    return checksum.hexdigest()


def safe_archive_path(name):
    path=PurePosixPath(name)
    if not name or '\x00' in name or len(name)>1024 or len(path.parts)>32 or not path.parts or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError('Invalid archive member')
    return path


def extract_tar(archive,destination):
    """Materialize internal file links, exclude vendor pip/site-packages.

    Never invoke tar/system shells, extract devices or follow an archive link
    outside its root. This remains compatible with Python 3.9's tarfile API.
    """
    pending=[];total=0;seen=set()
    with tarfile.open(archive,'r:gz') as bundle:
        for member in bundle:
            relative=safe_archive_path(member.name)
            if relative in seen or len(seen)>=10_000:raise ValueError('Duplicate or excessive archive members')
            seen.add(relative)
            if 'site-packages' in relative.parts or '__pycache__' in relative.parts or relative.name in ('pip','pip3','pip3.14'):
                continue
            path=destination.joinpath(*relative.parts)
            if member.isdir():path.mkdir(parents=True,exist_ok=True)
            elif member.isfile():
                total+=member.size
                if total>500_000_000 or member.size>100_000_000:raise ValueError('Runtime archive exceeds bounds')
                path.parent.mkdir(parents=True,exist_ok=True)
                with bundle.extractfile(member) as source,path.open('wb') as out:shutil.copyfileobj(source,out)
                path.chmod(member.mode & 0o777)
            elif member.issym() or member.islnk():
                if any(x in member.linkname for x in ('\\',':','\x00')) or not member.linkname:
                    raise ValueError('Invalid runtime link')
                target=(relative.parent/PurePosixPath(member.linkname)) if member.issym() else PurePosixPath(member.linkname)
                # Legitimate relative links may contain .. inside the runtime.
                resolved=destination.joinpath(*target.parts).resolve()
                if not resolved.is_relative_to(destination.resolve()) or PurePosixPath(member.linkname).is_absolute():
                    raise ValueError('Archive link escapes runtime')
                pending.append((path,resolved))
            else:raise ValueError('Unsupported runtime archive member')
    while pending:
        next_pending=[]
        for path,target in pending:
            if target.is_file():path.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,path)
            else:next_pending.append((path,target))
        if len(next_pending)==len(pending):raise ValueError('Runtime link target missing or cyclic')
        pending=next_pending


def extract_zip(archive,destination):
    with zipfile.ZipFile(archive) as bundle:
        seen=set();total=0
        for member in bundle.infolist():
            path=safe_archive_path(member.filename)
            if path in seen or len(seen)>=5000:raise ValueError('Duplicate or excessive archive members')
            seen.add(path);total+=member.file_size
            if member.file_size>100_000_000 or total>500_000_000:
                raise ValueError('Runtime archive exceeds bounds')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Runtime ZIP links are not allowed')
        bundle.extractall(destination)


def prepare(target):
    lock=json.loads((ROOT/'desktop'/'runtime-lock.json').read_text(encoding='utf-8'))
    runtime=lock[target] # Unsupported platform must fail, never use system Python.
    prior=STAGE/'manifest.json'
    if prior.exists():
        previous=json.loads(prior.read_text(encoding='utf-8'))
        if previous['target']!=target or previous['runtime_version']!=lock['version']:
            raise ValueError('Runtime target/version changed; use a fresh build workspace')
    cache=ROOT/'dist'/'desktop-downloads';cache.mkdir(parents=True,exist_ok=True)
    archive=cache/Path(runtime['url']).name
    if not archive.exists():
        partial=archive.with_suffix(archive.suffix+'.partial')
        try:
            # Use the official fixed HTTPS URL, no unverified installer scripts.
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(runtime['url'],timeout=30) as response, partial.open('wb') as out:
                received=0
                for chunk in iter(lambda:response.read(1_048_576),b''):
                    received+=len(chunk)
                    if received>250_000_000:raise ValueError('Runtime download exceeds bound')
                    out.write(chunk)
            if digest(partial)!=runtime['sha256']:raise ValueError('Runtime SHA256 mismatch')
            partial.replace(archive)
        finally:
            if partial.exists():partial.unlink() # Owned .partial build artifact only.
    if digest(archive)!=runtime['sha256']:raise ValueError('Cached runtime SHA256 mismatch')
    py=STAGE/'runtime';py.mkdir(parents=True,exist_ok=True)
    if runtime['format']=='zip':
        extract_zip(archive,py)
    elif runtime['format']=='tar.gz':extract_tar(archive,py)
    else:raise ValueError('Unsupported pinned runtime format')
    # Windows embeddable CPython does not import system site/PYTHONPATH.
    # The source sidecar path is a fixed resource path, not user config.
    if target.startswith('windows-'):
        pth=list(py.glob('python*._pth'))
        if len(pth)!=1:raise ValueError('Unexpected embeddable runtime layout')
        pth[0].write_text('python314.zip\n.\n../app\n',encoding='utf-8')
    app=STAGE/'app';app.mkdir(parents=True,exist_ok=True)
    for module in MODULES:shutil.copy2(ROOT/(module+'.py'),app/(module+'.py'))
    (app/'web').mkdir(exist_ok=True)
    for name in WEB:shutil.copy2(ROOT/'web'/name,app/'web'/name)
    for name in ('LICENSE','README.md','speedbench.ico'):shutil.copy2(ROOT/name,app/name)
    shutil.copy2(ROOT/'desktop/README.md',app/'Desktop-README.md')
    (app/'THIRD-PARTY-NOTICES.txt').write_text(notices(ROOT),encoding='utf-8')
    # Refuse accidental/stale unexpected app files; never silently ship them.
    expected=set(MODULES[i]+'.py' for i in range(len(MODULES)))|{'LICENSE','README.md','Desktop-README.md','THIRD-PARTY-NOTICES.txt','speedbench.ico'}|{'web/'+x for x in WEB}
    actual={p.relative_to(app).as_posix() for p in app.rglob('*') if p.is_file()}
    if actual!=expected:raise ValueError('Unexpected resource files; use a fresh build stage')
    files={p.relative_to(STAGE).as_posix():digest(p) for p in sorted(STAGE.rglob('*'))
           if p.is_file() and p.name!='manifest.json'}
    revision=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,timeout=5)
    dirty=subprocess.run(['git','status','--porcelain'],cwd=ROOT,capture_output=True,text=True,timeout=5)
    manifest=dict(schema=1,app_id='com.voodookyo.clash-speedbench',version='1.1.0-alpha.1',
                  runtime_version=lock['version'],target=target,executable='runtime/'+runtime['executable'],
                  files=files,source_revision=revision.stdout.strip() if revision.returncode==0 else None,
                  source_dirty=bool(dirty.stdout))
    (STAGE/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    # Reuse the existing standard-library icon renderer. These are build
    # artifacts, not runtime Python dependencies or additional user assets.
    icon_spec=importlib.util.spec_from_file_location('speedbench_build_icon',ROOT/'scripts/make_icon.py')
    icon_module=importlib.util.module_from_spec(icon_spec);icon_spec.loader.exec_module(icon_module)
    icons=ROOT/'desktop'/'src-tauri'/'generated-icons';icons.mkdir(exist_ok=True)
    (icons/'icon.png').write_bytes(icon_module.render(256))
    if target.startswith('macos-'):
        subprocess.run([sys.executable,str(ROOT/'scripts/make_icon.py'),str(icons/'icon.icns')],check=True)
    print('Prepared fixed runtime/resources:',target,lock['version'],len(files),'files')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--target',default='windows-x86_64')
    prepare(parser.parse_args().target)
