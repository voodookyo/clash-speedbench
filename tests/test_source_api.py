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
