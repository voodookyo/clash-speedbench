# C 实时订阅与历史轮次证据

接续 shared-ui-navigation 报告明确列出的 C 本机缺口，依据 stage-c-shared-ui-design 的实时目录/历史区分、历史模式/范围/耗时/流量/partial/覆盖、可操作空/失败提示和窄窗口要求。

## 实现与审阅

订阅页新增实时目录，独立呈现已加载、未加载和来源未知，说明多来源节点不能跨订阅相加；历史汇总保留原 subscription_id 统计与旧来源未知。实时选择按钮仅把已加载 subscription_id 带到节点页，并将焦点交给范围控件，不启动任务、不切订阅。目录刷新失败清除旧目录缓存，给出 Verge/外部控制器/刷新操作，历史请求独立进行。

历史页增加模式/目标、完整或部分状态、原范围与已返回节点、耗时、已报告真实字节，以及 probe/bandwidth/intel 的完成/失败/未请求/未选择/取消/未知计数。新 history-view.js 只描述白名单字段，用 textContent 展示；源码 Web 与桌面资产白名单均包含该静态模块。兼容 mode 不猜测为串行执行。

CLI 在来源、身份、名称、provider 与 limit 过滤后保存 selected_node_count，完整和失败/取消 raw 均可保留该值；旧 raw 不修改。/api/history 按确切 job_id 批量关联 SQLite task_runs/task_metrics，失败或清理终态不能被此前 raw 的 completed 标签掩盖。实际字节从已保存 metrics 求和，不用 MB×轮数预算；缺 checkpoint/metrics、CLI 旧记录、未记录原范围均显示未知，不按时间戳关联任务、不臆造耗时。无数据库 schema 变化，/api/latest 原 raw 回放不变。

确认空目录时，开始操作给出“在 Verge 加载后刷新”并不发新 job。全部失败或当前 Profile 缺必要观测时明确没有可推荐结果；切换 Profile 同步更新此说明。窄窗口实测发现历史主栏按表格固有宽度撑开页面，给纵向布局的 hist-main 设置 width:100%，表格保留内部滚动。

## 执行边界与验证

Codex 继续在已结束的 normal-code --write 隔离工作区完成这些接续修正。此前两个 normal-code 任务全部候选超时/403，重新读取路由、policy、registry、health 后六个候选仍 RED，重复委派开销已超过这些窄改动；未新增自动重试、未改全局环境。源仓库保留 clean 98d25db，功能检查与 diff 审阅后手动整合；没有新的 worker 成功或外部独立审阅证据。

先用真实 HTTP/SQLite/CLI history 测试复现两项缺失字段失败和一项 scope 缺字段错误。首次 75 项检查还暴露旧“空目录可启动”正向 fixture 与桌面固定资产预期需按新合约更新；明确保留无节点负向测试，不删除断言以隐藏行为。随后 83 项专项、34.265s，OK。涵盖精确 job 关联而非相同时间戳、raw 不变、无 checkpoint 未知、数值/状态白名单、实际 CLI partial 范围、实时目录计数/转义/不启动和 Profile 切换。

功能冻结点：Python3.14.7 与 Python3.12.13 各 1207 项全量，81.763s / 81.672s，OK（skipped=6 / 10）。全量后仅修改窄窗口一行 CSS 和未知完整性中文文案；最终 7 项 history_summary 专项与实际浏览器渲染通过，不重复全量。node --check 和 git diff --check 通过。

真实浏览器仍使用自有 localhost 合成数据：最新轮显示 4/5 节点、1.75s、0.50MB、已取消/部分及独立覆盖；旧轮显示未知原范围/耗时/流量/完整性。订阅实时3节点与历史2节点、未加载订阅、旧来源未知并列；Enter 带入 opaque ID、焦点落 f-source，没有启动 job。无节点开始保持空历史；全部失败呈现无推荐及下一步。浅色常规窗口、深色390px历史/订阅均实际操作，修复后文档宽375px、主栏347px，无页面水平溢出；该浏览器 warn/error 为空。

截图：/tmp/clash-speedbench-c-history-metadata.jpg、/tmp/clash-speedbench-c-live-history-sources.jpg、/tmp/clash-speedbench-c-all-failed-guidance.jpg、/tmp/clash-speedbench-c-history-narrow.jpg。三项自有 fixture 正常 exit=0，标签已关闭，viewport 已恢复。无真实 Verge 设置、节点/STUN 流量、付费 API 或用户历史操作。

C 整阶段仍等待原生 WebView、真实 controller/流量和其他平台门槛；B/D 与最终逐规格验收也保持未完成。安装开发工具、真实网络与外部执行的既有授权问题仍待用户答复。
