# CueFlow v0.1 架构

状态：设计基线

本文冻结一套最小架构：它既足以支撑当前私人开发版，也能在未来迁移到独立桌面软件。本文没有明确写入的内容，不视为已经批准的实现决策。

## 1. 产品边界

CueFlow 是高质量字幕生成与自动检查引擎，不是字幕编辑器，也不是模型能力评测产品。

用户流程为：

```text
视频或独立音频
        +
系统词库与项目词库
        |
        v
时间轴探测、工作音频渲染与分块
        |
        v
固定处理档位的语义转写
        |
        v
强制对齐
        |
        v
字幕切分
        |
        v
内部 QA
        |
        v
output/subtitles.srt
```

正常情况下，用户只能看到当前的 `subtitles.srt`。以前的 Transcript、Alignment、Subtitle 和 QA 版本属于项目内部 Artifact。CueFlow 不导出 `review_report.json`，也不让用户从多个历史 SRT 中选择。

以下内容明确不属于 v0.1：

- 时间线、波形和视频编辑 UI；
- 软件内文字修正、Cue 拆分/合并、撤销和重做；
- WER、基准集、Provider 排名、能力评价和审计 UI；
- 通用 DAG 调度器、区间级增量重算和分布式 Worker；
- 多用户协作、云同步和项目锁系统；
- 地理实体、地理编码、FX 换算和产品收费；
- 剪辑软件集成和 SRT 以外的导出格式。

私人质量评测必须位于一个独立、不会发行的审计项目中。发行代码可以提供审计项目需要的稳定只读 Artifact 接口，但不能包含其数据集、指标或报告。

## 2. 架构不变量

1. 只有语义转写 Provider 决定“说了什么”。
2. 只有强制对齐器决定“已经确定的文字在什么时候说”。
3. Alignment、QA 和 Export 都不得改写实音 Atom；Segmentation 只能调整标点、空格和 Cue 边界等显示装饰。
4. 语义转写和强制对齐必须处理同一个媒体 Chunk。
5. 每个持久化中间结果都是不可变且按内容寻址的 Artifact。
6. 当前上游 Artifact 发生变化时，所有当前下游 Artifact 必须先标记 stale，之后才能再次导出。
7. SQLite 是项目控制平面；文件系统是 Artifact 数据平面。
8. Provider 原生 payload 必须止于 Adapter 边界。
9. 用户只选择 CueFlow 固定的本地或云端 Processing Profile，不能自由选择 Provider 或 Model。
10. QA 只能报告问题；自动返工必须由 Orchestrator 路由回语义转写模块并产生新 Transcript。

## 3. 模块

### `project`

创建和打开项目、登记源资产、解析带 `scope_key` 的当前 Artifact 指针、保存 Processing Profile。v0.1 假设一个项目同一时间只有一个活动 Orchestrator。

### `media`

探测媒体时间轴。视频输入时，不做会把首个音频采样重置到 0ms 的裸抽音轨，而是渲染一条与视频播放时间轴同起点、同总时长的工作音频：开始前缺少的采样以静音补齐，尾部保持到视频结束。直接音频输入则以该文件自身的 0ms 和总时长作为 CueFlow 时间轴。

工作音频生成后，`media` 建立确定性的静音感知 `ChunkPlan`。v0.1 的两个固定档位都以约 3–4 分钟为目标，并使用小于 5 分钟的内部硬上限；用户看不到也不能修改 Provider 能力计算。以后模型能力变化时，通过版本化 Processing Profile 更新 Chunk 参数，而不是建立任意 Provider 路由器。

### `glossary`

生成语义模型可见的有效词库：

```text
System Glossary + Project Glossary -> Effective Glossary
```

辅助文件不得整份直接进入转写 Prompt。Extractor 可以读取受支持的辅助资产并提出项目词条，但其唯一的下游产物是词库数据。v0.1 发行版可以只有空的 Extractor 注册表；在这种状态下，辅助文件只被登记，直到词条被加入 Project Glossary 前都不会影响转写。

System Glossary 只读，Project Glossary 只影响一个项目。Effective Glossary 是两者经过最小 Unicode 规范化和精确去重后得到的扁平 `terms[]`。它不表达 aliases、同一实体、canonical name 或替换关系，语义模型必须以音频实际发音为准。

### `transcription`

根据项目的固定 Processing Profile，使用一个 Media Chunk 和 Effective Glossary 调用对应的语义 Adapter。输出该 Chunk 的权威实音文字，以及确定性的可声学对齐 Atom；标点和空格只作为 Atom 的显示装饰，不拥有独立声学身份或时间。

v0.1 只提供以下两个档位：

| Processing Profile | 语义转写 | 强制对齐 | 媒体出境 |
|---|---|---|---|
| `LOCAL_PROFILE` | 本地 `Qwen3-ASR-1.7B` | 本地 `Qwen3-ForcedAligner-0.6B` | 不出境 |
| `CLOUD_PROFILE` | 云端 `Qwen3.5-Omni` | 本地 `Qwen3-ForcedAligner-0.6B` | 上传当前 Chunk 音频、Effective Glossary 词条及必要的语言/转写配置 |

两个档位共享 Media Prep、Chunker、Glossary、Forced Aligner、Segmenter、QA 和 Export，唯一替换的是 Semantic Transcriber。具体 Model Snapshot 由应用内部配置并写入 provenance，不暴露给用户选择。

`LOCAL_PROFILE` 默认按阶段串行加载和释放 `Qwen3-ASR-1.7B` 与 `Qwen3-ForcedAligner-0.6B`，不要求两个模型同时驻留，以降低显存峰值。

### `alignment`

用一个 `MediaChunk` 和相同 `chunk_id` 的 `TranscriptArtifact` 调用强制对齐 Adapter。Provider 的 Chunk 局部时间只能在这里转换一次 CueFlow 全局时间。

### `segmentation`

根据各 Chunk 的 Transcript Atom 和全局对齐区间生成字幕 Cue。它可以选择 Cue 边界、时间包络，并按版本化字幕风格减少标点或用空格/Cue 边界代替标点；不得增删或替换可发音 Atom 的文字。必要标点附着在相邻实音 Atom 之后，不参与 Alignment。

### `qa`

逐块读取当前 Transcript/Alignment，并读取全局 Subtitle 与 Glossary，生成内部 `QaArtifact`。阻塞问题禁止导出，warning 不阻塞。v0.1 至少包含时间结构检查，以及相邻 Transcript Chunk 边界疑似重复的 warning。

普通低 confidence 或可疑文字先作为内部 Issue 交给 Orchestrator。每个 Chunk、每次 Run 最多执行 4 次 Semantic Attempt（首次 + 最多 3 次自动返工），且只有版本化白名单内的 QA rule code 可以触发自动返工。Orchestrator 把受影响 Chunk 路由回 Semantic Transcriber，生成新 Transcript 并重新对齐；QA 本身不改字。达到上限后停止返工，仍未解决的问题成为用户界面的 ReviewIssue。它们仍是内部数据，不导出 `review_report.json`。

### `export`

把当前且内部一致的 Subtitle Artifact 渲染为 SRT。先写临时文件，再原子替换唯一的用户可见文件 `output/subtitles.srt`。不暴露历史版本，也不额外输出报告文件。

### `orchestration`

执行固定的 v0.1 阶段顺序，记录 Run 和 Invocation，登记 Artifact 依赖，传播 stale 状态，执行有界返工与导出闸门。它是普通应用协调代码，不是 AI 组件，也不是通用工作流引擎。

### `registry` 与 `artifact_store`

SQLite 保存身份、关系、当前指针和运行状态。文件存储保存不可变 payload 和大型媒体对象。SQLite 不保存音视频 BLOB；可变 `manifest.json` 不能充当状态数据库。

## 4. 时间轴与分块契约

视频输入时，Media Prep 必须探测视频流、音频流、容器呈现起点、time base、时长、edit list 及能够识别的 timestamp discontinuity，并把结果写入 `media_probe`。程序不能只假设音视频已经从同一时刻开始。

Media Prep 根据探测结果执行三级处理：

- `normal`：时间轴无需修正，直接渲染工作音频。
- `corrected`：异常可以确定性修复，例如音轨相对视频晚开始；通过补静音、裁到视频呈现边界或补足尾部，将修正烘焙进工作音频。
- `unverified`：存在无法可靠修复的 discontinuity 或时长关系；仍允许继续生成和导出 SRT，但完成界面必须明确提醒用户检查开头、中段和结尾的同步。

容差是版本化 Media Prep 配置，不写死为领域常量。无论状态如何，下游都只读取渲染后的工作音频和一条从 0ms 开始的 CueFlow 时间轴，不读取或叠加长期 `source_offset_ms`。

直接音频输入以文件自己的 0ms 为 CueFlow 0ms。CueFlow 不猜测它在某个未提供视频中的原始位置；如果用户上传的是截取音频，生成的 SRT 也只对应这份音频自己的时间轴。

Core 时间统一使用有符号 64 位整数毫秒。字段名包含单位，区间统一使用半开语义 `[start_ms, end_ms)`。

每个 `MediaChunk` 保存全局边界。Provider 输出的是 Chunk 局部时间，因此 Alignment Adapter 执行：

```text
global_start_ms = chunk.global_start_ms + provider_local_start_ms
global_end_ms   = chunk.global_start_ms + provider_local_end_ms
```

后续模块不得再次增加 Chunk offset。媒体探测数据、修正动作和 `timeline_status` 必须持久化用于诊断，但 Core 不存在需要在 Export 阶段补回的全局 `source_offset` 路径。

## 5. 状态与依赖流

v0.1 使用固定依赖图：

```text
Source Media -> Media Probe -> Timeline Audio -> Chunk Plan -> Media Chunk
System Glossary + Project Glossary -> Effective Glossary
Media Chunk[n] + Effective Glossary -> Transcript[n]
Media Chunk[n] + Transcript[n] -> Alignment[n]
all Transcript[n] + all Alignment[n] + Segmenter Config -> global Subtitle
all Transcript[n] + all Alignment[n] + global Subtitle + Effective Glossary -> global QA
Subtitle + passing QA -> SRT projection
```

最小 stale 路由为：

- Media、Timeline Audio 或 ChunkPlan 改变：所有 Chunk 的 Transcript/Alignment，以及所有全局下游 Artifact stale。
- EffectiveGlossary 改变：所有 Chunk 的 Transcript/Alignment，以及所有全局下游 Artifact stale。
- `Transcript[n]` 改变：`Alignment[n]` 与全局 Subtitle、QA、导出投影 stale；其他 Chunk 的 Transcript/Alignment 保持 current。
- `Alignment[n]` 改变：全局 Subtitle、QA 和导出投影 stale；其他 Chunk Alignment 保持 current。
- Segmenter 配置改变：Subtitle、QA 和导出投影 stale。
- QA 规则改变：QA 和导出资格 stale。

Chunk 级 `scope_key` 只允许复用没有受影响的完整 Chunk Artifact；全局 Subtitle 和 QA 仍整体重算。任意 Atom 范围的局部执行继续推迟，不建设通用增量调度器。

## 6. 数据生命周期

1. 登记输入资产并计算完整文件内容哈希。
2. 探测媒体，渲染时间轴工作音频并记录 `timeline_status`。
3. 从工作音频创建不可变 ChunkPlan。
4. 解析本次 Run 使用的不可变 EffectiveGlossary 和固定 Processing Profile。
5. 将每个 Chunk 的转写和本地对齐记录为独立 Invocation 与 Artifact。
6. Subtitle 直接组合所有 current Chunk Transcript/Alignment；不生成 Global Alignment Artifact。
7. 生成内部 QA，并按每个 Chunk、每次 Run 最多 4 次 Semantic Attempt 的上限路由白名单问题；存在阻塞问题时停止导出。
8. 原子替换当前用户可见 SRT；`timeline_status = unverified` 或未解决 warning 只触发界面提示。
9. 在内部保留旧 Artifact，用于复现和恢复。

临时文件只能在不可变替代文件安全登记后删除。崩溃遗留、尚未登记的内容寻址文件属于可恢复 orphan，可以由之后的显式维护操作清理。项目删除和保留策略是独立产品动作，不能作为 Run 的隐式副作用。

## 7. 未来辅助 SRT 流程

用户以后可以把在外部剪辑软件中修订过的 SRT 重新导入为辅助资产。v0.1 不会因此生成可编辑的 CueFlow 字幕版本，也不会训练或静默修改模型。未来的 SRT Extractor 只能从中提出实际可能出现的扁平字符串，不能建立 alias 或实体归一化关系。完整 SRT diff、分割学习和质量指标仍属于私人模块或未来能力。
