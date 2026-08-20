# CueFlow v0.1 Domain Model

状态：冻结基线

## 1. Project 与 SourceAsset

Project 保存 `project_id`、显示名、创建时间和固定 `processing_profile`。v0.1 假设同一项目同一时间只有一个活动 Orchestrator。

SourceAsset 表示外部输入身份，包括原文件完整 SHA-256、byte length、format、`storage_mode = external_reference`、绝对路径和登记时间。路径不是内容身份。CueFlow 不复制、移动或删除外部媒体；Media Prep 时路径缺失产生 `SourceMissingError`，内容变化产生完整性错误。已提交的下游 Artifact 可供同一 Run 的 targeted retry 使用，无需重新访问外部源。

## 2. Artifact 基础模型

v0.1 Artifact kinds：`media_probe`、`timeline_audio`、`chunk_plan`、`media_chunk`、三类 Glossary、`transcript`、`alignment`、`subtitle`、`qa`、`srt_render`。

Artifact 是不可变、内容寻址的 Envelope。身份覆盖 kind、scope、Schema semantic version、Producer identity、按顺序排列的 exact inputs 和 payload；不覆盖创建时间与存储路径。`scope_key` 对 `media_chunk`、`transcript`、`alignment` 使用 `chunk_id`，其余 Artifact 使用 `global`。

Artifact input 必须精确引用一个 Artifact 或 SourceAsset，并保存 role 与可选 coordinate range。依赖引用具体版本，不引用“当前同类对象”。

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

最终 accepted Transcript 是稳定性规则结束时的实际 Semantic 结果。Glossary 永不直接替换文字。

## 5. Alignment 与 Subtitle

Alignment 只为 accepted Transcript 创建，引用精确 MediaChunk 与 Transcript，Assignment 按 Atom 顺序完整覆盖全部可发音 Atom。未对齐、越界或引用错位阻塞 Subtitle/Export。Alignment execution 对结构非法结果最多自动修复一次；QA 后续 Repair Wave 是独立预算。

Subtitle 组合全部 accepted Transcript 与 Alignment，保存 exact input IDs、Segmenter Config hash、ordered Cues、时间包络、text、display units、Atom refs 与 spans。切分默认最多 10 个显示单位，除非一个不可拆保护单元本身超限。标点样式不得删除、替换或改写 Atom。

## 6. QA

QaArtifact 引用本轮实际检查的 ChunkPlan、Transcript、Alignment、Subtitle 与 Effective Glossary，保存 versioned ruleset、result 和 ReviewIssue。

非法/重叠/越界时间、未对齐 Atom、Chunk/Transcript/Alignment 引用错位、Subtitle exact inputs 不一致，以及 Envelope/Registry dependency identity 不一致都是 blocking error。Glossary stability、Provider uncertainty、Chunk 边界疑似重复、保护单元超长和 timeline unverified 是 warning。

QA 不能修改 Transcript。一个 Run 最多执行一个 QA Alignment Repair Wave；workset 内 Alignment 批量更新后必须重建 Subtitle 并重跑 QA。

## 7. Run、Invocation 与状态

Run 保存 input identity、完整 config hash、状态和错误。`run` 始终创建新 Run。显式 retry 可以把原 `failed`/`interrupted` Run reopen 为 `running`。

Invocation 保存 run/chunk、operation、logical key、attempt number、Semantic budget window、provider/model、status、response/output/error，以及 ordered exact input Artifact bindings。状态至少包含 `created`、`sending`、`succeeded`、`definitely_not_sent`、`delivery_ambiguous`、`explicit_failure`。

SemanticBudgetReset 只由显式 targeted retry 创建，绑定原 Run、Chunk、触发 Invocation和新 window index。每个 Run/Chunk 最多两行。它不是通用 retry/cache 抽象。

## 8. SrtRender

SrtRenderArtifact 引用 current Subtitle 与非 blocked QA，保存 UTF-8 SRT text 与 byte length。发布 Artifact 成功后，使用同目录临时文件和原子替换更新唯一用户输出 `output/subtitles.srt`。
