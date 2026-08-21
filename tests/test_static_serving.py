# -*- coding: utf-8 -*-
"""静态文件分发安全（v0.6）：白名单 STATIC_FILES 之外的 GET 一律 404。

- 目录穿越：原始 path 含 ".."（http.client 原样发送，等价 curl --path-as-is）
  以及 %2e%2e 编码变体——服务端按白名单精确匹配、不做任何路径拼接，
  匹配不上即 404，且响应体不得泄露被穿越目标的文件内容；
- 非白名单路径：/static/ 目录本身、web/ 里真实存在但不在白名单的文件
  （/static/index.html）、不存在的文件名、/web/index.html 直访，全 404；
- web/index.html 磁盘缺失时 GET / 返回 503（把 WEB_DIR patch 到空临时目录
  模拟，不碰真实文件）。

全程只打本机随机端口，不起 mihomo、不碰真 Clash、不发外网请求。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_web as web
from tests.web_server_case import WebServerCase


class TraversalTest(WebServerCase):
    # 被穿越目标是仓库根的 Python 源码；404 响应体绝不能带上它的内容
    LEAK_MARK = b"STATIC_FILES"

    def test_raw_dotdot_404(self):
        # http.client 把 path 原样写进请求行，服务端收到未规范化的 ".."
        for path in ("/static/../speedbench_web.py",
                     "/../speedbench_web.py",
                     "/static/../../clash_speedbench.py"):
            with self.subTest(path=path):
                status, body = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(self.LEAK_MARK, body)

    def test_encoded_dotdot_404(self):
        # %2e%2e 编码：路由不做百分号解码，白名单匹配不上即 404
        for path in ("/static/%2e%2e/speedbench_web.py",
                     "/static/%2E%2E/speedbench_web.py",
                     "/%2e%2e/%2e%2e/etc/passwd",
                     "/static/..%2fspeedbench_web.py"):
            with self.subTest(path=path):
                status, body = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(self.LEAK_MARK, body)


class WhitelistTest(WebServerCase):
    def test_non_whitelisted_paths_404(self):
        for path in ("/static/index.html",  # web/ 里真实存在，但不在白名单
                     "/static/other.txt",   # 白名单外文件名
                     "/static/",            # 目录本身
                     "/static",             # 无尾斜杠
                     "/web/index.html"):    # 磁盘相对路径直访
            with self.subTest(path=path):
                status, body = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertFalse(json.loads(body)["ok"])


class MissingIndexTest(WebServerCase):
    def test_index_missing_returns_503(self):
        with tempfile.TemporaryDirectory() as td:
            # WEB_DIR 指向空目录模拟前端文件缺失，不碰真实 web/
            with mock.patch.object(web, "WEB_DIR", Path(td)):
                for path in ("/", "/index.html"):
                    with self.subTest(path=path):
                        status, headers, body = self.request_full("GET", path)
                        self.assertEqual(status, 503)
                        self.assertTrue(
                            headers.get("content-type", "").startswith("text/plain"))
                        self.assertIn("index.html", body.decode("utf-8"))
                # 白名单内文件磁盘缺失同样 404 而非 500
                status, _ = self.request("GET", "/static/app.js")
                self.assertEqual(status, 404)
        # patch 恢复后真实文件照常服务
        status, _ = self.request("GET", "/")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
