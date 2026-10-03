# 任务事件与共享界面实施检查点

本报告为增量检查点，不代表 A–D 全部交付。未 push/tag/Release，未替换 Downloads 安装，未修改真实 Verge 或进行真实带宽/付费 API 测试。

## 已实现

- 70e1377：结构化任务事件、SSE/snapshot/resync、任务取消、部分结果、task_runs/task_metrics。
- 35e85d0：IPv4/IPv6 独立发布、生产计时、实际 curl 字节；缺失/未测不伪装失败或满分。
- 共享 UI：来源/手选/稳定 ID 收藏，目标和模式，预算与实际流量区分，折叠高级设置与日志，任务中心，刷新续接，浅/深/系统主题，键盘展开、确认对话框焦点。
- tasks.js 管理协议/续接/非敏感偏好；view.js 保留增量表格焦点/展开证据。既有 app.js 页面渐进保留，未重写测量公式。
- 历史趋势各入口传 node_id，来源趋势传 subscription_id；旧名字历史与未知来源不自动合并。ip_profiles 增量 node_result_id 链接，仅唯一旧 run/name 回填，runs.raw 不改写。

## 本机验证

- 事件/取消/计时完成时，Windows Python 3.9.25 / 3.12 / 3.14.7 各 665 tests，OK，6 skipped。
- UI 首轮全量 674 tests，OK，6 skipped；补充身份/计时测试后，Windows Python 3.9.25 / 3.12 各 677 tests，OK，6 skipped。
- 最新针对 UI、profiles、resume、安全测试 42 tests，OK。提交前 Windows Python 3.14.7 全量重跑 677 tests，OK，6 skipped；git diff --check 通过。
- 隔离 UI fixture：tests.ui_fixture_server，临时数据库，127.0.0.1:8965；未连接 controller，所有数字明确为 fixture，不构成测速性能证据。
- 浏览器实际验证：开始 → 刷新续接 → 单一任务 → 任务详情；取消保留 3 条已完成 probe 行；收藏范围 1 节点/手选；HTML/引号/emoji 转义；键盘 Enter 展开，后续事件到终态仍保留焦点和展开行；浅/深主题；760px 窄窗 body 宽 745px 无水平页面溢出；泄漏/设置页保留科学边界；控制台无相关 error/warn。

## 视觉参考五点比较

参考为本机 Image Gen 的任务导向概念，已与实际浅色截图用 view_image 对照；不是嵌入的图片 UI，也不声称像素级 10/10。

1. 层级/布局：210px 侧栏 + 配置/任务/结果分区符合参考，旧历史/订阅导航顺序保留。
2. 颜色：白表面、浅灰背景、石墨文字、indigo 强调；深色使用独立变量，无远程字体。
3. 控件：三列等宽来源/目标/模式与清晰开始按钮；高级设置采用原生 details，位置与概念略有不同。
4. 信息密度：实际加入范围/预算/未知剩余说明，比概念更密；不删除必要的测量边界来追求截图简洁。
5. 状态与表格：来源副标题、IP N/A、有限覆盖推荐、任务阶段分离；概念的桌面窗口 chrome 属于 D，当前浏览器不模拟已交付桌面。

截图本机位置：`.codex/generated_images/01a04bf1-7164-7b20-9bf7-4fa9066c62f5/speedbench-ui-qa*.png`。只作本机证据，不提交生成图片。

## 未完成项

- B：生产历史候选提示/目标排序、全部阶段取消与清理预算、同覆盖调度基线、完整资源回收检查。
- C：配置根目录选择、更多职责拆分、完整异常与证据矩阵、所有页面交互、桌面 WebView 环境区分。
- D：真实 Tauri 客户端、bundled Python、握手/目录锁/进程树、跨平台包、无系统 Python及生命周期验收；当前没有桌面产物或跨平台成功结论。
- 6 格 CI 未运行，不以 Windows 本地结果代替 macOS/Linux。

下一步继续完成未完成项。所有勾选以规格实测证据为准。
