# CueFlow v0.5.3 Schema Contracts

## Envelope

当前 schema_version=11.0.0。Envelope 保存 kind、scope、Producer、ordered inputs、payload、
RFC 8785 + SHA-256 内容身份和创建时间；创建时间与本地路径不参与语义哈希。

合法 kind：

```text
job_input media_probe timeline_audio media_object
base_asr peer_asr asr_comparison correction_transcript
merge_plan selection_batch selection_result edit_resolution
review_queue review_resolution transcript ata_response ata_result srt_render
```

correction_transcript 的 scope 等于 arm（qwen/kimi）；selection_batch 和 selection_result
的 scope 等于 batch_id。其余都是 global。没有音频裁决窗或模型三字段 edits 的兼容入口。

## 内容校验

- JobInput 保存有序 References 和最多 100 个非空、exact unique 的 UserKeywords。
- BaseAsr/PeerAsr 保存原文、timed units、关键词和 Provider metadata。
- CorrectionTranscript 保存完整、非空 corrected_text。v0.5.3 的 BaseTranscript 必须非空，
  因此空 corrected_text 是已完成但无效的 Provider 返回。
- MediaObject 绑定 timeline_audio_artifact_id，且 hash/长度必须与该 Frozen TimelineAudio blob 相同。
- MergePlan 保存四份原始版本、精确 patches、case 候选和 source_intervals。Schema 从四份全文
  重算整个合并结果，防止伪造候选来源或自行拼出文本。
- SelectionBatch 保存 request 和每个 case 的 provenance、核心区间、Base original。候选 ID
  和文字必须各自唯一，KEEP 必须等于 original，版本 target 必须等于对应候选。
  batch_id 绑定精确 case/request；prompt_sha256 校验落盘的完整专用提示词。
- SelectionResult 恰好覆盖其冻结 request 内的所有 case，禁止重复、漏项、未知候选和额外字段。
- EditResolution 的 corrected_preview 必须能由 Base 与非重叠 exact patches 重建；插入使用
  [p,p)，删除的 replacement 是空字符串。sealed 必须没有 review 或 pending_selection。
- Transcript 的 correction_mode 是 dual_fulltext_selection，保存 source_text 并绑定 Base 和 sealed resolution。
- AtaResponse 保存 run_id、invocation_id、transcript/media_object/timeline_audio_artifact_id、
  实际提交的 audio_text、provider_metadata，以及 response_blob 的 content_hash/byte_length/media_type。
  blob 保留完整原始结果 bytes，不使用 invocation 小诊断的截断策略。
- AtaResult 保存相同 Run/输入身份、ata_response_artifact_id、utterances 和 diagnostics。
  utterances 为有序数组（可以为空）；每项 text 为字符串（可以为空），start_ms/end_ms 为整数
  （排除 bool）。Schema 不校验非负、方向、重叠、时长上限、媒体范围或 ATA 文本一致性。
- diagnostics 保存 utterance_count、text_comparison 和分项计数；text_comparison 保存
  relation=identical/differs、input_length、output_length、可正可负的 length_delta。
  其他计数为 empty_text_count、negative_time_count、reversed_interval_count、
  zero_duration_count、overlap_count；字段形状受 Schema 约束，数值不决定成功或失败。
- SrtRender 保存 run_id、ata_result_artifact_id、encoding=utf-8、byte_length 和 text。
  每句原样复制 ATA 文字和两个时间端点；serializer 仅拒绝负时间或 start>end。
  零时长、重叠、时间倒序、超媒体时长、空文本、超长字幕、文字变化都不构成导出 gate。
- Export 校验 current/sealed/stale、在盘内容身份、Run checkpoint、精确依赖边与 raw blob；
  不做 subtitle identity、lexical coverage 或任何内容质量评分。

ReviewQueue 绑定 run、final、稳定 review_id 和冻结的 Base `[start,end)`/original。
提交必须带 expected_review_queue_artifact_id，并恰好覆盖全部项目。ReviewResolution 保存
人工 keep/qwen/kimi/peer/replace 决定。replace 的持久形式是 review_id + action + replacement；
不保存调用方提交的 original、source_sentence 或 offset。

## Registry 与事务

PRAGMA user_version=13，表为 projects、source_assets、artifacts、artifact_dependencies、
current_pointers、runs、invocations、invocation_inputs、run_checkpoints。
空库初始化当前版本；非空旧版本或表结构不符均拒绝，不迁移、不改写。

source_assets 以 `(project_id, normalized_absolute_locator)` 唯一。同名不同路径是不同 SourceAsset；
重新打开只验证 exact path，不按 filename、hash、mtime 或 size 搜索替代文件。

invocations 按每次实际请求分别记录状态、模型身份、response ID、usage、prompt、retry ancestry
和已完成无效返回的 diagnostic_json。
invocation_inputs 按 ordinal 保存原始 Artifact ID；定向重试复用这些输入和原 idempotency key。
run_checkpoints 的键是 (run_id,stage,scope_key)，input_digest 绑定 run/config/prompt，实际上游
有序依赖保存在 Artifact 与 invocation inputs。

成功结果、current pointer、invocation success 与 checkpoint 同一个 SQLite transaction 提交。
文件先按内容地址原子发布；未被数据库引用的残留文件不代表付费调用成功，也不授权自动重发。
最终 resolution 与 review queue 成组提交。人工封存后的结果不能被晚到的 GLM retry 覆盖。

只接受当前版本，任何其他版本均不读取或转换为本版项目。旧文件不改写，用户应新建项目。
