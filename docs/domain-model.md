# CueFlow v0.5.3 Domain Model

Project 保存 Registry。SourceAsset 的身份是 project_id 与 normalized absolute locator；
Windows 路径按大小写等价处理。TimelineAudio 是与 presentation 时钟对齐的本地 PCM；
MediaObject 保存该 TimelineAudio 上传后的 TOS provider、bucket、key、hash、长度和可选版本。
临时 GET URL 不属于持久领域对象。

JobInput 冻结 SourceAsset、ordered References 和 UserKeywords。本地文本是内容快照；
PDF/Image URL 是可变远端 locator。原始关键词是两路 ASR 唯一领域先验。

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

Run 操作是 run 或 correct。Invocation 的 operation 闭集为 media_upload、qwen_asr、
doubao_asr、qwen_correction、kimi_correction、glm_selection、ata。每个付费尝试独立保存
requested/resolved model、response ID、usage、elapsed time、prompt hash、有序输入、retry ancestry，
以及已完成但违反契约的受控诊断数据。
Checkpoint 按 run/stage/scope 绑定结果；current pointer 仅是当前投影，不能替代重试输入事实。

同一项目只有一个写者。纠错网络请求可以并行，只有主线程发布结果。成功结果、invocation 和
checkpoint 同事务；final 与 review queue 同事务。ATA 的成功断点是完整 AtaResponse；
本地规范化、serializer 不属于付费调用。它们失败时 raw 和成功调用记录保持可追溯，
normalized AtaResult 已生成时也保留。旧版本数据库和 Artifact 不转换。
