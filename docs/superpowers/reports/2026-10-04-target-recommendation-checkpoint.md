# 使用目标的最终推荐检查点

日期：2026-10-04。承接 `5d23232`，继续已批准 B/C 子任务；不代表全部 B/C/D
验收完成，没有桌面包、真实节点流量、系统代理或用户历史变更。

## 行为

五种使用目标接入 CLI 排序、表格目标分、CSV、历史顺序及显式自动切换。
`speedbench_profiles.py` 与 `web/profiles.js` 对同一归一化结果采用既有公式；
前端从 app.js 提取纯评分责任，保留标准库后端与静态零依赖前端。
Overall/Network/样本保持原值，默认旧 CLI 仍保留排序与 CSV 表头；显式
非综合目标的 CSV 追加 target_profile/target_score。部分结果也沿用该目标。

新网络模式按完整带宽、已保留有效样本的部分带宽、失败、未测分组，再按
目标分推荐；IP/住宅不按带宽分组，下载不读取 IP 分类。IP 专项没有下载
冠军或下载首次推荐里程碑。自动 IP/住宅推荐要求已观测的 Quality/Grade，
不把旧基础 flags 的中性分数作为自动 IP 推荐证据。

新模式和 backend-child 自动切换前重新读取 runtime/catalog，要求强身份
仍一致且有效 Selector 包含冠军；验证改名可使用新 runtime 名，不重写
测量结果。未知/来源变更/组失效时保持原选择。读写位于取消作用域，恢复
路径保持分离。旧 CLI 默认自动切换行为兼容。

共享表格、收藏与地区榜用同一比较器。历史新轮次的冠军按保存的目标和
覆盖排序；新历史 API 只补传评分所需的白名单字段/目标，旧无 task/scope
历史维持原 slim 格式，不重写 raw。静态和 desktop/legacy 构建白名单显式
加入新 Python/JS 文件；没有执行打包或资源摘要生成。

## 审阅裁定

本地 K3/Claude read-only leaf 审阅 verdict 为 no_blocking_issue。GLM 当前
health 为 RED/连续 timeout，未重复尝试，独立模型覆盖仍为 partial。
审阅者越过文件名单读取了相关公开模块；controller 不认可该越界，并
独立核实所用线索，未读取私有配置或其他审阅结果。

已修正并加回归：partial 样本与 failed 排序、空画像的 JS/Python 真值差异、
中文/emoji 完全并列排序、升序保留覆盖/未知规则、历史冠军旧口径、新模块
HTTP MIME/Host 分发。另补 IP 自动推荐资格与 IP 专项下载里程碑回归。
F3 关于 Python 对 null 自动返回 None 的说法与源码不符；非归一化 null 行
和嵌套/字符串 score 不在实际 Result 合约内，没有为此扩写防御层。
主干 CI 分支名称的疑问未核实远端，未修改工作流触发条件。

失败先行用例复现 target 参数未接入、空画像公式不同。全量发现给旧历史
附加 IP 字段违反 slim 兼容断言，改为仅新 task/scope 记录补传，旧断言保留。
专项：先 87 项，再审阅修正后的 53 项，OK；3.12 目标专项也通过。
最终全量结果在下面记录，不把初轮失败或中间成功当成最终冻结验证。

隔离浏览器实际加载新静态评分模块，运行 synthetic standard/download、
切换下载 Profile、查看完成结果；无 error/warn。截图临时保存到
`/tmp/clash-speedbench-download-profile.png`，服务和 tab 已关闭。它是浏览器
fixture 证据，非真实性能、Mihomo 或 native WebView 验收。

## 最终验证与剩余任务

- `python3 -m unittest discover -s tests -q`：Python3.14.7，
  **971 tests / 32.331s，OK (skipped=6)**。
- `/opt/homebrew/bin/python3.12 -m unittest discover -s tests -q`：Python3.12.13，
  **971 tests / 32.295s，OK (skipped=10)**。
- `git diff --check` 通过。macOS 跳过 Windows pipe/ACL；3.12 另缺可选
  PyYAML。没有 Python3.9、Rust、其他 OS 或 native GUI 成功声明。

B 尚需全准备/汇总取消边界、30/100/300 同覆盖 fixture 与限定实测、
Python3.9/各 OS 矩阵。C 尚需完整页面/错误/键盘/主题与 WebView 验收。
D 首次历史安全导入、电源恢复、原生构建/GUI/安装升级均未完成。
先前三个授权问题保持待答，不把它们视为已批准，也不降低整阶段门槛。
