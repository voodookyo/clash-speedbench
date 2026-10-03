# -*- coding: utf-8 -*-
"""Local controller discovery fixtures; never read real Verge configuration."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clash_speedbench as csb
import speedbench_controller as ctl
import speedbench_web as web
import speedbench_switch as switch
import speedbench_db as db
from tests.web_server_case import WebServerCase

KEY = "fixture-controller-secret-not-a-real-key"
BASE = "http://127.0.0.1:19097"
CFG = "external-controller: 127.0.0.1:19097\nsecret: %s\n" % KEY


class ControllerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "clash-verge.yaml"
        self.path.write_text(CFG, encoding="utf-8")
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {}, clear=True).start()
        self.paths = mock.patch.object(ctl, "config_paths", return_value=[self.path]).start()


class ScalarTest(unittest.TestCase):
    def test_plain_and_comments(self):
        data = ctl.parse_controller_fields(CFG + "proxies:\n- name: ignored\n  secret: nested\n")
        self.assertEqual(data["secret"], KEY)
        self.assertEqual(ctl.parse_controller_fields("secret: a#b:c # trailing\n")["secret"], "a#b:c")

    def test_quotes_and_yaml_escapes(self):
        examples = {
            "'a''b # c: d\\e'": "a'b # c: d\\e",
            '"a\\\\b\\\"c # :"': 'a\\b"c # :',
            '"\\x41\\u0042\\U00000043"': 'ABC',
            '"\\/abc"': '/abc',
            "' true '": ' true ',
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(ctl.parse_controller_fields("secret: " + value)["secret"], expected)

    def test_null_empty_and_yaml_looking_secrets_remain_strings(self):
        for value in ("", "null", "~"):
            self.assertEqual(ctl.parse_controller_fields("secret: " + value)["secret"], "")
        for value in ("false", "01234", "123456", "on"):
            self.assertEqual(ctl.parse_controller_fields("secret: " + value)["secret"], value)

    def test_quoted_keys_bom_and_crlf(self):
        self.assertEqual(ctl.parse_controller_fields('\ufeff---\r\n"secret": "abc"\r\n')["secret"], "abc")

    def test_malformed_and_unsupported_values_fail_without_echo(self):
        for value in ('"' + KEY, "'" + KEY, '|', '>', '*alias', '&anchor x', '[a]', '{a: b}',
                      '"a\\nb"', '"a\\rb"', '"a\\q"', '"abc"junk', "plain: text"):
            with self.subTest(value=value):
                with self.assertRaises(ctl.ControllerConfigError) as caught:
                    ctl.parse_controller_fields("secret: " + value)
                self.assertNotIn(KEY, str(caught.exception))

    def test_duplicate_or_multiple_documents_are_rejected(self):
        for data in ("secret: a\nsecret: b", "---\nsecret: a\n---\nsecret: b",
                     "external-controller: 127.0.0.1:9097\n--- # second\nsecret: a",
                     "<<: *defaults\nsecret: a", "'<<': *defaults\nsecret: a"):
            with self.assertRaises(ctl.ControllerConfigError):
                ctl.parse_controller_fields(data)

    def test_folded_plain_continuation_is_not_silently_truncated(self):
        with self.assertRaises(ctl.ControllerConfigError):
            ctl.parse_controller_fields("secret: first\n  continuation\n")


class TargetTest(unittest.TestCase):
    def test_tcp_loopback_and_wildcard(self):
        examples = {
            "127.0.0.1:9097": "http://127.0.0.1:9097",
            "0.0.0.0:1234": "http://127.0.0.1:1234",
            "[::]:1234": "http://[::1]:1234",
            "[::1]:1234": "http://[::1]:1234",
            "https://127.0.0.1:1234/": "https://127.0.0.1:1234",
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(ctl.local_tcp_base(value), expected)

    def test_remote_and_ambiguous_targets_rejected(self):
        for value in ("198.51.100.2:9097", "localhost:9097", "evil.example:9097",
                      "http://user:pass@127.0.0.1:9097", "http://127.0.0.1:9097?x=a",
                      "http://127.0.0.1:9097#x", "http://127.0.0.1:9097/api",
                      "127.0.0.1:0", "127.0.0.1:99999", "ftp://127.0.0.1:9097"):
            self.assertIsNone(ctl.local_tcp_base(value), value)

    def test_config_paths_all_platforms(self):
        self.assertEqual(ctl.config_paths("win32", {"APPDATA": "/fixture/roaming"}, "/fixture/home")[0],
                         Path("/fixture/roaming/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"))
        self.assertIn("Library/Application Support", ctl.config_paths("darwin", {}, "/fixture/home")[0].as_posix())
        self.assertEqual(ctl.config_paths("linux", {"XDG_CONFIG_HOME": "/fixture/xdg"}, "/fixture/home")[0].parent,
                         Path("/fixture/xdg/io.github.clash-verge-rev.clash-verge-rev"))
        self.assertIn(".config", str(ctl.config_paths("linux", {}, "/fixture/home")[0]))
        self.assertEqual(ctl.config_paths("win32", {}, "/fixture/home"), [])


class RedactionTest(unittest.TestCase):
    def setUp(self):
        with ctl._SECRET_LOCK:
            self.snapshot = list(ctl._KNOWN_SECRETS)
        self.addCleanup(self.restore)

    def restore(self):
        with ctl._SECRET_LOCK:
            ctl._KNOWN_SECRETS.clear()
            ctl._KNOWN_SECRETS.extend(self.snapshot)

    def test_json_escaped_error_body_is_redacted(self):
        secret = 'fixture-"quoted"-\\-secret'
        ctl.remember_secret(secret)
        body = json.dumps({"echo": secret})
        redacted = ctl.redact_text(body)
        self.assertEqual(json.loads(redacted)["echo"], "[REDACTED]")

    def test_json_boolean_and_schema_keys_are_not_corrupted(self):
        ctl.remember_secret("true")
        payload = {"true": True, "ok": True, "score": 100, "message": "true"}
        output = ctl.redact_payload(payload)
        self.assertTrue(output["true"])
        self.assertEqual(output["message"], "[REDACTED]")
        self.assertEqual(output["score"], 100)


class DiscoveryTest(ControllerFixture):
    def test_discovers_credentials_not_in_repr(self):
        targets, warnings = ctl.discover_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual((targets[0].base, targets[0].secret), (BASE, KEY))
        self.assertEqual(warnings, [])
        self.assertNotIn(KEY, repr(targets[0]))

    def test_dynamic_pipe_local_only_and_platform_filtering(self):
        self.path.write_text(CFG + r"external-controller-pipe: '\\.\pipe\verge-mihomo-hash-123'" + "\n"
                             + "external-controller-unix: /tmp/verge/current.sock\n", encoding="utf-8")
        win, _ = ctl.discover_targets(platform="win32")
        posix, _ = ctl.discover_targets(platform="darwin")
        self.assertEqual(win[0].base, "pipe://verge-mihomo-hash-123")
        self.assertFalse(any(t.base.startswith("unix://") for t in win))
        self.assertEqual(posix[0].base, "unix:///tmp/verge/current.sock")
        self.assertFalse(any(t.base.startswith("pipe://") for t in posix))
        self.path.write_text("external-controller-pipe: '\\\\remote\\pipe\\key'\nsecret: " + KEY,
                             encoding="utf-8")
        self.assertEqual(ctl.discover_targets(platform="win32")[0], [])

    def test_runtime_missing_uses_sibling_config_but_corrupt_does_not(self):
        fallback = self.path.with_name("config.yaml")
        fallback.write_text(CFG, encoding="utf-8")
        self.path.unlink()
        self.assertEqual(ctl.discover_targets()[0][0].secret, KEY)
        self.path.write_text('secret: "' + KEY, encoding="utf-8")
        targets, warnings = ctl.discover_targets()
        self.assertEqual(targets, [])
        self.assertTrue(warnings)
        self.assertNotIn(KEY, str(warnings))

    def test_permission_errors_are_sanitized(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError(KEY)):
            targets, warnings = ctl.discover_targets()
        self.assertEqual(targets, [])
        self.assertTrue(warnings)
        self.assertNotIn(KEY, str(warnings))

    def test_oversized_or_non_utf8_configs_do_not_return_credentials(self):
        self.path.write_bytes(b'\xff\xfe')
        self.assertEqual(ctl.discover_targets()[0], [])
        self.path.write_text(CFG, encoding="utf-8")
        with mock.patch.object(ctl, "MAX_CONFIG_BYTES", 8):
            self.assertEqual(ctl.discover_targets()[0], [])

    def test_independent_configs_do_not_cross_pair_credentials(self):
        other = self.path.parent / "other.yaml"
        other.write_text("external-controller: 127.0.0.1:19098\nsecret: other-key\n", encoding="utf-8")
        self.paths.return_value = [self.path, other]
        targets, _ = ctl.discover_targets()
        self.assertEqual([(t.base, t.secret) for t in targets],
                         [(BASE, KEY), ("http://127.0.0.1:19098", "other-key")])


class ConnectionTest(ControllerFixture):
    def fake_get(self, api, path):
        self.assertEqual(path, "/version")
        if api.secret != KEY or api.base != BASE:
            raise csb.Unauthorized("no")
        return {"version": "fixture"}

    def test_auto_auth_and_no_environment_mutation(self):
        with mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get):
            api = csb.connect_controller()
        self.assertEqual(api.secret, KEY)
        self.assertNotIn("MIHOMO_SECRET", os.environ)

    def test_manual_overrides_and_explicit_empty_do_not_fallback(self):
        for env, argument in (({"MIHOMO_SECRET": "wrong"}, None),
                              ({"MIHOMO_SECRET": KEY}, "wrong"), ({"MIHOMO_SECRET": KEY}, "")):
            with self.subTest(env=env, argument=argument), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get), \
                    mock.patch.object(csb.getpass, "getpass") as prompt:
                with self.assertRaises(csb.ApiError) as caught:
                    csb.connect_controller(secret=argument)
                self.assertNotIn(KEY, str(caught.exception))
                prompt.assert_not_called()

    def test_environment_and_argument_success(self):
        with mock.patch.dict(os.environ, {"MIHOMO_SECRET": KEY}), \
                mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get):
            self.assertEqual(csb.connect_controller().secret, KEY)
        with mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get):
            self.assertEqual(csb.connect_controller(secret=KEY).secret, KEY)

    def test_explicit_target_never_gets_unrelated_auto_secret(self):
        for target in ("http://198.51.100.1:9097", "http://127.0.0.1:19098"):
            def unauthorized(api, path):
                self.assertEqual(api.secret, "")
                self.assertEqual(api.base, target)
                raise csb.Unauthorized("auth")
            with self.subTest(target=target), \
                    mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=unauthorized):
                with self.assertRaises(csb.ApiError):
                    csb.connect_controller(explicit=target)

    def test_matching_explicit_target_uses_bound_key(self):
        with mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get):
            self.assertEqual(csb.connect_controller(explicit=BASE + "/").secret, KEY)

    def test_legacy_defaults_when_no_config(self):
        self.path.unlink()
        with mock.patch.object(csb, "DEFAULT_CONTROLLERS", (BASE,)), \
                mock.patch.object(csb.MihomoAPI, "get", return_value={"version": "fixture"}):
            self.assertEqual(csb.connect_controller().secret, "")

    def test_authorized_candidate_preferred_over_unauthorized_one(self):
        other = self.path.parent / "other.yaml"
        other.write_text("external-controller: 127.0.0.1:19098\nsecret: wrong\n", encoding="utf-8")
        self.paths.return_value = [other, self.path]
        with mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get):
            self.assertEqual(csb.connect_controller().secret, KEY)

    def test_rereads_once_on_rotation_without_global_secret(self):
        attempts = []
        def rotated(api, path):
            attempts.append(api.secret)
            if api.secret == KEY:
                self.path.write_text(CFG.replace(KEY, "rotated-key"), encoding="utf-8")
                raise csb.Unauthorized("changed")
            if api.secret == "rotated-key":
                return {"version": "fixture"}
            raise csb.ApiError("down")
        with mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=rotated), \
                mock.patch.object(csb, "DEFAULT_CONTROLLERS", ()):
            self.assertEqual(csb.connect_controller().secret, "rotated-key")
        self.assertEqual(attempts, [KEY, "rotated-key"])
        self.assertNotIn("MIHOMO_SECRET", os.environ)

    def test_noninteractive_has_no_hidden_prompt_and_retry_is_bounded(self):
        with mock.patch.object(csb.MihomoAPI, "get", side_effect=csb.Unauthorized("auth")), \
                mock.patch.object(csb, "DEFAULT_CONTROLLERS", ()), \
                mock.patch.object(csb.sys, "stdin", None), \
                mock.patch.object(csb.getpass, "getpass") as prompt:
            with self.assertRaises(csb.ApiError):
                csb.connect_controller(interactive=True)
        self.assertEqual(self.paths.call_count, 2)
        prompt.assert_not_called()

    def test_interactive_cli_can_prompt_only_after_discovery(self):
        self.path.unlink()
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        with mock.patch.object(csb, "DEFAULT_CONTROLLERS", (BASE,)), \
                mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get), \
                mock.patch.object(csb.sys, "stdin", stdin), \
                mock.patch.object(csb.getpass, "getpass", return_value=KEY) as prompt:
            self.assertEqual(csb.connect_controller(interactive=True).secret, KEY)
        prompt.assert_called_once()

    def test_manual_crlf_and_error_body_are_redacted(self):
        with self.assertRaises(csb.ApiError):
            csb.connect_controller(secret="bad\r\nheader", explicit=BASE)
        with mock.patch.object(csb.MihomoAPI, "get", side_effect=csb.ApiError("bad " + KEY)), \
                mock.patch.object(csb, "DEFAULT_CONTROLLERS", ()):
            with self.assertRaises(csb.ApiError) as caught:
                csb.connect_controller()
        self.assertNotIn(KEY, str(caught.exception))

    def test_http_header_and_echoed_error_are_safe(self):
        response = mock.Mock()
        response.status = 500
        response.read.return_value = ("echo " + KEY).encode()
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(csb.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(csb.ApiError) as caught:
                csb.MihomoAPI(BASE, KEY).get("/version")
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer " + KEY)
        self.assertNotIn(KEY, str(caught.exception))

    def test_controller_data_is_not_part_of_result_history(self):
        with mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get):
            api = csb.connect_controller()
        record_path = self.path.parent / "history.jsonl"
        csv_path = self.path.parent / "results.csv"
        db_path = self.path.parent / "history.db"
        result = csb.Result(name="fixture-node", provider="fixture", proto="ss", status="ok",
                            latency_ms=20, speeds_mbps=[20.0], median_mbps=20.0, best_mbps=20.0)
        csb.append_history([result], record_path, 1, 1, csv_path)
        csb.write_csv([result], csv_path)
        db.import_jsonl(db_path, record_path)
        self.assertEqual(api.secret, KEY)
        self.assertNotIn(KEY, record_path.read_text(encoding="utf-8"))
        self.assertNotIn(KEY, csv_path.read_text(encoding="utf-8-sig"))
        with contextlib.closing(sqlite3.connect(db_path)) as connection:
            self.assertNotIn(KEY, connection.execute("SELECT raw FROM runs").fetchone()[0])

    def test_cli_serial_banner_and_cancel_do_not_change_nodes(self):
        def responses(api, path):
            self.assertEqual(api.secret, KEY)
            if path == "/version":
                return {"version": "fixture"}
            if path == "/configs":
                return {"mode": "rule", "mixed-port": 7897}
            if path == "/proxies":
                return {"proxies": {"GLOBAL": {"type": "Selector", "all": ["nodeA"], "now": "nodeA"},
                                    "nodeA": {"type": "Shadowsocks"}}}
            raise AssertionError("unexpected endpoint")
        with mock.patch.dict(os.environ, {"SPEEDBENCH_HOME": self.tmp.name}), \
                mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=responses), \
                mock.patch.object(csb.MihomoAPI, "put") as write, \
                mock.patch.object(sys, "argv", ["clash_speedbench.py", "--workers", "1", "--no-ip"]), \
                mock.patch.object(csb, "clear_cancel_request"), \
                mock.patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(csb.main(), 0)
        write.assert_not_called()
        self.assertIn(BASE, output.getvalue())
        self.assertNotIn(KEY, output.getvalue())


class WebControllerTest(WebServerCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "clash-verge.yaml"
        self.path.write_text(CFG, encoding="utf-8")
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {}, clear=True).start()
        mock.patch.object(ctl, "config_paths", return_value=[self.path]).start()
        mock.patch.object(csb, "DEFAULT_CONTROLLERS", ()).start()
        self.select = mock.patch.object(csb.MihomoAPI, "select").start()
        mock.patch.object(csb.MihomoAPI, "get", autospec=True, side_effect=self.fake_get).start()

    def fake_get(self, api, path):
        if api.secret != KEY:
            raise csb.Unauthorized("needs key")
        if path == "/version":
            return {"version": "fixture"}
        if path == "/proxies":
            return {"proxies": {"Main": {"type": "Selector", "all": ["nodeA", "nodeB"], "now": "nodeA"},
                                "nodeA": {"type": "Shadowsocks"}, "nodeB": {"type": "Shadowsocks"}}}
        raise AssertionError("unexpected route: " + path)

    def test_current_http_authenticates_without_echoing_key(self):
        status, raw = self.request("GET", "/api/current")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertNotIn(KEY, raw.decode())

    def test_switch_http_uses_auto_auth_once(self):
        status, raw = self.post_authorized("/api/switch", {"name": "nodeB"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        self.select.assert_called_once_with("Main", "nodeB")
        self.assertNotIn(KEY, raw.decode())

    def test_web_and_switch_utility_share_resolver(self):
        with mock.patch.object(web, "connect_controller", return_value=csb.MihomoAPI(BASE, KEY), create=True) as conn:
            self.assertTrue(web.get_current()["ok"])
            conn.assert_called_once_with()
        with mock.patch.object(switch, "connect_controller", return_value=csb.MihomoAPI(BASE, KEY), create=True) as conn, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(switch.switch_to("nodeA"), 0)
            conn.assert_called_once_with()

    def test_run_preflight_and_child_no_secret_in_args_or_env(self):
        proc = mock.Mock()
        proc.stdout = []
        proc.wait.return_value = 0
        with mock.patch.object(web.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(web, "sync_db"):
            web.run_benchmark({})
        popen.assert_called_once()
        args = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertIn("--non-interactive", args)
        self.assertNotIn(KEY, str(args))
        self.assertNotIn(KEY, str(env))
        self.assertNotIn("MIHOMO_SECRET", env)

    def test_preflight_failure_never_spawns_or_prompts(self):
        with mock.patch.object(web, "connect_controller", side_effect=csb.ApiError("auth " + KEY), create=True), \
                mock.patch.object(web.subprocess, "Popen") as popen, \
                mock.patch.object(web, "sync_db"):
            web.run_benchmark({})
        popen.assert_not_called()
        self.assertFalse(web.STATE["running"])
        self.assertNotIn(KEY, str(web.STATE["lines"]))

    def test_error_http_and_runtime_logs_are_redacted(self):
        ctl.remember_secret(KEY)
        with mock.patch.object(web, "connect_controller", side_effect=csb.ApiError("echo " + KEY), create=True):
            status, raw = self.request("GET", "/api/current")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["ok"])
        self.assertNotIn(KEY, raw.decode())
        self.assertNotIn(KEY, web._redact_runtime_text("echo " + KEY))

    def test_write_failure_is_not_replayed(self):
        self.select.side_effect = csb.Unauthorized("changed")
        response = web.do_switch("nodeB")
        self.assertFalse(response["ok"])
        self.select.assert_called_once()


if __name__ == "__main__":
    unittest.main()
