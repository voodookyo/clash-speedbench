# Provider 取消、目标候选与串行确认检查点

日期：2026-10-04。本批是 B/C 子功能检查点，B/C/D 整项尚未完成。
起点为 `e916af4`，不改交接包，不 push/tag/Release，不产生桌面安装包。

## 实现

- `speedbench_process.py`：取消作用域内的 TCP/Unix connect 使用 nonblocking
  polling；DNS 在本任务持有的独立 Python 子进程中执行并等待回收；TLS 使用
  原验证 context/SNI，在同一绝对连接预算内轮询 handshake。HTTP send/read
  共用该请求绝对预算，取消时关闭本请求资源。作用域外的恢复请求保持原行为。
- `speedbench_ip_intel.py`：urllib HTTP/HTTPS handler 显式传递取消作用域，
  保留禁止 redirect/环境代理、HTTPError 与 response 所有权；single-flight
  等待者可退出而不取消独立 owner。自定义不合作 provider 仍须等待其返回，
  不承诺任意函数或内核资源的硬截止。
- `speedbench_tasks.py`、`speedbench_db.py`、`speedbench_workers.py`：新模式
  候选先保留订阅/已知地区代表，再应用日常、下载、IP、住宅历史提示；仅近七天
  强身份使用提示。日常纳入此次 probe 抖动/失败统计；IP 提示只读取本次历史
  出口对应的画像并取较差地址族。历史不复制为此次成绩，legacy 排序保持兼容。
- Web 默认拒绝静默串行回退。串行需 legacy/workers=1 和此次明确布尔确认；
  仅该确认启动携带 opt-in。UI 列出 GLOBAL/策略组影响并刷新当前节点，冻结
  确认时的强节点身份。空或不可核验范围拒绝启动，目录新成员不能扩大任务。
  串行预算按全范围计算；前后端偏好均允许 legacy，但不保存启动确认。

## 验证与审阅

专项覆盖 stalled HTTP header/body/error body、总预算、TLS handshake、DNS
子进程取消/超时回收、TLS 校验失败、single-flight waiter、候选历史/范围及
首次生产选择、CLI/API 串行拒绝、逐次 UI 确认、订阅新增成员与偏好往返。
新增失败先行测试均修正；没有真实节点或付费 API 调用。

最终冻结本批工作差异的全量命令：

- `python3 -m unittest discover -s tests -q`：Python 3.14.7，
  **959 tests / 31.922s，OK (skipped=6)**。
- `/opt/homebrew/bin/python3.12 -m unittest discover -s tests -q`：3.12.13，
  **959 tests / 31.915s，OK (skipped=10)**。
- `git diff --check` 通过。Windows pipe/ACL 在 macOS 跳过；3.12 另缺
  可选 PyYAML。未安装依赖，不把 skip 当 native Windows 或 Python3.9 通过。

本地 `ai-run review-k3 --read-only --timeout 300` 分别审阅 transport 与
候选/串行实现；controller 核对并修复 DNS 畸形输出分类、前端 legacy 偏好
遗漏、确认范围扩大及过期当前节点展示。部分 advisory 不适用于实际生产
调用或 stdlib 已有关闭行为，未扩大实现。K3 第二包越过显式文件边界读到
preferences.js；该提示经 controller 自行核实采用，不能据此认可越界。
GLM 路由此前连续超时/不可用，本批独立模型覆盖为 partial；不修全局路由。

真实浏览器仅运行临时 localhost fixture：确认/取消、重复确认、刷新后重选、
宽/窄窗口、键盘焦点、任务取消后 partial、legacy 导出/预览。末次760×900
无 document 横向溢出、长名称转义、无 error/warn；截图暂存于 `/tmp`。
fixture 后端及浏览器已关闭，未读真实数据。此证据不代表 Tauri WebView。

## 未完成边界

目标 Profile 的最终 CLI/自动切换推荐尚待统一；全准备/汇总取消预算与
30/100/300 同覆盖 fixture、限定真实性能对照尚待完成。C 全页面/错误与
可访问性矩阵仍待逐项验收。D 首次历史导入、电源恢复及所有原生桌面矩阵
未完成。开发工具安装、真实网络测试、Windows/Intel/Linux executor 的
已提出授权问题保持待答，不据此停止可独立推进的源码工作。
