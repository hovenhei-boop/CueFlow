# CueFlow v0.1 失败模型

状态：设计基线

本失败模型优先防止那些会生成“看起来正常但语义已经错配”的 SRT：混合不同版本、配错 Chunk、重复增加时间 offset、歧义付费请求和部分状态更新。

## 1. 失败原则

1. 违反不变量时必须明确失败，不能伪造看似合理的 SRT。
2. 新状态完全持久化以前，保留最后一组已知有效的 Current Pointer。
3. 每次实际 Provider 尝试必须单独记录。
4. 不得静默切换 Processing Profile、Provider、Model Snapshot 或数据出境路径。
5. 默认不得自动重试送达状态不明确的请求。
6. QA warning 可以降低可置信程度，但不能修改数据。
7. Blocking QA 和 stale 依赖都必须阻止导出；`timeline_status = unverified` 是明确警告，不阻止用户取得 SRT。
8. Glossary 只能提供冲突与返工证据，永远不得直接覆盖 Semantic Transcriber 的文字。
9. Filler Review 只能隐藏经过验证的候选 Atom；失败时保留原显示文字，不能改写或扩大 suppression。

## 2. Provider Invocation 结果

Provider 尝试按“已知发生了什么”分类，不能只用一个通用 `failed` Boolean。

### `definitely_not_sent`

已知请求没有到达 Provider，例如发送前本地输入校验失败。修正本地条件后，或按明确重试策略，可以安全自动重试。

### `delivery_ambiguous`

请求可能已到达 Provider，但没有收到确定响应。例如上传完成后连接中断，或长请求仍可能在运行时发生超时。默认行为：

- 保留 Invocation 和逻辑操作键；
- 不自动重试；
- 向调用方暴露显式重试决定；
- 如果重试，创建新的 Invocation，并保留两次结果。

本地幂等键可以复用已知成功结果，但不能承诺 Provider 会避免重复计费或重复执行。

### `explicit_failure`

Provider 返回明确失败。只有明确的 Provider Policy 把该状态标记为可重试时才重试；重试仍是新的 Invocation。

### `succeeded`

响应已经通过 Adapter 解析和 Core Schema 校验，并且所生成 Artifact 已安全登记。HTTP 在语法上成功但语义 payload 无效时，不能记为 `succeeded`。

## 3. Run 恢复

应用启动时，应在核对持久化 Artifact 和 Invocation 后，把遗留在 `running` 的 Run 标记为 `interrupted`。只有完整输入身份与配置哈希都匹配时，恢复流程才能复用已有 Artifact。

恢复不能只凭文件存在就推断成功，而要检查：

- 已登记 Artifact Row；
- 文件存在且 Content Hash 正确；
- 精确输入依赖身份；
- Schema Version 可被当前版本解释；
- Producer 配置身份。

存在文件但没有 Registry Row 时，它属于 orphan。Registry Row 存在但文件缺失时，属于完整性错误，不能成为 Current Artifact。

视频 SourceAsset 默认只保存外部定位器和完整内容身份。若 Timeline Audio、Video Proxy 等已登记 Artifact 完整，下游恢复不要求外部原视频仍存在；只有需要重新执行 Media Prep 时才必须重新访问外部文件并校验完整哈希。CueFlow 不得删除、移动或修改外部原文件。

## 4. Artifact 原子发布

Artifact 发布顺序：

1. 在目标文件系统内把 Canonical Payload 序列化到临时文件；
2. Flush 并关闭文件；
3. 计算并校验 Content Hash；
4. 原子重命名到不可变内容寻址路径；
5. 开始 SQLite 写事务；
6. 插入 Artifact 与 Dependency Row；
7. 更新所有相关 Current Pointer 和 stale 状态；
8. Commit。

进程在第 4 步前停止时，临时文件可以安全丢弃。第 4、5 步之间停止时，只会留下 orphan，当前状态仍然有效。SQLite 事务失败时，所有指针更新一起回滚。

多步“读取—校验—写入”操作使用 SQLite 写事务，通常采用 `BEGIN IMMEDIATE`，避免两个写者同时根据旧 Current Pointer 作出决定。这只是正确使用数据库，不是项目锁子系统。除此以外，v0.1 假设一个项目只有一个活动 Orchestrator。

## 5. 媒体时间轴异常

视频输入必须先产生可审计的 `media_probe`。Media Prep 的结果分为：

- `normal`：按探测到的呈现时间轴渲染 Timeline Audio；
- `corrected`：确定性应用静音 pad、呈现边界 trim 或尾部补静音，并把动作写入 Artifact；
- `unverified`：检测到无法可靠修复的 discontinuity 或时长异常，但仍渲染当前最佳 Timeline Audio。

Media Prep 不允许把首个音频采样无条件搬到 0ms，也不允许在未记录的情况下修正 timestamp。修正完成后，后续模块只认识从 0ms 开始的 Timeline Audio；不存在 Export 时再次补回的全局 offset。

`unverified` 不得被伪装成 `normal`，也不阻止生成 SRT。完成界面必须明确提示字幕可能存在整体或局部偏移，并建议用户检查开头、中段和结尾。用户可见提示来自项目内部状态，不产生额外报告文件。

视频 Media Prep 还必须发布正式 `video_proxy` Artifact。Timeline Audio 与 Video Proxy 都安全落盘并登记以前，不得把新 Media Prep 状态切换为 current。Proxy 生成失败会使本次 Media Prep 失败，但绝不能影响外部原视频；已经写入但尚未登记的文件按 orphan 处理。

## 6. SRT 原子投影

用户可见 SRT 只能由已登记的内部 `srt_render` Artifact 生成；它的 Subtitle、QA 与 `filler_review` 依赖链必须 current 且非 stale。

发布流程：

```text
写 output/subtitles.srt.tmp
校验 SRT 已完整渲染
原子重命名/替换 -> output/subtitles.srt
```

替换前崩溃时，原来的有效 SRT 保持不变。不存在需要与 SRT 跨文件原子同步的第二份用户报告。

## 7. 导出闸门

存在以下任何情况时都禁止导出：

- 任一 Active Chunk 缺少相同 `scope_key` 的 Transcript 或 Alignment 指针；
- 全局 Subtitle 或 QA 指针缺失；
- 全局 Filler Review 指针缺失；
- 任一参与导出的 Chunk 或全局 Artifact 已 stale；
- Artifact 没有依赖精确的 Active Upstream Version；
- Transcript、Alignment 与 MediaChunk 的 `chunk_id`/`scope_key` 不一致；
- 任一 Chunk Alignment 引用了缺失或不同 Transcript 中的 Atom；
- Cue 时间非法、无序或超出媒体时间线；
- QA result 为 `blocked`；
- Filler Review suppression 引用未知、非候选或不属于对应 Cue 的 Atom；
- 所需 Schema Major Version 未知。

导出校验必须枚举当前 ChunkPlan 中的每个 Active Chunk，不能只检查一个项目级“Active Transcript”。所有 Chunk 校验通过后，Subtitle 才能直接组合它们；不存在需要单独校验的 Global Alignment Artifact。

Warning（包括 Chunk 边界疑似重复、`stable_glossary_conflict`、`unstable_glossary_conflict`、`protected_unit_exceeds_display_limit`、Filler Review unavailable 和 `timeline_status = unverified`）不阻塞导出。CLI 展示这些内部 warning，但 v0.1 不生成公开 Review Report，也不要求 `human_verified_final` 状态。

## 8. Processing Profile 与隐私边界

用户只能选择 `LOCAL_PROFILE` 或 `CLOUD_PROFILE`，不能输入 Provider/Model ID：

- `LOCAL_PROFILE`：Qwen3-ASR-1.7B 和 Qwen3-ForcedAligner-0.6B 都在本地运行，媒体不因语义转写或对齐出境。两个模型默认按阶段串行加载和释放，不要求同时驻留，以降低显存峰值。
- `CLOUD_PROFILE`：语义转写会把当前 Chunk 音频、Effective Glossary 词条及必要的语言/转写配置上传到 `qwen3.5-omni-plus-2026-03-15`；返回 Transcript 后，原 Chunk 与文字交给本地 Qwen3-ForcedAligner-0.6B。

调用失败不得触发自动切换 Profile、把 Local 改成 Cloud、把 Cloud 改成其他 Provider，或把本地 Alignment 改成云端 Alignment。应用升级内部 Model Snapshot 时必须产生新的 Profile/Producer 配置身份，旧 Run 仍可解释其真实生产者。

Semantic Transcriber 未返回当前固定 Forced Aligner 支持的语言名称时，本次调用按契约失败处理；不得默认猜成中文，也不得换用其他 Aligner。

Cloud Semantic Transcriber 固定为 `qwen3.5-omni-plus-2026-03-15`。Local 和 Cloud 都固定使用本地 `Qwen3-ForcedAligner-0.6B`，不得切换到云端 Alignment。设备、dtype、显存和 CUDA 由运行时能力检测与版本化运行配置决定；资源不足时明确失败，不得自动量化、换模型或切换 Profile。

Cloud Filler Review 使用同一个 Omni Plus Snapshot 进行纯文本、候选 Atom ID 限定的判断。它是显示优化而不是 Semantic Attempt。失败或送达不明确时记录独立 Invocation，不自动重试，以空 suppression 和 warning 继续；Local Profile 使用确定性规则且不增加第三个模型。

v0.1 默认保留策略提案：

- 在项目中保留规范化 Artifact 和最小 Invocation Metadata；
- 保留 Provider/Model ID、时间、可用时的 Response ID 和失败分类；
- 普通日志不得包含密钥、完整媒体和完整请求体；
- 原始 Provider Response 只能保存在明确隔离、可配置的内部区域，而且不是 Core 必需状态。

改变此保留策略需要显式产品决策，因为它同时影响隐私与复现能力。

## 9. QA 与内部返工失败行为

对于一个 Ruleset Version 和一组精确输入 Artifact，QA 应当是确定性的。Rule Engine 执行失败会产生失败的 QA Run，不能被解释成“没有发现问题”。

时间戳非法、Cue 重叠或越界、实音 Atom 未正确对齐、Transcript/Alignment/Chunk 引用错位及 Artifact 依赖身份不一致是 blocking structural error。Orchestrator 对受影响的确定性阶段或本地 Alignment执行一次修复性重算；重算后仍非法则 Run 失败并禁止导出。不得把 structural error 降为 warning。

`glossary_single_atom_conflict` 是 v0.1 语义返工白名单规则。单 Atom Glossary term 不参与；至少 2 个 Atom 的 term 与 Transcript 窗口必须 Atom 数量和 class 序列完全相同，NFC/casefold 后恰好一个 Atom 不同。Provider 明确标记为不确定的可映射跨度也可独立触发。v0.1 不使用 Levenshtein、拼音/音素、NER 或 confidence 阈值。

只有 Orchestrator 可以请求 Semantic Transcriber 重新处理对应 Chunk；QA 不得直接改字。每个 Chunk、每次 Run 最多 4 次 Semantic Attempt（首次 + 最多 3 次自动返工）。每次返工创建新的 Transcript、Alignment、Invocation 和依赖记录，并只切换该 Chunk 的 Current Pointer。Glossary 只能进入受限提示并提醒模型核对实际发音，不能强制替换结果。

连续两次候选 Atom 序列完全相同表示稳定。稳定结果与 Glossary term 一致时 Issue 标记为 `resolved`；稳定但仍保持同一个冲突时停止返工，保留 Semantic Transcriber 的实际结果，并生成非阻塞 `stable_glossary_conflict` ReviewIssue。达到 4 次仍未连续稳定时生成非阻塞 `unstable_glossary_conflict` ReviewIssue。返工循环不能无限运行，也不能把一次 `delivery_ambiguous` 云请求当作普通自动返工再次发送。

`possible_chunk_boundary_duplication` 比较相邻 Transcript Chunk 的规范化前后缀窗口。它只能发出 warning，并记录两个位置和观测到的重叠文字。它不得编辑文字，因为重复内容可能本来就是真的；没有重叠也不能证明没有吞字。

## 10. 首次实现必须验证的内容

首次实现至少要有以下契约测试：

- Canonical Hash，包括 Atomizer Version 改变；
- 用延迟起始音轨验证 Timeline Audio 补静音后仍与视频 0ms/总时长一致；
- 视频 SourceAsset 只登记外部原文件身份，生成受 640×360/约 1Mbps 配置约束的不可变 Video Proxy，且从不删除外部文件；
- 用 timestamp discontinuity 样本验证状态为 `unverified`、SRT 仍输出且界面警告存在；
- Chunk 局部时间转全局时间，以及防止重复加 offset；
- 拒绝不匹配的 Chunk ID 和 Transcript 引用；
- Current Pointer 能按 `scope_key` 独立切换一个 Chunk，且导出逐块校验；
- 单个 Transcript Chunk 改变只使对应 Alignment 与全局下游 stale；
- Glossary 改变使所有 Chunk Transcript/Alignment 与全局下游 stale；
- 标点和空格只作为 Decoration，不产生 Alignment Assignment 或 unaligned warning；
- Segmenter 普通 Cue 不超过 10 个版本化显示单位、不可拆 Glossary 保护单元超限时只产生 warning；
- Subtitle 直接组合多个 Chunk Alignment，项目中不存在 Global Alignment Artifact；
- Artifact 原子发布与 orphan 恢复；
- SQLite 回滚时保留原来的 Current Pointer；
- Ambiguous Invocation 不自动重试；
- 固定本地/云端 Profile，禁止用户选择 Model 和静默 Profile/Provider fallback；
- 云端档上传当前 Chunk 音频、Effective Glossary 词条及必要的语言/转写配置，Forced Alignment 保持本地；
- 单 Atom Glossary term 不触发 `glossary_single_atom_conflict`，至少 2 个 Atom 且恰好一个 Atom 冲突才触发；
- QA 返工生成新版本并遵守每个 Chunk/Run 最多 4 次 Semantic Attempt；稳定匹配、稳定冲突和 4 次仍不稳定分别产生 resolved、`stable_glossary_conflict` 和 `unstable_glossary_conflict`；
- Structural blocking error 触发一次对应阶段修复性重算，仍非法则禁止导出；
- QA warning 与 blocking 对导出的不同影响；
- Local Filler Review 只隐藏白名单内的 Cue 末尾 Atom；Cloud Filler Review 只能返回候选 ID 子集，失败时保留原显示并告警；
- Filler suppression 不改变 Transcript、Alignment、Subtitle Atom span 或 Cue 时间包络；
- 原子替换唯一用户可见的 `subtitles.srt`；
- 辅助资产不能绕过 Glossary 边界。

Provider 能力和字幕质量 Benchmark 属于私人审计项目，不是发行项目测试。
