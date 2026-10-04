# C 共享界面键盘与合成场景验收

依据 stage-c-shared-ui-design 的键盘、focus、历史身份、错误/partial、主题与窄窗口验收要求。本批是 C 的子项检查，不代替整阶段或原生 WebView 验收。

## 实现与发现

历史轮次、订阅选择、地区榜和排序使用原生 button；保持表格 th 语义，更新 aria-current、aria-pressed、aria-expanded、aria-sort，并在列表重绘后按稳定标识恢复焦点。历史节点键盘激活复用当前轮次的鼠标选择，不再通过 gotoTrend 跳到最新轮次；失败首行仍保留 node_id。泄漏检测禁用按钮结束后仅在焦点退到 body 时返回按钮，不抢走用户已经移动的焦点。

实际历史页面发现：多节点轮次中，失败节点没有出口 IP，信誉查询却把唯一一条其他节点情报当作它的情报。修正 speedbench_db 的 intel-only 回退，要求整轮只有一个节点且只有一条情报；单节点旧记录兼容保留。新增失败测试先复现跨节点借用，再验证修复。

显式测试 fixture 增加三个真实 raw/SQLite 历史轮次、订阅/节点改名、来源多义/未知/旧格式、同 IP 信誉恶化及 IP 变更；增加断开/空目录/超时、全部探测失败和部分任务。仅选择 --leak-fixture 时替换浏览器 IP/STUN 传输，生产 evaluate/save/auth/history 代码不替换，页面持续标记合成输入。fixture 不使用继承的 provider 凭据，退出等待自有运行线程完成写入后清理临时目录。版本查询的合成比较也与本地 stable/alpha 版本一致。

## 本地路由与整合责任

重新读取 registry/routes/policy/health，在 clean 3ec1f11 上启动两个 normal-code --write：0e771817-d17e-4ae0-9dbd-f1f43c08e436（键盘）和 c52c9567-b0a7-4450-80a5-407e11dc46c3（fixture）。候选组合在 VERIFIED/allowlist 内；实际 GLM flash/GLM/MiniMax/DeepSeek/Kimi 路由均因超时或 403 失败，两个调用 exit=1。第一个仅留下三个 Web 文件草稿，第二个没有代码产出，没有 worker 成功或测试通过证据。

两份返回记录确认 source HEAD/status unchanged、write_isolated=true，自动 commit/merge/push=false。Codex 在已经结束的隔离目录中审阅、补足实现与测试，再手动整合；未改 ai-dev、认证或用户配置。本批没有成功的外部只读独立审阅。

## 验证与实际浏览器

最终同一功能 diff 的 Python3.14.7 / Python3.12.13 各 110 项专项通过，40.368s / 39.861s，OK：navigation_ui_js、ip_history_db、source_history、web_api_node、ui_fixture_scenarios、ui_fixture_lifecycle、source_js、task_ui_js、resume_js、web_security。node --check web/app.js、git diff --check 通过。没有重复跑未经改动的全量测试；此前 1185 项全量属于 result-metadata 冻结点，不能作为本批全量证据。

浏览器使用自有 localhost fixture 操作全部七页：Enter/Space 选择历史轮次/订阅/失败节点与排序，确认稳定 ID 和焦点、旧轮次不跳转；合成泄漏“无法确认→保存→刷新历史”；无 Key 设置、合成 Release 查询；快速任务刷新续接、确认取消先显示清理再终止、刷新后的任务中心回放部分结果；断开/无节点/目录超时、全部失败均实际呈现。深色主题、390px 长名称与浅色常规窗口均实际查看；390px 文档宽 375px，没有页面水平溢出，表格内部可横向滚动。主 fixture 页面浏览器 warn/error 为空。

截图：/tmp/clash-speedbench-c-subscriptions-keyboard.jpg、/tmp/clash-speedbench-c-narrow-dark.jpg、/tmp/clash-speedbench-c-cancelled-light.jpg、/tmp/clash-speedbench-c-all-failed.jpg、/tmp/clash-speedbench-c-partial-replay.jpg。最后一张是刷新后任务中心的真实 SQLite 任务回放；合成 runner 不写生产 benchmark raw，本次不声称主页刷新会回放刚结束的合成任务。自有六个服务均正常 exit=0，浏览器标签已关闭、viewport 已恢复。

## 剩余门槛

本批确认仍需补齐订阅页“实时目录/历史汇总”显式区分、历史轮次模式/范围/耗时/流量/partial/指标覆盖，以及空目录和全部失败的更直接引导。共享静态资源的原生 WebView 操作、真实控制器/流量、休眠及其他平台矩阵仍待授权/环境。B/C/D 整项保持未验收。
