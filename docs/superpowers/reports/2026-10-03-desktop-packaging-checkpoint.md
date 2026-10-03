# 桌面打包与后端生命周期检查点

本报告是阶段 D 的本机检查点，不代表 A–D 完整验收。没有 push/tag/Release，没有安装到 Downloads，没有修改真实 Verge、用户历史、系统代理或调用付费 API。

## 代码与产物

- 473ebf6：数据目录内核锁、桌面私有后端、非敏感偏好持久化、受限外链请求及共享界面环境标识。
- ab7467b：Tauri 2 客户端、固定运行时、依赖锁、完整性与许可证、原生包配置及桌面 CI。
- c4d9d10：解压后的 Windows 产物独立验收、按源码修订隔离产物、澄清本地写 token 与 provider Key 的区别。
- 产物固定源码：`c4d9d10a52ae840bcb180666c4eda107efb1a94e`，构建时 `source_dirty=false`。
- 桌面版本 `1.1.0-alpha.1`，运行时 CPython `3.14.8`，平台 `windows-x86_64`。核心稳定版入口与 legacy 测速默认未改为桌面 alpha。
- 当前包目录 `dist/desktop-artifacts/c4d9d10a52ae/`，二进制未提交 Git；旧忽略目录内的包不是本次交付证据。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| Clash SpeedBench_1.1.0-alpha.1_x64-setup.exe | 12744387 | 83e22d9f04d941cb10d9896fe6691059c0fb0d3339b115b73ae3ec784e3e005d |
| Clash-SpeedBench-1.1.0-alpha.1-windows-x86_64-portable.zip | 15714017 | 8b94179f6f09258048378ae28650a041d9ab3f933633d182f61c2a7c21f98013 |

SHA-256 已额外通过 PowerShell `Get-FileHash` 与 build-provenance.json 核对。包 **unsigned**，自动更新禁用；WebView2 是系统组件，不是单文件免运行时承诺。

## 本阶段变更文件组

- 后端：speedbench_desktop.py、speedbench_owner.py、speedbench_preferences.py、speedbench_web.py、speedbench_jobs.py。
- 共享界面：web/app.js、web/index.html、web/tasks.js；区分浏览器与 WebView，受控 DNS 引导外链，不把 WebView 检测当作 Chrome/Edge 审计。
- 桌面：desktop/src-tauri/src/backend.rs、main.rs、Cargo.toml/Cargo.lock、Tauri 配置、capabilities、NSIS hooks、原生平台配置。
- 工具与依赖：desktop/prepare_resources.py、license_notices.py、package_windows.py、verify_windows_package.py、collect_artifacts.py、runtime-lock.json、package.json/package-lock.json。
- 文档/CI/打包：README.md、desktop/README.md、.github/workflows/desktop.yml、test.yml、release.yml、build_app.sh、.gitignore；专项 tests。

## 实现边界

Tauri 加载同源共享 UI；测速仍由标准库 Python、系统 curl 和隔离 Mihomo worker 执行，没有 pip 运行依赖。65 项资源逐个校验，编译进原生程序的清单是信任锚；不能通过同时替换源码和外部清单绕过校验。

后端只绑定 127.0.0.1 动态端口。私有 stdin/stdout 启动协议校验 nonce、实例、PID、版本；nonce/write token 不进入 argv、URL、日志或 localStorage。公开身份 JSON 不含凭据。既有同源 HTML 的 sb-token meta 包含本实例本地写 token，这是鉴权所需，不是 provider/controller API Key。后者不会返回前端。

数据目录内核锁在同步/迁移前取得；旧锁元数据不等于活跃所有权。桌面与独立 Web 使用同一目录时互斥。尚未覆盖直接 CLI 所有写入协调；不同数据目录的实例不是全局互斥。

Windows 自有 backend 在接收启动许可前加入 Job Object；关闭对象只回收所属子树。POSIX 实现独立进程组，但未做原生崩溃/信号实测。退出先取消并等待，超时明确失败并限定自身子树，不按进程名杀用户 Mihomo。

前端无通用 shell/filesystem IPC。外链为固定枚举；系统浏览器审计仅允许当前后端的 loopback 泄漏页，不携带 token 查询参数。桌面通知默认关闭，只有脱敏计数摘要。安装器发现运行实例或无法确认占用时中止，不强制关闭程序。

本阶段没有新增 SQLite 表/列：既有 runs.raw 保留；环境标识进入 leak_audits 原有 details_json；非敏感偏好进入私有 ui-preferences.json，不进历史/缓存。IP provider、评分、TTL/single-flight 规则未更改。

## 已执行验证

- Windows Python 3.14.7：`python -m unittest discover -s tests -v`，706 tests，OK (skipped=7)，16.158s。
- Windows Python 3.9.25：同一全量命令，706 tests，OK (skipped=7)，15.815s。
- Windows Python 3.12：同一全量命令，706 tests，OK (skipped=7)，15.404s。
- Rust：`cargo test --locked --offline --manifest-path desktop/src-tauri/Cargo.toml`，7 tests 通过；实际临时中文/空格/emoji 路径、无 Python PATH、重复目录所有权、原 raw 保持、Job Object 子孙进程回收且不影响独立进程。
- Windows Tauri Release/NSIS 构建成功；`python desktop/package_windows.py`、`verify_windows_package.py`、`collect_artifacts.py` 成功。
- 独立产物验收实际解压 ZIP，再验证原生完整性/篡改、内置 Python 启动、私有握手、Origin 拒绝、两轮起停、偏好跨重启、旧 raw 不改。没有创建窗口，因此不把它称为 GUI 验收。
- 浏览器隔离 fixture 泄漏页：实际截图、浏览器环境边界提示、控制台无相关 error/warn；未点击 STUN/外部 DNS 服务，没有真实环境泄漏结论。
- `git diff --check` 通过。桌面五平台与原六格 Python CI 已配置，但未推送/运行。

## 仍未完成

- Windows 原生窗口/WebView2 缺失路径、托盘/通知、重复启动、休眠、活跃任务退出、安装器交互与升级验收。当前会话原生 GUI 自动化不可用，不用构建成功或浏览器截图代替。
- macOS Intel/Apple Silicon 和 Linux 原生构建、安装、运行、信号及依赖验收；只有配置和有界资源 fixture，不能称兼容性已通过。
- 首次旧历史路径发现/导入、显式非敏感偏好导出导入、可信官方版本比较与手动升级流程、直接 CLI 与目录所有权协调。
- B 的生产历史候选提示/目标策略、全阶段取消/清理预算、同覆盖性能实测；C 的配置根目录选择、更多前端职责拆分和完整错误/页面矩阵。
- 整体规格逐项完成审计和最终交付。因此不标记完整升级已完成，也不建议覆盖现有稳定安装。
