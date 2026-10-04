# 显式历史目录导入与中断恢复

范围：在 `e030fad` 上续接 C/D 已批准的数据迁移要求。此批为源码、临时目录和共享浏览器验收，不是新版桌面包或全部 B/C/D 完成结论。

## 实现

- 设置页展示本实例数据位置；用户明确输入旧目录、预览、确认源程序已退出，再确认合并。没有自动导入，不读取浏览器 profile。
- 只读固定 JSONL/SQLite 文件；有 JSONL 时以其为准，并提示未包含的数据库记录数。保留精确 raw（含空白及合法 Unicode）、已有数据库 ID 和来源身份；同一时间戳／任务 ID 的不同数据阻止合并。原目录不变，源偏好、seed、provider 缓存不迁入。
- 导入前保存本实例 JSONL、一致 SQLite、偏好、seed 私有备份。SQLite 从私有 DB/WAL 副本读取，再用 backup API 生成一致备份，避免在源目录创建辅助文件。机制依据 [SQLite WAL 文档](https://sqlite.org/wal.html) 和 [SQLite backup API](https://sqlite.org/backup.html)。
- 预览十分钟有效，源或本实例变化使其失效；已有相同记录时幂等且不新增备份。旧未结束任务导入为 interrupted/partial，不重新测速。
- 已持有的 backend lease 配合 task writer lease；源活跃 lease 阻止预览。历史／目录／偏好读写与两文件替换互斥，任务准入和退出接口有导入屏障；任务状态、事件与取消不被数据锁阻断。
- prepared/applied/rolling_back/rolled_back 事务保存私有收据；启动在其他数据写入前核验恢复。未知新数据、缺失备份或不安全文件停止恢复。撤回取得 writer lease 后再次复核，拒绝删除新历史。
- 历史、趋势、最近候选提示按实际时间排序；带时区与无时区旧时间统一解释，不重写时间戳/raw/ID。无时区按本机时区；无效旧时间仍保留，不进入日期窗口。
- UI 丢弃过期预览和撤回前发出的任务详情响应，清理已撤回详情，按时间戳保留有效历史选择；确认取消及完成后的焦点恢复。模块加入显式运行资产白名单。

## 验证

冻结生产改动后执行：

| 环境／命令 | 结果 |
|---|---|
| macOS arm64，Python 3.14.7；`python3 -m unittest discover -s tests -q` | 1016 tests，34.049s，OK，skipped=6 |
| 同机 Python 3.12.13；`/opt/homebrew/bin/python3.12 -m unittest discover -s tests -q` | 1016 tests，34.070s，OK，skipped=10 |
| `node --check web/app.js`、`git diff --check` | 通过 |

新增 36 项测试覆盖精确 raw/幂等/冲突、读源 WAL 且源文件不变、备份私有权限、DB-only 旧历史、源 JSONL 权威、active task partial、预览变化、源活跃 lease、所有权、各提交／撤回中断点、未知新数据保护、HTTP token/Host/Origin 和严格请求、导入准入屏障、取消通道及 UI 确认／过期响应。真实 Python backend 子进程验证恢复发生在私有 bootstrap 与启动 DB 写入之前；未知更改时无 bootstrap、无新 DB 写入。

独立 Codex 只读审阅发现并已修复：撤回获得 writer lease 前的新写入竞态（P1）、合法 Unicode 分隔字符被错误拆行（P2）、applied 收据到 LAST 指针之间中断后找不到备份（P2）、撤回后任务详情／迟到响应残留（P2）。前三项独立复核关闭；第四项失败先行回归及完整回归通过。第二轮审阅因该 agent 使用额度耗尽结束，不能算完整审阅。

用户更新路由后，重新检查 registry/routes/policy/health：normal-code 候选的 OpenCode 写隔离记录为 VERIFIED，健康为 GREEN，write allowlist 匹配；K3 已改为独立 OpenCode 身份。按用户要求通过 normal-code 发起本批只读审阅，执行结果在后续记录中补齐，尚不将健康探测等同于完成审阅。

随后从 clean `eb2c31e` 发起 `normal-code --write`：GLM 5.3 / OpenCode 首次超时，路由自带第二次尝试成功。隔离 worktree 仅修改 `speedbench_desktop.py` 与 `tests/test_desktop_bridge.py`；元数据确认 source HEAD/status 未变，auto commit/merge/push 均为 false。Codex 检查 diff 后手动整合：导入未在退出等待截止前结束，或遗留 import_failed 时，backend 返回 2；及时完成则返回 0。三个新增用例驱动生产 main/shutdown，使用临时目录、私有管道及 fake clock，不等待真实 25 秒。控制器在隔离目录以 Python 3.14、整合后以 Python 3.12 执行 `tests.test_desktop_bridge`，各 11 项通过；`git diff --check` 通过。路由测试结构字段为 unknown，以实际执行结果为依据。

导入备份目录、pending/last 指针及原子写临时文件加入 Git 忽略规则；`git check-ignore --no-index --stdin` 验证固定生成路径，包括中断暂存 raw 和私有收据。无新用户数据文件或额外备份。

真实 Codex 浏览器只连接独占临时 fixture，完成中文／空格路径预览、未关闭源确认时合并禁用、Escape 取消及焦点恢复、显式合并、历史回放、显式撤回、刷新后备份发现、浅色／深色与 390px 窄窗口。窄窗口内容宽 375px，小于 390px；完成后焦点回到预览按钮。截图 `/tmp/clash-speedbench-history-import-narrow-final.jpg`。自有临时服务已停止、浏览器临时 viewport 恢复、标签页关闭；未调用真实节点或付费 API，未改用户历史、seed、Verge、系统代理或全局 AI 配置。

## 未完成门槛

- 本批进程中断测试不证明突然断电的文件系统持久性；Windows ACL 与原生 WebView 迁移仍需对应环境。
- 无新版 native build/package/run；最新 Windows 原生包仍为先前冻结源码。macOS Intel/Linux、六格 Python 3.9/3.12、休眠／唤醒、安装／升级与全部 GUI 生命周期仍未完整验收。
- 项目隔离工具安装、限定真实流量、其他原生执行环境的已提出问题仍等待用户回答；无 push/tag/Release、下载安装覆盖或新基线吸收。
- B/C/D 整阶段与最终完成审计仍未勾选。后续委派写入必须从此批具体实现提交后的有效 HEAD 出发，用 `ai-run normal-code --write` 隔离并检查返回 diff、测试与源树保护元数据。
