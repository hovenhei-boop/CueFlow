# CueFlow v0.1.1 Architecture

状态：冻结基线

## 1. 产品边界

CueFlow v0.1.1 处理已经剪辑完成的视频或音频，唯一主链为：

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

目标是忠实转写、精确时间轴、纠错和导出。v0.1.1 不主动编辑或净化成片内容，不删除实际说出的 Atom，不做视频编辑、视觉理解或额外用户界面。

## 2. 核心不变量

1. SourceAsset 身份只由 `Path.name` 精确字符串确定；外部内容可在原 locator 覆盖，locator 不参与身份但 v0.1.1 不提供 relink，项目内 Artifact 仍然内容寻址且不可变。
2. Media Prep 对 LOCAL/CLOUD 使用同一个本地确定性实现；Profile 差异只从 Semantic Provider 开始。
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

## 4. Processing Profiles

| Profile | Semantic Transcriber | Forced Aligner |
|---|---|---|
| `LOCAL_PROFILE` | 本地 `Qwen3-ASR-1.7B` pinned revision | 本地 `Qwen3-ForcedAligner-0.6B` pinned revision |
| `CLOUD_PROFILE` | `qwen3.5-omni-plus-2026-03-15`，OpenAI-compatible API | 同一个本地 `Qwen3-ForcedAligner-0.6B` pinned revision |

凭据和地域 endpoint 只从环境变量读取。设备、dtype 和模型缓存位置由运行时检测与配置决定；资源不足时明确失败，不量化、不换模型、不切 Profile。

## 5. Semantic Transcription

Transcription Stage 创建一个 Semantic Provider，顺序处理所有需要工作的 Chunk 及其全部 Attempt，最后关闭一次。阶段无工作时不得创建 Provider。

一个新 Run 中，每 Chunk 初始最多 4 个 Semantic Attempt；该上限属于版本化 QA Ruleset 的运行规则。每个成功 Attempt 创建 Transcript Artifact、Invocation 和 exact input bindings。每产生一个返工 Attempt，都对该 Attempt 的完整 Chunk 文本重新执行 conflict scan，下一轮只以当前实际 conflict 驱动局部返工。Glossary 稳定闭环只使用冻结规则：

- 单 Atom Glossary term 不触发冲突；
- term 至少 2 个 Atom；
- 候选与 term 的 Atom 数量和 class 序列完全相同；
- NFC/casefold 后恰好一个 Atom 不同才触发；
- 不使用编辑距离、拼音/音素、NER 或 confidence threshold。

连续两次候选序列相同且与 term 一致时 resolved；连续两次相同但当前仍冲突时接受实际转写并产生非阻塞 `stable_glossary_conflict`；当前 conflict 未稳定且尚有预算时继续返工该 Chunk，4 次内无法连续稳定时产生非阻塞 `unstable_glossary_conflict`。QA 只报告，不修改文字，也不额外扫描整篇 Transcript。

只有显式 targeted retry 可以为目标 Chunk 重置 4-Attempt budget；同一原 Run、同一 Chunk 固定最多重置 2 次，因此绝对上限为 12 次。两次 reset 是 v0.1.1 SQLite window 数据模型约束，不是运行时可配置项。进入 `sending` 的 Attempt 计入预算；`definitely_not_sent` 不计。reset count 持久化并可审计。

## 6. Forced Alignment

Transcription Stage 完成后关闭 Semantic Provider。Alignment Stage 只读取每个 Chunk 的 accepted Transcript；rejected Attempt 不创建 Alignment。

Alignment Stage 创建一个本地 Aligner，批量处理全部需要工作的 Chunk，最后关闭一次。阶段无工作时不创建 Aligner。每个 accepted Transcript 首次 Alignment 产生结构非法结果时，最多执行 1 次 execution structural repair；仍失败则本阶段失败。

## 7. Subtitle 与 QA

Segmenter 将全部 current Transcript/Alignment 按全局时间顺序组合。每个 Cue 默认最多 10 个显示单位；CJK 每字一个单位，完整 word、number 和 pronounceable-symbol Atom 不从内部拆开。优先语义完整边界；保护单元超过 10 时保留完整单元并产生 warning。标点样式只影响 Subtitle/SRT，不修改 Transcript。

QA 检查时间戳、Cue 重叠/越界、未对齐 Atom、Chunk/Transcript/Alignment 引用以及 Artifact dependency identity。QA 不改文字。

若 QA 发现 alignment-related blocking issues，Orchestrator 最多执行一个独立 QA Alignment Repair Wave：一次加载 Aligner，批量处理本轮 workset，更新 Alignment，重建 Subtitle，重跑 QA，再进入 Export。该预算与 Alignment execution repair 独立。第二轮仍有 structural blocking error 时 Run 失败。

## 8. Artifact 与依赖图

```text
SourceAsset → MediaProbe → TimelineAudio → ChunkPlan → MediaChunk[n]
EffectiveGlossary + MediaChunk[n] → Transcript Attempt[n]
accepted Transcript[n] + MediaChunk[n] → Alignment[n]
all accepted Transcript/Alignment + EffectiveGlossary → Subtitle
ChunkPlan + Subtitle + all accepted Transcript/Alignment → QA
passing QA + Subtitle → SrtRender → output/subtitles.srt
```

Artifact publish 顺序为：写临时文件、flush/fsync、原子替换到内容路径、read-back validation、SQLite 事务登记 Artifact/dependency、切换 current/stale pointer、commit。

## 9. Run、Invocation 与 Retry

Invocation 保存 operation、provider/model、attempt number、状态、输出 Artifact，以及按顺序绑定的上游 Artifact IDs。失败或 delivery ambiguous 的 Invocation 不被自动重放。

只有真正开始 `run` 或 `retry` 时才执行单 Orchestrator crash recovery：遗留 `created` Invocation 收口为 `definitely_not_sent`，遗留 `sending` 收口为 `delivery_ambiguous`，对应 `running` Run 收口为 `interrupted`。本次 Ctrl+C 同样使 Run 进入 `interrupted`；其他未预期异常使 Run 进入 `failed`。Run 与 in-flight Invocation 在一个 SQLite 事务中收口。打开项目、`status`、glossary 和 asset 管理不会触发恢复。

显式 retry 可以把同一个 `failed` 或 `interrupted` Run 重新置为 `running`。Retry 从绑定输入读取项目内 Artifact，不重新访问源媒体，不依据当前 pointer 猜测输入，不重跑其他已成功 Chunk；目标完成后只重建真正必要的下游。

Run 状态转换：

```text
created → running → succeeded
                  → failed
                  → interrupted
failed/interrupted --explicit targeted retry→ running
```

## 10. CLI 与输出

v0.1.1 唯一界面是现有 CLI：`init`、`glossary set`、`asset add`、`run`、`status`、`retry`。不增加模型、Provider、Chunk 或字幕风格选择。失败 JSON 在可用时包含 Run/Invocation identity、Invocation 当前状态和合法下一步；它只描述显式操作，不自动 retry。

唯一正常用户输出是 `output/subtitles.srt`。内部 Artifact、SQLite、blob 和临时文件位于 `.cueflow/`；临时文件完成后清理。
