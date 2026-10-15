"""Build-only notices from the exact locked Cargo registry sources."""
import json
from pathlib import Path
import subprocess


def notices(root):
    result=subprocess.run(['cargo','metadata','--locked','--offline','--format-version','1',
                           '--manifest-path',str(root/'desktop/src-tauri/Cargo.toml')],
                          cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=30)
    if result.returncode:raise ValueError('Fetch locked Cargo sources before staging license notices')
    packages=json.loads(result.stdout)['packages']
    parts=['Clash SpeedBench desktop third-party notices\n',
           'Includes locked Rust dependencies (including build tooling). Python runtime licenses are bundled in runtime/.\n']
    total=0
    for package in sorted(packages,key=lambda p:(p['name'],p['version'])):
        if not package.get('source'):continue # Project MIT license already bundled.
        directory=Path(package['manifest_path']).parent
        parts.append('\n=== '+package['name']+' '+package['version']+' ===\n')
        parts.append('SPDX: '+str(package.get('license') or 'See upstream license files')+'\n')
        parts.append('https://crates.io/crates/'+package['name']+'/'+package['version']+'\n')
        files={p for pattern in ('LICENSE*','LICENCE*','COPYING*','NOTICE*') for p in directory.glob(pattern) if p.is_file()}
        named=package.get('license_file')
        if named:
            path=(directory/named).resolve()
            if not path.is_relative_to(directory.resolve()):raise ValueError('Unexpected dependency license path')
            if path.is_file():files.add(path)
        for path in sorted(files):
            if not path.resolve().is_relative_to(directory.resolve()):raise ValueError('Dependency notice escaped its package')
            size=path.stat().st_size;total+=size
            if size>2_000_000 or total>20_000_000:raise ValueError('Dependency notices exceed bounds')
            parts.append('--- '+path.name+' ---\n'+path.read_text(encoding='utf-8',errors='replace')+'\n')
    return ''.join(parts)
