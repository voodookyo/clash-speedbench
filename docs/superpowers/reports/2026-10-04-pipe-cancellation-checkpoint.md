# macOS 接续与 Windows 管道取消检查点

日期：2026-10-04。这是 B 的一个实现检查点，Windows 原生验收仍未完成，
不代表 A–D 完整交付，也没有新桌面产物。

实现提交：`35e3433`（本地开发检查点，尚未完成 Windows 原生验收）。

## 接收检查

当前打开目录是交接包而非 Git 仓库。按 HANDOFF 在包内 `Clash-SpeedBench/`
恢复 bundle，设置原 GitHub remote，没有 pull/reset/切换 master。

- 接收分支：`codex/fix-controller-autodiscovery`。
- 接收 HEAD：`7cdf4fd45aadd6d453ae231d1b1aa1fb79f63e5d`，符合交接要求。
- 恢复时仅 `tests/pipe_fixture.py`、`tests/test_pipe_cancel.py` 未提交；原样保留，
  本批纳入测试，不删除或把 macOS skip 当 Windows 成功。
- 阅读 roadmap、A/B/C/D 规格、实施清单、三份检查点、README、desktop README、
  Actions、最近提交及取消/Windows/资源测试。继续既有获批规格。
- 本机 Darwin 25.6.0 / arm64；Python 3.14.7、3.12.13；Node 26.9.0、系统 curl。
  PATH 未发现 Rust/cargo 或 Python 3.9。Node 与桌面锁定的 24.20 不一致；
  未安装依赖，没有尝试桌面构建。
- 本机接收 baseline：`python3 -m unittest discover -s tests -v`，
  **895 tests / 20.498s，OK (skipped=6)**。其中 5 项是 Windows 管道 fixture，
  1 项是 Windows ACL；不是交接中旧 Windows 的 890 项结果。

## 变更与范围

新增 `speedbench_pipe.py`，导入不加载 Windows DLL；仅取消作用域内的 pipe
连接创建 `FILE_FLAG_OVERLAPPED` 句柄。ABI 采用固定 Windows DWORD/BOOL 和
指针宽度，便于 POSIX mock 验证，同时保持标准库运行依赖。

每次 ReadFile/WriteFile 使用独立 OVERLAPPED、manual-reset event、缓冲区；
最多 50ms polling。取消、超时或异常仅针对该 handle 的该次操作调用
CancelIoEx，继续用 GetOverlappedResult 确认终态才释放缓冲/事件。
ERROR_NOT_FOUND 与正常完成的竞争不会把任务取消改成成功；第二次
KeyboardInterrupt 发生在 drain 入口、setup 或 polling 时仍保留资源并继续确认。
主线程的默认 SIGINT 在提交 I/O 前改为本次请求的暂存标志，经请求检查发起
取消，清理期间再次 Ctrl+C 不直接展开 pending frame；清理后恢复原处理器并
抛出 KeyboardInterrupt。工作线程和自定义处理器不替换，不改 SIGBREAK 或
持久配置。自定义处理器自行抛出异常、任意强杀等不属于默认 Ctrl+C 保证。
部分写入保留未写剩余段；共用原写入预算，预算耗尽不开始下一次写入。

保留 `_PipeSock.makefile → _PipeRawReader` 句柄所有权移交，Connection: close
后响应仍可读；正常碎片/chunked 响应不丢缓冲、不重发 HTTP。
预取消在 open 前退出。作用域外恢复 PUT/PATCH 仍使用原 plumbing，
不读取全局取消标志来阻断恢复。

保留 Win32 路径失败后的 NtCreateFile fallback。通过 `git show HEAD` 核对，
原 NT CreateOptions 就是 0；本批没有改变它。旧的非作用域 NT 路径和真实
服务模式恢复仍需 Windows 验证，本批不擅自改其打开方式。

显式打包清单同步加入新模块：`desktop/prepare_resources.py`、`build_app.sh`、
release.yml、test.yml；资源测试同步检查。README/desktop README 区分源码新增
实现与原 39f8b44 冻结包，原包不包含这次改动。

没有 schema、评分、测速样本、跨节点带宽串行、provider/TTL/single-flight
或 Python 第三方运行依赖变化。

## 验证

失败先行：旧代码的预取消测试实际仍调用 open，断言失败。实现后补出的部分
写入预算测试复现预算已耗尽仍提交第二段；修复为预算耗尽前置退出。
独立审查指出 drain setup 中断窗口，新增 DWORD 构造时第二次中断测试确认
旧清理路径未等待终态；扩大清理保护后通过。复审进一步指出循环条件的
异步信号窗口，用 trace 在该行发出真实 SIGINT 再次复现；增加默认 SIGINT
暂存及处理器恢复后通过，不以极低发生概率放过 pending buffer 释放风险。

`tests/test_pipe_io.py` 新增 **25 项** portable 测试：ABI/惰性绑定、即时与 pending
成功、精确 read/write 取消、正常完成竞争、超时、第二次中断、EOF/MORE_DATA/
普通错误、部分/零进度写、总写入预算、真实默认 SIGINT/清理条件再次 SIGINT、
处理器恢复、自定义处理器保留、worker 不修改信号、句柄移交、失败 setup、Win32/NT 打开
参数、碎片 chunked HTTP，以及 stalled header/body 取消后恢复。
fake 只保留 pending buffer/OVERLAPPED 的弱引用，断言原生完成前资源仍存活，
事件关闭不早于已观察终态。

- `python3 -m unittest tests.test_pipe_io -v`：25 tests，OK。
- 与 transport_cancel/windows/cancel/process_cancel/upgrade_packaging/
  desktop_resources 合计专项首轮 126 tests，OK；后续新增两项失败先行均已修正。
- 最终 `python3 -m unittest discover -s tests -v`：Python 3.14.7，
  **920 tests / 20.584s，OK (skipped=6)**。
- 最终 `python3.12 -m unittest discover -s tests -v`：Python 3.12.13，
  **920 tests / 20.589s，OK (skipped=10)**。
- 3.14 跳过 5 项 Windows pipe 与 1 项 Windows ACL；3.12 另缺可选 PyYAML
  对拍相关覆盖，没有为通过测试安装它。Python 3.9、Windows/Linux、六格 CI
  和 Rust/native build 本批未执行。
- `git diff --check` 通过。两轮全量并行执行，耗时不是测速性能对照。

## 独立只读审查与裁定

通过 ai-dev-delegation 的 `review-k3`、`review-glm` 独立发送同一受限 leaf
审查包，不授权 shell/写入/嵌套委派。交接 WIP 不在 HEAD，不委派 worker 写入，
也没有为了委派而建立基线。实现与集成由 controller 完成。

K3 首轮要求澄清 NT 基线与修复中断 setup；旧 NT 参数用 Git 原文确认未变，
两次可复现的中断窗口与部分写预算均已修复并做专项测试。最终信号复审确认
默认 SIGINT 暂存关闭了 pending frame 展开窗口，处理器恢复与线程/自定义
处理器边界正确。其 `changes_requested` 仅针对“重复 Ctrl+C 不能逃出无限
drain”，并明确可在记录该取舍后降为 no_blocking_issue。Controller 依据交接
要求“确认完成后才释放 buffer/event/OVERLAPPED”接受继续等待的边界；允许
Ctrl+C 展开并释放 pending 内存会违背已批准的生命周期契约。此项不是硬取消
预算完成证据。重复请求风险建议通过实际调用链核对：作用域仅探测/就绪读，
KeyboardInterrupt 不进入自动重发；恢复写仍在作用域外。关于 worker guard 的
建议不采纳：测试删除 guard 时会调用被禁止的 signal.signal，能直接捕获回归。

GLM 的 Router 调用最终退出 1，未返回可用最终审查；没有把它算作通过，
也没有由 controller 另启额外审查调用。
**review_coverage=partial**，不称双审通过。审查仅是源码建议，不代替真实
Windows fixture/产物验收。

Windows fixture 在本机跳过，保持 Windows 上真正执行的测试入口：
`python -m unittest discover -s tests -p test_pipe_cancel.py -v`。
真实 fixture、Windows Python 3.9/3.12、新包内私有后端→CLI 与恢复链路均待执行，
不自动 push 触发 CI 或调用远程 executor。

## 限制与下一步

这不是所有传输的硬取消 deadline。CancelIoEx 不承诺立即结束；若内核未完成，
必须继续保留资源等待，不能用超时后释放仍被使用的缓冲来伪造清理成功。
在这个等待期间，默认 Ctrl+C 的再次按下不会跳过 drain；若系统 I/O 永不
终态，重复 Ctrl+C 也无法让本次调用安全退出。只有系统强制结束进程等额外
手段能结束这种情况，但会失去优雅恢复/部分报告保证；本批未执行强杀。
读超时按单次 raw I/O，写预算覆盖同次 write_all 的所有部分写入；不是 HTTP
总请求时限。任意后续强杀/信号不能保证事件清理与最终报告持久化。

连接建立、TLS、provider urllib 在途请求、OS/磁盘/汇总预算仍未完整完成。
30/100/300 同覆盖性能、完整目标策略、C 页面/错误/可访问性矩阵、D 历史导入
及 macOS/Linux 原生包仍待推进，B/C/D 整项不勾选。可在本机继续独立可验证的
provider 传输/目标策略；Windows fixture 和产物验收需相应 Windows 执行环境。

没有真实节点测速、付费 API、修改用户历史/Verge/系统代理、依赖安装、
push/tag/Release/merge 或稳定安装覆盖。

实现依据：
[CancelIoEx](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelioex)、
[GetOverlappedResult](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-getoverlappedresult)、
[ReadFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-readfile)、
[Named pipe modes](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-type-read-and-wait-modes)。
