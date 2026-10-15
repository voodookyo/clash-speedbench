# 控制器自动发现实现计划

设计已于 2026-10-02 经用户确认。writing-plans / test-driven-development 技能未安装，采用以下可审计步骤。

1. 运行当前全量 unittest，记录基线；添加使用临时文件和 mock API 的 controller 回归测试，先确认失败。
2. 新增独立 speedbench_controller.py：跨平台配置路径、受限 YAML 顶层标量解析、仅本机地址规范化、地址/密钥配对、脱敏。
3. 在 clash_speedbench.py 中新增统一 connect_controller；保留 legacy detect_controller 接口；CLI 支持显式空密钥优先级、非交互失败。
4. Web 当前节点/切换/测速预检接入统一入口；密钥不进命令行；子进程自己重新发现；测试用 mock 阻断真实预检。
5. 补齐隔离、安全、认证重读、HTTP/日志脱敏、worker/provider 无自动密钥传播测试；更新 README、CI AST、双平台打包清单。
6. 运行针对性测试、全量 unittest、AST Python 3.9 语法检查、git diff --check；检查每个新增模块的发布清单。
7. 使用本机控制器只读验收；记录真实 Windows 与模拟 macOS/Linux 的区别。
8. 校验下载版文件和后台状态；备份精确替换文件并应用经过验证的修改，保留历史。只读验收下载版，不运行测速/切节点。
9. 保存测试/验收结果，提交小范围本地 commits；不 push、不 tag、不创建 Release。

实现文件责任：本次均由主 Agent 完成，未新增子 Agent。
无需变更数据库 schema、测速算法、launchers 或用户 Clash 配置。
