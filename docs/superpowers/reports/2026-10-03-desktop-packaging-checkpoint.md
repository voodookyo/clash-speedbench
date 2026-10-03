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
- 3ff2443／8548d62：直接 CLI 与私有委派子任务目录所有权、EOF 取消、正常完成管道退出及缓存写入者回收。
- 8755c87：CLI 取消／异常部分结果留存、报告失败不掩盖原退出码、包内 JSONL／SQLite／任务状态独立验收。
- 52092d0：串行阶段计时与所有已报告下载样本计数、取消尝试计数、主实例已完成探测计数留存。
- a416425：逐次 probe 留存、独立 main／worker／serial 元数据、取消停止未开始的队列、共享详情说明和包内取消任务验收。
- 3bfead6：provider/cache 实际调用计数、独立最终等待时间、worker 就绪启动数、任务接受时钟的五个首次里程碑与历史持久化。
- 最新产物固定源码：`3bfead6d0daf49924535548b69ef96157be273e3`，构建时 `source_dirty=false`。早期 c4d9d10、5c1f796、bd444df、e73eb9b、f1b055c、1243345、8548d62、8755c87、52092d0、a416425 包保留，只作对应修订的历史证据，不包含此后的功能。3ff2443 包未通过新增正常完成验收，不作为可交付包。
- 桌面版本 `1.1.0-alpha.1`，运行时 CPython `3.14.8`，平台 `windows-x86_64`。核心稳定版入口与 legacy 测速默认未改为桌面 alpha。
- 当前包目录 `dist/desktop-artifacts/3bfead6d0daf/`，二进制未提交 Git；其他修订的包不是本次最新交付证据。后续验收夹具修正／报告提交不改变此包的冻结源码。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| Clash SpeedBench_1.1.0-alpha.1_x64-setup.exe | 12769997 | e6babb37620a570cf7c2ba568c11a5eedae180f8f25010dfde4338a16104c507 |
| Clash-SpeedBench-1.1.0-alpha.1-windows-x86_64-portable.zip | 15750950 | 91dfc263828c6cdd6d89e0c4e5557f8dfc1082924a65973366af8c30e0b546e5 |

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

数据目录内核锁在同步/迁移前取得；旧锁元数据不等于活跃所有权。桌面、独立 Web 和直接 CLI 使用同一目录时互斥，CLI 同时协调显式历史与 identity home。后端子任务使用实际父进程绑定的私有管道及独立 writer 锁，异常路径的查询池在释放目录锁前取消未开始任务并等待在途缓存写入；不同数据目录的实例不是全局互斥。

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

### CLI／后端子任务目录所有权最终验证

- 新增 16 项回归，直接 CLI 在 Controller／身份／缓存／历史写入前核验目录；默认历史遵循 SPEEDBENCH_HOME，显式历史与 home 不同则同时持锁。闲置 Web／桌面也阻止同目录独立 CLI；不是全局多目录锁。没有新增 SQLite schema、pip 依赖或持久化密钥。
- hidden 子任务参数仅标识入口，不能授权；实际父 PID、私有实例、canonical history 路径、owner metadata 与活跃内核锁均须一致。私有 stdin 有 32KiB／5s 引导限制，帧不进 argv/env/日志/历史；环境中的 HOME 为 canonical 父目录，防止子进程 cwd 导致相对路径误解。子任务 writer 锁留到报告/清理结束，父 owner 释放后仍阻止新后端，EOF 请求取消；不删除锁 inode、不把旧 PID 当活跃实例。
- 私有管道异常时关闭并等 8s；不能确认退出则保留原句柄／running、标记 cleanup incomplete 并阻断新任务，由持有句柄的 watcher 收尾，不按 PID 名称杀进程、不将超时称为清理成功。
- 真实包验收先发现正常完成时父管道尚打开，daemon BufferedReader 阻止解释器退出并导致 Fatal Python error。新增真实子进程回归在修复前失败，改原始 descriptor 读取后通过；不以仅 EOF 退出夹具代替正常完成。另补异常路径查询池生命周期：取消未执行查询、等待在途缓存写入后才释放 CLI lease，保持网络／IP 计分逻辑不变。
- `python -m unittest discover -s tests -v`：Windows Python3.14.7 797 tests，28.711s；3.9.25 797 tests，28.687s；3.12 797 tests，27.457s，均 OK (skipped=7)。本轮 27 项专项也通过。三版并行，不是测速性能对照。
- 冻结 8548d62，70 项资源、bundled CPython3.14.8；Rust 7 tests（2.23s）通过。locked/offline unsigned NSIS／ZIP 构建成功；解压后实际调用包内后端 run_benchmark→CLI main 私有管道，fixture 测量体正常退出、直接 CLI 同目录拒绝、原 raw 保持，完整性/篡改、Origin、两轮起停、设置静态哈希与 worker 清理隔离均通过。独立 Get-FileHash 与 provenance 一致。
- 重建时旧 staging runtime/python.exe 被系统占用且无法确认占用进程身份，未猜 PID／终止它。仅核验 workspace 内明确构建 resources 目录后移动至 ignored `dist/desktop-staging-quarantine/3ff2443-resources/` 留存，重新建立 staging；没有删除用户数据或原安装，也不称旧系统占用已清理。
- 当前 Git 生产提交 3ff2443／8548d62；本报告后续提交不改变冻结包。未 push、tag、Release、安装覆盖，未测试真实第三方 API 或带宽。

此项本机验收不代表 B 全生命周期取消预算、CLI 异常部分历史或完整 C/D 已完成。下一步仍是直接 CLI 异常部分结果保存。

### CLI 取消／异常部分结果留存

- 8755c87：每任务内存 journal 在发布事件前冻结快照，不依赖 stdout 或 Web 父进程存活。按本次冻结目录的唯一 runtime name 去重（不是跨历史身份）；早期出口事件保留主探测的成功／失败计数。串行和 worker 每轮保存已完成样本，第二轮中断不丢弃第一轮、已报告下载字节和 connect；IPv6 中断不丢弃已完成 IPv4。未完成指标不是网络不可达，未知 IP 仍 N/A。
- CLI lease 内等待／关闭登记 Intelligence 写入者后尝试导出；取消返回 130、持续 worker 清理失败 3、其他异常 1。部分报告失败不能覆盖原退出码；关闭的事件通道不打断清理和留存；无自动切换或新测量。串行恢复原组／模式后才导出。Scoring／原测速方法和第三方配额策略未重写。
- CSV 失败仍尝试 JSONL，`--no-history` 保持有效；两者失败明确提示未持久化。已提交历史不重写／不因稍后中断重复追加。没有 SQLite schema 变化；可选 task.status 只接受固定状态，新 partial 元数据增量导入，旧 legacy 成功行结构保持、旧 runs.raw 字节与重复导入均通过。
- 失败先行复现：Phase 1 中断无报告、cleanup code 3 无历史、第二轮丢样本、导出失败误标保存，以及串行报告异常将 130 改为 1。新增 21 项回归（专项 57 项与最后 partial 18 项均通过）；无真实节点／付费 API。
- 最终 `python -m unittest discover -s tests -v`：Windows Python3.14.7 818 tests／34.726s、3.9.25 818／33.677s、3.12 818／34.684s，均 OK (skipped=7)。日志在 ignored `dist/partial-history-python*-tests.log`；三版并行，不作测速提速证据。`git diff --check`、验收脚本 py_compile 通过；Rust locked/offline 7 tests／2.29s。
- 冻结 8755c87，70 项资源、包内 CPython3.14.8、source_dirty=false，Windows unsigned NSIS／ZIP locked/offline 构建成功。独立解包实际调用包内后端→CLI 私有通道，fixture code 3 后原 JSONL 不改、部分行追加、SQLite raw 精确导入、task failed/partial 与 run 关联通过；同时原有 integrity/tamper、私有引导、Origin、两轮起停／偏好、worker 清理隔离和独立 CLI 同目录拒绝验收通过。两包 Get-FileHash 与 provenance 独立一致。
- 修改文件：clash_speedbench.py、speedbench_progress.py、speedbench_workers.py、speedbench_web.py；test_cli_partial_history、test_cli_ownership、test_ip_intel_integration、test_job_api、test_progress_stream；desktop/verify_windows_package.py、README、desktop/README 和本报告／实施清单。未 push/tag/Release、覆盖用户安装或修改真实 Verge。

这是失败留存子功能验收，不是完整原子磁盘提交／全生命周期硬取消预算保证。强制 kill、磁盘不可写或连续再次中断仍可能无法落盘；已提交行不追溯改为后来中断状态。B5/B7 与 C/D 仍不整体勾选。

### 串行／worker 测量观测计数与阶段覆盖

- 52092d0：DownloadCounter 只负责同节点的已调用请求尝试、返回成功和 curl 已报告字节，使用锁保护多流 callbacks。开始下载即冻结 partial 快照；warmup 中断不误标未选精测。单流／warmup／多流均沿原函数／顺序执行，直接 CLI 也计算所有已报告样本字节；中断流的未知字节不以预算补齐。没有跨节点并发下载、采样大小／时限／测速源／评分更改。
- 串行路径补 delay、warmup、download、restore、provider wait；summary span 覆盖整个成功或部分报告（包括 CSV／JSONL 和错误），不是只计算 append_history。worker fallback probe 单独计数，main delay 已完成节点的统计逐次累计，随后池取消不清空为零。字段仍为既有白名单的 duration_ms/attempts/successes/bytes，没有 schema 或依赖新增。
- 失败先行：串行缺少 spans、取消的第二轮漏 attempt、无进度通道的 CLI 漏 warmup／multi 字节、多流中断丢已完成流、warmup 中断无 partial、池中断清空已完成探测统计，均用 fake curl/controller 与临时数据复现并修复。新增 test_measurement_accounting 11 项，82 项专项通过；同节点多流通过 barrier 证明四条都开始后再打断一条，不用调度巧合宣称全部尝试。
- 初次 3.9／3.12 全量暴露新夹具对快速失败报告强求真实耗时 >0：较粗 monotonic 时钟可以合法测出 0。夹具只替换 progress 模块的时钟，以确定性正向步进验证 finally span；没有给生产耗时加虚假最小值，包验证检查存在且非负。
- 最终 `python -m unittest discover -s tests -v`：Windows Python3.14.7 829 tests／34.304s、3.9.25 829／33.816s、3.12 829／33.864s，均 OK (skipped=7)。日志在 ignored `dist/accounting-python*-tests.log`，三版并行，不是同覆盖性能对照。git diff --check、验收脚本 py_compile 通过；Rust locked/offline 7 tests／2.30s。
- 冻结 clean 52092d0，70 项资源／包内 Python3.14.8；Windows unsigned NSIS／ZIP locked/offline 构建、独立解包验收通过。真实包内后端→CLI failed fixture 除原 raw／partial task，还检查 250000 已报告下载字节、1 attempt／1 success、summary metric 持久化到 SQLite。原有完整性／篡改／Origin／重启／偏好／worker 隔离／CLI 所有权均通过。独立 Get-FileHash 与 provenance 一致。
- 文件：clash_speedbench.py、speedbench_progress.py、speedbench_workers.py、tests/test_measurement_accounting.py、desktop/verify_windows_package.py、README、desktop/README、本报告与实施清单。未 push／tag／Release、安装覆盖，未使用用户网络或真实第三方 API。

这不是 B5／B7 的完整出口：单个多次 probe 中途取消的已完成样本、provider/cache 计数和五个性能里程碑还须补齐；原生传输与全阶段取消资源预算、相同覆盖真实测速对照仍待实现／验收。标准 CLI 的阶段指标不另写入旧 legacy raw；Web／桌面任务事件沿既有 task_metrics 入库。并发累计 span 不可直接相加为总等待时间。

### 逐次应用层探测留存与取消队列

- a416425：每次返回后冻结 completed 样本；主实例、worker 兜底、串行三条路径分别保留 `probe_sources`。已调用次数与完成样本分母不同，未返回的取消请求不补造成功／失败。主字段采用当前有效路径，不能把主实例 100% 失败与 worker 50% 失败简单合并；所有原有效延迟、jitter、探测次数和测速方法保留。
- 失败先行覆盖：串行 CLI 组内中断、主实例池部分节点、worker 兜底中断／完成、通道关闭、未开始取消、非有限返回、子线程取消和 queued nodes。停止标志在失败线程重新领取队列前设定，取消后不启动排队节点或下一 sample，但在途 controller 请求仍等待返回／超时后 join。一次 callback TypeError 不得导致再次实际探测。冻结 runtime name 作为本任务 legacy 行键，修复 Shadowsocks／ss 拼写造成重复行，不改 stable ID 或切换权限。
- 14 项新 probe 回归＋2 项详情 JS 回归；71 项专项通过（1.191s）。Windows Python3.14.7、3.9.25、3.12.10 全量各 **845 tests，OK (skipped=7)**，耗时 40.265／42.067／42.481s，三轮并行，不能作为测速性能比较。日志在 ignored `dist/probe-partial-python{314,39,312}-tests.log`。`node --check web/app.js`、`git diff --check` 通过。Rust 7 项通过，test 2.21s。
- 前端验收目标：`#/nodes` → 点击 fixture 节点名称单元格 → 展开独立路径 → 刷新后再次展开。Browser plugin/browser skill 未列出，使用已提供 CUA IAB `tab.playwright`／DOM／console／viewport／screenshot，不安装依赖；fixture API 全为合成数据，不读取真实历史／controller／provider。URL 127.0.0.1:10652、标题、非空、无框架 overlay、error/warn=[]、完整／部分／已调用／完成分母与非 ICMP 说明均通过。1280×1000 和 390×844 检查后恢复 viewport 并关闭临时 tab/server；窄表格仍需横向／纵向滚动，不能称完整移动适配通过。IAB full-page 截图出现错误布局，DOM rect 与 viewport 截图正常，本次仅采用 viewport 截图证据，不据此修改产品 CSS。
- 截图未入仓库／包：`C:/Users/VoodooKyo/AppData/Local/Temp/speedbench-probe-qa-20261003/desktop.jpg` 与 `narrow.jpg`。临时 fixture 仅 ignored dist 脚本，不进资源清单；没有启动真实测速或外部 DNS／STUN。
- 冻结 clean a416425，70 项资源／CPython3.14.8，locked/offline NSIS／ZIP unsigned 构建与独立解包验收通过。实际包内后端→CLI 使用真实 probe primitive＋fake controller 值（20、None、中断），退出130，追加 cancelled/partial JSONL，SQLite raw 精确保留，任务只一行，完成2／失败1／loss50%、started3 与 delay metrics 3 attempts／1 success 一致，lease 最终可重新取得。此前 failed download 的250000字节／1attempt／1success fixture 与完整性／Origin／重启／私有所有权／worker 隔离继续通过。两包 Get-FileHash 与 provenance 独立一致。

没有 schema／依赖新增、IP 评分／缓存策略改动、push 或覆盖 Downloads。剩余 provider/cache 计数／五性能里程碑、全传输硬预算、同覆盖真实性能、C/D 完整矩阵／迁移／原生 GUI／macOS/Linux 仍待完成；目标 active，B5/B7、C/D 不全勾选。

### 情报成本观测与五个任务里程碑

- 3bfead6：各 provider 的真实 transport 尝试／HTTP 2xx／解析可用结果分别计数，缺少 Key、disabled、timeout、quota、限流／冷却可独立观察。可选 numeric-only observer 不包含 IP／URL／错误／Key，失效不阻塞测速。兼容 adapter 调用前用 signature 选参数，不把函数内部 TypeError 当作参数不匹配重试；失败先行复现旧 transport 重复三次／自定义 query 重复两次，现均只进一次。参数根本无法匹配时不计未发生的调用。
- cache hit/miss 记录真实 get 检查（首次＋竞争复查），single-flight reuse 单独统计等待共享结果，cache write/error 与真正开始的 unique IP 任务数分开。不是每次 get_or_query 的逻辑命中率，也不是每节点 provider 调用。既有 7 天／24 小时 TTL、失败不缓存、单飞规则保留。`provider_wait` 不再混同 provider 实际服务时间；累加的并发 span 不能直接相加作壁钟耗时。worker_count 是成功就绪累计启动次数，不是峰值进程数；补上 Phase 2 追加 DNS 解析计时，不改变解析范围。
- 首次结果／首次可用推荐候选／网络结束／情报结束／确认清理完成由父后端 monotonic 接受时钟记录，首次值不可改。子进程只可报告网络／情报完成信号，不能伪造耗时或清理成功。IP 目标需有效 quality/grade，partial 下载不是推荐候选；这是当前目标、已测范围的有限候选，不改变评分或全球冠军承诺。正常／取消子任务已退出才记录清理成功，code 3 或未 reaped 不记录。未到达的里程碑缺省 N/A；旧历史无此字段仍可回放。
- 复用 task_metrics.counters_json，五里程碑使用 phase=milestones 独立行，无 schema／依赖新增，raw 原文不改。数据库事务内保留已落盘首次值，迟到 active checkpoint 不删除后来观察或重写首次耗时。纯前端任务状态合约接收 counters/milestones，不新增可视化面板，本批未声称新的 rendered UI 验收。
- 新增 25 项回归：test_intel_metrics 13、test_task_milestones 10、test_task_milestones_js 1、test_job_api 1；35 项专项通过，涵盖 cache 冷热、single-flight、内层 TypeError、计数脱敏、两个真实串行／worker orchestration、存储／HTTP／SSE 合约。最终 `python -m unittest discover -s tests -v`：Windows Python3.14.7 **870／33.337s**、3.9.25 **870／33.184s**、3.12.10 **870／33.779s**，均 OK (skipped=7)。跳过项为 Windows 不适用 POSIX 权限及可选 PyYAML 对照，无真实 API；日志 ignored dist/intel-milestones-python*-tests.log。node --check、git diff --check、验收脚本 py_compile 通过；Rust locked/offline 7 tests／2.65s。
- 冻结 clean 3bfead6，70 项资源／包内 CPython3.14.8，NSIS／ZIP locked/offline unsigned 构建通过。新包解压后实际后端→CLI 私有委派运行 real coordinator/cache/provider parser/report，fake transport／同出口两节点／两次冷热 enrichment：API calls=1、usable=1、cache hit=1、cache write=1、unique IP 执行任务=2，五里程碑全量持久化，raw 字节一致，canary Key 不出现在 public task／日志／JSONL／CSV／cache，最终 lease 可重新取得。原 integrity/tamper／Origin／重启／偏好／worker 隔离／direct CLI／failed download／cancelled probe 继续通过。Get-FileHash 独立核对两包 bytes/SHA-256/provenance 一致。
- 新夹具初验失败：synthetic execute 漏发 probing/enriching 导致 finalizing 被合法拒绝；模拟 Key 嵌入 python -c 文本被命令日志捕获。夹具改为完整阶段协议及仅环境变量传递 Key 后独立包验收通过；provider 构造仍显式读取该环境 Key。修正仅为仓库验收脚本（不在 70 项运行资源中），生产资产未改，无需改变冻结版本或重建。未输出真实凭据／未请求真实 controller／provider，没有 push/tag/Release／安装覆盖。

本批完成可观察性子功能，不等于 B/C/D 完成或相同覆盖实测提速。剩余目标策略、所有传输取消／资源预算、30/100/300 fixture 与真实同覆盖对比、C 全页面／错误／可访问性矩阵、D 历史导入／原生多平台生命周期仍待完成，整体目标 active。

## 仍未完成

- Windows 原生窗口/WebView2 缺失路径、托盘/通知、重复启动、休眠、活跃任务退出、安装器交互与升级验收。当前会话原生 GUI 自动化不可用，不用构建成功或浏览器截图代替。
- macOS Intel/Apple Silicon 和 Linux 原生构建、安装、运行、信号及依赖验收；只有配置和有界资源 fixture，不能称兼容性已通过。
- 首次旧历史目录选择/安全导入完整流程。目录所有权协调、CLI 异常部分留存已按上段通过本机验收；显式非敏感偏好迁移及正式版本检查已实现并按上述边界验证，但不代表完整数据导入/升级安装与回退交互验收。
- B 的完整目标策略、全阶段取消/清理预算与剩余失败传播、同覆盖性能实测；下载历史提示已接入并通过 fixture，但不构成真实提速证据。C 的更多前端职责拆分和完整错误/页面矩阵；配置根目录选择本机子功能已验收，不代表 C 整体完成。
- 整体规格逐项完成审计和最终交付。因此不标记完整升级已完成，也不建议覆盖现有稳定安装。
