"""Origin fixtures never contain real subscription URLs or node credentials."""
import json
import tempfile
import unittest
from pathlib import Path

from speedbench_sources import build_catalog, canonical_connection, read_catalog, SourceError


SEED = b'speedbench-test-seed-never-production'


def proxy(name='节点', password='CANARY-password', **extra):
    return dict(name=name, type='ss', server='test.example', port=443,
                cipher='aes-128-gcm', password=password, **extra)


def profile(uid='uid-a', name='订阅甲', nodes=None):
    return {'uid': uid, 'name': name, 'type': 'remote',
            'proxies': nodes if nodes is not None else [proxy()]}


class SourceMappingTest(unittest.TestCase):
    def catalog(self, runtime=None, profiles=None, providers=None):
        return build_catalog(runtime or [proxy()], profiles or [profile()],
                             seed=SEED, namespace='fixture', providers=providers)

    def test_empty_provider_exact_definition_recovers_subscription(self):
        cat = self.catalog()
        origin = cat['nodes'][0]
        self.assertEqual(origin['source_status'], 'verified')
        self.assertEqual(origin['subscription_name'], '订阅甲')
        self.assertTrue(cat['sources'][0]['loaded'])

    def test_rename_does_not_change_identity(self):
        a = self.catalog()['nodes'][0]
        b = self.catalog([proxy('新节点名')], [profile(name='新订阅名')])['nodes'][0]
        self.assertEqual(a['node_id'], b['node_id'])
        self.assertEqual(a['subscription_ids'], b['subscription_ids'])

    def test_same_name_same_endpoint_different_password_is_isolated(self):
        profiles = [profile(), profile('uid-b', '订阅乙', [proxy(password='other')])]
        a = self.catalog(profiles=profiles)['nodes'][0]
        b = self.catalog([proxy(password='other')], profiles)['nodes'][0]
        self.assertNotEqual(a['node_id'], b['node_id'])
        self.assertEqual(b['subscription_name'], '订阅乙')

    def test_same_definition_multiple_subscriptions_is_ambiguous(self):
        cat = self.catalog(profiles=[profile(), profile('uid-b', '订阅乙')])
        self.assertEqual(cat['nodes'][0]['source_status'], 'ambiguous')
        self.assertEqual(len(cat['nodes'][0]['subscription_ids']), 2)
        self.assertFalse(cat['nodes'][0]['subscription_name'])

    def test_provider_membership_or_current_profile_is_not_proof(self):
        cat = self.catalog([proxy(password='changed-by-script')], providers={
            '订阅甲': {'proxies': [{'name': '节点'}]}})
        self.assertEqual(cat['nodes'][0]['source_status'], 'unknown')
        self.assertFalse(cat['sources'][0]['loaded'])
        self.assertEqual(cat['nodes'][0]['provider_names'], ['订阅甲'])

    def test_unloaded_and_unavailable_are_distinct(self):
        cat = self.catalog(profiles=[profile(), profile('uid-b', '乙', [proxy(password='other')]),
                                    dict(uid='uid-c', name='丙', type='remote', available=False)])
        self.assertFalse(cat['sources'][1]['loaded'])
        self.assertTrue(cat['sources'][1]['available'])
        self.assertFalse(cat['sources'][2]['available'])

    def test_public_catalog_does_not_expose_connection_or_seed(self):
        serialized = json.dumps(self.catalog(), ensure_ascii=False)
        for secret in ('CANARY-password', 'test.example', 'aes-128-gcm', SEED.decode()):
            self.assertNotIn(secret, serialized)

    def test_result_origin_does_not_copy_nested_connection_secrets(self):
        from speedbench_sources import result_origin
        origin = self.catalog()['nodes'][0]
        origin['subscriptions'][0]['password'] = 'CANARY-password'
        origin['subscriptions'][0]['connection'] = proxy()
        self.assertNotIn('CANARY-password', json.dumps(result_origin(origin)))

    def test_revalidate_changed_worker_credentials_drops_old_origin(self):
        from speedbench_sources import revalidate_origins
        origin = self.catalog()['nodes'][0]
        self.assertEqual(revalidate_origins({'节点':origin}, [proxy(password='rotated')],
                                          seed=SEED, namespace='fixture'), {})

    def test_revalidate_unchanged_worker_definition_preserves_identity(self):
        from speedbench_sources import revalidate_origins
        origin = self.catalog()['nodes'][0]
        checked = revalidate_origins({'节点':origin}, [proxy()], seed=SEED, namespace='fixture')
        self.assertEqual(checked['节点']['node_id'],origin['node_id'])

    def test_connection_normalizes_port_and_server_without_mutating_input(self):
        p = proxy()
        b = dict(p, name='other', port='443', server='TEST.EXAMPLE')
        self.assertEqual(canonical_connection(p), canonical_connection(b))
        self.assertEqual(p['name'], '节点')
        self.assertNotEqual(canonical_connection(p), canonical_connection(dict(p, **{'dialer-proxy': '前置'})))
        self.assertNotEqual(canonical_connection(p), canonical_connection(dict(p, **{'interface-name': 'eth2'})))

    def test_weak_runtime_identity_does_not_claim_profile(self):
        cat = self.catalog([{'name': '节点', 'type': 'ss'}])
        self.assertEqual(cat['nodes'][0]['source_status'], 'unknown')
        self.assertEqual(cat['nodes'][0]['identity_strength'], 'weak')

    def test_chain_credentials_change_is_not_ignored(self):
        child = proxy('child', **{'dialer-proxy':'front'})
        original = proxy('front',password='original-front')
        changed = proxy('front',password='changed-front')
        cat = self.catalog([child,changed],[profile(nodes=[child,original])])
        self.assertTrue(all(n['source_status']=='unknown' for n in cat['nodes']))

    def test_chain_dependency_rename_keeps_effective_identity(self):
        front = proxy('front')
        child = proxy('child',**{'dialer-proxy':'front'})
        a = self.catalog([front,child],[profile(nodes=[front,child])])
        renamed = proxy('renamed-front')
        updated = proxy('renamed-child',**{'dialer-proxy':'renamed-front'})
        b = self.catalog([renamed,updated],[profile(nodes=[renamed,updated])])
        self.assertEqual(a['nodes'][1]['node_id'],b['nodes'][1]['node_id'])

    def test_cycle_and_missing_chain_are_safe_unknown(self):
        child = proxy('child',**{'dialer-proxy':'child'})
        cat = self.catalog([child],[profile(nodes=[child])])
        self.assertEqual(cat['nodes'][0]['source_status'],'unknown')


class CatalogFileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'profiles').mkdir()

    def write(self, name, content):
        # Fixture creation only: all writes remain inside TemporaryDirectory.
        (self.root / name).write_text(content, encoding='utf-8')

    def test_read_machine_yaml_catalog(self):
        self.write('profiles.yaml', 'current: a\nitems:\n  - uid: a\n    name: 机场甲\n    type: remote\n    file: a.yaml\n')
        self.write('profiles/a.yaml', json.dumps({'proxies': [proxy()]}))
        self.write('clash-verge.yaml', json.dumps({'proxies': [proxy('改名节点')]}))
        cat = read_catalog(self.root, seed=SEED)
        self.assertEqual(cat['nodes'][0]['subscription_name'], '机场甲')

    def test_flow_mapping_sequence_in_real_subscription_style(self):
        self.write('profiles.yaml', 'items:\n  - {uid: a, name: 机场甲, type: remote, file: a.yaml}\n')
        self.write('profiles/a.yaml', "proxies:\n  - {name: '节点', type: ss, server: test.example, port: 443, cipher: aes-128-gcm, password: 'CANARY-password'}\n")
        self.write('clash-verge.yaml', json.dumps({'proxies': [proxy()]}))
        cat = read_catalog(self.root, seed=SEED)
        self.assertEqual(cat['nodes'][0]['subscription_name'], '机场甲')

    def test_path_escape_is_rejected_without_echoing_sensitive_input(self):
        self.write('profiles.yaml', json.dumps({'items': [dict(uid='a', name='甲', type='remote', file='../CANARY-secret.yaml')]}))
        self.write('clash-verge.yaml', json.dumps({'proxies': [proxy()]}))
        cat = read_catalog(self.root, seed=SEED)
        self.assertFalse(cat['sources'][0]['available'])
        self.assertNotIn('CANARY-secret', json.dumps(cat))

    def test_unsupported_yaml_is_safe_unknown_not_executed(self):
        self.write('profiles.yaml', '!!python/object/apply:os.system [CANARY-secret]\n')
        self.write('clash-verge.yaml', json.dumps({'proxies': [proxy()]}))
        cat = read_catalog(self.root, seed=SEED)
        self.assertEqual(cat['status'], 'metadata_unavailable')
        self.assertNotIn('CANARY-secret', json.dumps(cat))

    def test_size_and_depth_budgets(self):
        with self.assertRaises(SourceError):
            canonical_connection({'type': 'ss', 'server': 'x', 'port': 1,
                                  'opts': [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]})

    def test_symlink_escape_is_rejected(self):
        outside = self.root / 'outside.yaml'
        outside.write_text(json.dumps({'proxies':[proxy()]}), encoding='utf-8')
        try:
            (self.root / 'profiles' / 'linked.yaml').symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable on this Windows account')
        self.write('profiles.yaml', json.dumps({'items':[dict(uid='a',name='甲',type='remote',file='linked.yaml')]}))
        self.write('clash-verge.yaml', json.dumps({'proxies':[proxy()]}))
        self.assertFalse(read_catalog(self.root,seed=SEED)['sources'][0]['available'])

    def test_file_byte_budget(self):
        from unittest import mock
        self.write('profiles.yaml', 'items: []\n' + '#' * 1024)
        self.write('clash-verge.yaml', json.dumps({'proxies':[proxy()]}))
        with mock.patch('speedbench_sources.MAX_BYTES', 512):
            self.assertEqual(read_catalog(self.root, seed=SEED)['status'], 'metadata_unavailable')


if __name__ == '__main__':
    unittest.main()
