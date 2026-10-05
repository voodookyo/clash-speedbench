# 本地原生包与限定实测验收

日期：2026-10-05。B/C/D 继续实施，整阶段尚未全部验收。
用户已经批准项目隔离工具安装、限定真实网络样本和外部执行；这不包含
push、合并、tag、Release 或覆盖稳定安装。

## 冻结与委派

生产包冻结 `3b8a3a6102dddca1be23dc1f89b319e587e0339f`，source_dirty=false；
最终测试冻结 `8f1b8742f2d98e50f9e03bd4bc3d2da4e47edc5e`。
两者 Git diff 仅为 tests/test_history_summary.py 的旧夹具修正，运行资产无差异。
工具全部安装于外层 .toolchain 或自有 Linux VM；未修改全局 ai-dev 路由、
认证、原生 AI 工具设置、Verge 配置或系统代理。测试历史位于专用 .acceptance。

桌面退出子任务由 normal-code --write 执行，run_id
`e0b53f87-3d4f-4605-b5f8-26eefbe312f9`，最终为 OpenCode/DeepSeek flash。
有效已提交 HEAD `8e507bc` 作为基线；write_isolated/source_status_unchanged/
source_head_unchanged 均为 true；auto_commit/merge/push 均为 false。
控制器审查返回 diff 后补入合并 bootstrap/control 帧的失败先行回归，再人工整合。
UI 子任务 `db41379e-431b-48c7-81e6-82432bd798ec` 使用同一路由/隔离机制，
所有候选超时、没有改动。控制器在已结束 worktree 内完成小修复与回归后人工整合，
没有将超时当成功或回退为 worker 直接写源目录。

## 本次修复与测试

真实 macOS 窗口及已安装 Linux 包原先在认证退出后返回 2。包内 Python3.14.8
独立复现了 daemon control thread 持有 stdin BufferedReader 锁导致 finalization
abort（后端 -6）。现使用有界 os.read 读私有描述符，并从 bootstrap 开始复用
同一 reader，避免一个 pipe write 中后续控制帧被读前缓冲吞掉。
父 stdin 保持打开的实际 HTTP quit、真实 pipe 帧/EOF、超长/残缺帧和合并帧均覆盖。
Windows 分支仍需真实系统运行，不用 POSIX 成功替代。

失败 IP-only 任务虽有 probe 延迟与派生 score=0，原 UI 仍显示有限推荐及历史冠军。
现活跃/终态任务推荐要求有效的父确认 first_recommendation（0 也有效）及当前
Profile 所需观测；历史新轮次按相同观测资格筛选。地区榜也遵守资格。
缺失/非有限/错误类型 milestone、无有效 IP quality/Grade 或切到缺带宽的下载
Profile 不显示推荐。旧无 task 元数据历史仍按原综合分数回放，评分/排序公式未改。

- 包内 Python3.14.8 desktop bridge/power：36 tests，OK。
- macOS Python3.9.25 与3.12.13 bridge/desktop power/native clock：各109，OK。
- Node24.20.0 task UI/resume：36，OK；含 history summary 的后续专项39，OK。
- 最终 macOS Python3.9.25 全量：1231 / 79.371s，OK (skipped=10)。
- 最终 Ubuntu22.04 ARM Python3.12.13 全量：1231 / 79.053s，OK (skipped=10)。
- 此前两次最终全量各发现1个旧 UI 夹具仍允许仅 probe 推出推荐；修正夹具为
  有父确认和有效 quality/Grade 的 IP 样本后完成上述最终全量，不隐藏失败。
- Intel Rust locked native：6 tests / 22.85s；Linux native：6 / 17.64s，OK。
  其中包内后端测试实际使用对应架构固定运行时，保留 raw；Intel 通过 Rosetta 执行。
- 返回 diff 和整合 diff 的 git diff --check 通过。

## 三个本地包

均为1.1.0-alpha.1，内置 Python3.14.8，unsigned，自动更新关闭。
依赖锁定 Node24.20.0、Rust1.97.0、Tauri2.12.1。
构建流程 prepare_resources → locked Tauri build → collect_artifacts，
下表 SHA-256 来自项目既有 provenance 协议，没有额外全仓哈希。

| 目标 | 包 | 字节 | SHA-256 |
|---|---|---:|---|
| macOS ARM | Clash SpeedBench_1.1.0-alpha.1_aarch64.dmg | 46752531 | c2553e3c4f98f5ff59815e141865f0af0691376aee8cd754b50c45c363490cc0 |
| macOS Intel | Clash SpeedBench_1.1.0-alpha.1_x64.dmg | 45341237 | 021a5a1a418f0fa7ec24b5a76f370d0d96707fda0d0bc9820809376bf35905d6 |
| Linux ARM | Clash SpeedBench_1.1.0-alpha.1_arm64.deb | 62683124 | 4faf9421ba07862690092b8d24998acae9ea509c6f131ee097bd5ced120f2597 |

ARM 包在 source/dist/desktop-artifacts/3b8a3a6102dd；Intel 包在外层
.acceptance/macos-intel-source/dist/desktop-artifacts/3b8a3a6102dd；
Linux 包已从自有 VM 取回 source/dist 对应 linux-aarch64 子目录。
Intel 交叉构建输出位于 target/x86_64-apple-darwin/release/bundle；只将对应真实
DMG 放入现有 collector 的 canonical staging，未修改 collector 扩大扫描或伪造 target。

ARM 与 Intel app 均在中文/空格路径打开实际 Tauri/WebView 窗口，PATH=/usr/bin:/bin，
对应包内 Python 为实际 backend。两者认证 UI quit 均返回 native exit=0，后端消失。
Intel app/backend 均为 x86_64 Mach-O，Rosetta 运行不等于实体 Intel Mac 验收。
Linux 在独立 Ubuntu22.04 ARM VM 中 dpkg 安装真实 deb，以 GTK/WebKit 和
dbus-run-session/Xvfb 启动；HTTP200、实际包内 backend、认证 quit exit=0、后端回收
通过。这不等于交互式 Linux 桌面、托盘或所有发行版验收。
macOS 实际 bundle 含 PrivacyInfo.xcprivacy；未执行签名、notarization 或绕过系统警告。

## 实际桌面数据与生命周期

早先冻结8e507bc的真实窗口已验证：关闭隐藏，第二次启动激活同一窗口/端口；
5节点 IP-only 完成、15/15主探测、IPv4/IPv6各5次均失败，0MB带宽。
失败是本次查询失败，不证明节点永久不支持 IPv6。没有可用 IP 推荐里程碑。
新包回放同一真实轮次，失败 IP 轮次不再显示冠军，前两轮有效带宽冠军保持。

通过真实设置页预览、确认导入一条中文/emoji 的旧无身份历史，并确认撤回。
导入时原3轮 JSONL bytes 为完整前缀、SQLite runs.raw 逐行一致；撤回后 JSONL
与原 bytes 完全一致。旧行显示来源未知，未猜测 ID。随后新包正常退出仍保留原历史。
使用自有源/目标夹具；自动备份是既有导入协议所需，不接触真实用户历史。
截图留在本机 /tmp/speedbench-native-history-undo.png 与
/tmp/speedbench-native-final-history.png，未将历史/图片上传或装包。

新 ARM 包强制崩溃验收：固定5节点 IP-only，小流量、0MB带宽，实际捕获 native、
backend、CLI 与5个自有临时 Mihomo PID。只对该 backend SIGKILL；native 正确
报告2，所有捕获进程最终消失，原 raw 前缀保持。重新启动将该任务标为
interrupted/partial，不自动重测。另一次空闲 native-shell SIGKILL 后，私有 stdin
EOF 使自有 backend 退出，raw bytes 不变。用户 Verge kernel 仍存在、控制器
HTTP 可用，mode 与全部 Selector.now 与实测前快照一致。
首次 PID 观察夹具漏认 verge-mihomo 名字，在任务完成前未施加崩溃；修正 owned-tree
内识别后才得到上述有效崩溃结果，未把首次夹具失败称为产品通过。

## 固定覆盖真实对照

实际8e507bc包内 Python/CLI → production progress parser/JobStore，固定相同5个
强身份、相同 runtime 名称过滤；旧 CLI 不指定 --mode 的静态 worker 对照新 deep
动态队列，并非 UI“兼容串行切 GLOBAL”。两者 workers=3、probe=3、单轮10MB、
max-time=3s、--all、--no-ip、--deny-serial-fallback，没有自适应 warmup/多流/自动切换。
两轮均15/15主探测、5/5有效下载样本、5条结果、60个事件、exit=0。
100MB为两轮带宽样本请求上限；不能将剩余实际未返回字节用于追加第三轮。

| 父确认里程碑 | 静态 worker ms | 动态 deep ms |
|---|---:|---:|
| 首结果 | 2014.439 | 1519.887 |
| 首可用推荐 | 9822.888 | 10433.234 |
| 网络完成 | 23384.221 | 24000.644 |
| 情报完成 | 23384.272 | 24000.683 |
| 最终清理 | 23400.029 | 24015.665 |

实际 wall=23.400252/24.015909s；下载 bytes=15028912/12816039，合计27844951
（27.84十进制MB，不含探测/DNS/传输开销）。首结果提前约24.5%，总耗时增加约2.6%。
单次波动、返回字节不同、3s限时部分样本，不能声称同精度整体提速。
--no-ip 没有 provider 工作，情报完成里程碑不证明免费/付费情报端点可用或实测冷热缓存。
同覆盖30/100/300冷/热/失败/IPv6不可用 fixture 的独立证据见
2026-10-04-same-coverage-performance-fixture.md，不能把 synthetic bytes 当真实流量。
观测时 Verge界面 TUN开启、系统代理关闭、IPv6开关关闭；未更改设置，也不据此归因。

## 未验收与下一步

本地功能/平台子项通过不使 B7/C6/D3–D8 全部完成。Windows真实命名管道取消、
Windows当前源码原生包/安装器/WebView2及完整生命周期仍待对应系统；Linux x86-64、
实体 Intel 与五平台完整 CI 仍缺当前冻结点证据。托盘/系统通知/真实睡眠恢复、
Gatekeeper及完整共享页面/错误/可访问性矩阵仍待逐项验收。
Linux VM没有/sys/power/state，不能以 clock simulation 充作真实睡眠。
另发现旧 IP-only结果顶部样本文案含 nullMB，作为剩余 C 文案项记录，未扩展本次修复。

当前 GitHub 公共仓库仅 master=e4e33d3，只有 test.yml/release.yml；desktop.yml
尚未在远端，workflow_dispatch 不能凭本地文件触发当前候选。
下一外部步骤需明确授权推送一个独立开发分支并创建草稿 PR 触发既有六格 Python
和新增五平台 desktop CI；不合并、tag、Release或覆盖稳定安装。
