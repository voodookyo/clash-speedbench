# Windows 端适配交接文件（v0.8.0）

> 给 Windows PC 上的 Kimi Code 会话：本项目在 macOS 上完成了 v0.8.0 Windows 适配的全部离线开发（326 个单元测试全绿，CI 含 windows-latest 矩阵），但**没有经过真机验证**。你的任务是按本文档做真机验收、修补问题、然后发版。

## 30 秒项目速览

- **Clash SpeedBench**：给 Clash Verge Rev (mihomo) 做的节点测速工具。不自己解析订阅、不自己实现协议，而是利用正在运行的 mihomo：通过 external-controller API 拿节点/测延迟，另起临时 mihomo worker 进程测出口 IP 与带宽。
- **架构**：两阶段测速。Phase 1 = 并发粗筛（延迟走主 mihomo `/delay` API + worker 池探测出口 IP，不抢带宽）；Phase 2 = 单 worker 串行带宽精测 Top N（curl 真实下载 Cloudflare）。
- **入口**：`speedbench_web.py` = Web 面板（127.0.0.1:8950，token 鉴权）；`clash_speedbench.py` = 测速核心 CLI；`speedbench_workers.py` = worker 池/配置解析/网卡检测；`speedbench_db.py` = SQLite 历史。
- **Windows 入口**：根目录 `SpeedBench.bat`（双击 → 最小化控制台跑面板 → 自动开浏览器）。数据目录 `%APPDATA%\ClashSpeedBench`。
- **硬约束**：零第三方依赖（仅标准库 + 系统 curl；PyYAML 只可 try-import 可选使用）；中文注释/文案；改代码必须保持 `python -m unittest discover -s tests` 全绿。
- 仓库：github.com/voodookyo/clash-speedbench，分支 master。

## 已完成的 6 个适配点（代码位置）

| # | 适配点 | 位置 |
|---|--------|------|
| 1 | controller 候选按平台过滤（win32 跳过 unix socket） | `clash_speedbench.py:44-52` |
| 2 | Verge 配置/mihomo 二进制 Windows 路径候选 | `speedbench_workers.py:91-118`，`find_mihomo_bin()` :131 |
| 3 | YAML 解析 fallback 链：PyYAML → ruby（posix）→ 内置迷你解析器 → 清晰报错 | `extract_proxies()` :672；迷你解析器 :149-634 |
| 4 | Windows 网卡检测（PowerShell Get-NetRoute）+ 虚拟网卡前缀扩充 | `physical_interface()` :742；`WIN_VIRTUAL_IFACE_PREFIXES` :782 |
| 5 | 中断测速：win32 用 CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT + SIGBREAK handler（走与 SIGINT 相同的 finally 恢复路径） | `speedbench_web.py:176-185`、:215-235；`clash_speedbench.py:893-901` |
| 6 | `SpeedBench.bat` 启动器、CI windows-latest 矩阵、release.yml windows job、README Windows 章节 | 根目录 / `.github/workflows/` |

测试：`tests/test_windows.py`（39 个，mock 平台分支）、`tests/test_verge_yaml.py`（54 个，迷你解析器），全部在 mac 上 mock 验证过。

## 真机验收 checklist（按顺序执行）

1. **环境确认**：`python --version` ≥ 3.9（推荐 Microsoft Store 版）；Clash Verge Rev 已安装且正在运行。
2. **路径确认（最重要）**：
   - 确认 `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml` 存在；
   - 找到 `verge-mihomo.exe` 实际位置，对照 `speedbench_workers.py:91-118` 的候选列表。不在列表里就补进去（常见：`%LOCALAPPDATA%\Programs\Clash Verge\`、`%ProgramFiles%\Clash Verge\`）。
3. **跑测试**：仓库根目录 `python -m unittest discover -s tests`，应全绿（少数 PyYAML 对拍用例在无 PyYAML 时自动 skip，正常）。
4. **双击 `SpeedBench.bat`**：应出现最小化控制台窗口 + 自动打开浏览器到面板。若报「未检测到 Python」但实际已装，检查 PATH（商店版 Python 有时要关掉「应用执行别名」干扰）。
5. **小量测速**：面板里节点过滤填一个小组名（如 `香港`），每轮 10MB × 1 轮，开始测速。确认：实时进度/日志正常、结果表格有数据、IP 画像列有内容。
6. **中断测速（重点验证项）**：测速跑到一半点「中断测速」，然后打开 Clash Verge 确认**代理模式/策略组选择已恢复原样**（这是 CTRL_BREAK_EVENT 路径的真机首验，mac 上只能 mock）。
7. **排序与切换**：点表头按延迟/带宽排序；点「切换」换一个节点，确认 Clash Verge 里当前节点跟着变。
8. **TUN 模式场景**（如果平时开 TUN）：开着 TUN 跑一次测速，worker 模式应检测虚拟网卡拒绝启动并回退串行模式（面板日志有提示），不应给出离谱结果。
9. **全量测速**：不填过滤跑一次完整测速，确认 100+ 节点时的总耗时与稳定性。

## 已知风险与排查指引

- **verge-mihomo.exe 路径变体**：不同版本/安装方式位置可能不同。症状 = 面板报「未找到 mihomo」。修法 = 往 `MIHOMO_BIN_CANDIDATES` 加候选，补一个 mock 测试。
- **CTRL_BREAK_EVENT 不生效**：症状 = 中断后 Clash 配置没恢复。排查 = 确认 bat 是用 `python`（不是 `pythonw`）启动、`speedbench_web.py` 的 Popen 带 CREATE_NEW_PROCESS_GROUP。兜底方案：给测速核心加「取消标志文件」轮询（节点间检查），绕开控制台信号。
- **虚拟网卡名不在前缀表**：症状 = 开 TUN 时 worker 模式没有拒绝启动。修法 = `Get-NetAdapter` 看实际接口名，往 `WIN_VIRTUAL_IFACE_PREFIXES` 加前缀。
- **PowerShell 输出乱码**：网卡检测已钉 UTF-8 输出 + `errors="replace"`，若中文 Windows 上接口名解析异常，先手动跑 `powershell -NoProfile -Command "(Get-NetRoute -DestinationPrefix '0.0.0.0/0').InterfaceAlias"` 看原始输出。
- **迷你 YAML 解析器报错**：Verge 机器生成配置理论上是 serde_yaml 稳定子集。若真机配置触发了 `VergeYAMLError`，优先建议用户 `pip install pyyaml`（fallback 链第一级），再把触发样本的**结构**（脱敏后）补进 `tests/test_verge_yaml.py`。

## 验收通过后：发版

```bash
# 在 Windows PC 上（或回 mac 上）：
git tag v0.8.0 && git push origin v0.8.0
```

tag 推送会触发 `.github/workflows/release.yml`：mac job 产 `Clash-SpeedBench-v0.8.0-macos.zip`，windows job 产 `Clash-SpeedBench-v0.8.0-windows.zip`，自动建 GitHub Release（说明取 `.github/release-notes.md`）。两个 job 并发，先到的 create、后到的 upload --clobber，属正常设计。

发版后建议在 Releases 页面下载 windows zip 做一次**全新解压安装**验证（模拟小白用户路径）。

## 开发约定（在那边改代码时遵守）

- 零第三方依赖；中文注释/文案；改动配套 unittest；commit message 中文、仿 git log 风格。
- 任何真实订阅配置/节点信息**绝不提交**（测试 fixture 一律手工脱敏构造）。
- commit 用 `git -c user.name=voodookyo -c user.email=voodookyo@users.noreply.github.com commit ...`。
