# 来源链路与任务基础：本地验收检查点

日期：2026-10-03；分支：codex/fix-controller-autodiscovery。
这是第一批实施记录，不是四阶段完成或新 Release 报告。

## 已实现

来源读取与身份：speedbench_identity.py、speedbench_sources.py；安全有界读取本机
profiles 与生成配置，以含认证、传输及 dialer-proxy 实际依赖的连接定义匹配。
唯一匹配 verified，多匹配 ambiguous，不可证明 unknown；provider 仍是独立成员关系。
只排除节点显示名，不为提高匹配率忽略路由选项。目录与 worker 提取间凭据变化会
重新校验，显式 ID 范围发现漂移则拒绝运行。内联 flow mapping YAML fixture 修复了
真实本机订阅无法解析的问题。

核心结果/CLI：clash_speedbench.py、speedbench_workers.py。保留 node_key/provider
旧字段，新 JSONL 添加版本化 node_id 与来源；来源过滤和切换按 ID 重新验证。
导出来源使用嵌套白名单，连接定义和种子不进入结果。

历史：speedbench_db.py 增加 subscription_sources、node_identities、node_origins，
以及 node_results.node_id/identity_version/identity_strength/source_status 的增量列与索引。
旧 runs.raw 保留原文，旧来源不回填；新来源按 ID 汇总，改名保留 snapshot。
probe 在线率只用已知探测数据做分母，缺失返回 N/A，另列 probe/bandwidth 覆盖率。

Web：speedbench_web.py、web/app.js、web/index.html。新增目录/来源历史/身份趋势
入口，已加载订阅选择、刷新、未加载禁用、过期选择不静默扩大范围；切换优先按 ID。
原 token、Host/Origin、静态白名单与 HTML escaping 保留。

任务基础：speedbench_tasks.py 实现 legacy/quick/standard/deep/ip 共享参数和限制，
speedbench_jobs.py 实现独立 JobStore 与 PhaseTimer。新模式 CLI 与 /api/run 参数
已接入，/api/task-config 返回合约。新模式 worker 采用动态队列和完整候选依赖配置，
快速/标准只对候选取得出口，延迟先于这些节点的 DoH；下载仍只有一个节点执行。
主探测成功记录不会因 worker 启动失败或其他节点失败而丢失；已精测覆盖范围优先。
旧 CLI 不加 mode 保持原 Top 15 流程。新增请求在 dispatch 前原子预留 running，
拒绝 NaN/Infinity/极大数/非法范围/未识别参数，API Key 不作为任务参数接收。

打包/文档：build_app.sh、.github/workflows/test.yml、release.yml、.gitignore、README.md，
明确包含新增模块且不打包 identity-seed；没有增加 pip 依赖。

新增测试：test_node_identity、test_source_catalog、test_source_history、test_source_api、
test_source_js、test_task_config、test_task_worker_modes、test_task_api、test_task_events、
test_upgrade_packaging。

## 验证证据

- A 完成时 Windows Python 3.9.25 / 3.12.10 / 3.14.7 均为 586 tests，OK (skipped=6)。
- B 基础接入及打包检查后，Windows Python 3.9.25 / 3.12.10 / 3.14.7 最终均为 630 tests，OK (skipped=6)。3.9 与 3.14 执行了 `python -m unittest discover -s tests -v`（使用对应解释器并加 -X utf8）；3.12 最终全量使用同一 discover 的 -q。全部 exit 0。
- 本机只读：保存订阅 2、可解析 2、当前加载 1；40 个运行节点，40 个 verified，40 个与实际 worker 提取定义重新匹配。仅打印计数，用内存测试种子，没有持久化 seed、切换节点、下载样本或写用户历史。
- Windows 私有 seed ACL、四个进程并发首次创建、线程创建、损坏文件拒绝测试通过。POSIX 实际权限用例在 Windows 跳过，不能据此声称 POSIX 验证完成。
- 原历史与迁移测试在 TemporaryDirectory，验证 raw 原文及幂等导入；密钥 canary 的 API/JSONL/CSV/SQLite 安全链路测试继续通过。
- node --check web/app.js、13 个根目录 Python 模块 AST、git diff --check 均通过（仅正常 CRLF 提示）。
- 没有真实测速效率对照、付费 API 查询、真实客户端安装替换、GitHub Actions 新运行或自动 push/tag/release。

## 边界与剩余工作

1. 源码开发分支有效，不代表 Downloads 中的已安装版本已更新。新增版本号与交付包需最终阶段验收。
2. 自定义目录已有 CLI --config-file；UI 目录选择、来源历史页面、收藏/趋势全部按 ID 续接未完成。
3. 种子迁移需要备份；配置根目录作为命名空间，根目录变化也可能产生新 ID，不自动合并旧历史。
4. 模式目前不接历史速度提示；未画像的节点不以名称猜地区。快速覆盖缩减不等于同精度提速。
5. 新模式当前要求隔离 worker；不可用时退出，不静默变成会切当前节点的串行全量测试。旧串行路径仍保留。
6. JobStore 与计时器已有独立测试，但生产 runner/HTTP SSE/前端还未接入。现有页面仍使用旧日志进度，不宣称实时任务页完成。
7. 还需 IPv4/IPv6 独立状态与预算、全生命周期取消、partial 保存、实际下载字节、task_runs/task_metrics、真实冷/热缓存对照。
8. C 共享 UI 和 D Tauri 桌面没有实施或构建；Windows 不能替代 macOS/Linux 原生验证，六格 CI 待后续授权推送或可用环境。

提交均为本地分拆提交；不会自动推送。实施清单只勾选有完成证据的核心来源工作，
任务阶段与后续 UI/桌面仍保持未完成状态。

## 本地实现提交

- 8729531：记录书面批准与实施检查点。
- e353ab0：来源验证、私有身份及解析回归。
- 5101272：历史关系、结果链路与 ID API 校验。
- 2295762：兼容 Web 的来源选择与迁移说明。
- 9c078e6：模式、任务状态、事件缓冲与计时基础。
- 8a061fa：模式接入、动态队列、覆盖排序与后端范围校验。
