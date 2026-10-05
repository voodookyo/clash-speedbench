# macOS Verge 服务模式控制器发现修复

实际环境：macOS arm64，Clash Verge 2.5.6／Mihomo 1.19.31。标准生成配置中的临时 Unix socket 已不存在，而当前 uid 的服务模式 socket 可用。修复前桌面窗口能够启动，但 Web 自动连接及目录绑定失败。

只在 macOS 自动配置目录可读时，把当前 uid 的固定服务 socket 加入候选，并保留同一配置文件的密钥。要求真实 socket、当前 uid 所有、非符号链接；不枚举其他用户、端口或进程。自定义目录不回退。目录归属校验也只允许标准生成文件绑定这个地址。空字符串“自动”仍覆盖启动时的目录环境变量。

实施委派：本地 normal-code --write，run 1acc47af-23ca-4846-9196-3e2067dd4c19，DeepSeek flash／OpenCode，HEAD baeb067，独立 worktree。worker 未提交、合并或推送，source HEAD/status 保持不变。控制器检查 diff 后补齐空自动目录与来源目录校验两个集成缺口，并用失败先行测试复现后修复。

验收：Python 3.9.25 与 3.12.13 分别运行 tests.test_controller、tests.test_source_catalog、tests.test_source_api、tests.test_config_root，各 118 tests，2.470s／2.428s，OK。覆盖缺失、普通文件、符号链接、异 uid、显式／环境自定义目录、密钥配对及 Web 自动入口。实际只读连接返回 /version；目录 status=ok，40 个加载节点均有稳定强身份，未输出密钥或原始节点凭据。

真实测速、重建后的原生 GUI、其他平台仍需分别验证。这次只读连通性不是带宽或订阅服务器测速成功。
