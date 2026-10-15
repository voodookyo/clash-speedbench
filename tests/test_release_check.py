import concurrent.futures
import io
import json
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError

import speedbench_releases as releases
import speedbench_web as web
from tests.web_server_case import WebServerCase


def fixture(tag='v1.2.0', **extra):
    return dict(tag_name=tag, draft=False, prerelease=False,
                html_url=releases.RELEASES_URL+'/tag/'+tag, **extra)


class ReleaseCheckTest(unittest.TestCase):
    def test_only_stable_semantic_versions_are_compared_not_release_dates(self):
        for current, tag, state in [('1.0.1','v1.2.0','update_available'),
                                    ('1.2.0','v1.2.0','current'),
                                    ('1.3.0','v1.2.0','ahead'),
                                    ('1.1.0-alpha.1','v1.0.1','ahead'),
                                    ('1.1.0-alpha.1','v1.1.0','update_available')]:
            with self.subTest(current=current), mock.patch.object(releases,'fetch_latest',return_value=fixture(tag)):
                result=releases.ReleaseChecker().check(current)
                self.assertEqual(result['comparison'],state)
                self.assertEqual(result['current_prerelease'],'-' in current)
                self.assertFalse(result['automatic_updates'])

    def test_untrusted_response_is_rejected_without_echoing_body_urls_or_keys(self):
        variants=[[],{**fixture(),'draft':True},
                  {**fixture(),'prerelease':True},{**fixture(),'draft':0},
                  {**fixture(),'tag_name':'v1.2.0-CANARY'},
                  {**fixture(),'html_url':'https://evil.example/CANARY'},
                  {**fixture(),'tag_name':'v01.2.0'},
                  {**fixture(),'html_url':releases.RELEASES_URL+'/tag/v1.2.0?CANARY'},
                  {**fixture(),'html_url':releases.RELEASES_URL.replace('https:','http:')+'/tag/v1.2.0'}]
        for value in variants:
            with self.subTest(value=value),mock.patch.object(releases,'fetch_latest',return_value=value):
                out=releases.ReleaseChecker().check('1.0.1')
                self.assertEqual(out['status'],'invalid_response')
                self.assertNotIn('CANARY',json.dumps(out))
                self.assertIsNone(out['comparison'])

    def test_extra_remote_body_and_assets_never_cross_public_boundary(self):
        with mock.patch.object(releases,'fetch_latest',return_value=fixture(body='<script>CANARY</script>',assets=[{'key':'CANARY'}])):
            out=releases.ReleaseChecker().check('1.0.1')
        self.assertNotIn('CANARY',json.dumps(out))
        self.assertNotIn('body',out);self.assertNotIn('assets',out)

    def test_success_and_failures_have_bounded_cache_and_no_false_clean_status(self):
        clock=mock.Mock(return_value=1.0)
        with mock.patch.object(releases,'fetch_latest',return_value=fixture()) as fetch:
            check=releases.ReleaseChecker(clock=clock)
            first=check.check('1.0.1');second=check.check('1.0.1')
            self.assertFalse(first['cached']);self.assertTrue(second['cached']);self.assertEqual(fetch.call_count,1)
            clock.return_value=1+releases.CACHE_TTL+1;check.check('1.0.1')
            self.assertEqual(fetch.call_count,2)
        with mock.patch.object(releases,'fetch_latest',side_effect=TimeoutError('CANARY')) as fetch:
            check=releases.ReleaseChecker()
            out=check.check('1.0.1');again=check.check('1.0.1')
            self.assertEqual(out['status'],'timeout');self.assertIsNone(out['comparison'])
            self.assertTrue(again['cached']);self.assertEqual(fetch.call_count,1)
            self.assertNotIn('CANARY',json.dumps(out))

    def test_concurrent_requests_share_one_fetch_and_return_separate_results(self):
        entered=threading.Event();release=threading.Event()
        def fetch():entered.set();self.assertTrue(release.wait(2));return fixture()
        checker=releases.ReleaseChecker()
        with mock.patch.object(releases,'fetch_latest',side_effect=fetch) as call,concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(checker.check,'1.0.1') for _ in range(4)]
            self.assertTrue(entered.wait(2));release.set()
            values=[f.result(3) for f in futures]
        self.assertEqual(call.call_count,1)
        values[0]['latest']='CANARY';self.assertNotIn('CANARY',json.dumps(values[1]))

    def test_http_failures_are_fixed_statuses_never_raw_error_or_authorization(self):
        for code,status in [(403,'rate_limited'),(429,'rate_limited'),(404,'not_published'),(500,'unavailable')]:
            with mock.patch.object(releases,'fetch_latest',side_effect=HTTPError('CANARY',code,'CANARY',{},None)):
                out=releases.ReleaseChecker().check('1.0.1')
                self.assertEqual(out['status'],status);self.assertIsNone(out['comparison'])
                self.assertNotIn('CANARY',json.dumps(out))
        stream=io.BytesIO(b'CANARY')
        with mock.patch.object(releases,'fetch_latest',side_effect=HTTPError('CANARY',403,'CANARY',{},stream)):
            self.assertEqual(releases.ReleaseChecker().check('1.0.1')['status'],'rate_limited')
        self.assertTrue(stream.closed)

    def test_fetch_uses_fixed_https_no_auth_no_redirects_bounded_read_and_timeout(self):
        response=mock.MagicMock()
        response.__enter__.return_value=response
        response.status=200;response.geturl.return_value=releases.API_URL
        response.read.return_value=json.dumps(fixture()).encode()
        response.headers={'Content-Type':'application/json'}
        opener=mock.Mock();opener.open.return_value=response
        with mock.patch.object(releases.request,'build_opener',return_value=opener) as build:
            self.assertEqual(releases.fetch_latest()['tag_name'],'v1.2.0')
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,releases.API_URL)
        self.assertFalse(any(k.lower()=='authorization' for k in req.headers))
        self.assertEqual(opener.open.call_args.kwargs['timeout'],releases.REQUEST_TIMEOUT)
        response.read.assert_called_once_with(releases.MAX_BYTES+1)
        self.assertIsInstance(build.call_args.args[0],releases.NoRedirect)
        self.assertIsNone(releases.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example'))
        for data in (b'x'*(releases.MAX_BYTES+1),b'CANARY-not-json',b'{"tag_name":"v1.2.0", "tag_name":"v2.0.0"}'):
            response.read.return_value=data
            with mock.patch.object(releases.request,'build_opener',return_value=opener),self.assertRaises(releases.ReleaseError):releases.fetch_latest()
        response.geturl.return_value='https://evil.example/CANARY'
        with mock.patch.object(releases.request,'build_opener',return_value=opener),self.assertRaises(releases.ReleaseError):releases.fetch_latest()


class ReleaseApiTest(WebServerCase):
    def test_local_info_does_not_query_network_and_auth_rejects_remote_origin(self):
        with mock.patch.object(web.RELEASE_CHECKER,'check') as check:
            status,raw=self.request('GET','/api/releases')
            self.assertEqual(status,403)
            status,raw=self.request('GET','/api/releases',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
            self.assertEqual(status,200);self.assertEqual(json.loads(raw)['status'],'not_checked');check.assert_not_called()
            status,_=self.post_authorized('/api/releases/check',{},headers={'Origin':'https://evil.example'})
            self.assertEqual(status,403);check.assert_not_called()

    def test_check_requires_empty_strict_body_and_only_user_triggered_network(self):
        with mock.patch.object(web.RELEASE_CHECKER,'check',return_value={'status':'ok'}) as check:
            for body in ({'url':'https://evil.example/CANARY'},[],{'key':'CANARY'}):
                status,_=self.post_authorized('/api/releases/check',body)
                self.assertEqual(status,400)
            status,_=self.request('POST','/api/releases/check',body='CANARY',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
            self.assertEqual(status,400);check.assert_not_called()
            status,raw=self.post_authorized('/api/releases/check',{})
            self.assertEqual(status,200);check.assert_called_once_with(releases.CORE_VERSION)
        with mock.patch.object(web,'DESKTOP_IDENTITY',{'version':'1.1.0-alpha.1'}),mock.patch.object(web.RELEASE_CHECKER,'check',return_value={'status':'ok'}) as check:
            self.post_authorized('/api/releases/check',{});check.assert_called_once_with('1.1.0-alpha.1')
