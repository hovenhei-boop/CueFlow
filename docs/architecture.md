# CueFlow v0.5.1 Architecture

## 1. 产品边界

CueFlow 处理已剪辑完成的视频或音频，Source 主链为：

```text
Source Media
→ Media Probe / Presentation Timeline Analysis
→ Timeline Normalization
→ Timeline Audio
→ Chunk Plan / Audio Chunks
→ Semantic Transcription
→ Forced Alignment
→ Subtitle Segmentation
→ QA
→ output/subtitles.srt
```

目标是忠实转写、精确时间轴、纠错和导出。独立的 Reference 路径为 `Reference Material → deterministic extraction / optional Reference ASR, Vision, Cloud Document Parse → Reference Evidence → automatic terminology discovery → Suggested Terms → human review → Project Lexicon`。该旁路不创建或修改 Source Transcript、Effective Glossary、Alignment、Subtitle、QA 或 SRT；v0.5.1 不增加 UI。

## 2. 核心不变量

1. SourceAsset 身份只由 `Path.name` 精确字符串确定；外部内容可在原 locator 覆盖，locator 不参与身份，不提供 Source relink，项目内 Artifact 仍然内容寻址且不可变。
2. Media Prep、Timeline normalization 和 Chunking 在本地确定性执行；语义转写由远端 Provider 完成，Forced Alignment 使用本地模型。
3. Timeline Audio 固定为 16kHz、mono、PCM s16le，且其 sample 0 对应源媒体 presentation timeline 0。
4. Timeline Audio 是 Chunker、Semantic Transcriber 和 Forced Aligner 的唯一音频权威。
5. Transcript 完整保留 Provider `source_text`、全部可发音 Atom 和 Decoration；不得摘要、改写或按字幕风格删词。
6. Glossary 只能提示核对，不能覆盖 Transcript。
7. Alignment 只绑定一个精确 MediaChunk 和一个最终 accepted Transcript；不存在 Global Alignment。
8. Subtitle 只切分和渲染 Decoration；SRT 忠实显示全部 Transcript Atom。
9. Structural blocking error 修复失败时禁止导出；warning 不阻塞。
10. Artifact 不可变；当前状态由 Registry 中的 exact dependency 与 current/stale pointer 决定。
11. `cueflow run` 总是新建 Run 并执行新的处理链；不同 Run 之间不自动沿用处理结果。
12. `cueflow retry` 只在原 Run 内按失败 Invocation 的精确输入做最小重放。

## 3. Media Prep

### 3.1 Source Media

`project` 登记文件名、格式、媒体类型和外部绝对 locator，不读取 Source 内容来建立身份，也不保存或比较 Source hash/长度。原 locator 上的同名文件可直接覆盖并用于新 Run。路径缺失、不是普通文件或不可读取时抛 `SourceMissingError`；系统不搜索其他同名文件，也不提供 relink。

### 3.2 Presentation timeline analysis

`media` 直接读取原始媒体，不通过转码结果判断 presentation timing。

Opening analysis 最多读取开头 50 秒的 frame/packet evidence，并在证据充分后停止。它寻找首个有效 audio presentation sample，而不是首次非静音或首次人声。整数 PTS、time base numerator/denominator、skip-sample 等原始证据保存在 MediaProbe 中。

全文件 packet continuity check 与 opening analysis 独立执行。它使用流式、O(1) 内存扫描检测 gap、backward jump 和 discontinuity，只保存上一 packet end 与异常摘要。无法确定性解释的异常使 `timeline_status = unverified`，不猜测补偿。

### 3.3 Timeline correction actions

所有 offset 决策使用整数与有理数。精确 offset 只在 16kHz 输出边界量化一次，量化结果是带符号 integer sample count：绝对值恰好半个采样时向远离零方向取整。

MediaProbe 必须记录一个 origin action：

- `timeline_origin_unchanged`：可靠 offset 为 0，记录性 no-op；
- `pad_silence_before`：可靠正 offset，携带正 `sample_count`；
- `trim_before_timeline`：可靠负 offset，携带正 `sample_count`；
- `timeline_origin_unverified`：无法可靠确定，记录 intentional no-op 并保持 unverified。

还必须记录 `fit_presentation_duration`，携带 Timeline Audio 的 `total_sample_count`。Render 先归一化到 16kHz，再严格按 action 映射为 filter；不得重新从 stream metadata 推导 offset。未知 action fail-closed。

### 3.4 Chunking

ChunkPlan 使用版本化 Chunker Config。默认目标为 180 秒、硬上限 225 秒，优先选择目标附近持续至少 500ms 且低于 -40dB 的静音中点，否则在实际 config 的硬上限切分。Chunk 连续覆盖完整 Timeline Audio，无重叠、无间隙。

## 4. Semantic Transcription

Semantic Provider 使用 `qwen3.5-omni-plus-2026-03-15` 的 OpenAI-compatible API，凭据和地域 endpoint 从环境变量读取。请求使用 `response_format={"type": "json_object"}` 约束返回合法 JSON；`parse_cloud_semantic_response` 继续严格校验字段集合、正文与 Alignment language，非法契约仍明确失败。Transcription Stage 创建一个 Semantic Provider，顺序处理所有需要工作的 Chunk 及其全部 Attempt，最后关闭一次。阶段无工作时不得创建 Provider。

一个新 Run 中，每 Chunk 初始最多 4 个 Semantic Attempt；该上限属于版本化 QA Ruleset 的运行规则。每个成功 Attempt 创建 Transcript Artifact、Invocation 和 exact input bindings。每产生一个返工 Attempt，都对该 Attempt 的完整 Chunk 文本重新执行 conflict scan，下一轮只以当前实际 conflict 驱动局部返工。Glossary 稳定闭环只使用冻结规则：

- 单 Atom Glossary term 不触发冲突；
- term 至少 2 个 Atom；
- 候选与 term 的 Atom 数量和 class 序列完全相同；
- NFC/casefold 后恰好一个 Atom 不同才触发；
- 不使用编辑距离、拼音/音素、NER 或 confidence threshold。

连续两次候选序列相同且与 term 一致时 resolved；连续两次相同但当前仍冲突时接受实际转写并产生非阻塞 `stable_glossary_conflict`；当前 conflict 未稳定且尚有预算时继续返工该 Chunk，4 次内无法连续稳定时产生非阻塞 `unstable_glossary_conflict`。QA 只报告，不修改文字，也不额外扫描整篇 Transcript。

只有显式 targeted retry 可以为目标 Chunk 重置 4-Attempt budget；同一原 Run、同一 Chunk 固定最多重置 2 次，因此绝对上限为 12 次。两次 reset 是 SQLite window 数据模型约束，不是运行时可配置项。进入 `sending` 的 Attempt 计入预算；`definitely_not_sent` 不计。reset count 持久化并可审计。

## 5. Forced Alignment

Transcription Stage 完成后关闭 Semantic Provider。Alignment Stage 只读取每个 Chunk 的 accepted Transcript；rejected Attempt 不创建 Alignment。

Alignment 使用 pinned revision 的本地 `Qwen3-ForcedAligner-0.6B`，provider identity 为 `qwen-local`。设备、dtype 和模型缓存由运行时配置决定；资源不足时明确失败，不量化、不换模型。Alignment Stage 创建一个本地 Aligner，批量处理全部需要工作的 Chunk，最后关闭一次。阶段无工作时不创建 Aligner。每个 accepted Transcript 首次 Alignment 产生结构非法结果时，最多执行 1 次 execution structural repair；仍失败则本阶段失败。

## 6. Subtitle 与 QA

Segmenter 将全部 current Transcript/Alignment 按全局时间顺序组合。每个 Cue 默认最多 10 个显示单位；CJK 每字一个单位，完整 word、number 和 pronounceable-symbol Atom 不从内部拆开。优先语义完整边界；保护单元超过 10 时保留完整单元并产生 warning。标点样式只影响 Subtitle/SRT，不修改 Transcript。

QA 检查时间戳、Cue 重叠/越界、未对齐 Atom、Chunk/Transcript/Alignment 引用以及 Artifact dependency identity。QA 不改文字。

若 QA 发现 alignment-related blocking issues，Orchestrator 最多执行一个独立 QA Alignment Repair Wave：一次加载 Aligner，批量处理本轮 workset，更新 Alignment，重建 Subtitle，重跑 QA，再进入 Export。该预算与 Alignment execution repair 独立。第二轮仍有 structural blocking error 时 Run 失败。

## 7. Artifact 与依赖图

```text
SourceAsset → MediaProbe → TimelineAudio → ChunkPlan → MediaChunk[n]
EffectiveGlossary + MediaChunk[n] → Transcript Attempt[n]
accepted Transcript[n] + MediaChunk[n] → Alignment[n]
all accepted Transcript/Alignment + EffectiveGlossary → Subtitle
ChunkPlan + Subtitle + all accepted Transcript/Alignment → QA
passing QA + Subtitle → SrtRender → output/subtitles.srt
```

Artifact publish 顺序为：写临时文件、flush/fsync、原子替换到内容路径、read-back validation、SQLite 事务登记 Artifact/dependency、切换 current/stale pointer、commit。

## 8. Run、Invocation 与 Retry

Invocation 保存 operation、provider/model、attempt number、状态、输出 Artifact，以及按顺序绑定的上游 Artifact IDs。失败或 delivery ambiguous 的 Invocation 不被自动重放。

只有真正开始 Source `run/retry`、Reference `extract/retry` 或内部 Lexicon 执行/retry 时才执行对应类别的单 Orchestrator crash recovery：遗留 `created` Invocation 收口为 `definitely_not_sent`，遗留 `sending` 收口为 `delivery_ambiguous`，对应 `running` Run 收口为 `interrupted`。三类入口互不恢复其他 Run kind。本次 Ctrl+C 同样使 Run 进入 `interrupted`；其他未预期异常使 Run 进入 `failed`。Run 与 in-flight Invocation 在一个 SQLite 事务中收口。打开项目、`status`、glossary、asset、Reference add/relocate/status 和词库人工管理不触发恢复。

显式 retry 可以把同一个 `failed` 或 `interrupted` Run 重新置为 `running`。Retry 从绑定输入读取项目内 Artifact，不重新访问源媒体，不依据当前 pointer 猜测输入，不重跑其他已成功 Chunk；目标完成后只重建真正必要的下游。

Run 状态转换：

```text
created → running → succeeded
                  → failed
                  → interrupted
failed/interrupted --explicit targeted retry→ running
```

## 9. CLI 与输出

CLI 提供 `init`、`glossary set`、`asset add`、`run`、`status`、`retry`、Reference 管理以及 Suggested Terms、Project Lexicon、Project Blacklist 和 Official Pack 管理。不提供用户主动创建或 rebuild Lexicon Run 的命令，也不提供 Project→Pack select。失败 JSON 在可用时包含 Run/Invocation/work-item identity、当前状态和合法下一步；它只描述显式操作，不自动 retry。

唯一正常用户输出是 `output/subtitles.srt`。内部 Artifact、SQLite、blob 和临时文件位于 `.cueflow/`；临时文件完成后清理。

## 10. Reference 旁路架构

ReferenceAsset 与 SourceAsset 是两个独立领域对象；二者都使用 filename 精确 identity，但 Reference 不挂为 auxiliary Source。顶层仍是 Project → Run；Reference Run 与 Source Run 都是 Project 任务实例，`reference_runs` 只是 runs 扩展，不形成 ReferenceAsset → Run 层级。

每次 `reference extract` 新建 Run，先确定性识别当前文件，再生成 ordered work items。确定性 TXT/MD、cue、OOXML 和完整 text-layer PDF 分支不创建 Invocation。真实 Reference ASR、Vision 或 Cloud document work item 才创建 Invocation；各 evidence role 独立，bundle 只收集引用，不融合。Retry 在原 Run 内只追加失败 work item 的 attempt，成功项及其 Evidence 保持不变。

Reference Artifact 图：

```text
ReferenceAsset → ReferenceInput → ReferenceEvidence[role]
all succeeded ReferenceEvidence in one Run → ReferenceBundle
```

显式 `run` 上传 Source 音频 Chunk 到语义 Provider。显式 `reference extract` 按路由上传文档、位图 cue、full-frame window、独立图片或 PCM/WAV 音频段。full-frame 只保留 manifest/hash/timestamp/执行参数，图像请求正文不持久化；Cloud document file_id 在 finally 删除。完整 Reference 格式与路由见 [Reference Extraction](reference-extraction.md)。

## 11. Lexicon 构建旁路

Reference bundle 发布后自动触发内部 `kind=lexicon` Run。系统只处理尚无 coverage 记录的 exact Evidence Artifact ID；Reference retry 的旧 Evidence 不重跑，新 Evidence 独立进入有界 batch。一个 Run 只发布一个 `lexicon_input` manifest，每个 batch 可发布一个 `term_candidate_set`。Candidate 必须逐字引用所发送 Evidence unit 的 field path 和半开 offset；客户端重新绑定完整 Evidence 并校验，非法位置使该 work item 失败。

Suggested Term 经过 Accept、Edit & Accept、Dismiss 或 Temporary/Permanent Block 后更新项目状态。Project Lexicon 的 Add、Edit、Disable、Remove 与原子 Block 每次发布不可变 `project_lexicon` revision。Dismiss/Remove 进入逻辑 Neutral；Block 建立仅作用于当前 Project 的规则。Temporary 在到期时解除，Permanent 只由用户 Unblock；两者都只阻止同一精确词面再次作为 Reference suggestion 出现，不禁止 Source/SRT 输出该词。同一 exact term 不能同时存在于 Active Project Lexicon 与有效 Project Blacklist。

Official Packs 位于应用级数据目录，由全部项目共享。用户在显式 setup/install 时按领域选择，setup 未指定领域时默认全选；不存在项目分类或项目绑定。Pack version 不可变，安装校验 catalog manifest hash、Pack schema、terms hash/count 与 license，并使用目录锁、临时目录、原子 rename 和 current pointer。详见 [Lexicon](lexicon.md)。
