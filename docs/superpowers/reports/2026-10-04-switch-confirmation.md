# C 手动切换新鲜确认

规格依据：stage-c-shared-ui-design 的切换目标／订阅／策略组确认与过期映射拒绝。原 UI 已有文本确认，但使用缓存 currentGroup，不能证明它就是后端实际选择的 Selector。

实现：认证 POST `/api/switch/preview` 解析当前 node_id 或唯一运行名称，返回字段白名单计划。UI 展示当前目标、来源、实际 Selector 与其当前选择；取消无切换。确认时重新读取目录／策略组／配置目录版本，逐字段比较后才 select；变化提示刷新，不回退到旧名称。旧 API 未携带确认计划的调用保持兼容。计划不含目录路径、配置、凭据。

委派：重新核对本地 registry/routes/policy/health；normal-code 六个 OpenCode 组合 VERIFIED、write allowlist、Git worktree 隔离、自动 commit/merge/push 均关闭，但健康记录已有超时／403。写任务 `c754b3ae-0a03-48bc-a2e1-93d9c99f0d2b` 从 clean a164efa 开始，GLM flash、GLM、MiniMax 连续超时，DeepSeek flash 首次超时后第二次成功。日志确认 source HEAD/status unchanged、write_isolated=true；tests 结构字段为 unknown，worker 文字声明 47 项通过，控制器另行实测，不以该声明替代验收。

Codex 审阅补正：预览也校验计划并在 STATE_LOCK 中核对配置目录版本；确认请求 node_id 必须和计划一致；长 Unicode 名称可确认；来源未知／多来源不使用遗留订阅名称猜测。实浏览器发现预览禁用按钮丢失焦点，增加显式返回按钮及替换行恢复；成功切换后把焦点移到结果行。修复合成 UI fixture 原先无效的 switch_node stub，用自有内存 controller 运行真实预览／确认代码。

验证：Python3.14 隔离区 51 项 source_api/task_ui_js + 新增切换后焦点 1 项通过；Python3.12.13 最终隔离区 source_api/task_ui_js/web_api_node/ui_fixture_lifecycle 共 66 项、23.717s，OK。手动应用后源仓库 Python3.14.7 的 source_api/task_ui_js/ui_fixture_lifecycle 共 53 项、22.948s，OK；node --check、git diff --check 通过。涵盖身份／名称／策略组／当前选择／来源／root revision 变化拒绝、Host/Origin/token、唯一名称解析、未知／弱身份、legacy 与长名称。

实际浏览器：127.0.0.1 合成 fixture，快速任务生成三行；确认显示真实 fixture 计划，HTML/引号/emoji 仅作文字；Esc 返回切换按钮，Enter/Tab/Enter 切换后当前节点及使用中标记更新、焦点落到结果行。截图 `/tmp/clash-speedbench-switch-confirmation-final.jpg`。全部控制器／下载／历史均为临时或合成，无真实 Verge、节点流量、付费 API、用户历史。

C/B/D 整阶段保持未验收：后续指标元数据已完成本机合约验收，见 result-metadata 报告；该冻结点两版最终全量各 1185 项通过，并更新旧 source_js 回归为 preview→确认协议。真实流量、原生 WebView／休眠／各平台包和六格 Python 矩阵依赖待答授权及执行环境。本次不是原生 GUI 或真实控制器验收，无独立外部只读审阅成功记录。
