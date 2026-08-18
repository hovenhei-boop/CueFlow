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
时间轴探测、工作音频/视频代理渲染与分块
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
Filler Review（只标记可隐藏 Atom）
        |
        v
output/subtitles.srt
```

正常情况下，用户只能看到当前的 `subtitles.srt`。以前的 Transcript、Alignment、Subtitle、QA、Filler Review 和 Video Proxy 版本属于项目内部 Artifact。CueFlow 不导出 `review_report.json`，也不让用户从多个历史 SRT 中选择。

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
3. Alignment、QA、Filler Review 和 Export 都不得改写实音 Atom；Segmentation 只能调整标点、空格和 Cue 边界等显示装饰。Filler Review 只能按版本化规则标记少量 Cue 末尾 Atom 在 SRT 中隐藏，并保留可审计引用。
4. 语义转写和强制对齐必须处理同一个媒体 Chunk。
5. 每个持久化中间结果都是不可变且按内容寻址的 Artifact。
6. 当前上游 Artifact 发生变化时，所有当前下游 Artifact 必须先标记 stale，之后才能再次导出。
7. SQLite 是项目控制平面；文件系统是 Artifact 数据平面。
8. Provider 原生 payload 必须止于 Adapter 边界。
9. 用户只选择 CueFlow 固定的本地或云端 Processing Profile，不能自由选择 Provider 或 Model。
10. QA 只能报告问题；自动返工必须由 Orchestrator 路由回语义转写模块并产生新 Transcript。
11. Glossary 永远不能直接覆盖 Transcript；实际音频和 Semantic Transcriber 的结果仍是语义权威。

## 3. 模块

### `project`

创建和打开项目、登记源资产、解析带 `scope_key` 的当前 Artifact 指针、保存 Processing Profile。v0.1 假设一个项目同一时间只有一个活动 Orchestrator。

### `media`

探测媒体时间轴。视频输入时，不做会把首个音频采样重置到 0ms 的裸抽音轨，而是渲染一条与视频播放时间轴同起点、同总时长的工作音频：开始前缺少的采样以静音补齐，尾部保持到视频结束。直接音频输入则以该文件自身的 0ms 和总时长作为 CueFlow 时间轴。

工作音频生成后，`media` 建立确定性的静音感知 `ChunkPlan`。v0.1 固定使用 16kHz、mono、PCM s16le 的 Timeline Audio，`timeline_tolerance_ms = 20`，Chunk 目标为 180 秒、硬上限为 225 秒；优先在目标附近持续至少 500ms、低于 -40dB 的静音中点切分，无合适静音时在硬上限强制切分。Chunk 无重叠、无间隙，用户看不到也不能修改 Provider 能力计算。以后模型能力变化时，通过版本化 Processing Profile 更新 Chunk 参数，而不是建立任意 Provider 路由器。

视频输入还必须生成一个内容寻址的 `video_proxy` Artifact：保持宽高比并限制在 640×360 边界内，视频目标码率约 1Mbps。代理可以携带从 Timeline Audio 派生的低码率审阅音轨，仅用于项目内部同步复核和低容量备份，不是转写或对齐的权威输入。CueFlow 只登记外部原视频的完整哈希、长度、格式和定位器，不在项目内长期复制原始大视频，也绝不删除用户的外部原文件。

### `glossary`

生成语义模型可见的有效词库：

```text
System Glossary + Project Glossary -> Effective Glossary
```

辅助文件不得整份直接进入转写 Prompt。Extractor 可以读取受支持的辅助资产并提出项目词条，但其唯一的下游产物是词库数据。v0.1 发行版可以只有空的 Extractor 注册表；在这种状态下，辅助文件只被登记，直到词条被加入 Project Glossary 前都不会影响转写。

System Glossary 只读，Project Glossary 只影响一个项目。Effective Glossary 是两者经过最小 Unicode 规范化和精确去重后得到的扁平 `terms[]`。它不表达 aliases、同一实体、canonical name 或替换关系，语义模型必须以音频实际发音为准。

### `transcription`

根据项目的固定 Processing Profile，使用一个 Media Chunk 和 Effective Glossary 调用对应的语义 Adapter。输出该 Chunk 的权威实音文字，以及确定性的可声学对齐 Atom；标点和空格只作为 Atom 的显示装饰，不拥有独立声学身份或时间。

Semantic Adapter 还必须声明本地 Forced Aligner 所需的语言名称，并且该值必须属于固定 Model Snapshot 的支持集合。Cloud Adapter 用同一次 Omni 响应返回受限的 `source_text` 与 `language`，不能为缺失语言默认猜成中文；缺失或不受支持时明确失败。

v0.1 只提供以下两个档位：

| Processing Profile | 语义转写 | 强制对齐 | 媒体出境 |
|---|---|---|---|
| `LOCAL_PROFILE` | 本地 `Qwen3-ASR-1.7B` | 本地 `Qwen3-ForcedAligner-0.6B` | 不出境 |
| `CLOUD_PROFILE` | 云端 `qwen3.5-omni-plus-2026-03-15` | 本地 `Qwen3-ForcedAligner-0.6B` | 上传当前 Chunk 音频、Effective Glossary 词条及必要的语言/转写配置 |

两个档位共享 Media Prep、Chunker、Glossary、Forced Aligner、Segmenter、QA 和 Export，唯一替换的是 Semantic Transcriber。具体 Model Snapshot 由应用内部配置并写入 provenance，不暴露给用户选择。

`LOCAL_PROFILE` 默认按阶段串行加载和释放 `Qwen3-ASR-1.7B` 与 `Qwen3-ForcedAligner-0.6B`，不要求两个模型同时驻留，以降低显存峰值。

### `alignment`

用一个 `MediaChunk` 和相同 `chunk_id` 的 `TranscriptArtifact` 调用强制对齐 Adapter。Provider 的 Chunk 局部时间只能在这里转换一次 CueFlow 全局时间。

### `segmentation`

根据各 Chunk 的完整 Transcript Atom 和全局对齐区间生成字幕 Cue。Semantic Transcriber 必须先完整逐字转写实际人声，不得摘要、改写、因显示风格删除口语或擅自修正实际说出的内容。Segmenter 只能选择 Cue 边界、时间包络，并按版本化字幕风格处理标点和空格；不得修改 Transcript。

v0.1 每个 Cue 最多 10 个显示文字单位。CJK Atom 计 1 个单位，完整 `word`、`number` 和 `pronounceable_symbol` Atom 各计 1 个单位且不得从内部拆开。Effective Glossary 中至少 2 个 Atom 的精确匹配跨度视为不可拆保护单元。若单个保护单元本身超过 10 个单位，允许只为容纳该单元而超限，并产生 `protected_unit_exceeds_display_limit` warning；除此以外 10 单位是硬上限。切分优先使用 Provider 原始标点表达的完整语义单元，声学停顿只能辅助，不能凌驾于语义完整性之上。

Transcript 保留全部 Provider 原始 Decoration 以便重建 `source_text`。默认 SRT 风格删除句号、分号、问号、感叹号和破折号；需要表达停顿的逗号转换为空格。该处理只影响 Subtitle render，不改变 Atom 或 Alignment。

### `qa`

逐块读取当前 Transcript/Alignment，并读取全局 Subtitle 与 Glossary，生成内部 `QaArtifact`。阻塞问题禁止导出，warning 不阻塞。v0.1 至少包含时间结构检查，以及相邻 Transcript Chunk 边界疑似重复的 warning。

时间戳非法、Cue 重叠或越界、实音 Atom 未正确对齐、Transcript/Alignment/Chunk 引用错位及 Artifact 依赖身份不一致属于 blocking structural error。Orchestrator 必须重新执行对应的确定性阶段或本地 Alignment；若重新执行后仍不合法，则明确失败并禁止导出。QA 不得直接改字。

v0.1 的 `glossary_single_atom_conflict` 是版本化语义返工白名单规则。单 Atom Glossary term 不参与；只有至少 2 个 Atom 的 Glossary term 与 Transcript 候选窗口 Atom 数量和 class 序列完全相同、经 NFC/casefold 后恰好一个 Atom 不同时才触发。Provider 明确标记为不确定的可映射跨度也可独立触发返工。每个 Chunk、每次 Run 最多执行 4 次 Semantic Attempt（首次 + 最多 3 次返工），每次都生成新的 Transcript 和 Alignment，Glossary 不能直接覆盖结果。

连续两次候选 Atom 序列完全相同表示稳定：若稳定结果与 Glossary term 一致，Issue 为 `resolved`；若稳定但仍保持相同冲突，停止返工并产生非阻塞 `stable_glossary_conflict` ReviewIssue；若到 4 次仍没有连续两次一致，产生非阻塞 `unstable_glossary_conflict` ReviewIssue。v0.1 不使用通用 Levenshtein、拼音/音素相似度、NER 或跨 Provider confidence 阈值。

### `filler_review`

在最终 Subtitle 已生成且结构 QA 通过后，只审查每个 Cue 最后一个实音 Atom 是否可以从 SRT 显示中隐藏。候选白名单固定为 `啊`、`呀`、`哦`、`嗯`、`呃`，不处理 `呢`；每个 Cue 最多隐藏一个 Atom。结果只包含 Artifact/Atom 引用、`terminal_filler` reason 和证据，不得返回或应用重写后的字幕文字。被隐藏 Atom 仍保留在 Transcript、Alignment 和 Cue 时间包络内。

`CLOUD_PROFILE` 使用同一个 `qwen3.5-omni-plus-2026-03-15` 做一次受限纯文本 Atom suppression 判断；Adapter 只接受预先给定候选 Atom ID 的子集。`LOCAL_PROFILE` 不增加第三个模型，依据原始句末标点、后续声学停顿、Chunk/音频结束和上述极小白名单执行保守确定性规则，有歧义就保留。Cloud Filler Review 失败或送达状态不明确时不自动重试，记录 warning 并以空 suppression 继续导出。

### `export`

把当前且内部一致的 Subtitle、QA 和 FillerReview Artifact 渲染为 SRT。先写临时文件，再原子替换唯一的用户可见文件 `output/subtitles.srt`。不暴露历史版本，也不额外输出报告文件。

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
                         \-> Video Proxy（仅视频输入，非音频权威）
System Glossary + Project Glossary -> Effective Glossary
Media Chunk[n] + Effective Glossary -> Transcript[n]
Media Chunk[n] + Transcript[n] -> Alignment[n]
all Transcript[n] + all Alignment[n] + Segmenter Config -> global Subtitle
all Transcript[n] + all Alignment[n] + global Subtitle + Effective Glossary -> global QA
all Transcript[n] + all Alignment[n] + global Subtitle + Filler Review Config -> global Filler Review
Subtitle + passing QA + Filler Review -> SRT projection
```

最小 stale 路由为：

- Media、Timeline Audio 或 ChunkPlan 改变：所有 Chunk 的 Transcript/Alignment，以及所有全局下游 Artifact stale。视频源身份改变还会使 Video Proxy stale。
- EffectiveGlossary 改变：所有 Chunk 的 Transcript/Alignment，以及所有全局下游 Artifact stale。
- `Transcript[n]` 改变：`Alignment[n]` 与全局 Subtitle、QA、Filler Review、导出投影 stale；其他 Chunk 的 Transcript/Alignment 保持 current。
- `Alignment[n]` 改变：全局 Subtitle、QA、Filler Review 和导出投影 stale；其他 Chunk Alignment 保持 current。
- Segmenter 配置改变：Subtitle、QA、Filler Review 和导出投影 stale。
- QA 规则改变：QA 和导出资格 stale。
- Filler Review 配置或 Cloud Filler Review Model Snapshot 改变：Filler Review 和导出投影 stale。

Chunk 级 `scope_key` 只允许复用没有受影响的完整 Chunk Artifact；全局 Subtitle 和 QA 仍整体重算。任意 Atom 范围的局部执行继续推迟，不建设通用增量调度器。

## 6. 数据生命周期

1. 登记外部输入资产的完整文件内容哈希、长度、格式与定位器，不删除外部文件。
2. 探测媒体，渲染时间轴工作音频并记录 `timeline_status`；视频输入同时生成不可变低容量 Video Proxy，但不保留原始大视频副本。
3. 从工作音频创建不可变 ChunkPlan。
4. 解析本次 Run 使用的不可变 EffectiveGlossary 和固定 Processing Profile。
5. 将每个 Chunk 的转写和本地对齐记录为独立 Invocation 与 Artifact。
6. Subtitle 直接组合所有 current Chunk Transcript/Alignment；不生成 Global Alignment Artifact。
7. 生成内部 QA，修复 structural blocking error，并按每个 Chunk、每次 Run 最多 4 次 Semantic Attempt 的上限执行稳定性返工；仍有 blocking error 时停止导出。
8. 对最终 Subtitle 生成只含 Atom suppression 决策的 Filler Review。
9. 原子替换当前用户可见 SRT；`timeline_status = unverified`、Filler Review unavailable 或未解决 warning 只触发界面提示。
10. 在内部保留旧 Artifact，用于复现和恢复。

临时文件只能在不可变替代文件安全登记后删除。崩溃遗留、尚未登记的内容寻址文件属于可恢复 orphan，可以由之后的显式维护操作清理。项目删除和保留策略是独立产品动作，不能作为 Run 的隐式副作用。

## 7. 未来辅助 SRT 流程

用户以后可以把在外部剪辑软件中修订过的 SRT 重新导入为辅助资产。v0.1 不会因此生成可编辑的 CueFlow 字幕版本，也不会训练或静默修改模型。未来的 SRT Extractor 只能从中提出实际可能出现的扁平字符串，不能建立 alias 或实体归一化关系。完整 SRT diff、分割学习和质量指标仍属于私人模块或未来能力。
