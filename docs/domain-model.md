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

用户输入内容身份的一次不可变登记。SourceAsset 记录完整字节哈希，但不等于项目必须长期保存原始字节。

必需概念：

- `source_asset_id`
- `asset_kind`：media 或 auxiliary
- 媒体/文档格式
- 完整文件内容哈希与字节长度
- 存储定位器和存储模式
- 登记时间

路径和文件名只是定位器与标签，不是身份。同一路径下的文件内容哈希改变时，应产生新的 SourceAsset。v0.1 对视频使用 `external_reference` 存储模式：读取外部原文件完成哈希、探测、Timeline Audio 和 Video Proxy 后，不在项目中复制原始大视频，也绝不删除或修改用户外部文件。需要重新执行 Media Prep 时，外部文件必须仍可访问且完整哈希匹配；已安全登记的下游 Artifact 不因外部文件之后不可访问而自动失效。

### Artifact

CueFlow 生成的不可变、版本化结果。共享 envelope 在 `schema-contracts.md` 中定义，各 kind 拥有不同 payload。

v0.1 支持：

- `media_probe`
- `timeline_audio`
- `video_proxy`
- `chunk_plan`
- `media_chunk`
- `system_glossary`
- `project_glossary`
- `effective_glossary`
- `transcript`
- `alignment`
- `subtitle`
- `qa`
- `filler_review`
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

把 `(project_id, artifact_kind, scope_key)` 映射到一个当前 Artifact ID。`scope_key` 对 Chunk Artifact 使用 `chunk_id`，对 Timeline Audio、Video Proxy、Glossary、Subtitle、QA、Filler Review 和 SRT Render 等项目级 Artifact 使用 `global`。

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

### VideoProxyArtifact

只对视频输入生成的项目内部低容量同步复核副本。它必须引用精确 SourceAsset、MediaProbe 和 TimelineAudio，保持原视频宽高比并限制在 640×360 边界内，使用约 1Mbps 的版本化视频目标码率，并明确标记为非转写、非对齐权威输入。它可以携带由 Timeline Audio 派生的低码率审阅音轨。

Video Proxy 是不可变、内容寻址的正式 Artifact，不是旁路缓存。原始视频完整哈希仍属于 SourceAsset 身份，Proxy Hash 不能替代它。

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
- 声明当前固定 Forced Aligner 支持的语言；缺失或不受支持时本次 Run 失败，不默认猜成中文
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

对所有 Current Chunk 的完整 Transcript Atom 和对应 Alignment 区间进行全局确定性切分。Transcript 必须先完整保存实际人声，不得为字幕显示而摘要、改写、删口语或替换文字。

每个 Cue 包含：

- `cue_id`
- 全局 `[start_ms, end_ms)` 区间
- 连续的 Transcript Atom span
- 从这些 Atom 派生的渲染文字

Cue 中的实音文字序列必须可以从引用的 Transcript Atom 重建。Segmenter 可以按版本化样式删除显示标点、以空格或 Cue 边界代替逗号，但禁止修改 Transcript Atom。Cue 起止时间取该 Cue 第一个和最后一个 Aligned Atom，不由装饰字符决定。

v0.1 的显示单位由版本化 Segmenter Config 定义：`cjk_character`、完整 `word`、完整 `number` 和完整 `pronounceable_symbol` Atom 各计 1 个单位。普通 Cue 最多 10 个单位。Effective Glossary 中至少 2 个 Atom 的精确匹配跨度是不可拆保护单元；单个保护单元自身超过 10 单位时允许该 Cue 仅为容纳它而超限，并产生 warning。

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

它不能包含会自动应用到上游文字的指令。只有版本化白名单内的 QA rule code 才能由 Orchestrator 请求 Semantic Transcriber 重新处理对应 Chunk；每次返工都生成新 Transcript/Alignment，QA 自身仍不改字。每个 Chunk、每次 Run 最多 4 次 Semantic Attempt（首次 + 最多 3 次自动返工）。普通 Provider confidence 记录不直接暴露，也不在 v0.1 中转换为统一概率或阈值。

`possible_chunk_boundary_duplication` 是根据相邻 Transcript Chunk 前后缀产生的 warning；它不能证明一定出错，也不能触发自动删除。

`glossary_single_atom_conflict` 只处理至少 2 个 Atom 的 Glossary term。候选窗口必须与 term 的 Atom 数量和 class 序列相同，NFC/casefold 后恰好一个 Atom 不同；单 Atom term 不参与。该 Issue 可以触发 Semantic Rework，但 Glossary 不能直接覆盖 Transcript。

连续两次 Attempt 的候选 Atom 序列完全相同表示稳定。稳定结果与 Glossary term 一致时 Issue 为 `resolved`；稳定但仍为同一冲突时产生非阻塞 `stable_glossary_conflict` ReviewIssue；4 次 Attempt 内始终没有连续一致结果时产生非阻塞 `unstable_glossary_conflict` ReviewIssue。

时间戳非法、Cue 重叠或越界、实音 Atom 未正确对齐、Transcript/Alignment/Chunk 引用错位及 Artifact 依赖身份不一致属于 blocking structural error。它们必须由 Orchestrator 重新执行对应的确定性阶段或 Alignment；仍无法修复时明确失败并禁止导出。

## 8. Filler Review 模型

### FillerReviewArtifact

对精确 Subtitle、Transcript 和 Alignment 版本进行只读显示审查。它不生成替代字幕文字，只能在预先限定的候选中标记 SRT 可隐藏的 Atom。

每个 suppression 必须包含精确 Transcript Artifact ID、Atom ID、Cue ID、原 Atom 文字、`reason = terminal_filler`、review mode、规则或 Provider/Model 配置身份，以及用于判定的结构化证据。

候选只能是 Cue 最后一个实音 Atom，白名单固定为 `啊`、`呀`、`哦`、`嗯`、`呃`，每个 Cue 最多一个，不处理 `呢`。被隐藏 Atom 仍保留在 Transcript、Alignment、Subtitle Atom span 和 Cue 时间包络内。SRT Render 根据 FillerReviewArtifact 省略其显示；取消 suppression 即可恢复显示，不需要重算 Transcript 或 Alignment。

`LOCAL_PROFILE` 使用保守确定性规则；`CLOUD_PROFILE` 使用固定 Omni Plus Snapshot 做受限纯文本判断，并且返回值只能是候选 Atom ID 的子集。Cloud review unavailable 时 payload 明确记录状态和空 suppressions，不阻塞导出。

## 9. 运行模型

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

## 10. Confidence、位置与金额

Confidence 必须按维度分开：

- 语义/转写 confidence；
- 声学/对齐 confidence；
- 词库提取 confidence；
- QA severity；
- 未来人工确认状态。

Provider 分数必须保留 Provider、分数名称和尺度语义。v0.1 不把它们转换为跨 Provider 概率，也不冻结 `0.90/0.95` confidence 阈值。缺失分数保持缺失并记录 `unavailable`；人工确认绝不能用 `confidence = 1.0` 表示。v0.1 的专名/术语返工使用确定性 Glossary 冲突与跨 Attempt 稳定性证据，而不是伪造概率。

内容中提到的地点仍是普通 Transcript 或 Glossary 文本。`SourceLocator` 只记录证据出现在哪里；v0.1 不建立地理实体或地理编码模型。

Provider 成本是可选 Invocation Metadata，不是字幕领域数据。如果记录金额，只能使用十进制字符串或整数最小货币单位，并明确保存 ISO 货币代码。禁止浮点金额和按系统地区推断币种。
