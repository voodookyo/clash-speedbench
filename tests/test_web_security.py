# -*- coding: utf-8 -*-
"""Web 面板安全基线（v0.4）。

在 127.0.0.1 随机空闲端口用线程起真实 HTTP server，http.client 发请求验证：
- GET / 返回的 HTML 把 __SB_TOKEN__ 占位符替换成真实 WEB_TOKEN（<meta> 注入）
- 所有 POST 路径先过 _check_post 闸门：无令牌/错令牌/坏 Host/坏 Origin → 403 JSON
- 正确令牌 + 同源 Origin（或 curl 式无 Origin）→ 放行（非 403）
- 不存在的 POST 路径也在闸门之后：无令牌 → 403，有令牌 → 404
- GET 只读接口不需要令牌
- 前端无 inline onclick（事件委托 + data-name）

全程只打本机随机端口，不起 mihomo、不碰真 Clash、不发外网请求。
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_web as web
from tests.web_server_case import TOKEN, WebServerCase

# 未运行测速时调 /api/run/cancel 无副作用（返回 ok:false），用它验证闸门放行
SAFE_POST = "/api/run/cancel"
# 未知路径用 ASCII：http.client 要求请求行可 ASCII 编码（浏览器会自动百分号转码）
ALL_POST_PATHS = ("/api/run", "/api/switch", "/api/run/cancel", "/api/quit",
                  "/no-such-endpoint")


class TokenInjectionTest(WebServerCase):
    def test_index_meta_has_real_token(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn(f'<meta name="sb-token" content="{TOKEN}">', html)
        self.assertNotIn("__SB_TOKEN__", html)  # 占位符必须被替换干净

    def test_page_template_keeps_placeholder(self):
        # 模板里仍是占位符，替换发生在响应时（令牌每进程随机生成）
        self.assertIn("__SB_TOKEN__", web.PAGE)

    def test_web_token_format(self):
        # secrets.token_hex(16) → 32 位小写十六进制
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", TOKEN))

    def test_get_endpoints_open_without_token(self):
        # 只读 GET 不需要令牌；/api/current 会探测真 Clash controller，不测
        for path in ("/api/run/status", "/api/latest", "/api/history"):
            with self.subTest(path=path):
                status, _ = self.request("GET", path)
                self.assertEqual(status, 200)

    def test_get_unknown_path_404(self):
        status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertFalse(json.loads(body)["ok"])


class PostGateTest(WebServerCase):
    def test_no_token_403_on_all_post_paths(self):
        # 令牌校验在路由分发之前：包括 /api/quit 和不存在的路径，一律 403
        for path in ALL_POST_PATHS:
            with self.subTest(path=path):
                status, body = self.post_json(path, {})
                self.assertEqual(status, 403)
                self.assertFalse(json.loads(body)["ok"])

    def test_wrong_token_403(self):
        for bad in ("0" * 32, TOKEN[:-1] + ("0" if TOKEN[-1] != "0" else "1")):
            with self.subTest(bad=bad):
                status, body = self.post_json(
                    SAFE_POST, {}, headers={"X-SpeedBench-Token": bad})
                self.assertEqual(status, 403)
                self.assertFalse(json.loads(body)["ok"])

    def test_bad_host_403(self):
        # DNS rebinding 场景：令牌对了但 Host 不是本机端口
        for host in ("evil.example.com", "169.254.1.1:8950",
                     f"127.0.0.1:{self.port + 1}"):
            with self.subTest(host=host):
                status, body = self.post_json(
                    SAFE_POST, {},
                    headers={"X-SpeedBench-Token": TOKEN, "Host": host})
                self.assertEqual(status, 403)
                self.assertFalse(json.loads(body)["ok"])

    def test_bad_origin_403(self):
        for origin in ("http://evil.example.com",
                       f"http://127.0.0.1:{self.port + 1}", "null"):
            with self.subTest(origin=origin):
                status, body = self.post_json(
                    SAFE_POST, {},
                    headers={"X-SpeedBench-Token": TOKEN, "Origin": origin})
                self.assertEqual(status, 403)
                self.assertFalse(json.loads(body)["ok"])

    def test_valid_token_same_origin_allowed(self):
        for origin in (f"http://127.0.0.1:{self.port}",
                       f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                status, body = self.post_authorized(
                    SAFE_POST, {}, headers={"Origin": origin})
                # 穿过闸门进入路由：无测速时 cancel 返回 200 + ok:false
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["ok"])

    def test_valid_token_no_origin_allowed(self):
        # curl 这类非浏览器客户端不带 Origin，视为同源放行
        status, body = self.post_authorized(SAFE_POST, {})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["ok"])

    def test_valid_token_localhost_host_allowed(self):
        # Host 白名单同时含 127.0.0.1:port 与 localhost:port
        status, _ = self.post_authorized(
            SAFE_POST, {}, headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_unknown_path_with_valid_token_404(self):
        # 过了闸门才轮到路由：路径不存在 → 404 而非 403
        status, body = self.post_authorized("/no-such-endpoint", {})
        self.assertEqual(status, 404)
        self.assertFalse(json.loads(body)["ok"])


class FrontendSourceTest(unittest.TestCase):
    def test_no_inline_onclick(self):
        # v0.4 移除了全部 inline onclick，改事件委托；这里防回退
        src = Path(web.__file__).read_text(encoding="utf-8").lower()
        self.assertNotIn("onclick=", src)
        self.assertNotIn("onclick=", web.PAGE.lower())

    def test_event_delegation_present(self):
        self.assertIn("addEventListener", web.PAGE)
        self.assertIn("dataset.name", web.PAGE)


if __name__ == "__main__":
    unittest.main()
