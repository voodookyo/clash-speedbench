import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import speedbench_config as config
import speedbench_controller as controller
import clash_speedbench as core
import speedbench_web as web
from tests.web_server_case import WebServerCase
from tests.test_source_catalog import proxy


def layout(folder, port=19997):
    root=Path(folder)/'Verge 配置 空格 🧪';root.mkdir()
    (root/'profiles').mkdir()
    (root/'clash-verge.yaml').write_text(json.dumps({'external-controller':f'127.0.0.1:{port}',
        'secret':'CANARY-controller-key','proxies':[proxy()]}),encoding='utf-8')
    (root/'profiles.yaml').write_text(json.dumps({'items':[{'uid':'fixture-source','name':'fixture 订阅',
        'type':'remote','file':'fixture.yaml'}]}),encoding='utf-8')
    (root/'profiles/fixture.yaml').write_text(json.dumps({'proxies':[proxy()]}),encoding='utf-8')
    return root.resolve()


class ConfigRootTest(unittest.TestCase):
    def test_absolute_local_layout_and_memory_snapshot_do_not_write_any_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root=layout(folder)
            before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(config.validate_root(str(root)),str(root.resolve()))
            choice=config.RootChoice();choice.apply(str(root))
            self.assertEqual(choice.snapshot(),(str(root),1))
            self.assertEqual(choice.public()['mode'],'custom')
            choice.apply('');self.assertEqual(choice.snapshot(),('',2))
            self.assertEqual(before,{str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_unsafe_relative_remote_file_and_missing_layout_fail_without_echo(self):
        with tempfile.TemporaryDirectory() as folder:
            root=layout(folder)
            for value in (None,False,[],{},'.','../CANARY','\\\\remote\\CANARY','//remote/CANARY',
                          'https://evil/CANARY',str(root/'clash-verge.yaml'),str(root/'missing'),
                          str(root)+'\nCANARY','x'*2049,'\ud800'):
                with self.subTest(value=value),self.assertRaises(config.ConfigRootError) as error:
                    config.validate_root(value)
                self.assertNotIn('CANARY',str(error.exception))
            (root/'profiles.yaml').unlink()
            with self.assertRaises(config.ConfigRootError):config.validate_root(str(root))

    def test_invalid_initial_env_never_becomes_auto_and_never_returns_bad_value(self):
        choice=config.RootChoice('CANARY-invalid')
        self.assertEqual(choice.public()['mode'],'invalid')
        self.assertNotIn('CANARY',json.dumps(choice.public()))
        self.assertEqual(choice.snapshot()[0],'CANARY-invalid')

    def test_symlinked_fixed_runtime_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=layout(folder);runtime=root/'clash-verge.yaml';outside=Path(folder)/'outside'
            outside.write_bytes(runtime.read_bytes());runtime.unlink()
            try:runtime.symlink_to(outside)
            except (OSError,NotImplementedError):self.skipTest('Symlink creation unavailable')
            with self.assertRaises(config.ConfigRootError):config.validate_root(str(root))

    def test_controller_and_worker_resolve_the_same_custom_root_and_never_default_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root=layout(folder)
            # Controller parser consumes YAML scalars, not a full JSON config.
            (root/'clash-verge.yaml').write_text('external-controller: 127.0.0.1:19997\nsecret: CANARY-controller-key\nproxies: []\n',encoding='utf-8')
            with mock.patch.dict(os.environ,{config.ENV:str(root)},clear=True):
                self.assertEqual(controller.config_paths(),[root/'clash-verge.yaml'])
                import speedbench_workers as workers
                self.assertEqual(workers.find_config_file(),str(root/'clash-verge.yaml'))
                api=mock.Mock();api.get.side_effect=core.ApiError('fixture unavailable')
                with mock.patch.object(core,'MihomoAPI',return_value=api) as create, self.assertRaises(core.ApiError):
                    core.connect_controller()
                self.assertEqual({c.args[0] for c in create.call_args_list},{'http://127.0.0.1:19997'})
                self.assertEqual(create.call_args.kwargs['secret'],'CANARY-controller-key')
            with mock.patch.dict(os.environ,{config.ENV:str(root)}):
                self.assertNotEqual(controller.config_paths(config_root=''),[root/'clash-verge.yaml'])

    def test_removed_custom_layout_never_contacts_controller_or_default_worker_config(self):
        import speedbench_workers as workers
        with tempfile.TemporaryDirectory() as folder:
            root=layout(folder);(root/'profiles.yaml').unlink()
            with mock.patch.dict(os.environ,{config.ENV:str(root)},clear=True):
                with mock.patch.object(core,'MihomoAPI') as api,self.assertRaises(core.ApiError):core.connect_controller()
                api.assert_not_called()
                with mock.patch.object(workers.os.path,'isfile') as candidate,self.assertRaises(workers.WorkerUnavailable):workers.find_config_file()
                candidate.assert_not_called()

    def test_cli_invalid_root_or_conflicting_config_fails_before_network_or_benchmark(self):
        with tempfile.TemporaryDirectory() as folder:
            root=layout(folder)
            for selected,extra in (('CANARY-invalid',[]),(str(root),['--config-file',str(root/'profiles.yaml')])):
                with mock.patch.dict(os.environ,{config.ENV:selected}),mock.patch('sys.argv',['clash_speedbench.py','--yes']+extra), \
                     mock.patch.object(core,'connect_controller') as connect,mock.patch('sys.stderr') as error:
                    self.assertEqual(core.main(),2);connect.assert_not_called()
                    self.assertNotIn('CANARY',str(error.write.call_args_list))


class ConfigRootApiTest(WebServerCase):
    def setUp(self):
        super().setUp();self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=layout(self.temp.name)
        self.choice=mock.patch.object(web,'CONFIG_ROOT',config.RootChoice());self.choice.start();self.addCleanup(self.choice.stop)
        self.set_state(running=False)

    def test_setting_auth_origin_strict_body_preview_and_explicit_apply(self):
        self.assertEqual(self.request('GET','/api/config-root')[0],403)
        self.assertEqual(self.post_authorized('/api/config-root',{'root':str(self.root)},headers={'Origin':'https://evil.example'})[0],403)
        for body in ({'root':str(self.root),'key':'CANARY'},[],{}, {'root':'../CANARY'}):
            code,raw=self.post_authorized('/api/config-root',body)
            self.assertEqual(code,400);self.assertNotIn(b'CANARY',raw)
        code,raw=self.post_authorized('/api/config-root/preview',{'root':str(self.root)})
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['path'],str(self.root));self.assertNotIn(b'CANARY',raw)
        self.assertEqual(web.CONFIG_ROOT.snapshot(),('',0))
        code,raw=self.post_authorized('/api/config-root',{'root':str(self.root)})
        self.assertEqual(code,200);self.assertEqual(web.CONFIG_ROOT.snapshot(),(str(self.root),1))
        self.assertEqual(self.post_authorized('/api/config-root',{'root':''})[0],200)
        self.assertEqual(web.CONFIG_ROOT.snapshot(),('',2))
        self.assertFalse((self.root/'ui-preferences.json').exists())

    def test_active_and_cancelling_task_cannot_change_root(self):
        self.set_state(running=True,cancel_requested=True)
        code,raw=self.post_authorized('/api/config-root',{'root':str(self.root)})
        self.assertEqual(code,409);self.assertEqual(web.CONFIG_ROOT.snapshot(),('',0))

    def test_unconfirmed_cleanup_blocks_root_changes_even_when_child_has_exited(self):
        self.set_state(running=False,cleanup_incomplete=True)
        self.assertEqual(self.post_authorized('/api/config-root',{'root':str(self.root)})[0],409)
        self.assertEqual(web.CONFIG_ROOT.snapshot(),('',0))

    def test_catalog_uses_selected_runtime_but_public_response_never_contains_path_or_config(self):
        web.CONFIG_ROOT.apply(str(self.root))
        api=mock.Mock(controller_base='http://127.0.0.1:19997')
        api.get.side_effect=lambda path:{'proxies':{'节点':{}}} if path=='/proxies' else {'providers':{}}
        with mock.patch.object(web,'connect_controller',return_value=api),mock.patch.object(web,'HISTORY',Path(self.temp.name)/'h.jsonl'):
            code,raw=self.request('GET','/api/catalog')
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['nodes'][0]['subscription_name'],'fixture 订阅')
        self.assertNotIn(str(self.root).encode(),raw);self.assertNotIn(b'CANARY',raw)

    def test_root_revision_change_during_selection_refuses_job(self):
        def check(params):web.CONFIG_ROOT.apply(str(self.root));return True
        with mock.patch.object(web,'_check_source_selection',side_effect=check),mock.patch.object(web,'run_benchmark') as run:
            code,raw=self.post_authorized('/api/jobs',{'mode':'quick'})
        self.assertEqual(code,409);run.assert_not_called();self.assertFalse(web.STATE['running'])

    def test_child_receives_private_snapshot_not_argv_or_persisted_job_config(self):
        web.CONFIG_ROOT.apply(str(self.root));proc=mock.Mock(stdout=iter(()));proc.wait.return_value=0
        with mock.patch.object(web,'connect_controller'),mock.patch.object(web,'sync_db'),mock.patch.object(web.subprocess,'Popen',return_value=proc) as spawn:
            web.run_benchmark({'_config_root':str(self.root)})
        self.assertEqual(spawn.call_args.kwargs['env'][config.ENV],str(self.root))
        self.assertNotIn(str(self.root),' '.join(spawn.call_args.args[0]))
        self.assertNotIn(str(self.root),' '.join(web.STATE['lines']))

    def test_accepted_job_freezes_root_privately_and_public_history_config_has_no_path(self):
        web.CONFIG_ROOT.apply(str(self.root))
        with mock.patch.object(web,'run_benchmark') as dispatch:
            code,raw=self.post_authorized('/api/jobs',{'mode':'quick'})
        self.assertEqual(code,202)
        self.assertEqual(dispatch.call_args.args[0]['_config_root'],str(self.root))
        job=web.JOBS.snapshot(json.loads(raw)['job_id'])
        self.assertNotIn(str(self.root),json.dumps(job));self.assertNotIn('_config_root',json.dumps(job))
        database=Path(self.temp.name)/'tasks.db';web.speedbench_db.save_task(database,job)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(database)) as db:
            dump='\n'.join(db.iterdump())
        self.assertNotIn(str(self.root),dump);self.assertNotIn('CANARY',dump)

    def test_runtime_path_is_redacted_in_log_spellings(self):
        web.CONFIG_ROOT.apply(str(self.root))
        for value in (str(self.root),str(self.root).replace('\\','/'),json.dumps(str(self.root),ensure_ascii=False)[1:-1]):
            self.assertNotIn(value,web._redact_runtime_text('fixture '+value))

    def test_root_change_during_switch_does_not_write_to_old_controller(self):
        api=mock.Mock();api.get.return_value={'proxies':{'GROUP':{'type':'Selector','all':['node'],'now':'old'},'node':{'type':'SS'}}}
        def resolve(*_):web.CONFIG_ROOT.apply(str(self.root));return 'GROUP'
        with mock.patch.object(web,'connect_controller',return_value=api),mock.patch.object(web,'pick_switch_group',side_effect=resolve):
            result=web.do_switch('node')
        self.assertFalse(result['ok']);api.select.assert_not_called()
