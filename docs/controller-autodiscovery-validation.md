# 控制器自动发现修复：验收记录

## 结果

已完成本地修复，并更新、重启 Windows 下载版。不再需要每次启动前手动设置 MIHOMO_SECRET。
实际控制器认证、读取配置/节点以及 Web 当前节点查询均通过。未运行测速，未切换节点。
这是 v1.0.1 的本地修复版；没有推送 GitHub、打 tag 或发布 Release。

## 变更文件

- speedbench_controller.py：新模块；跨平台配置路径、顶层 YAML 标量、仅本机地址、配对密钥、脱敏。
- clash_speedbench.py：统一 connect_controller、认证重读一次、手动优先、非交互失败、API 错误脱敏。
- speedbench_web.py：当前节点/切换/测速预检共享入口；子进程不等待隐藏密码；HTTP 字符串脱敏。
- speedbench_switch.py：CLI/SwiftBar 切换工具共享入口。
- tests/test_controller.py：39 个新用例，覆盖配置、认证、HTTP、子进程、历史和脱敏。
- tests/test_windows.py、tests/test_api_key_security.py：隔离新预检，补齐打包清单约束。
- tests/test_subscription.py：修正原有固定日期迁移 fixture 的过期问题；生产数据库逻辑不变。
- .github/workflows/test.yml、.github/workflows/release.yml、build_app.sh：新增模块加入 AST/Windows/macOS 打包清单。
- README.md：自动连接、优先级、路径、认证失败、密钥安全及升级后重启说明。
- docs/superpowers/specs/2026-10-02-controller-autodiscovery-design.md：用户批准的设计及完成状态。
- docs/superpowers/plans/2026-10-02-controller-autodiscovery-plan.md：实现和验收步骤。
- 本验收记录。

数据库 schema 无变更；测速算法、Phase 1/2、历史格式、IP Intelligence providers 未修改。
启动脚本 SpeedBench.bat 未修改，保留 ASCII/CRLF/无 BOM 约束。

## 验证命令及结果

基线：python -m unittest discover -s tests -v，500 项，1 个已有错误，5 项跳过。
错误是固定 2026-08-20 迁移样本被最近 30 天统计窗口排除；改用相对当前时间的前一天样本后通过。

最终每组均运行 python -m unittest discover -s tests -v：

| Windows Python | 总数 | 成功 | 跳过 | 失败/错误 | 结果 |
| --- | ---: | ---: | ---: | ---: | --- |
| 3.9.25 | 539 | 534 | 5 | 0 | OK |
| 3.12.10 | 539 | 534 | 5 | 0 | OK |
| 3.14.7 | 539 | 534 | 5 | 0 | OK |

Python 3.9 的隔离解释器通过 uv 安装在 dist/controller-fix-validation/python，使用 --no-bin --no-registry，
没有改默认 Python/PATH/Windows 注册表。未安装任何项目 pip 依赖。
5 项跳过分别是 Windows 不适用的 POSIX 0600 权限检查，以及 4 项可选 PyYAML 比对。
项目内置 YAML fallback 测试没有因此跳过。

完整输出保存在 dist/controller-fix-validation/tests-python3.9.log、tests-python3.12.log 和 tests-python3.14.log。
其他检查：Python 3.9 AST 语法兼容、Windows/macOS 发布包运行时模块清单、git diff --check 均通过。

## 安全验收

测试证明：自动密钥与同一文件地址绑定；不匹配本机/远程显式地址不收到自动密钥。
手动参数/环境变量（包括空值）优先；错误手动密钥不被偷偷替换。
认证重读有界；节点切换写操作不自动重放。
密钥不进入 HTTP 响应、STATE/log、子进程命令行、自动子进程环境、CSV、JSONL、SQLite runs.raw。
含引号和反斜杠的 JSON 转义形式也会脱敏；JSON 的布尔、数字和结构键不被文本替换损坏。

本机实际运行：移除仅本次验收进程的 MIHOMO_SECRET 后自动配置来源为 verge_config，
通过 TCP 连接当前 Mihomo v1.19.31，/version、/configs、/proxies 只读验证成功。
配置中的动态 pipe 被发现，但本机该 pipe 不可达；TCP 回退成功。动态 pipe 解析/传输兼容通过 mock 测试。
新 Web 后台 /api/current、/api/run/status、/api/ip-intel/status 均返回 HTTP 200，检查的响应不含控制器密钥。
Web 仍只监听 127.0.0.1:8950；Host/Origin、write token 和静态白名单未放宽。

## 下载版部署与回滚

目录：C:\Users\VoodooKyo\Downloads\Clash-SpeedBench。
更新前核对相关文件与仓库基线一致，没有覆盖其它本地改动。
先确认后台空闲、没有测速子进程，再通过受 write token 保护的 /api/quit 安全退出旧后台。
更新 clash_speedbench.py、speedbench_web.py、speedbench_switch.py、README.md，并新增 speedbench_controller.py。
以上程序文件与仓库经换行规范化后逐一 SHA-256 一致，启动器未变。
通过现有 SpeedBench.bat 在父进程无 MIHOMO_SECRET 的环境中重启，HTTP 当前节点查询成功。

旧文件备份：C:\Users\VoodooKyo\Downloads\Clash-SpeedBench-backup-controller-fix-19bfb4ad。
备份的 4 个原文件 hash 均核对一致，没有删除用户文件。
回滚时先退出 SpeedBench、用备份恢复这 4 个文件；新增模块可保留（旧代码不会导入它）。
更新不会删除或迁移 APPDATA/ClashSpeedBench 中的数据。

部署前后，历史 JSONL SHA-256 均为
b1d56f79cf1f38318336529ee55bd568b9fcea1b4692ff18449a2641d14f5327；
SQLite runs 记录数与最大 ID 均为 43，历史保留。
后台重启会正常轮换 Web 写令牌；旧浏览器页面需要刷新。

## 剩余限制

- macOS/Linux 验证的是平台 mock/路径和打包清单，没有在这两个系统上做实机验收，也未触发 GitHub Actions。
- 自动发现仅覆盖标准 Verge 配置目录；便携版/自定义目录暂需 CLI 手动指定控制器和密钥。
- 控制器字段仅接受生成配置常见的单行标量；不支持 alias/anchor/merge/多文档/跨行控制器值。
- 不在进行中的测速里热切换主控制器。更改密钥后后续新连接重读配置，正在运行的任务可能仍认证失败。
- 没有完整测速成绩验收，避免消耗用户流量或改变活动节点；测速回归由现有单元测试覆盖。
- 本地修复尚未发布；重新下载旧 GitHub Release 会覆盖修复，发布需另行确认。
