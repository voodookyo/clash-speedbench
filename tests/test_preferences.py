import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from speedbench_preferences import Preferences, PreferenceError, validate
import speedbench_web as web
from tests.web_server_case import WebServerCase


class PreferencesTest(unittest.TestCase):
    def test_patch_reopen_and_partial_update_retain_private_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs=Preferences(folder)
            self.assertEqual(prefs.read(),{})
            identity='node_v2_'+'a'*32
            prefs.patch({'sb_theme':'dark','sb_favs_v2':json.dumps([identity,identity])})
            Preferences(folder).patch({'sb_mode':'ip'})
            values=Preferences(folder).read()
            self.assertEqual(values['sb_theme'],'dark')
            self.assertEqual(values['sb_mode'],'ip')
            self.assertEqual(json.loads(values['sb_favs_v2']),[identity])
            if os.name!='nt':self.assertEqual(prefs.path.stat().st_mode & 0o777,0o600)

    def test_canary_keys_paths_urls_and_unrecognized_fields_are_not_stored(self):
        for values in ({'ipqs_key':'CANARY-secret'},{'controller_secret':'CANARY-secret'},
                       {'path':'C:/secret'},{'sb_theme':'CANARY-secret'},
                       {'sb_favs_v2':'["https://evil.example"]'},{'sb_mode':True},[]):
            with self.subTest(values=values),self.assertRaises(PreferenceError):validate(values)

    def test_corrupt_preference_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs=Preferences(folder);prefs.patch({'sb_theme':'light'})
            prefs.path.write_bytes(b'corrupt-untouched')
            with self.assertRaises(PreferenceError):prefs.patch({'sb_theme':'dark'})
            self.assertEqual(prefs.path.read_bytes(),b'corrupt-untouched')
            self.assertFalse(list(Path(folder).glob('.ui-preferences-*')))

    def test_failed_atomic_replace_preserves_existing_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs=Preferences(folder);prefs.patch({'sb_theme':'light'});before=prefs.path.read_bytes()
            with mock.patch('speedbench_preferences.os.replace',side_effect=OSError('CANARY-secret')):
                with self.assertRaises(PreferenceError) as error:prefs.patch({'sb_theme':'dark'})
            self.assertNotIn('CANARY-secret',str(error.exception))
            self.assertEqual(prefs.path.read_bytes(),before)
            self.assertFalse(list(Path(folder).glob('.ui-preferences-*')))


class PreferencesApiTest(WebServerCase):
    def test_preferences_are_authenticated_bounded_and_shared_across_ports(self):
        with mock.patch.object(web,'DATA_HOME',Path(self._task_temp.name)):
            status,_=self.request('GET','/api/preferences');self.assertEqual(status,403)
            status,_=self.post_json('/api/preferences',{'sb_theme':'dark'});self.assertEqual(status,403)
            status,_=self.post_authorized('/api/preferences',{'sb_theme':'dark'},headers={'Origin':'https://evil.example'})
            self.assertEqual(status,403)
            status,raw=self.post_authorized('/api/preferences',{'ipqs_key':'CANARY-secret'})
            self.assertEqual(status,400);self.assertNotIn(b'CANARY-secret',raw)
            status,_=self.post_authorized('/api/preferences',{'sb_theme':'dark'});self.assertEqual(status,200)
            status,raw=self.request('GET','/api/preferences',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
            self.assertEqual(status,200);self.assertEqual(json.loads(raw)['values'],{'sb_theme':'dark'})
            self.assertNotIn('CANARY-secret',Preferences(self._task_temp.name).path.read_text(encoding='utf-8'))
            self.assertEqual(Preferences(self._task_temp.name).read()['sb_theme'],'dark')
