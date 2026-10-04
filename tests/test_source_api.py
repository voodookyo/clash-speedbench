import json
import tempfile
from pathlib import Path
from unittest import mock

import speedbench_web as web
from tests.web_server_case import WebServerCase
from tests.test_source_catalog import SEED, profile, proxy
from speedbench_sources import build_catalog


class SourceApiTest(WebServerCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = mock.patch.object(web, 'HISTORY', Path(self.temp.name) / 'h.jsonl')
        self.history.start()
        self.addCleanup(self.history.stop)
        self.catalog = build_catalog([proxy()], [profile()], seed=SEED, namespace='fixture')

    def test_catalog_endpoint_is_safe_and_local(self):
        with mock.patch.object(web, 'get_catalog', return_value=self.catalog):
            status, body = self.request('GET', '/api/catalog')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['nodes'][0]['source_status'], 'verified')
            self.assertNotIn(b'CANARY-password', body)
            self.assertEqual(self.request('GET', '/api/catalog', headers={'Host':'evil.example'})[0], 403)

    def test_id_switch_validates_fresh_mapping_before_write(self):
        api = mock.Mock()
        api.get.return_value = {'proxies': {'GLOBAL':{'type':'Selector','all':['节点']},
                                          '选择':{'type':'Selector','all':['节点'],'now':'old'},
                                          '节点':{'type':'Shadowsocks'}}}
        with mock.patch.object(web, 'connect_controller', return_value=api), \
                mock.patch.object(web, 'get_catalog', return_value=self.catalog):
            status, body = self.post_authorized('/api/switch', {'node_id': self.catalog['nodes'][0]['node_id']})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['ok'])
            api.select.assert_called_once_with('选择', '节点')

    def test_stale_identity_does_not_fall_back_to_name(self):
        with mock.patch.object(web, 'get_catalog', return_value=self.catalog), \
                mock.patch.object(web, 'connect_controller', return_value=mock.Mock()) as connector:
            status, body = self.post_authorized('/api/switch', {'node_id':'stale', 'name':'节点'})
            self.assertFalse(json.loads(body)['ok'])
            connector.return_value.select.assert_not_called()

    def test_unloaded_subscription_cannot_start_a_run(self):
        with mock.patch.object(web, 'get_catalog', return_value=self.catalog), \
                mock.patch.object(web, 'run_benchmark') as run:
            status, body = self.post_authorized('/api/run', {'subscription_ids':['subscription_v2_' + '0'*32]})
            self.assertEqual(status, 400)
            run.assert_not_called()

    def test_sources_history_empty_is_json(self):
        status, body = self.request('GET', '/api/sources/history')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

    def test_source_series_preserves_opaque_id_and_rejects_host(self):
        sid=self.catalog['sources'][0]['subscription_id']
        with mock.patch.object(web.speedbench_db,'source_series',return_value=[]) as query:
            status,body=self.request('GET','/api/source?subscription_id='+sid+'&days=7')
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body),[])
            query.assert_called_once_with(web.db_path(),sid,days=7)
            self.assertEqual(self.request('GET','/api/source?subscription_id='+sid,headers={'Host':'evil.example'})[0],403)

    def test_slim_history_keeps_identity_and_scopes_without_credentials(self):
        row={'name':'旧名称','node_id':'node_v2_'+'a'*32,'subscription_ids':['subscription_v2_'+'b'*32],
             'subscription_name':'订阅改名','source_status':'verified','network_score':71,
             'measurement_scope':{'bandwidth':'not_selected'},'probe_attempts':3,
             'probe_successes':2,'password':'CANARY-password'}
        with mock.patch.object(web.speedbench_db,'all_runs',return_value=[{'ts':'fixture','results':[row]}]):
            status,body=self.request('GET','/api/history')
            public=json.loads(body)[0]['results'][0]
            self.assertEqual(public['node_id'],row['node_id'])
            self.assertEqual(public['subscription_ids'],row['subscription_ids'])
            self.assertEqual(public['network_score'],71)
            self.assertEqual(public['measurement_scope'],row['measurement_scope'])
            self.assertNotIn(b'CANARY-password',body)


class _Revision:
    def __init__(self, revision=0):
        self.revision = revision

    def snapshot(self):
        return '', self.revision


class SwitchPreviewApiTest(WebServerCase):
    """Fresh-confirmation switch flow at the production HTTP boundary."""

    def node_id(self):
        return self.catalog['nodes'][0]['node_id']

    def proxies(self, group='选择', current='old', extra=None):
        data = {
            'GLOBAL': {'type': 'Selector', 'all': ['节点']},
            group: {'type': 'Selector', 'all': ['节点'], 'now': current},
            '节点': {'type': 'Shadowsocks'},
        }
        if extra:
            data.update(extra)
        return data

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = mock.patch.object(web, 'HISTORY', Path(self.temp.name) / 'h.jsonl')
        self.history.start()
        self.addCleanup(self.history.stop)
        self.catalog = build_catalog([proxy()], [profile()], seed=SEED, namespace='fixture')
        self.set_state(running=False)

    def client(self, api, catalog=None, root=None):
        return [
            mock.patch.object(web, 'connect_controller', return_value=api),
            mock.patch.object(web, 'get_catalog', return_value=catalog or self.catalog),
            mock.patch.object(web, 'CONFIG_ROOT', root or web.CONFIG_ROOT),
        ]

    def preview(self, api, catalog=None, root=None, body=None):
        patches = self.client(api, catalog, root)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.post_authorized('/api/switch/preview', body or {'node_id': self.node_id()})

    def api_for(self, proxies):
        api = mock.Mock()
        api.get.return_value = {'proxies': proxies}
        return api

    def test_preview_returns_whitelisted_fresh_plan_without_writing(self):
        api = self.api_for(self.proxies())
        status, raw = self.preview(api)
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        plan = body['plan']
        self.assertEqual(plan['node_id'], self.node_id())
        self.assertEqual(plan['runtime_name'], '节点')
        self.assertEqual(plan['group'], '选择')
        self.assertEqual(plan['current'], 'old')
        self.assertEqual(plan['source_status'], 'verified')
        self.assertEqual(plan['subscription_name'], '订阅甲')
        self.assertEqual(plan['root_revision'], web.CONFIG_ROOT.snapshot()[1])
        self.assertEqual(set(plan), set(web._SWITCH_PLAN_FIELDS))
        api.select.assert_not_called()
        self.assertNotIn(b'CANARY-password', raw)

    def test_preview_group_matches_do_switch_even_when_get_current_group_is_larger(self):
        api = self.api_for(self.proxies(extra={
            '大组': {'type': 'Selector', 'all': ['a', 'b', 'c', 'd'], 'now': 'a'}}))
        # get_current would advertise 大组, but the switch planner must target the
        # largest group that actually contains the target node.
        status, raw = self.preview(api)
        plan = json.loads(raw)['plan']
        self.assertEqual(plan['group'], '选择')

    def test_name_only_preview_resolves_exactly_one_catalog_node(self):
        api = self.api_for(self.proxies())
        status, raw = self.preview(api, body={'name': '节点'})
        plan = json.loads(raw)['plan']
        self.assertEqual(status, 200)
        self.assertEqual(plan['node_id'], self.node_id())

    def test_ambiguous_name_is_not_guessed(self):
        import copy
        catalog = copy.deepcopy(self.catalog)
        other = dict(catalog['nodes'][0], node_id='node_v2_' + 'b' * 32)
        catalog['nodes'].append(other)
        api = self.api_for(self.proxies())
        status, raw = self.preview(api, catalog=catalog, body={'name': '节点'})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)['ok'])
        api.select.assert_not_called()

    def test_weak_unknown_source_is_labeled_not_guessed(self):
        catalog = {'version': 2, 'status': 'ok', 'sources': [], 'nodes': [
            {'node_id': 'node_v2_' + 'a' * 32, 'runtime_name': '节点', 'identity_strength': 'weak',
             'source_status': 'unknown', 'subscription_ids': [], 'subscription_name': '',
             'subscriptions': []}]}
        api = self.api_for(self.proxies())
        status, raw = self.preview(api, catalog=catalog, body={'node_id': 'node_v2_' + 'a' * 32})
        plan = json.loads(raw)['plan']
        self.assertEqual(status, 200)
        self.assertEqual(plan['identity_strength'], 'weak')
        self.assertEqual(plan['source_status'], 'unknown')
        self.assertEqual(plan['subscription_name'], '')
        self.assertEqual(plan['subscription_ids'], [])

    def confirmed(self, plan, api, catalog=None, root=None):
        patches = self.client(api, catalog, root)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.post_authorized('/api/switch', {'node_id': plan['node_id'], 'confirmation': plan})

    def plan_with(self, api, root=None, catalog=None):
        _, raw = self.preview(api, catalog=catalog, root=root)
        return json.loads(raw)['plan']

    def test_confirmed_switch_revalidates_exact_plan_then_selects(self):
        api = self.api_for(self.proxies())
        plan = self.plan_with(api)
        status, raw = self.confirmed(plan, api)
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        api.select.assert_called_once_with('选择', '节点')

    def test_confirmed_switch_refuses_when_node_identity_is_gone(self):
        api = self.api_for(self.proxies())
        plan = self.plan_with(api)
        empty = {'version': 2, 'status': 'ok', 'sources': [], 'nodes': []}
        status, raw = self.confirmed(plan, api, catalog=empty)
        self.assertFalse(json.loads(raw)['ok'])
        api.select.assert_not_called()

    def test_confirmed_switch_refuses_changed_name(self):
        api = self.api_for(self.proxies())
        plan = self.plan_with(api)
        import copy
        renamed = copy.deepcopy(self.catalog)
        renamed['nodes'][0]['runtime_name'] = '改名'
        proxies = {
            'GLOBAL': {'type': 'Selector', 'all': ['改名']},
            '选择': {'type': 'Selector', 'all': ['改名'], 'now': 'old'},
            '改名': {'type': 'Shadowsocks'},
        }
        api2 = self.api_for(proxies)
        status, raw = self.confirmed(plan, api2, catalog=renamed)
        self.assertFalse(json.loads(raw)['ok'])
        api2.select.assert_not_called()

    def test_confirmed_switch_refuses_changed_group(self):
        api = self.api_for(self.proxies())
        plan = self.plan_with(api)
        moved = {
            'GLOBAL': {'type': 'Selector', 'all': ['节点']},
            '另一组': {'type': 'Selector', 'all': ['节点'], 'now': 'old'},
            '节点': {'type': 'Shadowsocks'},
        }
        api2 = self.api_for(moved)
        status, raw = self.confirmed(plan, api2)
        self.assertFalse(json.loads(raw)['ok'])
        api2.select.assert_not_called()

    def test_confirmed_switch_refuses_changed_current_selection(self):
        api = self.api_for(self.proxies())
        plan = self.plan_with(api)
        api2 = self.api_for(self.proxies(current='other'))
        status, raw = self.confirmed(plan, api2)
        self.assertFalse(json.loads(raw)['ok'])
        api2.select.assert_not_called()

    def test_confirmed_switch_refuses_changed_root_revision(self):
        root = _Revision(0)
        api = self.api_for(self.proxies())
        plan = self.plan_with(api, root=root)
        root.revision = 1
        status, raw = self.confirmed(plan, api, root=root)
        self.assertFalse(json.loads(raw)['ok'])
        api.select.assert_not_called()

    def test_root_change_during_preview_returns_no_confirmation(self):
        api=self.api_for(self.proxies())
        root=mock.Mock();root.snapshot.side_effect=[('',0),('',1)]
        status,raw=self.preview(api,root=root)
        self.assertEqual(status,200);self.assertFalse(json.loads(raw)['ok'])
        self.assertNotIn('plan',json.loads(raw));api.select.assert_not_called()

    def test_long_unicode_names_survive_preview_and_confirmation(self):
        import copy
        catalog=copy.deepcopy(self.catalog);name='长节点🌏'*100;group='长策略组🌏'*100
        node=catalog['nodes'][0];node['runtime_name']=name
        node['subscription_name']='长订阅🌏'*100;node['subscriptions'][0]['name']=node['subscription_name']
        api=self.api_for({'GLOBAL':{'type':'Selector','all':[group]},
                         group:{'type':'Selector','all':[name],'now':'old'},name:{'type':'Shadowsocks'}})
        plan=self.plan_with(api,catalog=catalog)
        status,raw=self.confirmed(plan,api,catalog=catalog)
        self.assertEqual(status,200);self.assertTrue(json.loads(raw)['ok'])
        api.select.assert_called_once_with(group,name)

    def test_confirmed_payload_cannot_disagree_with_preview_identity(self):
        api=self.api_for(self.proxies());plan=self.plan_with(api)
        status,raw=self.post_authorized('/api/switch',{'node_id':'node_v2_'+'f'*32,'confirmation':plan})
        self.assertEqual(status,400);api.select.assert_not_called()

    def test_confirmed_switch_refuses_changed_source_attribution(self):
        api = self.api_for(self.proxies())
        plan = self.plan_with(api)
        import copy
        changed = copy.deepcopy(self.catalog)
        changed['nodes'][0]['subscription_name'] = '订阅乙'
        status, raw = self.confirmed(plan, api, catalog=changed)
        self.assertFalse(json.loads(raw)['ok'])
        api.select.assert_not_called()

    def test_malformed_confirmation_and_csrf_are_rejected_without_writing(self):
        api = self.api_for(self.proxies())
        patches = self.client(api)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for confirmation in ({'bogus': 1}, [], 'x', {'node_id': 'node_v2_' + 'a' * 32}):
            status, raw = self.post_authorized('/api/switch', {'node_id': self.node_id(),
                                                               'confirmation': confirmation})
            self.assertEqual(status, 400)
        status, _ = self.post_json('/api/switch/preview', {'node_id': self.node_id()})
        self.assertEqual(status, 403)
        status, _ = self.post_authorized('/api/switch/preview', {'node_id': self.node_id()},
                                         headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        api.select.assert_not_called()

    def test_preview_missing_identifier_is_rejected(self):
        status, _ = self.post_authorized('/api/switch/preview', {})
        self.assertEqual(status, 400)
        status, _ = self.post_authorized('/api/switch', {})
        self.assertEqual(status, 400)

    def test_legacy_switch_without_confirmation_is_unchanged(self):
        api = self.api_for(self.proxies())
        patches = self.client(api)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        status, raw = self.post_authorized('/api/switch', {'name': '节点'})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)['ok'])
        api.select.assert_called_once_with('选择', '节点')
