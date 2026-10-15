# 阶段 A：订阅归属、节点身份与历史兼容

日期：2026-10-03。状态：规格已确认，实施中。依赖：现有控制器连接层。

## 问题与交付

当前 `provider-name` 不足以代表 Verge 订阅。来源修复必须从本机元数据到新任务、历史、UI 全链路生效，而不是为表格硬填显示名称。

交付订阅目录、带证据的节点映射、版本化稳定节点身份、独立历史来源关系、CLI/Web 选择接口与 fixture。保留现有 provider/node_key 字段和旧接口，不改变旧 raw 数据。

## 目录与读取边界

新增来源适配模块，输入为 controller snapshot 与显式/自动发现的 Verge 配置根目录。复用已有配置目录发现，不在全磁盘搜索配置。

读取 `profiles.yaml` 的 UID、name、type、file、current 和运行配置中对应的节点定义。订阅 URL、Authorization、节点认证字段只能参与本机内存匹配，不返回 API 或作为错误文本。

元数据按机器生成 YAML 子集安全解析，严格限制文件大小、嵌套深度与集合数量；不执行 tags、脚本或 YAML 对象构造。已有解析器可以通过窄接口复用，但要给 profiles 特殊格式单独 fixture；不能因为现有 proxies 测试通过就假定所有 profile 可解析。

profile 文件解析的最终路径必须留在解析后的 `profiles` 根目录内；拒绝绝对路径、`..` 越界及符号链接越界。缺失或不支持格式产生安全状态，不输出原始 YAML 行或敏感路径内容。

目录同时列出已保存订阅与当前已加载范围；只对已加载可安全识别的节点提供默认测速。未加载项显示“未加载”，禁止通过本功能隐式切配置、下载订阅或执行 Merge/Script。

## 统一数据模型

`SubscriptionSource`：

- subscription_id：本机范围的稳定不透明标识，来源目录命名空间 + Verge UID 生成；不等于显示名称。
- name：允许改名的用户显示名称；原始历史保存该轮 snapshot。
- kind：remote/local/provider/unknown。
- loaded、available、mapping_status：区分已保存、已加载、无法解析。

`NodeOrigin`：

- node_id、identity_version=2、runtime_name。
- subscription_ids：可以多值；subscription_name 是该轮唯一来源的兼容显示值。
- provider_names：内核 provider 独立字段，不假称订阅。
- source_status：verified/ambiguous/unknown。
- evidence：非敏感说明，记录匹配途径，不记录完整节点配置。

如果来源唯一且已验证，新 provider 兼容显示可为订阅名；否则保留旧内核 provider 信息并依赖新字段显示多来源/未知，不能将其伪装为可靠订阅。

## 来源规则

1. 来自真正 proxy-provider 的成员关系只证明内核 provider 归属。仅当其本机配置关联能可靠对应 Verge UID 时，才提升为订阅归属。
2. inline 节点比较已保存 profile 中节点与运行节点的有效连接定义，不能只比较名称。匹配唯一且配置未产生未知转换时可认定来源。
3. 多个订阅含相同有效定义时保留多个来源，不任选当前订阅。
4. runtime 中改名节点可以由唯一连接定义匹配续上来源；认证/传输/链式依赖改变造成不匹配时保留未知。
5. Script/Merge 生成或替换、当前 profile 仅提供包装结构、文件缺失等场景不能根据 current UID 直接给所有节点归属。
6. 用户可在设置中明确选择自定义 Verge 配置根目录；前端只能通过受限设置/桌面选择器提交，不能提供任意文件读取 API。

先实现纯函数 fixture 映射，再读本机做脱敏核对。已有 provider 与 profile 的名称偶然相同只能作为提示，不能单独满足 verified。

## 稳定身份与隐私

旧 node_key 保持原算法供兼容回放。新 node_id 以安装本地随机身份种子和规范化连接定义生成带命名空间的 HMAC；包括有效认证/传输差异，排除显示名和测量瞬态。

身份种子不同于任何 API Key，首次需要身份时生成后保存到应用数据目录的单独私有文件；POSIX 0600、Windows 当前用户权限最佳可用检查，拒绝宽权限/异常文件。不能进入包、日志、HTTP response 或 Git。明确备份种子才能在迁移设备/数据目录后保持相同新身份。

公开派生 ID 不包含 server、URL 或凭据原文，且没有种子不能离线枚举低熵密码。按订阅隔离身份；多来源使用稳定的有序来源集合。未知来源使用独立命名空间，不能与 verified 记录自动串联。

认证轮换产生新的连接身份；UI 显示配置身份变化，而不是悄悄合并两个账号的测试结果。拿不到连接定义时只能用明确标注弱身份，不能保证跨改名续接。

## 历史与数据库

新增以下独立表，通过幂等迁移创建：

- subscription_sources(subscription_id PRIMARY KEY, current_name, kind, first_seen, last_seen)。
- node_identities(node_id PRIMARY KEY, identity_version, identity_strength, first_seen, last_seen)。
- node_origins(node_result_id, subscription_id, name_snapshot, source_status, evidence_json)，复合唯一键防重复。

node_results 增加 node_id、identity_version、source_status 等少量索引字段；provider、node_key、旧查询入口保留。来源表不得存 URL 或规范化连接定义/HMAC 原文。

新轮次历史 JSONL 记录脱敏来源和新身份。旧轮次来源空时显示“历史来源未知”，不按当前配置回填。旧 provider 非空只保留 legacy-provider 标签，不自动升级为 verified。

历史聚合按稳定 subscription_id，支持订阅改名 snapshot；去重节点按 node_id，旧行独立 legacy 分组。可用率按探测连通状态计算，不因“未精测”或下载失败就认定不通；另外显示带宽成功率与测量覆盖率。

## API 与操作

- 新目录接口返回来源状态及已加载节点，内容严格白名单。
- 新任务请求支持 subscription_ids、node_ids、收藏 ID；服务端重新解析并校验范围，不能相信前端传入的来源。
- 切换前以当前快照重新验证 node_id → runtime_name 与目标组成员，配置已刷新/身份变化时拒绝陈旧切换；不自动重放写操作。
- 旧 name/provider 接口保留；无法唯一映射时返回明确错误，不随便选同名对象。

## 验收用例

1. provider-name 空而 profile 可唯一验证：正确订阅名称。
2. 两个不同订阅同名节点、相同 server/port 不同密码：分离。
3. 同订阅节点改名/订阅改名：新身份与来源连续，历史 snapshot 保留。
4. 相同定义属于多个订阅：ambiguous，多来源展示。
5. provider 显示名碰巧等于订阅名：不能只凭名称判 verified。
6. current profile 与被脚本修改的节点不匹配：unknown。
7. 未加载、路径穿越、越界 symlink、unsupported YAML、超大文件：安全降级。
8. 新 seed 权限、损坏、并发首次创建、迁移备份与缺失种子：可判定处理。
9. 旧 raw 字节/hash 不变，旧趋势可回放；迁移重复执行结果不变。
10. 密钥/URL/password canary 不出现在 API、导出、历史、SQLite、错误日志。
11. 本机只读来源报告验证当前缺失字段的真实场景，不更改 Verge 配置。

退出条件：专项测试和全量测试通过、来源与历史接口链路验证、新旧前端兼容。未加载订阅独立测试不属于自动行为；如未来增加必须显式确认流量与隔离方式。
