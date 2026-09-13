# CueFlow Roadmap

## v0.6.3（trial-operation UI）

在 v0.6.2 底座上完成极简浅色工作台与运营控制台：媒体上传、辅助材料、关键词、
真实任务状态、SRT 下载、可展开的任务列表、可见性轮询、结构化错误反馈与基础可访问性。
运营暂停操作保持滚动时可见；成本、预算占用和未知费用估算分开呈现。
不改变 Trial API、数据库、身份机制、执行语义、字幕算法或 Artifact ID。
详细边界见 [v0.6.3 UI 设计](v0.6.3-trial-ui-design.md)。

版本治理：0.6.2 与 0.6.3 已用于试运营分支，main 不复用这些版本号。
0.7.0 保留给 main 后续经单独设计和验证的版本，不因本轮 UI 修改而发布。

## v0.6.2（trial-operation 实验分支）

一个月零收费、零注册、零登录公开试运营。v0.6.2 是资源保护和实验数据层，不是正式
账户、支付或反欺诈系统。它使用长期匿名 visitor Cookie、IP HMAC 和粗粒度 fingerprint，
提供任务/分钟/并发限额、独立 Workspace、周期 stale slot 回收、Provider invocation
执行栅栏、磁盘水位、全局预算、usage/cost 统计、长期字幕结果和简易 operator 后台。

并发占用与预算占用明确分离：stale request 可释放并发槽位，但在费用确认或 24 小时
unknown 期限到达前继续占用预算。estimated unknown cost 是事后估算，必须与当时已知的
calculated cost 和实时 budget occupancy 分开展示。

对象存储按前缀管理：原媒体和工作对象最多七天，final.srt 与必要的小型 result.json
不设自动删除规则；每次下载重新生成短期单对象签名 URL。详细契约见
[v0.6.2 冻结设计](v0.6.2-anonymous-trial-operation-design.md)。

该版本从 v0.6.1 的 1934a98 创建 trial-operation 分支。试运营后根据实际 UV、复访、
成本和稳定性决定哪些适配层合并回 main。即使不合并，main 后续发布也不得复用 v0.6.2
版本号。

## v0.6.1

Phone + Password Authentication：统一“手机号继续”的验证码登录/注册，首次注册强制密码，
密码登录/修改/重置，原子手机号换绑，15 分钟 Access、30 天滑动 Refresh、180 天 Family 上限，
以及独立长期 Phone Reputation 和双事件流处罚账本。Migration 002 对非空 v1 开发库明确
fail closed；Auth HTTP 使用 Starlette ASGI、Secure/HttpOnly/Strict Cookie、Origin 与 CSRF。
细节见 [v0.6.1 冻结设计](v0.6.1-phone-password-authentication-design.md)。

## v0.6.0

独立 Account Core：一个 User 绑定多个 AuthIdentity，每个 User 恰好一个 active E.164 手机；
Session family/rotation/revoke，最多 5 个 active families；独立 AccountStore、forward-only
migration、迁移锁/备份/ledger、审计和彻底注销。产品版本与 18 个 Artifact producer 语义版本
解耦，字幕主链保持 v0.5.4 Artifact ID。细节见
[Account Core 冻结设计](v0.6.0-account-core-design.md)。

本版不实现登录、注册、验证码、OAuth callback、HTTP、支付或权限。

## v0.5.4

同一主机多进程托管准备：可选 Project、Run 执行隔离、execution rounds、DAG 失败恢复、
持久取消、远端回执、usage 留痕、TOS 业务资产与本地 artifacts 分离、结果契约。
细节见 [冻结设计](v0.5.4-design.md)。真实平台和长媒体验收完成前不宣布发布。

## v0.6.4+ 候选

候选增加只读 `/trial/config`，统一上传限制与活跃时间窗口的展示来源；本轮不新增该 API。
在此之前，部署变更必须同步核对 UI 的文件大小、时长、材料数、关键词数和活跃窗口文案。
当单个 visitor 达到 1000 个任务，或试运营超过 60 天时，触发后端分页评估。
这是评估触发条件，不是性能保证；当前列表仅限制首批 DOM 行数，网络载荷仍随总任务数增长。

试运营验证后再决定其他身份绑定、Worker 调度、正式权限、套餐和支付等 Web 产品层。
浏览器媒体处理和分布式执行
需要单独设计及验证，不借 0.5.4 改动现有音频/字幕算法。

## 已验证算法底座

## v0.5.3

本轮主链为双 ASR、双全文纠错、确定性合并、局部 GLM 候选选择、ATA 和 SRT。
原始 Base/Peer 保持独立；GLM 允许按需联网，只选择已有目标文字，使用独立提示词。

落实内容：

- corrected_text 全文契约与精确 Unicode codepoint 映射，支持插入、删除和不同跨度；
- 一致/单路修改自动接受，冲突构建 Base/Peer/Qwen/Kimi 去重候选；
- 冻结 KEEP、上下文和候选 ID；每批最多 8 项，完整输入预算与严格输出校验；
- 两路纠错并行 I/O，主线程单写者；每次格式重试独立计费记录；
- Schema 11.0.0 / Registry 13，旧项目拒绝且保持不变；
- 移除声学裁决、闲置 VocaSync、旧模型 edits 提示词与配套运行入口；
- ATA 完整 raw、原序句级结果与非阻断诊断；机械 SRT serializer 与状态真实性检查；
- 故障恢复、类型/静态检查、打包安装；独立真实服务验收需另行授权。

不把模型升级、资料提取、关键词词库、Office 支持、标点美化或大范围目录改名并入本轮。
实际验证结果见本轮实施报告；规划或替身测试不能当作真实服务调用通过。

## v0.5.4～v0.5.x

后续集中于相同输入的校准和稳定性，不默认继续重构：

| 方向 | 测量内容 |
| --- | --- |
| 文本精度 | Base/Peer 错误、一致修改与单路修改的精度、共同漏改 |
| GLM 选择 | 选择精度、KEEP 比例、全部候选都错的比例、非法响应率 |
| 上下文 | 200/400/800 字的匹配音频对照，避免把更多文字直接当作质量提升 |
| 输出 | 独立人工评估 ATA 句级字幕、阅读速度、词内符号与最终口播，不加入运行时质量 gate |
| 成本/恢复 | 每分钟媒体的 tokens/费用、延迟、失败批次、显式重试和人工负担 |
| 安装/运行 | 干净环境、凭据缺失、服务端错误、并发与中断 |

真实服务、联网内容和辅助材料 locator 都可能变化。新增付费实验应明确输入、模型参数、
评价真值和预算；不能将错误候选的一致率解释为独立投票，也不能承诺 GLM 自动纠正所有错误。
