import threading
import unittest
from unittest import mock
import clash_speedbench as core


class ExitProgressTest(unittest.TestCase):
    def test_ipv4_published_before_slow_ipv6_but_function_joins_both(self):
        release = threading.Event()
        seen = []
        def fetch(proxy,timeout,ipv6):
            if ipv6:
                self.assertTrue(release.wait(2))
                return '2001:db8::1'
            return '203.0.113.1'
        def on_result(family,address,status):
            seen.append((family,address,status))
            if family=='ipv4':
                release.set()
        with mock.patch.object(core,'fetch_exit_ip',side_effect=fetch), \
                mock.patch.object(core,'fetch_ip_info',return_value=None):
            result = core.fetch_exit_ips('http://127.0.0.1:1',1,on_result=on_result)
        self.assertEqual([v for v in seen if v[0]!='legacy'][0],('ipv4','203.0.113.1','completed'))
        self.assertEqual(result[:2],('203.0.113.1','2001:db8::1'))
        self.assertEqual(len(seen),3)  # includes the basic-profile query

    def test_wrong_family_is_not_published_as_success(self):
        seen = []
        with mock.patch.object(core,'fetch_exit_ip',return_value='2001:db8::1'), \
                mock.patch.object(core,'fetch_ip_info',return_value=None):
            core.fetch_exit_ips('http://127.0.0.1:1',1,on_result=lambda *v:seen.append(v))
        self.assertIn(('ipv4',None,'failed'),seen)
