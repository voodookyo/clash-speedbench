"""Copy explicit native outputs and write provenance/checksums (not publish)."""
import argparse
import json
from pathlib import Path
import shutil

try:from .prepare_resources import ROOT, STAGE, digest
except ImportError:from prepare_resources import ROOT, STAGE, digest
try:from .package_windows import package_path, build_id
except ImportError:from package_windows import package_path, build_id


def collect(target):
    manifest=json.loads((STAGE/'manifest.json').read_text(encoding='utf-8'))
    if target!=manifest['target']:raise ValueError('Build/collection target mismatch')
    bundle=ROOT/'desktop/src-tauri/target/release/bundle'
    patterns={'windows-x86_64':('nsis/*-setup.exe',),
              'macos-x86_64':('dmg/*.dmg',),'macos-aarch64':('dmg/*.dmg',),
              'linux-x86_64':('deb/*.deb',),'linux-aarch64':('deb/*.deb',)}
    inputs=[p for pattern in patterns[target] for p in bundle.glob(pattern) if p.is_file()]
    if target=='windows-x86_64':
        portable=package_path(manifest)
        if not portable.is_file():raise ValueError('Current-source portable package is missing')
        inputs.append(portable)
    if not inputs:raise ValueError('No native packages found; compilation alone is not packaging')
    output=ROOT/'dist/desktop-artifacts'/build_id(manifest);output.mkdir(parents=True,exist_ok=True)
    records=[]
    for source in sorted(inputs):
        destination=output/source.name
        if destination.exists() and digest(destination)!=digest(source):raise ValueError('Refusing to overwrite different artifact')
        if not destination.exists():shutil.copy2(source,destination)
        checksum=digest(destination)
        (output/(destination.name+'.sha256')).write_text(checksum+'  '+destination.name+'\n',encoding='utf-8')
        records.append({'file':destination.name,'sha256':checksum,'bytes':destination.stat().st_size})
    provenance={k:manifest[k] for k in ('app_id','version','runtime_version','target','source_revision','source_dirty')}
    provenance.update(signing='unsigned',automatic_updates=False,packages=records)
    (output/'build-provenance.json').write_text(json.dumps(provenance,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print('Collected checksummed unsigned packages:',len(records))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--target',required=True)
    collect(parser.parse_args().target)
