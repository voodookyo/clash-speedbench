# 桌面休眠恢复源码接入

日期：2026-10-04。依据已批准 D 规格的休眠／唤醒 partial 与手动重试要求。
本批源码及本机验证完成；B/C/D 整项、原生休眠和包验收仍未完成。

## 委派与审阅责任

已按 ai-dev-delegation 做 assessment，读取当前 registry/routes/policy/health。
使用用户指定的 normal-code，不指定具体模型，不修改全局路由或认证配置。
两轮写入都通过 `ai-run normal-code --write --json --timeout 300`，从完整、clean
的 committed HEAD 创建隔离分支／worktree。源工作区在委派期间保持不变。

- 时钟模块：base `f85b663`，run `ab3b093b-d34c-4ef1-895c-6761748a1237`。
  GLM5.3 两次、MiniMax 两次超时后 DeepSeek flash 成功。Codex 在已结束的隔离
  worktree 修正 Win32 BOOL 的 32 位 ABI、失败 arm 与替代任务的并发覆盖，以及
  将宽泛模块扫描测试改为固定白名单测试，检查 diff 并实际执行测试后整合。
  本地提交 `45c371a`。
- 生产接入：base `45c371a`，run `d6b13037-819e-4397-9bec-485aaa6e5702`。
  GLM5.3 flash／DeepSeek 超时，Kimi 后续返回超时／403，路由最终 exit 1。
  这是未完成委派，不能称 worker 成功或测试完成。隔离产出保留；所有候选停止后，
  Codex 在该 worktree 审阅、补足实际子进程测试并修正：立即发布固定计数、拒绝
  无效中断原因、正确原因日志、保留清理失败提示优先级、确认 watcher join 及
  启动失败关闭已绑定 server，随后手动应用通过检查的 patch。

两轮 metadata 均为 write_isolated=true、source_status_unchanged=true、
source_head_unchanged=true，auto_commit/merge/push=false。结构化 tests=unknown，
以下控制器实际命令才是测试依据。最终审阅／整合／验收由 Codex 负责；本批没有
另行成功的外部只读审阅，不宣称完成了独立双模型审阅。

## 最终行为

- `speedbench_power.py` 标准库实现 Linux BOOTTIME／MONOTONIC、macOS continuous／
  absolute、Windows GetTickCount64／QueryUnbiasedInterruptTime。包含／排除休眠
  两类时钟组成保守采样区间；默认只有可确认的偏移增长大于 250ms 才视为恢复。
  普通卡顿仅扩大不确定区间；不使用系统时间跳变作启发式。
- 每个桌面任务在接受和分派之间登记新基线。watcher 和任务结束路径在 STATE_LOCK
  内共用检测／取消预约，避免一次性检测丢失、结束／取消冲突或取消之后新建的任务。
  即使子进程先正常退出，也在终态决定前检查是否跨过休眠。
- 仅对当前自有进程发送既有 SIGINT／Windows 哨兵；等待与兜底操作保留原进程句柄。
  task-specific HTTP cancel 同样传 expected_job_id。已有结果与历史 raw 保留，
  cancelled/failed 为 partial，不自动重测。清理失败仍明确为失败。
- 原始时钟／启动时间／休眠时长仅在内存中。`system_resumes`／`power_clock_errors`
  经既有安全 metrics 白名单持久化；实时与任务详情显示固定中文原因及手动重试提示。
- 启动时无有效时钟则不握手；运行中故障停止接受新任务直至重启。普通 Web 后端不
  启动 watcher。退出等待 watcher 结束后才释放数据所有权；既有 Rust 父进程 40s
  自有子树退出期限继续负责最终生命周期边界，原生实测仍待完成。
- 固定运行模块白名单已更新。macOS 的 `PrivacyInfo.xcprivacy` 只声明本次使用的
  SystemBootTime／35F9.1，映射到 Contents/Resources；原生包内放置仍待构建验证。

## 本机验收

macOS arm64；Python3.14.7、Python3.12.13；现有 Node 用于 JS 测试，未安装锁定构建工具。

| 命令／验证 | 结果 |
|---|---|
| `python3 -m unittest tests.test_power_monitor tests.test_upgrade_packaging -q`，隔离 worktree | 76，0.128s，OK |
| 同一专项，源工作区 Python3.12 | 76，0.126s，OK |
| `python3 -m unittest tests.test_desktop_power -q`，修正后的隔离 worktree | 18，1.068s，OK |
| Job API／history／desktop bridge／task UI JS／desktop resources，隔离 worktree | 68，12.669s，OK；随后新增启动失败 server 关闭测试 1 项通过 |
| Python3.12 上述五组加 desktop_power，源工作区 | 87，13.721s，OK |
| Python3.14 `unittest discover -s tests -q` | 1132，56.991s，OK，skipped=6 |
| Python3.12 同一全量命令 | 1132，56.946s，OK，skipped=10 |
| `node --check web/app.js`、`git diff --check` | 通过 |

新增 ABI 回归实际通过 ctypes C 回调检查非零 32 位 BOOL 不被截成一字节；并发 arm
失败不能擦除替代任务。生产接入测试使用临时目录／loopback／可控时钟，包含真实
自有 Python 子进程接收取消、exit130、SQLite partial／计数／结果留存和旧 JSONL 行
不变。没有系统 sleep、真实节点测速、付费查询、Verge 或系统代理修改。

整合时 diff 检查发现新测试的尾随空格并阻止应用；一次提前启动的 Python3.12 测试
因模块尚未应用而失败，不计为验收。修正后实际整合及以上专项／全量均成功。

日志：`/tmp/clash-speedbench-power-clock-write.log`、
`/tmp/clash-speedbench-power-integration-write.log`、
`/tmp/clash-speedbench-power-full-314.log`、`/tmp/clash-speedbench-power-full-312.log`。

## 剩余门槛

本批不证明真实硬件睡眠、休眠、系统 WebView、托盘、安装升级或 bundled Python
运行成功。Windows/macOS Intel/Linux 原生执行环境、项目隔离锁定工具安装，以及
限定真实流量性能对照仍需既有待答授权；不通过自动 push/CI 或系统配置变更绕过。
Python3.9 与跨 OS 六格测试仍待执行。模拟及 native-clock read smoke 不替代这些门槛。

官方依据：[Linux clocks](https://www.man7.org/linux/man-pages/man2/clock_gettime.2.html)、
[Microsoft interrupt time](https://learn.microsoft.com/en-us/windows/win32/sysinfo/interrupt-time)、
[Apple Mach clocks](https://developer.apple.com/documentation/driverkit/c-runtime-support)、
[Apple required reasons](https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacyaccessedapitypes/nsprivacyaccessedapitype)、
[Tauri macOS files](https://v2.tauri.app/reference/config/#macconfig)。
