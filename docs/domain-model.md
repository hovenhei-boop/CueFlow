# CueFlow v0.5.0 Domain Model

## 1. Project 与 SourceAsset

Project 保存 `project_id`、显示名和创建时间。同一项目同一时间只允许一个活动 Orchestrator。

SourceAsset 是可变的外部文件引用，identity 只取 `Path.name` 精确字符串；保存 filename、format、`storage_mode = external_reference`、绝对 locator 和登记时间，不保存内容 SHA-256 或 byte length。CueFlow 不复制、移动、删除或搜索外部媒体；Media Prep 时 locator 缺失、不是普通文件或不可读取产生 `SourceMissingError`。用户可在原 locator 覆盖同名文件后创建新 Run。不提供 Source relink。已提交的下游 Artifact 仍不可变，同一 Run 的 targeted retry 只读取原 Invocation 绑定的项目内 Artifact，无需重新访问外部 Source。

## 2. Artifact 基础模型

Artifact 种类见 [Schema Contracts](schema-contracts.md)。Producer 保存 component/version、provider、model 和 config hash；确定性阶段的 provider/model 为 null。

Artifact 是不可变、内容寻址的 Envelope。身份覆盖 kind、scope、Schema semantic version、Producer identity、按顺序排列的 exact inputs 和 payload；不覆盖创建时间与存储路径。`scope_key` 对 `media_chunk`、`transcript`、`alignment` 使用 `chunk_id`，Reference kinds 使用 `reference_asset_id`，`lexicon_input` 使用内部 Lexicon Run ID，`term_candidate_set` 使用 work-item ID，其余 Artifact 使用 `global`。

Artifact input 必须精确引用一个 Artifact、SourceAsset 或 ReferenceAsset，并保存 role 与可选 coordinate range；三种 identity 恰好出现一个。依赖引用具体版本，不引用“当前同类对象”。

`(project_id, artifact_kind, scope_key)` 的 CurrentPointer 指向当前 Artifact 并携带 stale 状态。历史 Artifact 保持不可变。内容寻址可以避免重复存储相同 bytes，但不能让新 Run 跳过实际阶段执行。

## 3. Media 时间模型

### MediaProbeArtifact

保存 FFmpeg 已解释的 presentation timing evidence 与决策：media kind、presentation duration、stream facts、opening scan limit、首个有效 frame/sample 的 integer PTS 与 rational time base、AAC skip-sample evidence、全文件 continuity summary、timeline status/issues 和唯一 render actions。

`normal` 只表示 offset 可靠且为零、连续性未发现无法解释的问题；`corrected` 表示可靠非零 offset 已量化为 sample correction；`unverified` 表示 evidence 缺失或存在无法确定性修复的 discontinuity。

### TimelineAction

Origin action 恰好一个：

- `timeline_origin_unchanged`；
- `pad_silence_before(sample_count)`；
- `trim_before_timeline(sample_count)`；
- `timeline_origin_unverified`。

另有 `fit_presentation_duration(total_sample_count)`。Sample count 以 16kHz 为坐标。Render 只能解释这些 action；未知值是 ContractError。

### TimelineAudioArtifact

Timeline Audio 固定为 16kHz、mono、PCM s16le WAV。其 sample 0 对应源 presentation timeline 0，总 sample count 对应 presentation duration。后续音频阶段只读取该 blob。

### ChunkPlanArtifact / MediaChunkArtifact

ChunkPlan 保存本次实际 versioned config、完整 timeline duration、silence evidence 与连续 Chunk 列表。MediaChunk 引用精确 TimelineAudio 和 ChunkPlan，并保存全局半开毫秒区间与自己的 WAV blob。

## 4. Glossary 与 Transcript

System 与 Project Glossary 经 NFC、trim、精确字符串去重和确定性排序生成 Effective Glossary。它只包含扁平 `terms[]`，不表达 alias、replacement 或实体关系。

每个成功 Semantic Attempt 产生一个 Transcript，保存完整 Provider `source_text`、Decoration、顺序 Atom、language、Provider confidence/uncertainty 原始元数据和 Atomizer version。`leading_decoration + concat(atom.text + atom.decoration_after)` 必须精确重建 `source_text`。

### Semantic budget window

新 Run 中每 Chunk 从 window 0 开始，window 内最多 4 个已发送 Attempt。4-Attempt 上限是版本化运行规则；显式 targeted retry 最多把同一 Chunk推进到 window 1 和 window 2，两次 reset 则是固定的数据模型约束，不是 Ruleset 配置。每次 reset 后重新获得最多 4 次，因此同一 Run/Chunk 总量最多 12。

每个 Invocation 持久化 `semantic_budget_window`。进入 sending 的 Attempt 消耗当前 window slot；`definitely_not_sent` 不消耗。Budget reset 作为持久化、可审计记录保存，不能由自动流程触发。

每个返工 Attempt 都完整扫描当前 Chunk 文本，下一轮以当前实际 conflict 为准；达到 4 次上限后仍存在或无法稳定的 conflict 按现有 warning 语义报告。最终 accepted Transcript 是稳定性规则结束时的实际 Semantic 结果。Glossary 永不直接替换文字，QA 不额外扫描整篇 Transcript。

## 5. Alignment 与 Subtitle

Alignment 只为 accepted Transcript 创建，引用精确 MediaChunk 与 Transcript，Assignment 按 Atom 顺序完整覆盖全部可发音 Atom。未对齐、越界或引用错位阻塞 Subtitle/Export。Alignment execution 对结构非法结果最多自动修复一次；QA 后续 Repair Wave 是独立预算。

Subtitle 组合全部 accepted Transcript 与 Alignment，保存 exact input IDs、Segmenter Config hash、ordered Cues、时间包络、text、display units、Atom refs 与 spans。切分默认最多 10 个显示单位，除非一个不可拆保护单元本身超限。标点样式不得删除、替换或改写 Atom。

## 6. QA

QaArtifact 引用本轮实际检查的 ChunkPlan、Transcript、Alignment、Subtitle 与 Effective Glossary，保存 versioned ruleset、result 和 ReviewIssue。

非法/重叠/越界时间、未对齐 Atom、Chunk/Transcript/Alignment 引用错位、Subtitle exact inputs 不一致，以及 Envelope/Registry dependency identity 不一致都是 blocking error。Glossary stability、Provider uncertainty、Chunk 边界疑似重复、保护单元超长和 timeline unverified 是 warning。

QA 不能修改 Transcript。一个 Run 最多执行一个 QA Alignment Repair Wave；workset 内 Alignment 批量更新后必须重建 Subtitle 并重跑 QA。

## 7. Run、Invocation 与状态

Run 保存 input identity、运行配置 hash、状态和错误。模型身份与执行配置由各阶段的 Producer、payload 和 Invocation 记录。`run` 始终创建新 Run。显式 retry 可以把原 `failed`/`interrupted` Run reopen 为 `running`。

Invocation 保存 run/chunk、operation、logical key、attempt number、Semantic budget window、provider/model、status、response/output/error，以及 ordered exact input Artifact bindings。状态至少包含 `created`、`sending`、`succeeded`、`definitely_not_sent`、`delivery_ambiguous`、`explicit_failure`。

开始 `run` 或 `retry` 时，单 Orchestrator recovery 将遗留 `created` 转为 `definitely_not_sent`、`sending` 转为 `delivery_ambiguous`，并把对应 Run 转为 `interrupted`。当前 Ctrl+C 产生 `interrupted`，其他未预期异常产生 `failed`；Run 与 in-flight Invocation 原子收口。普通打开和管理命令不执行 recovery。

SemanticBudgetReset 只由显式 targeted retry 创建，绑定原 Run、Chunk、触发 Invocation和新 window index。每个 Run/Chunk 最多两行。它不是通用 retry/cache 抽象。

## 8. SrtRender

SrtRenderArtifact 引用 current Subtitle 与非 blocked QA，保存 UTF-8 SRT text 与 byte length。发布 Artifact 成功后，使用同目录临时文件和原子替换更新唯一用户输出 `output/subtitles.srt`。

## 9. ReferenceAsset

ReferenceAsset 与 SourceAsset 完全分离，不能使用 `asset_kind=auxiliary` 模拟。其 identity 只取 `Path.name` 精确字符串；表中只保存 id、filename、locator、检测后的格式/媒体类别和登记时间，不含内容 hash、size、mtime 或 location history。同名重复登记返回原对象且不更新 locator。

`relocate` 只为 missing/unreadable locator 在用户指定文件夹的直接子项中找精确 filename；它不是 relink，不递归、不扫描其他目录、不碰 Source。Reference 外部文件可原位覆盖；之后的每次 extract 新建独立 Run 并读取当前内容。

## 10. Reference Artifact 与 Evidence role

Reference Artifact 使用 ReferenceAsset id 作为 scope。`reference_input` 保存当次 work item 的稳定 manifest、内部 blob 引用、权威时间和本地测量事实；外部 Reference 本体不因此获得内容 identity。`reference_evidence` 保存独立 content/provenance/usage；`reference_bundle` 引用同一 Run 已成功 Evidence，并列出失败项。

Evidence role 只允许：`text_subtitle`、`bitmap_subtitle`、`burned_subtitle`、`cloud_reference_asr`、`document_text`、`cloud_document_parse`、`image_visual`。不同 role 不自动融合。`image_visual` 只用于独立 PNG/JPEG/WebP，不用于 PDF 页面。

## 11. Reference Run、work item 与 Invocation detail

Project → Run 是唯一顶层关系。Reference Run 是现有 Run 加 `reference_asset_id`、聚合 outcome 和当前 bundle 的扩展；同一 ReferenceAsset 可有任意多个 Run。`reference status` 按资产列出全部 Run。

每个 Reference work item 保存有序 ordinal、branch、evidence role、冻结 work spec、状态、成功 Evidence 或失败详情。Reference Invocation detail 扩展现有 Invocation，保存 work item/branch、provider/model、实际 config、有序 input Artifact、response id、`local_measured_duration`、`provider_usage_duration`、provider usage/cost、retry parent/reason、failure 和 Cloud cleanup facts。Provider 未报告的数据为 null，不伪造成 0。

聚合映射固定为 `succeeded/complete`、`failed/partial`、`failed/failed`。Retry 只在原 Run 内追加失败 work item 的 attempt；成功项永不重跑，成功后发布新 bundle 并引用旧成功 Evidence。每模型 work item 每 Run 最多两个 sent attempt。

## 12. Suggested Term 与 Evidence occurrence

Candidate identity 为 `(normalization_version, NFC+trim exact surface)`；大小写、连字符和内部空格均保留差异。Candidate 不是词库条目，也不自动晋升。Occurrence 保存 raw surface、独立的 model suggested spelling、Evidence Artifact ID/role、field path、半开 offset、上下文、风险标签和来源坐标。模型没有提供或客户端不能验证的位置不得伪造。

分类固定为 `proper_noun`、`noun_or_term`、`verb`、`other`。专名 subtype 顺序固定为 person、organization、location、event、project_or_program、product_brand_model_software、standard_protocol_code、work_or_title、other。显示顺序按分类、词长降序、UTF-8 binary、Candidate ID 决定。

## 13. Project Lexicon、Neutral 与 Project Blacklist

Accept 或 Edit & Accept 创建 Project Lexicon entry；也可手工 Add。Candidate 的 Dismiss 与 entry 的 Remove 都进入逻辑 Neutral，不建立抑制。Entry 支持 Edit、Disable、Remove 和原子 Block，并用 revision 做并发冲突检查。每次 Project Lexicon 可见状态改变生成一条 decision 与不可变 `project_lexicon` revision。不存在 alias、merge、replacement 或实体归一化。

Project Blacklist 只作用于当前 Project，并阻止同一 exact term 进入 Reference suggestion/Project Lexicon 自动候选路径，但不审查 Source 或 SRT。Temporary rule 只允许 15/30/60/120 日，在 `now >= expires_at` 时自动解除；Permanent rule 不自动过期，但可由用户主动 Unblock。Temporary 可修改期限或转 Permanent；Permanent 不转 Temporary。

同一 exact normalized term 不能同时存在于 Active Project Lexicon 与有效 Project Blacklist。人工 Add、Edit & Accept 或 entry Edit 若命中 Blacklist，调用方必须明确选择 `unblock_and_add` 或 `cancel`；默认返回结构化冲突，不静默修改规则。手工 Blacklist Add 命中 Active Entry 时失败，必须改用带 entry revision 的原子 Block。

## 14. Official Lexicon Pack

Official Pack 是应用级全局只读资源，不属于任何 Project。Catalog 记录 Pack ID、领域、version、source 与 manifest hash；manifest 记录 Pack schema、identity、license、term count 与 terms hash。setup 默认安装 catalog 全部领域，也允许用户只选领域；所有项目读取同一 installed set。没有 Project 类型、Project→Pack 绑定、逐词 select 或项目复制。

v0.5.0 只构建和管理这些数据，不把 Project Lexicon 或 Official Pack 自动注入 Effective Glossary/Source。未来消费必须先做相关性与容量限制，并把所选 entry、Pack version 和选择算法一并冻结为 Source Run 的不可变 Effective Glossary Snapshot；targeted retry 继续绑定原 snapshot。
