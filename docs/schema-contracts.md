# CueFlow v0.5.2 Schema Contracts

## Artifact Envelope

当前 `schema_version = 7.0.0`。Envelope 包含 kind、scope、Producer、ordered inputs、payload、
RFC 8785 + SHA-256 content identity 和创建时间。哈希不包含创建时间或路径。只解释当前精确
Schema；旧版本 fail closed。

合法 Artifact kind：

```text
job_input media_probe timeline_audio media_object
base_asr peer_asr asr_comparison
acoustic_window_plan acoustic_window glm_adjudication_evidence acoustic_resolution
qwen_edit_proposal kimi_edit_proposal edit_proposal
agreement_resolution edit_resolution review_queue review_resolution transcript
alignment subtitle qa srt_render
```

`acoustic_window` 与 `glm_adjudication_evidence` 使用 `window_id` scope；
`acoustic_resolution` 使用 `disagreement_id` scope；其他全部是 `global`。没有
whole-file `chunk_plan`、`media_chunk`、`base_asr_chunk` 或 `chunk_id`。

## 关键 payload

`JobInput` 保存 SourceAsset ID、ordered References 和 ordered `user_keywords`。Keyword 最多
100 个，非空且 exact unique；`pdf_url`/`image_url` 必须是 HTTPS mutable locator，`text`
必须是 TXT/MD/CSV/JSON UTF-8 正文快照。

`MediaObject` 保存稳定 TOS object identity 和内容 hash；`get_url` 字段被 schema 明确拒绝。

`BaseAsr`/`PeerAsr` 保存完整 text、timed units、UserKeywords 和 Provider metadata。
`AsrComparison` 保存两份 ASR ID 和机械 hunks，不携带触发 GLM 的字段。
`AcousticWindowPlan` 绑定 agreement、Base 和 TimelineAudio，windows/unavailable 必须恰好
覆盖每个 disagreement 一次。`AcousticWindow`/`GlmAdjudicationEvidence` 绑定同一 window ID；
窗口 `end-start <= 30000`、blob `byte_length <= 25000000`。第三路证据仅绑定窗口和 JobInput，
不把任何纠错候选送进 GLM。

每个 correction arm 保存严格三字段 edits，inputs 绑定 JobInput、完整 Base、完整 Peer 和
机械差异。`AgreementResolution` 保存 accepted edits、lexical disagreements、ignored
prosody 与 contract review。Projection 的支持证据由 schema 重新计算，不能伪造新文字。

`AcousticResolution` 保存 disagreement ID、match policy、resolved/review、原因及证据 ID。
resolved 必须引用 `glm_adjudication_evidence`；所选文本来自既有 Base/Qwen/Kimi 候选，
不造第四个答案。

`EditResolution` 保存 run ID、Base ID、exact patches、review items、pending_acoustic、
sealed 和 corrected_preview。preview 必须可由 Base + 非重叠 patches 完整重建；sealed
不允许存在 review 或 pending acoustic。`Transcript` 只消费 sealed final，mode 为
`post_correction_adjudication`，不可绕开最终 gate。

`ReviewQueue` 绑定 run ID 和 exact final resolution ID，每项使用稳定 review ID，不能用数组
下标。提交要求 `run_id + expected_review_queue_artifact_id + decisions[]`，队列过期即拒绝。
`ReviewResolution` 保存每项人工决定，包括显式 keep；人工 replace 仍提交 exact 三字段 edit，
不接受用户或模型提供的数字 offset。人工结果、final 和 clear queue 同事务发布。

`Alignment` 保存精确 MediaObject/Transcript IDs、duration 和全局毫秒 assignments；
Assignment atom IDs 必须与 Transcript 完全同序同集。

## Registry

当前 `PRAGMA user_version = 9`。表只有：

```text
projects source_assets
artifacts artifact_dependencies current_pointers
runs invocations invocation_inputs run_checkpoints
```

空库初始化当前 Schema。非空旧库或缺表库明确拒绝并保持不修改；没有 migration、Trash 或
Legacy wrapper。

`invocations` 不含 `chunk_id`。它分列保存 requested/resolved model、idempotency key、remote
job/status、可选 response ID、elapsed/reasoning time、usage、prompt provenance 和 retry
ancestry。`invocation_inputs` 按 ordinal 保存精确 Artifact ID，是 targeted retry 的输入事实。

`run_checkpoints` 键为 `(run_id, stage, scope_key)`，保存 input_digest、artifact_id 和 revision。
input_digest 绑定 run/config/prompt 身份，实际有序上游依赖保存在 envelope inputs 与 invocation
inputs。恢复只读该 run 的 checkpoint，不从最新 current pointer 推断曾执行的输入。
Provider 成功结果、invocation success 和 checkpoint 同一 SQLite transaction 发布；final
resolution 与 review queue 也成组提交。文件先以内容地址原子发布，崩溃留下的未引用 blob
不代表已完成的调用，也不授权自动重发。

## 版本升级边界

此变更不是兼容性加字段：6.0.0 的 pre-correction GLM 证据与本版 post-correction
adjudication 语义、DAG、resume 契约均不同，因此升 major 到 7.0.0，而不是 6.1.0。
Registry 8 无 run checkpoints，本版升 9。旧库/旧 artifacts fail closed、保持字节不变；
不自动导入、不迁移、不重新解释旧证据。用户应创建新项目，旧项目保留给对应旧版本读取。
