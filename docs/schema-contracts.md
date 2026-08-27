# CueFlow v0.4.1 Schema Contracts

## 1. Envelope 与哈希

Artifact Envelope 包含 `schema_version`、`artifact_id`、kind、scope、content hash、created time、Producer、ordered inputs 和 payload。内容哈希使用 RFC 8785 JCS UTF-8 bytes 后计算 SHA-256，覆盖 kind、scope、Schema major/minor、Producer、inputs 与 payload；不覆盖 Artifact ID、created_at 或路径。只接受当前 Schema 版本，其他版本的 payload 不得解释。Producer 恰好包含 `component`、`component_version`、`provider`、`model`、`config_hash`；缺字段或额外字段均拒绝。

合法 Artifact kinds：`media_probe`、`timeline_audio`、`chunk_plan`、`media_chunk`、`system_glossary`、`project_glossary`、`effective_glossary`、`transcript`、`alignment`、`subtitle`、`qa`、`srt_render`、`reference_input`、`reference_evidence`、`reference_bundle`、`lexicon_input`、`term_candidate_set`、`project_lexicon`。当前 Artifact Schema 为 `3.0.0`。

## 2. SourceAsset

SourceAsset 必须包含 `source_asset_id`、精确 filename、asset/media kind、format、`storage_mode = external_reference`、绝对 storage locator 和 registration time。`(project_id, filename)` 唯一，Source identity 只取 filename；Source 不包含内容 hash 或 byte length。Media Prep 前 locator 必须存在、是普通文件且可读取，但不比较外部内容。

## 3. 时间与 MediaProbe

Transcript、Alignment、Subtitle 和 Chunk 的 Core 时间使用整数毫秒半开区间 `[start_ms, end_ms)`。MediaProbe 原始 evidence 使用 integer PTS 与 rational time base，不在决策前转换为 float 或整数毫秒。

Frame evidence 至少可以表达：

```json
{
  "stream_index": 1,
  "pts": -1024,
  "time_base_num": 1,
  "time_base_den": 48000,
  "skip_samples": 1024,
  "sample_rate_hz": 48000
}
```

MediaProbe payload 至少包含 media kind、presentation duration/total samples、opening scan limit、timeline status、stream facts、presentation evidence、continuity summary、issues 和 actions。

已持久化的 stream/frame evidence 必须保持生产器结构：PTS、stream index、duration ticks 和 packet ordinal 为 integer 或契约声明的 null；time base 与 fraction 使用 integer `numerator` 和正 integer `denominator`；skip/sample/count 字段不得为负。`presentation_evidence` 固定包含 nullable `media_origin`、`audio_start`、`exact_offset`。

`continuity_check` 必须包含 `status`、非负 `packets_scanned` 和 `first_anomaly`。status 只允许 `continuous`、`discontinuous`、`unavailable`；continuous 的 anomaly 必须为 null，其他状态的 anomaly 至少包含非空 code，若存在 packet ordinal 或 expected/observed fraction，则分别遵守非负 integer 与上述 fraction 结构。Schema 只验证数据结构和基本合法性，不重新执行 offset 或 discontinuity 算法。

Origin action 必须恰好一个：

- `timeline_origin_unchanged`，不得带 sample count；
- `pad_silence_before`，`sample_count > 0`；
- `trim_before_timeline`，`sample_count > 0`；
- `timeline_origin_unverified`，不得带 sample count。

`fit_presentation_duration` 必须恰好一个且 `total_sample_count > 0`。未知、重复或矛盾 action 无效。

`timeline_status` 只允许 `normal`、`corrected`、`unverified`。只有 evidence 可靠、offset 为零且 continuity 未发现无法解释问题时才是 normal。

## 4. Timeline Audio 与 ChunkPlan

TimelineAudio payload 必须包含正 `duration_ms`、正 `total_sample_count`、`sample_rate_hz = 16000`、`channels = 1`、`sample_format = s16le`、`timeline_origin_sample = 0` 和 WAV blob identity。

ChunkPlan payload 必须保存 `duration_ms`、精确 TimelineAudio ID、实际 versioned config、silence evidence 和 ordered chunks。Schema 必须读取 payload config 的 `hard_limit_ms` 校验；config 数值为正、target 不大于 hard limit，Chunk ordinal 连续、区间连续覆盖 timeline，且每块不超过实际 hard limit。

MediaChunk 的 `scope_key == chunk_id`，并引用精确 TimelineAudio 与 ChunkPlan；payload 保存对应 interval 和 WAV blob。

## 5. Glossary 与 Transcript

Glossary payload 为确定性排序的唯一非空 `terms[]`，并保存 `normalization_version = 0.1.0`。

Transcript payload 必须保存 `chunk_id`、完整 `source_text`、leading Decoration、Atomizer version、language、Semantic confidence evidence、Provider uncertainty 和 ordered atoms。Atom ID 唯一、position 从 0 连续、class 属于冻结集合，Decoration 重建精确等于 source_text。

## 6. Alignment

Alignment payload 必须包含 `chunk_id`、精确 MediaChunk/Transcript IDs、Provider coordinate system、一次全局 offset 记录和按 Transcript Atom 顺序排列的 assignments。

每个实音 Atom 恰好一个 Assignment。Aligned interval 必须完全位于 MediaChunk 内并按序不重叠；超界、缺失、重复或引用不匹配都是结构非法。Provider confidence 若存在，必须保存原始尺度元数据。

## 7. Subtitle

Subtitle payload 保存 Segmenter Config hash、精确 Transcript/Alignment ID arrays、duration 和 ordered cues。每个 Cue 使用合法非重叠区间；显示单位默认不超过 10；Atom refs 完整覆盖显示 Atom；Atom spans 精确引用 Transcript；text 只能由 Atom 与冻结 Decoration style 得出。

## 8. QA 与 SRT

QA payload 保存 `subject_artifact_ids`、`qa_ruleset_version = 0.1.1`、`result = passed | warnings | blocked` 与 issues。每个 issue 有唯一 ID、severity、code、resolution status、locations 和 evidence。

SrtRender payload 保存精确 Subtitle/QA IDs、UTF-8、byte length 和 text，只依赖 current Subtitle 与非 blocked QA。SRT 不删除任何可发音 Atom。

## 9. Registry

SQLite 包含 Source、Reference 与 Lexicon 三组完整表。公共 Run 表的 `kind` 非空且只允许 `source`、`reference`、`lexicon`；各查询和 crash recovery 必须正向按 kind 选择，不能再以“不是 Reference”推断 Source。

Registry 使用 SQLite `user_version = 5`。空库直接初始化当前完整结构；普通打开遇到非当前版本、缺表或 column 不匹配时明确拒绝且不修改已有库。v4 只能通过显式 `cueflow migrate PROJECT` 在单一事务中迁移；迁移以 `foreign_key_check` 和当前精确列契约为提交门槛。Project 列恰好为 `project_id`、`display_name`、`created_at`。

InvocationInput 主键为 `(invocation_id, ordinal)`，每行保存 role 与精确 `input_artifact_id`。Targeted retry 只能读取这些绑定。

SemanticBudgetReset 主键包含 Run、Chunk 与 window index；window index 固定只允许 1 或 2，并绑定触发 reset 的失败 Invocation。两次上限是数据模型常量，不属于 versioned runtime config；唯一约束保证同一 Run/Chunk 最多两次。

Artifact 文件必须按 temp → fsync → atomic replace → read-back validation 顺序完成落盘，再在同一 SQLite 写事务中登记 Artifact/dependency 与切换 pointer。Invocation、reset、crash recovery 和 Run reopen 必须可审计且持久化。

## 10. Reference schema

`reference_assets` columns 恰好为 `reference_asset_id`、`filename`、`locator`、`detected_format`、`media_category`、`registered_at`。没有外部文件 hash/size/mtime，也没有 `reference_asset_locations`。

Reference Artifact input 可以使用 `reference_asset_id`，并与 `artifact_id`、`source_asset_id` 满足三选一。Reference Artifact 的 `scope_key` 必须等于 payload 的 `reference_asset_id`。

`reference_input` 至少保存 reference/run/work-item id、input kind、branch、detected format、nullable `local_measured_duration` 和 manifest。`reference_evidence` 还必须保存冻结 evidence role、content、provenance、nullable `provider_usage_duration`、nullable provider usage/cost。`reference_bundle` 保存 run/outcome、非空 ordered Evidence IDs 和 failures；只有 complete/partial 才存在 bundle。

### usage 双时长不变量

`local_measured_duration` 与 `provider_usage_duration` 是两个语义不同、分别持久化且均允许 null 的字段。前者只来自本地媒体测量，用于源时间轴、provenance、分段和媒体事实；后者只来自 Provider usage，用于成本观测和账单解释。禁止定义或赋值 `media_duration = usage.duration`，禁止用 Provider usage 重建时间轴。Provider 未报告的 usage/cost 字段为 null，不得伪造成 0。

`reference_invocation_details` 必须保存 work item/branch、provider/model、当次实际 config、ordered input Artifact、response id、上述双时长、raw usage、nullable provider cost、retry parent/reason、failure details，以及 Cloud document remote file id/cleanup status。`reference_runs` 只扩展 runs；`reference_work_items` 的成功 Evidence 和失败详情可审计。

## 11. Lexicon schema

`lexicon_runs` 绑定触发它的 Reference Run、Reference Bundle、唯一 `lexicon_input` manifest 和聚合 outcome。`lexicon_work_items` 绑定 exact Evidence Artifact ID、batch ordinal、冻结 batch manifest、状态、candidate-set Artifact 或失败详情；`lexicon_evidence_coverage` 以 Evidence Artifact ID 为主键实现增量处理。`lexicon_invocation_details` 保存 provider/model、实际配置、usage/cost、retry parent/reason 和失败详情。

`lexicon_input.scope_key == payload.run_id`，payload 保存触发 Reference Run/Bundle、normalization version 与非空 batches。每个 unit 必须保存非空 field path、完整 Evidence 中的半开 source offset、切片 text hash 与来源坐标；manifest 不持久化发送正文。

`term_candidate_set.scope_key == payload.work_item_id`，绑定 run/work-item/Evidence，并保存候选 observation。当前生产的 disposition 只允许 `suggested`、`already_in_project_lexicon`、`suppressed_blacklist`。每个 observation 保留 normalized/display term、显示分类、disposition 与至少一个 exact occurrence；空模型结果用空 candidate array 表示成功。

`term_candidates` 对 `(normalization_version, normalized_surface_form COLLATE BINARY)` 唯一；`term_occurrences` 保存 Evidence/role、raw 与 suggested surface、分类、risk tags、field path、offset、context 和 coordinates。`project_lexicon_entries` 只对活动 exact normalized term 唯一，带 enabled/status/revision；`project_lexicon_revisions` 形成 ordinal/parent 不可变链。`lexicon_blacklist` 对 exact normalized term 唯一，保存 temporary/permanent kind、nullable `expires_at`、revision 与时间戳；Temporary 必须有到期时间，Permanent 必须没有到期时间。

`project_lexicon` 使用 global scope，保存 revision/parent/decision 与当次全部活动 entries。发布它不得 stale 或改写 `effective_glossary`、Transcript、Alignment、Subtitle、QA 或 SRT pointer。
