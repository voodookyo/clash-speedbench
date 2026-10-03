# 桌面打包与后端生命周期检查点

本报告是阶段 D 的本机检查点，不代表 A–D 完整验收。没有 push/tag/Release，没有安装到 Downloads，没有修改真实 Verge、用户历史、系统代理或调用付费 API。

## 代码与产物

- 473ebf6：数据目录内核锁、桌面私有后端、非敏感偏好持久化、受限外链请求及共享界面环境标识。
- ab7467b：Tauri 2 客户端、固定运行时、依赖锁、完整性与许可证、原生包配置及桌面 CI。
- c4d9d10：解压后的 Windows 产物独立验收、按源码修订隔离产物、澄清本地写 token 与 provider Key 的区别。
- 6b86ced：生产候选选择接入同强身份近 7 天带宽提示，不建库/迁移/复制为本次成绩；过期、未来时间、弱身份、旧格式或查询预算超时降级。
- 5c1f796：worker terminate/kill 后确认退出再移除临时配置，清理失败可重试；单独安全异常不触发串行测量回退。
- bd444df：显式白名单偏好导出/预览/确认合并，未匹配收藏保留待确认；认证的数据位置/文件存在性指引，不自动导入历史或复制 seed。
- e73eb9b：认证点击触发固定 GitHub 正式 Release 查询、内存 single-flight/TTL、版本比较、失败无法确认；不自动下载/安装/降级，不检查 alpha 更新；包验证覆盖新设置静态模块和本地接口。
- f0851bf：自定义 Verge 根目录、控制器/来源/worker 一致传递，接受任务冻结私有快照，失效不回退、任务忙碌不允许改目录。
- f1b055c：共享设置的预览/确认/恢复自动发现、过期响应保护、资源清单/CI/文档和测试。
- 1243345：登记 worker 并发清理、显式目录生命周期，持续失败不能被动态 shard 吞掉；CLI 专用退出码／后端 failed 和后续任务阻断。
- 最新产物固定源码：`1243345224e718e5b5b4fba76edd71853e93a345`，构建时 `source_dirty=false`。早期 c4d9d10、5c1f796、bd444df、e73eb9b、f1b055c 包保留，只作对应修订的历史证据，不包含此后的功能。
- 桌面版本 `1.1.0-alpha.1`，运行时 CPython `3.14.8`，平台 `windows-x86_64`。核心稳定版入口与 legacy 测速默认未改为桌面 alpha。
- 当前包目录 `dist/desktop-artifacts/1243345224e7/`，二进制未提交 Git；其他修订的包不是本次最新交付证据。后续报告／独立验收脚本提交不改变此包的冻结源码。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| Clash SpeedBench_1.1.0-alpha.1_x64-setup.exe | 12758867 | c953e82cd1f76534238fedc4debdfa745aa3303c0705daebaecbc06945bd7e66 |
| Clash-SpeedBench-1.1.0-alpha.1-windows-x86_64-portable.zip | 15735831 | fa3cb4ae809e69a06c35d5b9c8f49117198d63b9c38f81f95dc77430ad46b88d |

SHA-256 已额外通过 PowerShell `Get-FileHash` 与 build-provenance.json 核对。包 **unsigned**，自动更新禁用；WebView2 是系统组件，不是单文件免运行时承诺。

## 本阶段变更文件组

- 后端：speedbench_desktop.py、speedbench_owner.py、speedbench_preferences.py、speedbench_releases.py、speedbench_web.py、speedbench_jobs.py。
- 共享界面：web/app.js、web/index.html、web/tasks.js、web/preferences.js、web/releases.js；区分浏览器与 WebView，受控 DNS/Release 外链，不把 WebView 检测当作 Chrome/Edge 审计。
- 桌面：desktop/src-tauri/src/backend.rs、main.rs、Cargo.toml/Cargo.lock、Tauri 配置、capabilities、NSIS hooks、原生平台配置。
- 工具与依赖：desktop/prepare_resources.py、license_notices.py、package_windows.py、verify_windows_package.py、collect_artifacts.py、runtime-lock.json、package.json/package-lock.json。
- 文档/CI/打包：README.md、desktop/README.md、.github/workflows/desktop.yml、test.yml、release.yml、build_app.sh、.gitignore；专项 tests。

## 实现边界

Tauri 加载同源共享 UI；测速仍由标准库 Python、系统 curl 和隔离 Mihomo worker 执行，没有 pip 运行依赖。70 项资源逐个校验，编译进原生程序的清单是信任锚；不能通过同时替换源码和外部清单绕过校验。

后端只绑定 127.0.0.1 动态端口。私有 stdin/stdout 启动协议校验 nonce、实例、PID、版本；nonce/write token 不进入 argv、URL、日志或 localStorage。公开身份 JSON 不含凭据。既有同源 HTML 的 sb-token meta 包含本实例本地写 token，这是鉴权所需，不是 provider/controller API Key。后者不会返回前端。

数据目录内核锁在同步/迁移前取得；旧锁元数据不等于活跃所有权。桌面与独立 Web 使用同一目录时互斥。尚未覆盖直接 CLI 所有写入协调；不同数据目录的实例不是全局互斥。

Windows 自有 backend 在接收启动许可前加入 Job Object；关闭对象只回收所属子树。POSIX 实现独立进程组，但未做原生崩溃/信号实测。退出先取消并等待，超时明确失败并限定自身子树，不按进程名杀用户 Mihomo。

前端无通用 shell/filesystem IPC。外链为固定枚举；系统浏览器审计仅允许当前后端的 loopback 泄漏页，不携带 token 查询参数。桌面通知默认关闭，只有脱敏计数摘要。安装器发现运行实例或无法确认占用时中止，不强制关闭程序。

本阶段没有新增 SQLite 表/列：既有 runs.raw 保留；环境标识进入 leak_audits 原有 details_json；非敏感偏好进入私有 ui-preferences.json，不进历史/缓存。IP provider、评分、TTL/single-flight 规则未更改。

显式偏好迁移仅枚举白名单，限制 schema/大小/Unicode，先预览再确认，收藏合并且未匹配项保留。桌面单次原子保存，浏览器失败回滚仅触碰的白名单键；Key/token/路径/历史/seed 不进入导出。旧版没有导出按钮时，先备份并在同一浏览器/host/port 加载新版共享 Web，再手动导出；不读任意浏览器 profile。数据指引只检查本实例及固定源码目录的文件存在性，不查看原始内容或自动导入。

正式 Release 检查遵循 GitHub 官方 REST 文档：https://docs.github.com/en/rest/releases/releases#get-the-latest-release。固定 HTTPS endpoint，无凭据/自定义 URL，不跟随重定向，响应上限 256KiB、socket timeout 5s（不是硬总时限承诺）。只信任正式语义版本及正确仓库 tag 页面；远程 body/assets 不返回。成功内存缓存 15 分钟，失败 1 分钟；单一在途请求，不持久化。失败绝不当作 current；alpha 高于 stable 时不降级/不声称已是最新稳定版。HTTP403 包括限流和拒绝访问。校验和不是签名；系统 proxy/TUN 仍可影响网络访问，完整性/签名验证和更新安装仍由用户手动进行。

## 已执行验证

- Windows Python 3.14.7：`python -m unittest discover -s tests -v`，750 tests，OK (skipped=7)，38.457s。
- Windows Python 3.9.25：同一全量命令，750 tests，OK (skipped=7)，37.962s；使用既有 dist/controller-fix-validation 中的运行时，不新安装 Python。
- Windows Python 3.12：同一全量命令，750 tests，OK (skipped=7)，38.436s。三轮并行，因此耗时不是性能比较证据。
- 先前 worker 异常类 reload 隔离修复保留；版本检查新增 13 项 fixture 测试。首次 Node 夹具因 const window 的 TDZ 失败，只修夹具；首次 Python 3.9 全量因无响应流 HTTPError.close 差异报错，改为仅关闭存在的实际流，保留流关闭断言；随后以上最终全量均通过，没有跳过新增测试或放宽接口。
- Rust：`cargo test --locked --offline --manifest-path desktop/src-tauri/Cargo.toml`，7 tests 通过（2.27s）；实际临时中文/空格/emoji 路径、无 Python PATH、重复目录所有权、原 raw 保持、Job Object 子孙进程回收且不影响独立进程。
- Windows Tauri Release/NSIS 构建成功；`python desktop/package_windows.py`、`verify_windows_package.py`、`collect_artifacts.py` 成功。
- 独立产物验收实际解压 ZIP，再验证原生完整性/篡改、内置 Python 启动、私有握手、Origin 拒绝、两轮起停、偏好跨重启、旧 raw 不改；新增本地版本无自动查询、任意 Release 参数拒绝、认证数据指引、两份新 JS 与 manifest 哈希一致。没有创建窗口，因此不把它称为 GUI 验收。
- 浏览器隔离 fixture 泄漏页：实际截图、浏览器环境边界提示、控制台无相关 error/warn；未点击 STUN/外部 DNS 服务，没有真实环境泄漏结论。
- 设置页 CUA 已选 IAB 的 Playwright/DOM 路径（Browser plugin/browser skill 未列出，不安装浏览器依赖）：8965/8966 偏好导出/预览/确认、刷新保留、异常字段拒绝；8967 模拟桌面 alpha 对旧 stable 不降级、8968 浏览器 timeout 显示无法确认且按钮可重试。fixture 从不查询真实 GitHub/controller/provider。页面 identity、非空、无错误 overlay、console error/warn、交互与截图均核对；1280x900 / 760x900 无横向溢出，临时 viewport override 已恢复。仅模拟桌面偏好路径，不是 WebView/原生 GUI 证明。
- 截图在仓库之外：`.codex/visualizations/2026/08/29/01a04bf1-7164-7b20-9bf7-4fa9066c62f5/` 下的 speedbench-preferences-desktop-qa.png、speedbench-preferences-narrow-qa.png、speedbench-release-alpha-qa.png、speedbench-release-timeout-narrow-qa.png。没有将截图或 fixture 数据加入发行包。
- `git diff --check` 通过。桌面五平台与原六格 Python CI 已配置，但未推送/运行。

### 退出夹具补充验证（不改变冻结发行源码）

清理隔离 UI 服务时，一个夹具的 TemporaryDirectory 删除失败：ThreadingHTTPServer 默认 daemon handler 仍持有临时 SQLite 文件。测试专用 FixtureServer 改为非 daemon／block_on_close，在数据目录移除前等待在途请求。新增实际 HTTP + 打开文件句柄回归，Windows Python 3.9/3.14 专项通过；重新打开隔离页面并退出，辅助进程 exit=0，无残留请求错误。只删除此前明确核验的两个人工夹具文件（fixture.db、ui-preferences.json）和其空临时目录，不触碰用户历史。

补充后最终源码全量：Windows Python 3.14.7 / 3.9.25 / 3.12 各 751 tests，OK (skipped=7)，并行耗时 37.455s / 37.897s / 37.587s。所有打包 Python 模块、共享静态资源及包内 README 与 e73eb9b 清单 SHA-256 逐个复核仍完全一致；这次仅改测试夹具、测试与报告，不重新打包或冒称原生 GUI 验收。已停止本轮临时服务并关闭本轮验证页，恢复 viewport。

### 自定义配置根目录与最新产物验收

设置页新增 session-only 根目录，通过同源鉴权接口先预览布局再确认应用；输入变化使旧预览和确认失效，运行/取消/清理期间禁止更改。冻结根目录仅通过私有子进程环境传递，控制器与目录及 worker 同源；不进入 argv、任务快照/SQLite/JSONL、localStorage 或偏好导出，日志脱敏。显式无效目录、控制器不匹配或 CLI --config-file 冲突在业务前失败，不回退。自动发现恢复忽略本次启动环境覆盖；重启仍按 SPEEDBENCH_VERGE_ROOT 启动变量选择。只做固定布局 metadata 校验，不是内容/连接验证；没有任意文件读取 API 或原生目录选择器。身份 namespace 跟随根目录，不删除旧历史/收藏，不强行匹配旧 ID。

- 最新全量 `python -m unittest discover -s tests -v`：Windows Python 3.14.7 772 tests（30.504s）、3.9.25 772 tests（29.725s）、3.12 772 tests（30.415s），各 OK (skipped=7)。三轮并行，时间不是测速提速证据。
- 新增 21 项配置目录/UI 测试：非法/远程/链接/缺失布局、重启初值失效、Controller/worker 一致路径且不回退、CLI 冲突、Host/Origin/token、忙碌锁定、配置 revision 竞争、任务私有快照及 SQLite 脱敏、切换竞争不写旧控制器、旧目录响应不覆盖新来源、预览/显式确认/模糊写失败不重发。
- 新夹具初次失败分别源于 Windows 8.3 路径与 canonical 预期差异、mock 误替换共享 threading.Thread 导致 HTTP handler 不启动、SQLite context manager 未关闭读连接。修正测试边界/显式 close 后通过，未放宽生产校验或跳过新增测试。夹具 stdout 显式 UTF-8，中文/emoji 布局可见，退出 exit=0；未修改真实 Verge/数据。
- 8969 隔离页面（模拟 WebView 偏好，不是原生 GUI）：真实点击中文/空格路径预览→确认对话框→应用→运行 fixture 任务锁定控件→无效路径报错禁用应用→显式恢复自动发现→刷新状态正确。1280x720 / 760x900 无横向溢出，页面 identity/非空/无 overlay/相关 error、warn 为零均核对。新截图为仓库外同目录的 config-root-desktop.png、config-root-narrow-invalid.png。浏览器页已关闭，临时 viewport 已恢复，fixture 端口已停止。
- `node --check web/app.js`、`node --check web/config-root.js`、`git diff --check` 通过。Rust 7 tests 全部通过（2.24s）。
- `prepare_resources.py --target windows-x86_64` → Tauri locked/offline unsigned NSIS → `package_windows.py` → `verify_windows_package.py` → `collect_artifacts.py --target windows-x86_64` 全部成功；独立 ZIP 解包再运行原生 integrity/tamper、内置 Python/私有握手、Origin、两轮起停/偏好、原 raw 保持，新增认证根目录接口默认 auto/session、非法目录拒绝/不回显以及第三份设置 JS 清单哈希核对。70 项资源，source_dirty=false，两个包 SHA-256 经 Get-FileHash 单独复核与 provenance 一致。

配置选择子功能已本机验收，不表示 C 全页矩阵、D 原生 GUI 或 A–D 完整规格已完成。

### 登记 worker 并发清理与失败传播

中断注册表按句柄去重，最多 16 路 cleanup 并发，join 所有尝试后统一报错，不按进程名杀进程。原每进程 3s terminate＋3s kill 等待重叠，不是完整取消／OS／文件删除的硬 deadline 承诺。Phase 1 结束时统一复核并重试每个登记 worker，动态 shard 不再吞掉持续清理失败并继续 Phase 2。临时配置改为明确所有权目录；失败 reaping 后 GC 不会擅自删除它。CLI 清理失败返回专用 code 3、不显示原始异常、不回退串行；后端即使收到取消也标记 failed，保存已接收的部分结果，并在此 session 拒绝新任务或根目录变更。不能据此称直接 CLI 已自动保存完整部分历史。

- 失败先行证据：旧 TemporaryDirectory 在失败 reap 后 GC 真实删除配置（ResourceWarning／断言失败）；新显式目录通过保留断言。旧 CLI 未处理 cleanup error／后端误标 cancelled 由回归复现；修复后专用退出码、脱敏、partial/failed、后续请求 409 均通过。
- 新增 9 项回归：16 个 worker 在任一等待完成前全部进入 stop；异常收集／重复登记／空组；GC 保留；真实两进程清理且另一 fixture 进程仍存活；动态清理失败不跑下载；CLI 不串行回退；取消时失败传播／部分结果与后续任务阻断；root 清理失败锁定。没有真实 Mihomo、网络或用户进程参与。
- 最新三版 Windows 全量均 `781 tests, OK (skipped=7)`：Python 3.14.7 24.395s，3.9.25 24.321s，3.12 23.887s；并行执行，非性能对照证据。专项 114 tests 和后续 49 tests 通过；Rust 7 tests（2.18s）通过。
- 冻结 1243345 再构建 70 项资源 Windows NSIS／ZIP，locked/offline、unsigned/source_dirty=false；解包原生完整性、内置 Python、Origin、起停、原 raw／设置静态哈希全部通过，SHA-256 独立核对如上。
- 进一步在解压包的 CPython 3.14.8／app 模块中实际调用 stop_workers，两个登记的隔离 Python 子进程被 reaped，第三个未登记 fixture 进程仍活着；最终仅通过持有句柄回收测试进程。该独立脚本在冻结产物之后增强，不修改包内代码或校验和；不冒称真实原生窗口／Mihomo cancellation 验收。

仍待全生命周期取消预算、直接 CLI 部分历史与所有权协调、真实同覆盖性能和其他平台原生验收。B4/B7、C/D 不能据此全部勾选。

## 仍未完成

- Windows 原生窗口/WebView2 缺失路径、托盘/通知、重复启动、休眠、活跃任务退出、安装器交互与升级验收。当前会话原生 GUI 自动化不可用，不用构建成功或浏览器截图代替。
- macOS Intel/Apple Silicon 和 Linux 原生构建、安装、运行、信号及依赖验收；只有配置和有界资源 fixture，不能称兼容性已通过。
- 首次旧历史目录选择/安全导入完整流程、直接 CLI 与目录所有权协调。显式非敏感偏好迁移及正式版本检查已实现并按上述边界验证，但不代表完整数据导入/升级安装与回退交互验收。
- B 的完整目标策略、全阶段取消/清理预算与剩余失败传播、同覆盖性能实测；下载历史提示已接入并通过 fixture，但不构成真实提速证据。C 的更多前端职责拆分和完整错误/页面矩阵；配置根目录选择本机子功能已验收，不代表 C 整体完成。
- 整体规格逐项完成审计和最终交付。因此不标记完整升级已完成，也不建议覆盖现有稳定安装。
