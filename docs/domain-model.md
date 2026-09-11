# CueFlow v0.5.4 Domain Model

Workspace 持有 Registry；Project 仅组织 Run，可以有多个 Project，也支持 project_id=null。
每个 Run 持有独立的 SourceAsset、输入成员、artifacts、目录和执行锁。
原始媒体按路径与内容 hash 绑定；媒体准备后，标准 WAV 的本地 blob 与 TOS 对象同字节。
Reference 原件捕获一次；文本原样使用，Office 转 PDF 失败以 warning 排除，后续轮次可恢复。
用户不得替换输入；恢复成功允许有效 Reference 集合扩大，并使两路 Correction 与下游失效。
临时签名 URL 不属于持久身份，TOS 对象持有 bucket/key/version/hash/size/MIME。

| 对象 | 职责 |
| --- | --- |
| BaseAsr | Qwen 全文与原始 timestamps；冻结 Base |
| PeerAsr | 豆包全文与原始 timestamps |
| AsrComparison | 原始文本的机械差异，诊断输入 |
| CorrectionTranscript / qwen、kimi | 两份独立的完整 corrected_text 与 Provider metadata |
| MergePlan | 四份全文、精确合并 patches、公共争议区间、原候选与来源区间 |
| SelectionBatch | 冻结 case/候选 ID、上下文、匿名排列、来源映射和专用提示词 |
| SelectionResult | 每项唯一已有候选 ID、Provider metadata |
| EditResolution | Base、最终 patches、KEEP 决策、review items 与 sealed 状态 |
| ReviewQueue / ReviewResolution | 稳定队列身份与人工决定，包括显式 KEEP |
| Transcript | 从 sealed final 构建的完整原文，作为 ATA audio_text |
| AtaResponse | 完整 Provider 响应 blob、实际 audio_text、调用 metadata 和冻结输入 |
| AtaResult | ATA 原序 utterances 的 text/start_ms/end_ms，加非阻断 diagnostics |
| SrtRender | 句级结果的机械 SRT 格式化文本，与当前 Run/AtaResult 绑定 |

所有机器修改以原始 Unicode codepoint 的半开 Base 区间表示，支持零长度插入和空文本删除。
人工 replace 只接受 review_id/action/replacement；区间与 original 从 ReviewItem 取得，调用方不提交 locator 或 offset。
去重后的候选可以有多个来源；多个相同版本不构成独立投票。

Run 的用户重试产生新的 execution_round；失败节点及其下游重算，未受影响结果复用。
无失败/降级时，成功 Run 的 retry 从两路 Correction 开始。ASR 仅同 Run 复用。
Invocation 记录一次 Provider 尝试及其轮次，保留 requested/resolved model、回执、usage、错误与 retry ancestry。
未知 usage 是 null。已完成但格式无效的请求仍保留计量记录。

共享 checkpoint 的 round=0；其余以 run_id/execution_round/stage/scope 绑定。
current pointer 是当前执行投影，不能替代历史 checkpoint 或 invocation 输入身份。
本地 raw/normalized Artifact 与业务结果对象分开；完整 ATA raw 永不被小诊断截断替代。
更多状态和失败边界见 [0.5.4 设计](v0.5.4-design.md)。
