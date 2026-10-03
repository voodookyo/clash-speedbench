"""Build-only portable package. Never copy an entire working tree/data home."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

try:from .prepare_resources import ROOT, STAGE, digest, safe_archive_path
except ImportError:from prepare_resources import ROOT, STAGE, digest, safe_archive_path


def validate_stage(stage):
    manifest=json.loads((stage/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('target')!='windows-x86_64' or manifest.get('app_id')!='com.voodookyo.clash-speedbench':
        raise ValueError('Incorrect portable runtime/platform identity')
    expected=set(manifest['files'])
    actual={p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file() and p.name!='manifest.json'}
    if actual!=expected:raise ValueError('Unexpected or missing staged resource')
    for name,checksum in manifest['files'].items():
        safe_archive_path(name)
        path=stage/name
        if path.is_symlink() or digest(path)!=checksum:raise ValueError('Staged resource integrity failed')
    return manifest


def package():
    manifest=validate_stage(STAGE)
    exe=ROOT/'desktop/src-tauri/target/release/clash-speedbench-desktop.exe'
    if not exe.is_file():raise ValueError('Build the native release executable first')
    output=ROOT/'dist/desktop/windows-x86_64';output.mkdir(parents=True,exist_ok=True)
    name='Clash-SpeedBench-'+manifest['version']+'-windows-x86_64-portable.zip'
    archive=output/name
    # Refuse to overwrite a previous verified package, including its checksum.
    if archive.exists() or archive.with_suffix('.zip.sha256').exists():
        raise ValueError('Package already exists; retain it and use a fresh build output workspace')
    with tempfile.TemporaryDirectory(prefix='speedbench-package-',dir=output) as folder:
        root=Path(folder)/'Clash-SpeedBench';root.mkdir()
        shutil.copy2(exe,root/exe.name)
        shutil.copytree(STAGE,root,dirs_exist_ok=True)
        # The native executable validates its compile-time manifest, not just
        # the mutable JSON beside it. No backend/WebView/history is started.
        subprocess.run([str(root/exe.name),'--verify-package'],cwd=root,check=True,timeout=30)
        with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
            for path in sorted(root.rglob('*')):
                if path.is_file():bundle.write(path,path.relative_to(root.parent).as_posix())
    archive.with_suffix('.zip.sha256').write_text(digest(archive)+'  '+name+'\n',encoding='utf-8')
    print('Created unsigned portable package:',archive.name)


if __name__=='__main__':package()
