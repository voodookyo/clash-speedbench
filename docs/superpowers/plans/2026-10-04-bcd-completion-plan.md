# B/C/D 接续完成计划

批准依据：2026-10-04 用户要求先做 ai-dev delegation assessment、优先本地路由，再完成 B/C/D 全部任务。此清单细化已批准规格，不降低原验收标准。起点：clean `e916af4`；各阶段整项仍未验收。

## Delegation assessment 与责任

- Codex 负责最终计划、核心资源生命周期、整合、审阅裁决、验收及本地提交；不 push/tag/Release，不替换现有安装或改用户数据。
- 已检查 `~/.ai-dev` registry/routes/policy/health，使用 `repo-search --read-only` 完成公开源码与规格的独立缺口分析（DeepSeek flash / OpenCode）。输出为线索，须由控制器逐项核对。
- 在 valid committed HEAD 上尝试 `long-code --write` 的隔离候选策略子任务。路由选择 K3/Claude，但实际执行立即返回 `Not logged in`，无写入。read-only 健康记录不能证明 write harness 已登录；禁止修全局认证、直接写源树或自动创建 baseline。后续实现暂由 Codex 执行。
- 无 eligible `normal-code --write`；不使用 UNVERIFIED/manual-only 组合。完成可审阅的改动后，按当前健康状态使用本地只读审阅路由，不把外部建议视为最终结论。

2026-10-04 后续更新：用户明确通知路由已更新，并指定继续使用 normal-code。已重新核验 registry/routes/policy/health，normal-code 六个 OpenCode 候选均有 VERIFIED 隔离写入记录、GREEN 健康且在 write allowlist 内；先用该路由做本批只读审阅。前述无写路由结论是更新前的执行记录，不再作为当前限制。后续写委派在当前实现完成检查并本地提交后从有效 HEAD 启动，继续禁止直接写源树／自动 merge、commit、push。

## 执行顺序及完成门槛

| 批次 | 具体工作 | 最小实现与验证 | 完成依赖 |
|---|---|---|---|
| B1 目标策略 | 日常/下载/IP/住宅目标可解释候选；保持来源/已知地区代表、稳定 tie-break 和用户限定范围；只用近七天强身份历史提示 | 纯函数、历史读取、生产 worker 接入测试；CLI/Web 同输入默认和覆盖一致，legacy 不变 | 本机可实施 |
| B3/B4 生命周期 | DNS、worker 启动、connect/TLS、controller pipe/socket、provider 在途传输与 single-flight 等待、独立 v4/v6、下载、汇总/恢复的取消和预算 | 本地慢/失败 fixture，真实调用而非仅 Future.cancel；有界线程、逐节点独占带宽、确认 owned 资源回收；不能谎称无法保证的硬截止 | Windows pipe 真机验收另列 |
| B5 指标/留存 | 准备到清理 spans、五里程碑、实际字节、每指标状态、增量 migration 与所有阶段 partial | 针对缺口的生产路径测试；旧 raw 与失败/未请求/未知保留 | 本机可实施，跨平台矩阵另列 |
| B6/B7 性能 | 30/100/300 相同覆盖调度 fixture，冷/热情报缓存、失败和 IPv6 不可用；固定范围真实流量对照 | 报告首次结果/推荐/网络/情报/清理，声明 fixture 与实测区别；专项/全量、Python3.9/3.12 六格 OS 矩阵、独立审阅 | 真实节点流量授权及各 OS 执行环境 |
| C1–C4 合约/操作 | 按既有边界分离静态前端；串行 GLOBAL 回退须新 UI 显式确认；稳定 ID 切换/收藏/选择，实时目录与历史汇总、趋势/续接、每指标状态 | 后端契约与 JS 回归；操作不同来源/同名/未知/旧收藏/全部失败/partial，拒绝过期映射 | B 合约；本机浏览器可实施 |
| C5/C6 完整界面 | Verge 断开/无节点/超时/无 Key 的可操作错误；全部页面、键盘/focus/aria、明暗系统主题、窄窗口长名；浏览器/WebView 泄漏分开 | 真实浏览器逐页操作与截图；静态资源安全和无重复轮询；共享资源 native WebView 实际运行另列 | 原生桌面环境用于 WebView 验收 |
| D3/D6 数据与电源 | 明确选择的首次历史发现/预览/导入；同目录所有权、幂等迁移、旧 raw 保留、偏好/seed/一致 SQLite 备份与回退；休眠/唤醒 partial，无自动重试 | 临时目录与真实协议测试；实现有效的电源/恢复检测并作 native 验收，不把仅文本提示当实现 | 原生生命周期依赖各 OS |
| D3–D5 原生交付 | 锁版本 runtime/工具、Tauri 原生 build/package/run；单实例、托盘、通知、隐藏/退出/取消、前后端崩溃、信号/子树、无 Python、中文/空格路径、缺依赖 | Windows、macOS arm64/Intel、Linux 各自真实窗口及包；其他进程不受伤害、旧 Web 协调；安装升级前拒绝活跃任务 | 安装开发工具授权，Windows/Intel/Linux 机器或授权 CI |
| D7/D8 交付审计 | 运行资产白名单、许可证、锁定官方来源、包协议所需 SHA-256、unsigned 明示、手动更新；README 数据/升级/回退/依赖/泄漏边界 | 对最终冻结源码逐项核对、阶段报告、本地小提交；保留缺失平台/外部验收，不以 CI 配置替代运行证据 | 上述门槛全部满足才完成目标 |

## 执行规则

1. 每批先以针对性失败测试固定行为，再做最小实现；成功且 diff 未变不重复验。
2. 不因邻近问题扩写；不增加无必要抽象、重试、备份或校验清单。规格要求的迁移备份、运行资产完整性和验收报告仅服务其明确边界。
3. 全量测试与跨版本/原生验收在适当冻结点执行，记录版本、命令、结果和实际限制；portable/mock/浏览器与 native 证据分开。
4. 已提出的三个环境问题分别为项目隔离工具安装、最多五节点/100MB样本实测、Windows/Intel/Linux访问；需要回答的操作保持等待，其余工作继续。
5. 缺环境时不勾完成、不编造验收、不擅自 push 启动 CI。商业签名/自动更新按原规格明确 unsigned/disabled，无密钥不另扩范围。

## 当前记录

- [x] 当前路由 assessment 与只读缺口审阅。
- [x] Provider DNS/connect/TLS/HTTP 与等待者取消专项；原生 Windows 仍待验。
- [x] 目标候选、最终推荐与 CLI/共享 JS 公式对拍；同覆盖性能仍待验。
- [x] 串行逐次确认与固定身份范围；偏好往返及隔离浏览器专项。
- [x] 30／100／300 节点相同覆盖调度 fixture，冷/热缓存、70% 失败及 IPv6 不可用；Python3.14/3.12 本机运行。真实流量和原生平台不在此项证据范围。
- [x] 显式历史目录预览／冲突检查／合并、私有一致备份、幂等及受保护撤回；中断恢复先于启动写入。临时目录、HTTP、真实 Python backend 子进程、共享浏览器与 Python3.14/3.12 全量回归；原生迁移和断电门槛另列。
- [x] 更新后的 normal-code --write 从 clean eb2c31e 隔离完成桌面导入退出状态修复；Codex 检查限定 diff、源树保护元数据及 Python3.14/3.12 的 11 项桌面桥接测试后整合。私有导入产物 Git 忽略边界通过固定路径检查。
- [x] normal-code 只读导入审阅完成；两个 normal-code --write 从 clean 06fffc1 隔离完成桌面首页数据发现引导、SQLite-only 固定文件存在性、情报状态读互斥及导入总量上限。Codex 检查 diff／保护记录后手动整合，28／32 项专项与真实浏览器键盘、明暗、390px、导入／撤回同步验证通过；原生首次启动门槛另列。
- [x] D 休眠恢复源码接入：normal-code --write 从 clean f85b663／45c371a 隔离产出；第二轮因超时／403 未完成，由 Codex 在已结束的隔离工作区补足审阅修正和真实自有 Python 子进程取消测试后手动整合。两版本 76 项时钟专项、Python3.12 的 87 项接入专项、Python3.14/3.12 的 1132 项全量回归通过；仅记录固定原因计数，不自动重跑。实际 OS 休眠、原生窗口／包及平台矩阵仍未验收，见 desktop-power-recovery 报告。
- [ ] B 实现与全部验收。
- [ ] C 实现与全部验收。
- [ ] D 实现与全部验收。
- [ ] 最终逐规格完成审计。
