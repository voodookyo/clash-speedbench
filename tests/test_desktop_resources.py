import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from desktop import prepare_resources as resources
from desktop.package_windows import validate_stage, package_path, build_id
from desktop.license_notices import notices


class DesktopResourcesTest(unittest.TestCase):
    def test_artifact_namespaces_do_not_mix_or_overwrite_different_source_builds(self):
        first={'source_revision':'a'*40,'version':'1.1.0-alpha.1'}
        second={**first,'source_revision':'b'*40}
        self.assertNotEqual(package_path(first),package_path(second))
        self.assertEqual(package_path(first).name,package_path(second).name)
        self.assertEqual(build_id({'source_revision':'../private'}),'unversioned')
    def test_locked_license_notices_include_public_licenses_not_private_sibling_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);package=root/'crate';package.mkdir()
            (package/'LICENSE-MIT').write_text('PUBLIC-LICENSE',encoding='utf-8')
            (root/'private-key').write_text('CANARY',encoding='utf-8')
            item={'name':'fixture','version':'1.0.0','source':'registry+fixture','manifest_path':str(package/'Cargo.toml'),'license':'MIT','license_file':None}
            response=mock.Mock(returncode=0,stdout=json.dumps({'packages':[item]}))
            with mock.patch('desktop.license_notices.subprocess.run',return_value=response) as run:
                text=notices(root)
            self.assertIn('PUBLIC-LICENSE',text);self.assertNotIn('CANARY',text)
            self.assertIn('--locked',run.call_args.args[0]);self.assertIn('--offline',run.call_args.args[0])
            item['license_file']='../private-key';response.stdout=json.dumps({'packages':[item]})
            with mock.patch('desktop.license_notices.subprocess.run',return_value=response),self.assertRaises(ValueError):notices(root)
    def test_installer_running_app_policy_does_not_force_shutdown_or_kill(self):
        hook=(resources.ROOT/'desktop/src-tauri/installer-hooks.nsh').read_text(encoding='utf-8')
        config=json.loads((resources.ROOT/'desktop/src-tauri/tauri.conf.json').read_text(encoding='utf-8'))
        self.assertEqual(config['bundle']['windows']['nsis']['installerHooks'],'installer-hooks.nsh')
        self.assertIn('!macroundef CheckIfAppIsRunning',hook)
        self.assertIn('RmGetList',hook)
        self.assertNotIn('RmShutdown',hook)
        self.assertNotIn('TerminateProcess',hook)
        self.assertIn('SetErrorLevel 2',hook)
    def test_portable_stage_rejects_history_and_wrong_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'app').mkdir();source=root/'app/module.py';source.write_bytes(b'fixture')
            manifest={'target':'windows-x86_64','app_id':'com.voodookyo.clash-speedbench',
                      'files':{'app/module.py':resources.digest(source)}}
            (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            self.assertEqual(validate_stage(root)['target'],'windows-x86_64')
            (root/'history.jsonl').write_bytes(b'CANARY-private')
            with self.assertRaises(ValueError):validate_stage(root)
            (root/'history.jsonl').unlink() # Owned fixture only.
            manifest['target']='macos-aarch64';(root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            with self.assertRaises(ValueError):validate_stage(root)
    def test_public_bundle_allowlist_excludes_private_and_fixture_material(self):
        self.assertEqual(len(resources.MODULES),len(set(resources.MODULES)))
        self.assertIn('speedbench_pipe', resources.MODULES)
        for module in resources.MODULES:
            self.assertTrue((resources.ROOT/(module+'.py')).is_file())
            self.assertNotIn('test',module)
        self.assertEqual(set(resources.WEB),{'index.html','app.js','tasks.js','view.js','preferences.js','releases.js','config-root.js','style.css'})
        lock=json.loads((resources.ROOT/'desktop/runtime-lock.json').read_text(encoding='utf-8'))
        for target in ('windows-x86_64','macos-aarch64','macos-x86_64','linux-x86_64','linux-aarch64'):
            self.assertTrue(lock[target]['url'].startswith('https://'))
            self.assertRegex(lock[target]['sha256'],r'^[0-9a-f]{64}$')
            self.assertNotIn('latest',lock[target]['url'])

    def test_archive_paths_reject_drive_traversal_nul_and_excessive_depth(self):
        for path in ('','.', '../key','/secret','C:/key','a\\b','a/../b','a\x00b','a/'*33+'b'):
            with self.subTest(path=path),self.assertRaises(ValueError):resources.safe_archive_path(path)
        self.assertEqual(str(resources.safe_archive_path('python/bin/python3.14')),'python/bin/python3.14')

    def tar_fixture(self,folder,members):
        archive=folder/'fixture.tar.gz'
        with tarfile.open(archive,'w:gz') as bundle:
            for name,content,link in members:
                item=tarfile.TarInfo(name)
                if link is not None:item.type=tarfile.SYMTYPE;item.linkname=link;bundle.addfile(item)
                else:item.size=len(content);item.mode=0o755;bundle.addfile(item,io.BytesIO(content))
        return archive

    def test_tar_materializes_internal_links_without_pip_or_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);dest=root/'runtime';dest.mkdir()
            archive=self.tar_fixture(root,[('python/bin/python3.14',b'fixture',None),
                ('python/bin/python3',b'','python3.14'),('python/bin/python',b'','python3'),
                ('python/lib/site-packages/private.py',b'CANARY',None)])
            resources.extract_tar(archive,dest)
            self.assertEqual((dest/'python/bin/python').read_bytes(),b'fixture')
            self.assertFalse((dest/'python/bin/python').is_symlink())
            self.assertFalse((dest/'python/lib/site-packages').exists())

    def test_tar_rejects_external_links_cycle_and_duplicate(self):
        for members in ([('a',b'','../secret')],[('a',b'','C:/secret')],
                        [('a',b'','b'),('b',b'','a')],[('a',b'1',None),('a',b'2',None)]):
            with self.subTest(members=members),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);dest=root/'runtime';dest.mkdir()
                with self.assertRaises(ValueError):resources.extract_tar(self.tar_fixture(root,members),dest)

    def test_zip_rejects_traversal_and_links(self):
        for filename,mode in (('../secret',0),('symlink',0o120777 << 16)):
            with self.subTest(filename=filename),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);archive=root/'fixture.zip'
                with zipfile.ZipFile(archive,'w') as bundle:
                    member=zipfile.ZipInfo(filename);member.external_attr=mode
                    bundle.writestr(member,b'CANARY')
                with self.assertRaises(ValueError):resources.extract_zip(archive,root/'runtime')
