# CueFlow v0.1 领域模型

状态：设计基线

本模型刻意把语义权威、声学权威和运行账本分开。Core 概念不能只是某个 Provider 响应字段的别名。

## 1. 聚合边界

### Project

拥有项目配置和当前指针，不直接包含 Artifact payload。

必需概念：

- `project_id`
- 显示名称
- 创建时间
- `processing_profile`：`LOCAL_PROFILE` 或 `CLOUD_PROFILE`
- 按 Artifact kind 与 `scope_key` 区分的当前指针

用户不能在 Project 中填写任意 Provider 或 Model ID。Processing Profile 的内部 Model Snapshot 由 CueFlow 版本化配置决定，并在实际 Artifact provenance 中记录。

### SourceAsset

用户输入字节或其受管副本的一次不可变登记。

必需概念：

- `source_asset_id`
- `asset_kind`：media 或 auxiliary
- 媒体/文档格式
- 完整文件内容哈希与字节长度
- 存储定位器和存储模式
- 登记时间

路径和文件名只是定位器与标签，不是身份。同一路径下的文件内容哈希改变时，应产生新的 SourceAsset。

### Artifact

CueFlow 生成的不可变、版本化结果。共享 envelope 在 `schema-contracts.md` 中定义，各 kind 拥有不同 payload。

v0.1 支持：

- `media_probe`
- `timeline_audio`
- `chunk_plan`
- `media_chunk`
- `system_glossary`
- `project_glossary`
- `effective_glossary`
- `transcript`
- `alignment`
- `subtitle`
- `qa`
- `srt_render`

`srt_render` 是内部不可变导出结果。公开的 `output/subtitles.srt` 只是它的可替换投影。

### ArtifactDependency

记录一个 Artifact 依赖的是哪个具体 Artifact，而不只是“最新 transcript”之类的 kind。

必需概念：

- 生产者 Artifact ID
- 输入 Artifact 或 SourceAsset ID
- dependency role
- 可选的生产者坐标系范围，用于 provenance

Transcript 侧使用 Atom span；媒体侧使用全局毫秒区间。Alignment 完成后两者都可保存。v0.1 可以在 Chunk 边界复用未受影响的完整 Transcript/Alignment，但不做任意 Atom 范围的增量执行。

### CurrentPointer

把 `(project_id, artifact_kind, scope_key)` 映射到一个当前 Artifact ID。`scope_key` 对 Chunk Artifact 使用 `chunk_id`，对 Timeline Audio、Glossary、Subtitle、QA 和 SRT Render 等项目级 Artifact 使用 `global`。

因此数据库可以分别表达：

```text
(project_1, transcript, chunk_0001) -> transcript_v1
(project_1, transcript, chunk_0002) -> transcript_v3
(project_1, alignment,  chunk_0002) -> alignment_v2
(project_1, subtitle,   global)     -> subtitle_v4
```

指针更新必须是事务性的。历史由不可变 Artifact 表示，不能塞进 JSON manifest 的可变指针日志。

## 2. 媒体时间轴模型

### MediaProbeArtifact

保存视频流、音频流和容器呈现时间轴的诊断事实，包括起始时间、time base、时长、edit list 信息、可检测的 discontinuity，以及 Media Prep 采用的修正动作。它必须记录：

- `timeline_status`：`normal`、`corrected` 或 `unverified`；
- 版本化 `timeline_tolerance_ms`；
- 诊断出的时间轴问题；
- 已应用的 pad、trim 或 duration-fill 动作。

### TimelineAudioArtifact

视频输入时，由 Media Prep 渲染出的内部工作音频。它的 0ms 与视频呈现 0ms 相同，总时长与视频时间线一致，缺失的开始或尾部采样以静音表示。后续 Chunker、Transcriber 和 Aligner 只读取该工作音频，不再读取源流 offset。

直接音频输入以文件自身时间轴生成 Timeline Audio，不推断它在其他媒体中的位置。

## 3. 词库模型

### GlossaryArtifact

v0.1 的语义 payload 是一组扁平字符串：

```json
{
  "terms": [
    "Extended Kalman Filter",
    "EKF",
    "扩展卡尔曼滤波"
  ]
}
```

规则：

- 每项都是可能在项目音频中实际出现的非空词形。
- 只做最小 Unicode 规范化、首尾空白清理和精确去重，不做语义合并。
- 不存在 `aliases`、`canonical_name`、`same_entity`、`normalize_to` 或通用 `note`。
- 词库不得授权语义模型把音频中的简称扩写成正式名称，或把一种语言改写成另一种语言。
- 证据和创建来源属于 provenance 记录，不发送给 Semantic Transcriber。

System Glossary 和 Project Glossary 都是不可变 Artifact。编辑项目词库会创建新 Artifact。Effective Glossary 是二者 `terms[]` 的确定性去重并集。

辅助资产本身不是 Prompt Context。Extractor 存在时，只能产生带 Source Locator 的候选词条；用户或确定性策略把它们提升为新的 Project Glossary。

## 4. Transcript 模型

### TranscriptArtifact

一个 Media Chunk 的唯一语义权威。

必需 payload 概念：

- `chunk_id`
- Provider 返回的 `source_text`
- 有序 `atoms`
- `atomizer_version`
- 已知时声明语言
- 可用时保存 Provider 作用域内的语义 confidence

### TranscriptAtom

CueFlow 自己定义、与 Provider Token 无关且需要声学对齐的最小文本单位。标点和空格不是 Atom。

必需概念：

- Artifact 作用域内稳定的 `atom_id`
- 可发音文本 `text`
- 有序位置
- `atom_class`：`word`、`cjk_character`、`number` 或 `pronounceable_symbol`
- 可选显示装饰 `decoration_after`，用于保存紧随该实音单位的空格或必要标点

Atom ID 在不可变 Transcript Artifact 内稳定，不承诺延续到新的 Transcript 版本。因此依赖引用必须同时包含 Transcript Artifact ID。

Transcript 可以通过 `leading_decoration + concat(atom.text + atom.decoration_after)` 重建 Provider 的 `source_text`。显示装饰没有独立声学时间，也不参与未对齐检查。Transcript 内容身份覆盖 Source Text、Atoms、Atomizer Version 和其他语义 payload 字段；文字相同但 Atom 划分或装饰归属不同，仍是新的 Transcript Artifact，并使 Alignment 失效。

## 5. Alignment 模型

### AlignmentArtifact

一个 Media Chunk 及其对应 Transcript Artifact 的唯一声学时间权威。一块只有一个 Current Alignment；不存在 Global Alignment Artifact。

必需概念：

- 精确的 MediaChunk 和 Transcript Artifact 引用
- 有序 Atom 时间分配
- 全局整数毫秒区间
- 无法对齐的实音 Atom 必须显式表示，不能伪造时间
- 可用时保存 Provider 作用域内的 Alignment confidence

Aligner 可以报告文字无法对齐，但不得插入、删除或替换 Transcript Atom。

## 6. Subtitle 模型

### SubtitleArtifact

对所有 Current Chunk Transcript Atom 和对应 Alignment 区间的全局确定性切分。

每个 Cue 包含：

- `cue_id`
- 全局 `[start_ms, end_ms)` 区间
- 连续的 Transcript Atom span
- 从这些 Atom 派生的渲染文字

Cue 中的实音文字序列必须可以从引用的 Transcript Atom 重建。Segmenter 可以按版本化样式减少 `decoration_after` 中的标点、以空格或 Cue 边界替代标点，但禁止增删、替换或重排实音 Atom。Cue 起止时间取该 Cue 第一个和最后一个 Aligned Atom，不由装饰字符决定。

## 7. QA 模型

### QaArtifact

对精确输入 Artifact 版本进行只读分析。

每个 Issue 包含：

- QaArtifact 内稳定的 Issue ID
- `severity`：`blocking_error` 或 `warning`
- 类型化 Rule Code
- 受影响的 Artifact 引用和可选范围
- 结构化 observed values
- `resolution_status`：`detected`、`rework_requested`、`resolved` 或 `unresolved`
- 已执行的 Semantic Attempt 次数（包含首次，单个 Chunk、单次 Run 上限为 4）和产生的新 Artifact 引用

它不能包含会自动应用到上游文字的指令。只有版本化白名单内的 QA rule code 才能由 Orchestrator 请求 Semantic Transcriber 重新处理对应 Chunk；每次返工都生成新 Transcript/Alignment，QA 自身仍不改字。每个 Chunk、每次 Run 最多 4 次 Semantic Attempt（首次 + 最多 3 次自动返工）；达到上限后停止返工并生成 ReviewIssue。普通低 confidence 记录不直接暴露。

`possible_chunk_boundary_duplication` 是根据相邻 Transcript Chunk 前后缀产生的 warning；它不能证明一定出错，也不能触发自动删除。

## 8. 运行模型

### Run

Orchestrator 针对一组固定项目输入、Processing Profile 与配置执行的一次流程。

建议状态：

```text
created -> running -> succeeded
                   -> failed
                   -> cancelled
                   -> interrupted
```

### Invocation

一次真实 Provider 调用尝试。一个 Run 阶段可以包含多个 Invocation。即使复用本地逻辑操作键，每一次实际重发也必须产生新的 Invocation 记录。

建议状态：

```text
created -> sending -> succeeded
                   -> definitely_not_sent
                   -> delivery_ambiguous
                   -> explicit_failure
```

### Provenance

v0.1 的 provenance 由 Artifact envelope、Dependency Edge、Producer Metadata 和精确 Source Locator 共同提供，不建设独立通用图引擎。Locator 可以指向幻灯片、PDF 页、文档区域、Excel 单元格或媒体区间。

## 9. Confidence、位置与金额

Confidence 必须按维度分开：

- 语义/转写 confidence；
- 声学/对齐 confidence；
- 词库提取 confidence；
- QA severity；
- 未来人工确认状态。

Provider 分数必须保留 Provider、分数名称和尺度语义。v0.1 不把它们转换为跨 Provider 概率。缺失分数保持缺失；人工确认绝不能用 `confidence = 1.0` 表示。低 confidence 是内部检查或返工信号，不等于用户可见 Review Issue。

内容中提到的地点仍是普通 Transcript 或 Glossary 文本。`SourceLocator` 只记录证据出现在哪里；v0.1 不建立地理实体或地理编码模型。

Provider 成本是可选 Invocation Metadata，不是字幕领域数据。如果记录金额，只能使用十进制字符串或整数最小货币单位，并明确保存 ISO 货币代码。禁止浮点金额和按系统地区推断币种。
