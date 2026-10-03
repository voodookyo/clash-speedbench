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

- [x] D1：Tauri 外壳/权限/CSP、锁文件、共享静态资源与 Python 运行时打包清单。
- [x] D2：后端私有握手/版本身份/动态 loopback端口、数据目录级任务所有权。
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
- B1 已实现模式配置/候选纯函数、CLI/Web 共享限制和生产计时。下载目标已接同强身份近 7 天成功带宽的只读提示（每任务一次、250ms预算），不复制为当前成绩；完整目标策略和全部计时覆盖仍待验收，故整项不勾选。
- B2 已接 subprocess/HTTP/SSE/前端续接，单一所有权、seq、snapshot/resync、终态与并发请求通过本机 fixture；勾选不代表六格 CI 已执行。
- B3 已接新模式动态队列/限定 IP 范围/带宽串行；修复 --no-ip 混合成功遗漏、worker 启动失败丢失主探测数据。新模式全生命周期取消/部分结果保存尚待 B4/B5 验收，不勾选。
- B4/B5 已接独立出口发布、任务历史与实际字节，仍需全部阶段清理与迁移验收；B6/B7、C/D 尚未完整完成。此前检查点见 ../reports/2026-10-03-jobs-and-shared-ui.md；不构成真实耗时提速结论。
- D1/D2 本机实现与独立 Windows 包验证通过，见 ../reports/2026-10-03-desktop-packaging-checkpoint.md。桌面 alpha 已有 unsigned Windows NSIS/便携包，不能称 GUI 生命周期或 macOS/Linux 已验收。桌面/独立 Web/直接 CLI 同目录所有权及 CLI 异常部分留存已验证；不同目录不构成全局互斥。D3–D8 不勾选，待完整要求满足。
- 早期冻结源码 5c1f796：三版 Windows Python 全量各 725 tests，OK (skipped=7)，Rust 7 tests；Windows NSIS/便携包重新构建并解压验收，包含下载目标历史提示与 worker 清理重试。最新包及 SHA-256 见桌面报告；未 push 或替换用户安装。
- C/D 迁移体验继续推进：显式白名单偏好导出/预览/确认导入、收藏合并和未匹配 ID 提示、认证的数据路径/文件存在性指引；不自动导入 raw 或复制 seed。三版 Windows Python 全量各 737 tests，OK (skipped=7)。浏览器及模拟桌面偏好路径的实际交互通过，原生 GUI 与历史目录导入仍未验收，C4/D6 暂不整体勾选。
- bd444df 偏好迁移源码重新打包，66 项资源；Windows NSIS/便携 ZIP 完整性与解包启动通过，source_dirty=false，独立 SHA-256 与 provenance 一致。该包不包含此后的版本检查功能，不能混用源码与产物证据。
- D 手动升级：新增固定 GitHub 官方最新正式 Release 查询（只由认证点击触发）、严格响应/版本校验、内存 single-flight/TTL、失败无法确认和 alpha 不自动降级；不自动下载/安装/更新，也不检查 alpha feed。共享浏览器及模拟桌面设置页面宽窄窗口实际交互通过，未联网查真实 Release。首次 Python 3.9 回归发现无流 HTTPError.close 差异，修复后专项通过；最终全量与新包验证完成后补记，不将 D6/D7 整项勾选。
- 手动升级源码最终全量：Windows Python 3.14.7 / 3.9.25 / 3.12 各 750 tests，OK (skipped=7)，并行耗时分别 38.457s / 37.962s / 38.436s，不作为提速证据。版本检查新增 13 项 fixture 测试；延迟本地版本响应不能覆盖用户刚检查的结果。`node --check` 与 `git diff --check` 通过。原生平台及实测性能门仍保持未验收。
- 最新冻结源码 e73eb9b：68 项资源，Rust 7 tests；NSIS/便携 ZIP 构建、解包原生完整性/篡改、无系统 Python启动、认证接口、新设置 JS 哈希、本地版本无自动外联及原 raw 保持验收通过。两包均 unsigned/source_dirty=false，独立 SHA-256 与 provenance 一致，见桌面报告。文档后续提交不改该冻结源码，不 push/安装覆盖。D3–D8 和整体四阶段仍未完整验收。
- 隔离 UI 夹具退出补充：等待在途 HTTP handler 释放文件后再删临时目录，新增实际 HTTP/file-handle 回归并复验服务 exit=0。最终三版 Windows Python 全量各 751 tests，OK (skipped=7)；所有包内生产模块/UI/README 与 e73eb9b 哈希一致，最新包不需重建。仅清理明确拥有的人工夹具残留，不改用户数据。四阶段仍 active，下一步是配置根目录选择与取消预算等剩余实现，不因本机包验证完成而整体勾选。
- f0851bf/f1b055c：配置根目录选择子功能完成本机验收：只读固定布局，preview/confirm/恢复自动发现；session-only，不入偏好/日志/任务历史，冻结私有快照让 Controller/目录/worker 一致，失效不回退；配置/任务 revision 竞争拒绝，旧目录响应不覆盖新选择，任务忙碌锁定。新增 21 项 fixture；三版 Windows Python 各 772 tests，OK (skipped=7)，Rust 7 tests。1280x720 / 760x900 页面真实交互、错误态/锁定/刷新/控制台通过，非原生 GUI。冻结 f1b055c 的 70 项资源 Windows NSIS/ZIP 构建解包验证通过，source_dirty=false/unsigned，独立 SHA-256 与 provenance 一致；见桌面报告。CLI/历史目录迁移、取消资源预算、同覆盖实测及 C/D 全矩阵仍待实现验收，四阶段保持 active，不 push 或覆盖用户安装。
- 1243345：登记 worker 清理从逐个等待改为有界并发（最多 16），统一复核／重试、持续失败传播；GC 不再自动删除未确认 reaped 的配置。code 3 区分清理失败，后端保留已接收 partial 并 failed，阻止同 session 新任务/改 root。新增 9 回归，三版 Windows Python 全量各 781 tests，OK (skipped=7)，Rust 7 tests；冻结 1243345 的 70 项资源 Windows NSIS/ZIP 重新构建，独立哈希及解包验收通过，并在实际包内 Python3.14.8 运行清理 fixture，未登记进程保持存活。不宣称全阶段硬取消预算或真实提速；直接 CLI 部分历史／所有权与 B6/C/D 完整矩阵仍待实现验收。增强的验收脚本／报告不改冻结产物，不 push／覆盖安装。
- 3ff2443／8548d62：同目录直接 CLI 所有权协调已验收；后端子任务须实际父 PID＋实例＋canonical history＋内核锁匹配，经私有 stdin 引导，writer lease 阻止父崩溃时重启竞争，EOF 取消。正常完成管道的 BufferedReader 退出崩溃经真实包复现／失败先行修复，异常路径 Intelligence 取消待执行查询、等待在途缓存写入才 unlock。新增 16 项回归，三版 Windows 全量各 797 tests，OK (skipped=7)，Rust 7 tests；冻结 8548d62 Windows unsigned NSIS/ZIP 构建和独立解包私有后端→CLI／同目录拒绝／原 raw 不改验收通过，哈希见桌面报告。旧被占用 build staging 仅移入 ignored quarantine 保留，无用户数据改动。未 push／覆盖安装。直接 CLI 异常部分历史仍是下一步，B/C/D 全量矩阵和其他平台原生验收继续保持未完成。
- 8755c87：CLI 取消／异常按本任务逐节点冻结快照保存 partial，独立于 stdout；已完成下载轮次／计数／IPv4 不被随后中断抹掉。导出失败不掩盖 code 130/3/1，CSV 失败仍尝试 JSONL，两者失败明确未落盘，已提交历史不重写／重复。21 项新增回归；三版 Windows 全量各 818 tests，OK (skipped=7)，Rust 7 tests；冻结 clean 8755c87 Windows NSIS/ZIP 构建及包内实际后端→CLI code 3 留存 JSONL／SQLite raw／failed partial task 验收通过，独立哈希见报告。不 push／覆盖安装，不宣称全阶段取消预算或性能提升。下一步为 B 的剩余配置／计时／资源预算、同覆盖 fixture 与 C/D 迁移／原生验收；完整升级保持 active。
- 52092d0：串行 delay／warmup／download／restore／provider wait 与完整报告 summary spans 补齐，worker 独立兜底计数；中断请求计 attempt 而不补猜字节，main pool 已完成探测统计不会随后清零。统一已报告样本计数，直接 CLI 不依赖 progress 才计 warmup／multi；warmup 中断留 partial。新增 11 回归／82 专项；三版 Windows 全量各 829 tests，OK (skipped=7)，Rust 7 tests；冻结 clean 52092d0 NSIS／ZIP、独立解包指标／failed task／原 raw 不变验收和哈希核对通过。没有 schema／依赖／测量方法变化，不冒称真实提速。下一步补单节点 probe 中断样本、provider/cache 计数／五个性能里程碑与全阶段资源预算；B5/B7、C/D 完整矩阵仍不勾选，目标 active。
