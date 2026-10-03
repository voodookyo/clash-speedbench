# 四阶段升级实施清单

批准依据：2026-10-03 用户回复“规格确认，继续实施”。设计入口为 ../specs/2026-10-03-upgrade-roadmap-design.md。

本清单不能替代验收证据。只在实际实现且对应命令/运行证据已核对后勾选。

## A：来源与身份

- [x] A1：新增 tests/test_source_catalog.py、test_node_identity.py，覆盖空 provider、认证不同、改名、多来源、未知、非法路径/语法和密钥 canary。
- [x] A2：新增 speedbench_identity.py（私有种子/HMAC），speedbench_sources.py（有界解析/连接定义/目录与映射），不接触真实数据写入。
- [x] A3：接入 Result/result_to_dict、CLI 来源/ID 过滤、worker 元数据回填与串行结果；保持旧字段。
- [x] A4：speedbench_db 增量表/列、稳定 ID 趋势/订阅汇总、覆盖率；旧 runs.raw 不变。
- [x] A5：Web 目录接口/受限参数/ID 切换，更新打包白名单与 AST 检查，兼容旧 API。
- [x] A6：本机只读映射核对、Python 3.9/3.12 专项/全量测试、安全 fixture 与迁移验收，写阶段报告并提交。

A 的上述核心链路已完成本机验收；不代表整份规格或四阶段全部完成。配置根目录 UI 选择、历史/收藏全量改用稳定 ID 留在 C；macOS/Linux 原生执行仍待 CI/对应环境。

各子步骤先运行新失败测试，再实现；核心命令：`python -m unittest tests.test_source_catalog tests.test_node_identity -v`，随后 `python -m unittest discover -s tests -v`。数据库/API 部分另外添加 test_source_history、test_source_api。

## B：任务、调度、事件与耗时

- [ ] B1：纯任务配置/候选选择/计时模块与测试；CLI/Web 参数一致、legacy 默认保持。
- [x] B2：有界任务状态/事件存储、snapshot/resync/增量读取/SSE，测试 seq、重连、终态和并发请求。
- [ ] B3：现有 worker 接入动态队列与取消；按模式控制精测/IP范围，不跨节点并发下载。
- [ ] B4：IPv4/IPv6 独立结果状态、预算与在途请求清理；provider single-flight/cache 保持。
- [ ] B5：task_runs/task_metrics 增量迁移、部分结果保存、真实下载字节和完整等待时间。
- [ ] B6：慢节点/失败/缓存 mock 基线，限定流量实际对照测试；记录覆盖率相同与不同模式的区别。
- [ ] B7：专项/全量/六格 CI 核心矩阵、取消和恢复验收，阶段报告与提交。

新测试按职责拆为 task_config、task_events、worker_schedule、phase_metrics、job_history、job_api。原 latency、probe、curl、phase2、cancel 测试必须继续通过。

## C：共享 UI

- [ ] C1：确认 A/B 合约并按目录/任务/渲染/历史/泄漏分离前端责任，不重新编写测量规则。
- [ ] C2：来源选择、目标/模式、流量提示、折叠高级参数、串行回退确认。
- [ ] C3：实时结果/任务中心/覆盖范围/有限推荐/详情，刷新续接。
- [ ] C4：稳定 ID 收藏/选择/趋势，旧收藏唯一匹配迁移与未确认项。
- [ ] C5：主题、键盘、focus/aria、长名/窄窗口、明确错误/空状态。
- [ ] C6：来源/历史/IP/泄漏/设置完整渲染操作、安全测试、截图证据和阶段提交。

源码 Web 保持静态文件可直接服务；构建/测试工具需要锁版本且不能成为 Python 运行依赖。

## D：桌面及交付

- [ ] D1：Tauri 外壳/权限/CSP、锁文件、共享静态资源与 Python 运行时打包清单。
- [ ] D2：后端私有握手/版本身份/动态 loopback端口、数据目录级任务所有权。
- [ ] D3：单实例/托盘/通知/关闭隐藏/退出取消/崩溃清理，限定自身进程树。
- [ ] D4：Windows EXE/便携/安装包与 WebView2 策略、无系统 Python实际验证。
- [ ] D5：macOS Intel/Apple Silicon与Linux打包/构建/运行证据，不以Windows结果代替。
- [ ] D6：升级/回退保留 raw、偏好/身份seed备份说明，旧独立Web协调。
- [ ] D7：签名状态、SHA256/许可证、README迁移/限制、前端与桌面CI任务。
- [ ] D8：完整规格逐项完成审计、交付报告。用户历史不因验证改变；不自动push/tag/Release。

## 实施状态与证据

- 初始代码 HEAD：32e21dd；书面规格提交：1000ff7。
- 初始全量基线：Windows / Python 3.14.7，539 tests，OK (skipped=5)。不能据此称升级代码通过或其他平台已通过。
- writing-plans/test-driven-development 技能当前未提供，采用本清单与 unittest 失败先行作为明确替代，不安装未知技能或依赖。
- 真实用户历史与 Downloads 安装不作测试 fixture。实验仅使用 TemporaryDirectory/ignored dist 路径。
- A 核心验收：见 ../reports/2026-10-03-origins-and-task-foundations.md。本机 40/40 运行节点来源 verified，worker 定义复核 40/40；A 完成时三种 Windows Python 全量 586 tests，OK (skipped=6)。
- B1 已实现模式配置/候选纯函数、CLI/Web 共享限制，计时模块已通过 fixture 但尚未接生产计时。历史提示与完整目标排序尚未接入，故整项不勾选。
- B2 已接 subprocess/HTTP/SSE/前端续接，单一所有权、seq、snapshot/resync、终态与并发请求通过本机 fixture；勾选不代表六格 CI 已执行。
- B3 已接新模式动态队列/限定 IP 范围/带宽串行；修复 --no-ip 混合成功遗漏、worker 启动失败丢失主探测数据。新模式全生命周期取消/部分结果保存尚待 B4/B5 验收，不勾选。
- B4/B5 已接独立出口发布、任务历史与实际字节，仍需全部阶段清理与迁移验收；B6/B7、C/D 尚未完整完成。详见 ../reports/2026-10-03-jobs-and-shared-ui.md。没有真实耗时提速结论、桌面安装包或 macOS/Linux 成功结论。
