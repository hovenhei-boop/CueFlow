# CueFlow Roadmap

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

## v0.6.1+

手机号验证码登录、其他身份绑定、HTTP、Worker 调度、上传适配、权限和支付等 Web 产品层。
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
