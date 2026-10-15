# 原生包、跨平台 CI 与限定实测验收

日期：2026-10-05。B/C/D 继续实施，整阶段尚未全部验收。
B 核心规格验收完成；C6 和 D3–D6/D8 的完整人工验收仍保留待验。
用户已经批准项目隔离工具安装、限定真实网络样本和外部执行。
后续明确授权将候选分支推送公共仓库并创建草稿 PR，运行六格 Python 与五平台桌面 CI；
仍不包含合并、tag、Release 或覆盖稳定安装。

## 冻结与委派

本地首轮生产包冻结 `3b8a3a6102dddca1be23dc1f89b319e587e0339f`，source_dirty=false；
对应首轮最终测试冻结 `8f1b8742f2d98e50f9e03bd4bc3d2da4e47edc5e`。
两者 Git diff 仅为 tests/test_history_summary.py 的旧夹具修正，运行资产无差异。
工具全部安装于外层 .toolchain 或自有 Linux VM；未修改全局 ai-dev 路由、
认证、原生 AI 工具设置、Verge 配置或系统代理。受控验收历史位于专用 .acceptance；
后续一次非计划默认目录启动及其数据写入单列于文末，不能据此声称全程未写默认目录。

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
Windows 后续 CI 的真实运行证据见文末，不用 POSIX 成功替代。

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

本地功能/平台子项通过不使 C6/D3–D6/D8 全部完成。Windows交互式窗口、安装/升级、
缺WebView2、托盘/系统通知/真实睡眠恢复，Linux交互式桌面及实体Intel运行仍待对应系统。
五平台编译/包/原生bootstrap和六格核心测试另列CI证据，不代表上述人工生命周期通过。
Gatekeeper及完整原生错误/可访问性矩阵仍待逐项验收。
Linux VM没有/sys/power/state，不能以 clock simulation 充作真实睡眠。
旧 IP-only结果顶部样本文案含 nullMB 已在后续 normal-code 隔离写入中修复：
任务 mode=ip 显示“不请求带宽”；非 IP 且样本参数无效显示“带宽样本参数未知”，不推断旧数据。

## 后续授权与跨平台 CI

用户明确授权推送分支并创建草稿 PR。开发分支为 codex/bcd-native-acceptance-20261005，
[草稿 PR #1](https://github.com/voodookyo/clash-speedbench/pull/1) 针对 master；
未合并、tag、Release 或替换稳定安装。

首次0df3791运行：Ubuntu Python3.9/3.12通过；macOS两格失败；Windows两格卡在
超长 anonymous pipe fixture同步写入，在控制器取消后日志保留。原生 macOS ARM/Intel、
Linux ARM/x86-64四格通过；Windows7项 Rust中6项通过（含真实JobObject自有子树与
无关进程保留），包内启动因私有所有权检查失败。
[首次 Python](https://github.com/voodookyo/clash-speedbench/actions/runs/37248891464)、
[首次桌面](https://github.com/voodookyo/clash-speedbench/actions/runs/37248891563)。
以下定位与修复没有将首轮失败隐藏或当成环境问题跳过：

- normal-code --write run 0ed349eb-0227-4f8b-b2cc-a8eb507d0757从clean0df3791
  隔离完成最新IP样本文案；仅web/app.js与history summary测试，控制器10项专项通过。
- normal-code --write run 3ba7b437-a8f2-457b-a245-2e79260991ae从clean59f0611
  隔离替换旧SQLite不支持的裸HAVING为条件聚合；同run/name唯一匹配才回填，
  0/多条保留NULL。控制器Python3.9.25的44项迁移/历史专项通过。
- 超长帧拒绝测试的有效上限为256字节，只需259字节验证；旧4097字节同步写
  满Windows匿名管道，在开始读取前死锁。改小fixture仍验证超限拒绝，不改变生产上限。
  [Microsoft CreatePipe](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createpipe)。
- 临时启动栈记录确认macOS子进程实际阻塞socket.getfqdn→HTTPServer.server_bind；
  这不是时钟/管道/SQLite问题。后续采用固定numeric loopback绑定，不查询反向DNS。
- Windows原生子进程固定阶段诊断为lease；种子并发创建亦因owner/DACL检查失败。
  normal-code --write run 5d8d47b3-5428-46e9-831c-ef0451796689从clean bdb2f16
  隔离完成新私有文件owner设置，控制器16项专项通过（3项Windows skip）。
  真实CI仍失败，不能将这个中间修复当作已解决。控制器随后在结束的隔离区改为
  GetNamedSecurityInfoW返回owner PSID、ConvertSidToStringSidW取得数字SID再严格比较；
  SDDL可使用LA等账户缩写，仅在真实owner匹配后允许该owner同一缩写的ACE。
  BA旧owner及Everyone ACL继续拒绝；已有seed从不接管。17项专项通过（3 skip），
  Windows后续真实seed/lease与包内bootstrap通过。
  [Microsoft SID strings](https://learn.microsoft.com/zh-tw/windows/win32/secauthz/sid-strings)。
- loopback委派e7bd08dc-f27e-4206-a366-a0bd13887289因新测试漏server.shutdown而超时，
  无完成结果；控制器确认全部候选结束，在隔离区修正测试清理，54项专项通过后人工整合。
  固定numeric loopback不进行server_bind反向DNS，正常认证/静态资源/HTTP quit保持。
- localhost第一IPv6地址不通时原连接用尽整项deadline，不能再试IPv4。
  控制器保留绝对总deadline，将剩余connect时间分给剩余地址，失败socket回收、
  取消不被吞掉；真实loopback和确定性阻塞fixture等36项专项通过。
- Windows Linux时钟ABI合成测试需create=True建立宿主不存在的time API，
  Node profile/history对拍需显式UTF-8解码；不改变真实平台clock或排序公式。

## 后续冻结、独立审阅与包

生产冻结413751a；只读normal-code run 11fec4e4-a74c-430a-b41e-c8203f3353f7
由OpenCode/GLM5.3完成owner/DACL与connect预算的限定独立审阅，no_blocking_issue，
结论明确以真实Windows运行为条件，不等于全仓审计。此前一次normal-code只读调用因
NO_ELIGIBLE_CANDIDATE没有结论，未修改路由/冷却/认证绕过限制。

413751a的六格测试中macOS/Ubuntu四格成功，Windows尚有6 error/3 failure：
测试用第二个handle读持有强制字节锁的文件、用cp1252读取UTF-8中文、在线程池中
注册只能主线程注册的SIGBREAK。normal-code --write run
524e2009-da75-4aa8-9e5d-dc2ce41d36c3从clean413751a隔离完成两份测试夹具修正。
控制器检查只改test_history_transfer/test_prepare_cancel，保留锁、完整bytes/mtime/inode
断言与1秒取消时限，26项专项通过后人工整合9faeaae，运行资产无改动。
上述成功写委派write_isolated/source_head_unchanged/source_status_unchanged为true，
auto_commit/merge/push为false；全部生产整合/提交/推送由控制器执行。

[五平台桌面CI](https://github.com/voodookyo/clash-speedbench/actions/runs/37253509432)
已全部成功：Windows x64、macOS ARM/Intel、Linux ARM/x64。每格完成locked原生测试、
unsigned包与provenance；Windows另外完成portable/NSIS和独立无系统Python PATH包验收。
PR checkout为合并提交21637cfb472858d1fbf57e543de5cf3144872f89，实际Git diff核对与
413751a无文件差异；不要将PR merge SHA误写成开发分支HEAD。
产物已保存于dist/desktop-ci/413751a的五个平台目录，每项source_dirty=false、
runtime3.14.8、unsigned、automatic_updates=false；六个安装/便携包及各自SHA-256
见其build-provenance.json。GitHub artifact仅保留14天，不是正式Release。

[六格Python CI](https://github.com/voodookyo/clash-speedbench/actions/runs/37254784775)
在9faeaae修正测试夹具后全部成功，Windows真实命名管道取消不再以POSIX skip代替。
413751a到9faeaae只改上述两份测试文件，因此两轮冻结的运行资产相同。
同一9faeaae的[最新五平台桌面CI](https://github.com/voodookyo/clash-speedbench/actions/runs/37254784794)
也已全部成功，含Windows无系统Python PATH独立包验收；六格核心与五平台构建现在具有同一
开发分支冻结点。此前413751a的本地留存包仍按原provenance标记，不冒充最新构建产物。
后续仅文档提交不改变该测试冻结点；文档同步使用skip-ci，不能声称文档HEAD重新跑过上述测试。

| 核心CI环境 | 实际Python | 数量 / 秒 | skip | 结果 |
|---|---|---|---:|---|
| Ubuntu | 3.9.25 | 1242 / 86.969 | 12 | OK |
| Ubuntu | 3.12.14 | 1242 / 83.976 | 12 | OK |
| macOS | 3.9.13 | 1242 / 124.082 | 12 | OK |
| macOS | 3.12.10 | 1242 / 128.655 | 12 | OK |
| Windows | 3.9.13 | 1242 / 183.085 | 23 | OK |
| Windows | 3.12.10 | 1242 / 163.225 | 23 | OK |

Windows两格NativePipeCancellationTest的5项stalled header/body取消与restore、chunked
成功/强制NT fallback、预取消不打开、timeout失败语义均为ok；POSIX两OS的同5项是skip，
不能混同。skip总数还包含各平台不适用测试，不表示有1242个执行通过的用例。

本地后续ARM包ccbcf56亦source_dirty=false，46750982 bytes，由既有collector留在
dist/desktop-artifacts/ccbcf56527c1。该包实际七页导航、崩溃任务partial回放、订阅趋势
及名称观测、IP-only顶部不请求带宽、深色/系统主题切换、键盘focus通过；退出=0。
真实系统WebView WebRTC探测获得一个candidate，但采集未完成，正确显示“无法确认”，
保存的自有测试审计保持unknown/DNS unknown/系统WebView；不写成无泄漏，不公布出口地址。
未更改Verge或系统代理、未增加带宽样本，所有本次原生测试实例已退出。

后续ccbcf56的关闭/重复启动检查：关闭窗口后应用仍运行，第二次启动exit=0，
恢复相同50958端口与同一native/backend PID；认证退出后两者均消失、native exit=0。
CUA在隐藏窗口状态无法返回窗口快照，当前工具没有可操作的菜单栏托盘入口；
没有据此判定产品托盘失败，也没有把重复启动恢复当成托盘菜单通过。

## macOS 隔离同路径替换与回退

在中文/空格测试安装路径中，实际替换同一个Clash SpeedBench.app目录，依次运行
前驱3b8a3a6 → 候选ccbcf56 → 回退3b8a3a6。三个受控实例均使用同一隔离SPEEDBENCH_HOME、
PATH=/usr/bin:/bin；各次退出=0后才替换应用目录，没有新增测量或覆盖稳定安装。
`python3 /tmp/speedbench-native-upgrade-roundtrip.py`完成三次逐字节JSONL/seed/偏好比较，
SQLite只读比较每个id与runs.raw，全部保持原值。前驱与回退窗口实际回放原有四轮历史；
候选首页显示相同最近轮次及新的“不请求带宽”文案。
这是两个1.1.0-alpha.1开发冻结点间的Mac ARM包替换/回退，不是正式版本迁移、
Windows安装器/缺WebView2或其他平台迁移的完整验收，D6仍待验。

候选阶段操作系统Window菜单后，页面点击未生效。定向sample显示主线程停在
NSMenuTrackingSession的菜单事件循环，不提供Rust互斥锁死锁证据。菜单尺寸项目当时禁用，
未完成真实窄窗口操作；原生应用菜单Quit成功退出候选，候选完整历史页面操作未计通过。

退出后的界面观察出现额外测试副本：该安装路径的新native/backend使用默认数据目录，
超出隔离范围。控制器核对完整自有安装路径后只停止这个副本，两个进程均已回收。
默认目录的既有SQLite被启动写入，新建identity-seed和ui-preferences.json；
原JSONL修改时间仍为旧日期，随后只读对拍确认SQLite每条runs.raw与原JSONL文本一致。
这不是默认目录完整前后快照，不宣称其schema/衍生数据未改变。新增文件保留，未尝试
删除seed、回退SQLite或覆盖用户数据。真实历史内容、种子值及截图未上传公共仓库。
此后退出验证改用已知进程/退出码，避免在已退出实例上继续界面观察触发非计划启动。

## 本地 Apple Silicon 签名与 Gatekeeper

检查ccbcf56的原始构建.app与隔离副本，版本均为1.1.0-alpha.1。`codesign -dvvv
--entitlements :-`显示ARM64主程序为Signature=adhoc、TeamIdentifier=not set；
两个bundle均没有Contents/_CodeSignature/CodeResources资源签名封装。
`spctl -a -vv`对隔离副本返回1，实际诊断为“code has no resources but signature
indicates they must be present”。这是当前未完整签名bundle的系统评估失败，不能将
已有CLI启动/退出成功当作Gatekeeper放行。此结论仅覆盖该本地ARM包，不推断其他包。

正式分发的最小后续路径是使用获授权的Developer ID身份完整签名bundle并完成公证，
再验证系统信任；当前没有提供此类凭据，规格允许明确标识的unsigned alpha。
本次只读检查未修补签名、添加权限、关闭Gatekeeper或绕过系统提示。
系统图形安装/提示流程及其他平台生命周期仍保留未验收。

## 2026-10-15 Windows 回传与权限候选

Windows 回传基线为 clean `2bffb77`。原 CI 包实际 revision 为 PR merge
`c378b45173fa2bf7acdcf5e540e1d714cf70da51`，Windows 记录其文件内容与 `9faeaae`
一致；后续本地 v2 包为 `2bffb77` 加未提交补丁，source_dirty=true，不能冒充 CI 包。
原生七页、受限 PATH、中文/空格/emoji、限定主题/焦点/窄窗口、partial 回放及正常退出已有
Windows 操作记录；合成导入失败、撤回阻断，其余人工范围继续按原矩阵保留。

控制器在独立 worktree 审阅十文件回传补丁，201 项 Mac 相关回归通过后提交 `88145d9`：
有成功 probe 的带宽失败不再标作“不通”，旧 raw 仅纠正显示；curl 超时保留原因；
Windows 通过系统 PowerShell 绝对路径识别物理出口，TUN 下只选 Up 的物理 IPv4 默认路由。
Windows 已有两轮 182/144 项回归和单节点 1 MB 下载证据，范围相交不相加，不据此声称峰值或
全订阅性能；本批控制器没有增加真实节点测量。

原历史恢复失败测试经 SSH 在旧代码上也通过，说明执行上下文有差异。控制器另用真实 Win32
fixture 确认：当前数字 owner SID 和私有 DACL 已合格时，仅在自有短命测试进程禁用权限，
并仅对自有临时文件拒绝 WRITE_OWNER，会使旧代码不必要的 owner 重设失败。没有修改账户
权限或系统策略。此边界与原报告 ERROR_ACCESS_DENIED 一致，但未独立采集原 GUI token，
不能据此断言原 GUI 的所有原因已经排除。
owner 设置与 DACL 设置所需权限的差异见
[Microsoft SetNamedSecurityInfoW](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow)。

`normal-code --write` run `c70fe4e7-b472-4731-93a6-7eeab0992ff9` 从 clean `2bffb77`
隔离产出 `speedbench_identity.py` 和四项 portable 回归。write_isolated、source_head_unchanged、
source_status_unchanged 均为 true，auto_commit/merge/push 均为 false；控制器审阅后人工整合，
并加入独立子进程中的真实 Windows ACL 回归，提交 `a1fe6f3`。实际 owner 已匹配才跳过设置，
不同 owner 仍须调整，随后 DACL 收紧与最终核验保持；已有 seed 仍不接管。
最初 GLM 调用超时后由控制器停止本任务调用，动态选取 normal-code 当前 GREEN/VERIFIED
候选 DeepSeek 完成写入；另一次只读审阅未形成完整意见，不记为独立审阅通过。

当前本地测试冻结 `39352f3331f615c5ca4d5df18e100165c7e45708`（含接续计划文档）：

| 环境 | 验证 | 实际结果 |
|---|---|---|
| Mac ARM / Python 3.14.7 / Node 26.9 | 回传补丁相关回归 | 201 项，21.826s，OK |
| Mac ARM / Python 3.14.7 | owner/lease/history/API/bootstrap 专项 | 73 项，2.290s，OK，skip=4 |
| Mac ARM / Python 3.14.7 | 全量 | 1259 项收集，83.296s，OK，skip=9 |
| Windows x64 / Python 3.14.7 | 同一权限/历史专项，含真实受限权限子进程 | 73 项，15.876s，OK，skip=2 |
| Windows x64 / Python 3.12.10 | 同一权限/历史专项，含真实受限权限子进程 | 73 项，15.627s，OK，skip=2 |

skip 均不计执行通过；各组范围重叠，不相加为独立总数。源码测试没有替代原生导入/撤回、
完整托盘、通知、GUI 取消、自有崩溃、8950 反向顺序、真实睡眠及 NSIS/缺 WebView2 验收。
候选包构建、包协议验证和原生复测状态在后续补记中分别记录。

Windows 候选已在独立 checkout 从 clean `39352f3` 构建：Node 24.20.0、Rust 1.97.0，
prepare → locked Tauri build → portable package → 现有 verifier → collector 均 exit 0。
实际 provenance 为该完整源码 revision、source_dirty=false、windows-x86_64、Python 3.14.8、
unsigned、automatic_updates=false。portable ZIP 15,805,621 bytes，NSIS 12,815,937 bytes；
仅构建 NSIS，没有运行安装器。新包留在独立候选目录，没有替换供用户使用的旧包。
包内 Python 3.14.8 在 PATH 仅 System32 下额外运行同一真实 ACL/受限权限 fixture，exit 0，
实际 owner 与最终私有 ACL 验证通过，fixture raw 字节不变；该诊断使用 `-B` 防止污染包。

Windows 已准备新的桌面启动脚本与一轮明确标识的合成导入源。脚本固定独立数据及
APPDATA/LOCALAPPDATA，使用空 controller fixture，拒绝 Session0 和现存 native 实例，
不会自动停止用户正在使用的窗口。脚本仅完成语法检查，尚未在桌面执行；下一步由 Windows
Codex/用户实际确认导入、回放、撤回，再推进原有未验人工项。不得把本批源码/包测试
改写为这些 GUI 操作已通过，C6 与 D3–D6/D8 整项继续未勾选。

本批 [六格 Python CI](https://github.com/voodookyo/clash-speedbench/actions/runs/38703024398)
和 [五平台桌面 CI](https://github.com/voodookyo/clash-speedbench/actions/runs/38703024389)
在分支冻结 `8102a5bce6022e43eab27d83f7b6d661dd8237a4` 全部成功。实际 PR merge
checkout 为 `740b50b9faf32c271873f7770e391a878d341f82`，Git diff 核对与该分支冻结无文件
差异。CI 产物保留自身 merge revision，不改标为 `39352f3` 本地 Windows 包。
`39352f3` 到 `8102a5b` 仅更新上述计划和报告，运行资产相同。

| 核心 CI | 收集 / 秒 | skip | 结果 |
|---|---|---:|---|
| Ubuntu / Python 3.9 | 1259 / 86.163 | 13 | OK |
| Ubuntu / Python 3.12 | 1259 / 87.897 | 13 | OK |
| macOS / Python 3.9 | 1259 / 130.038 | 13 | OK |
| macOS / Python 3.12 | 1259 / 121.230 | 13 | OK |
| Windows / Python 3.9 | 1259 / 181.944 | 23 | OK |
| Windows / Python 3.12 | 1259 / 160.756 | 23 | OK |

新增真实受限权限子进程用例在两个 Windows 格均实际执行为 ok，POSIX 格明确 skip。
五个平台（Windows x64、Mac Intel/ARM、Linux x64/ARM）均完成 locked native 测试、构建、
包及 provenance；Windows 含 NSIS/portable 和无系统 Python PATH 的既有包验证。
这些成功仍不替代上述原生人工项目。后续文档补记使用 skip-ci，不声称文档 HEAD 重跑过此矩阵。


## 2026-10-15 GUI 复测失败与 OWNER RIGHTS 修复

`39352f3` 的真实 Windows GUI 导入仍失败：预览新增一轮成功，确认后显示“导入失败；已回退原文件或尚未替换，请保留私有备份”，回放和撤回受阻。备份目录已创建但为空，目标 JSONL 尚不存在、SQLite raw 仍空，源合成 JSONL 不变；API 状态 pending=false、can_rollback=false。这覆盖了前文“尚未执行桌面复测”的当时状态，不能将第一轮 owner 修复及其绿色 CI 写为 GUI 成功。

用户在 Windows 按 Escape 停止 Computer Use。Mac 未继续操作或关闭该窗口，保留 failed backup 和旧包；后续 GUI 须用户明确恢复。原生/backend PID 和包路径的核对、token 与 ACL 诊断均按此边界执行。实际 GUI token 为非提升 limited token，默认 owner 为当前用户；相关目录实际数字 owner 同样匹配。失败备份目录的 allow trustees 只有当前用户、System、Administrators 和 OWNER RIGHTS，没有额外普通用户。`icacls /inheritance:r /grant:r` 保留显式 OWNER RIGHTS；严格校验随后将 OW 判为过宽。

[Microsoft OWNER RIGHTS 定义](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-special-identities-groups#owner-rights)说明 OW / S-1-3-4 代表对象当前 owner。控制器在短命诊断进程只将自身 TokenOwner 设为自身用户（不改变账户、桌面进程或系统权限），于新临时目录复现：mkdir(0777) 通过，mkdir(0700) 在实际 owner 已匹配时被旧 `_windows_private` 拒绝。相同 token 默认 owner 下，两项合成历史合并/私有备份撤回测试在旧 `39352f3` 均失败，修复后均通过（2项 / 2.339s）；没有操作失败 GUI 数据或真实历史。这是进程默认 owner 的复现，不冒充整个 limited GUI token。

当前 GREEN/VERIFIED 且在 normal-code write allowlist 的 GLM-5.3/OpenCode 经 `normal-code --write` run `231129d1-a953-4fee-b7a0-dbab8934db5f`，从 clean `4748cdb` 隔离返回三个文件。源 HEAD/status unchanged 与 write_isolated 均 true，auto_commit/merge/push 均 false。portable OW/numeric OW 正例先在旧实现失败；外来 owner、Everyone、CreatorOwner 仍被拒绝。控制器审阅后修正测试夹具中 TokenOwner 名称遮蔽、SD 内部 owner 指针不能单独 LocalFree 两处问题，补充外来用户 ACE 反例，再人工整合为 `1ee965f`。生产只在数字 owner 核验成功后允许 OW / S-1-3-4，不增加磁盘授权、不接管已有 seed。

验证使用 `tests.test_node_identity tests.test_history_transfer tests.test_backend_owner tests.test_history_transfer_api`：

- Mac ARM / Python3.14.7：56项 / 1.284s，OK，skip5。
- Windows / Python3.14.7、3.12.10：首轮各56项 / 14.583s，只有新 native fixture 的源字节断言失败（文本写入 CRLF，断言误用 LF），其余通过，skip2。该失败未据此修改生产逻辑。`a0e45a8` 只将测试改为对比实际原始字节；仅复跑这一项，两版本分别0.594s/0.581s，通过，不重复其余成功测试。
- 新 native child fixture 实际设置自身默认 owner，确认 mkdir(0700) 自带 OW 且 owner 正确，再运行真实权限收紧和核验、合成导入、SQLite raw 比对与撤回；保留源原始字节。POSIX 明确 skip，未把 skip 计为执行。

本地新 Windows 包冻结 `a0e45a8`，在独立 checkout 构建及验证中；旧 `39352f3` 包和 GUI 现场保留。新跨平台 CI 与新包结果另补记，旧 `8102a5b` 的成功不替代本次修复验证。新 GUI 导入/回放/撤回仍待桌面实际复测；完整托盘、通知、GUI取消、自有崩溃、8950反向顺序、真实睡眠及安装器/缺依赖门槛仍未完成，本批没有额外真实测速或付费 API。
