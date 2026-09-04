# CueFlow v0.5.2 Domain Model

## Project、SourceAsset 与 MediaObject

Project 保存单项目 Registry。SourceAsset 是用户媒体的外部 locator；TimelineAudio 是本地
确定性 16kHz mono 工作音频。MediaObject 是上传到 TOS 的内容寻址对象，只保存 provider、
bucket、object key、content hash、byte length 和可选 version ID。Presigned GET URL 是临时
调用数据，不是领域对象，也不得持久化。

## JobInput

每个 `run` 或 `correct` 都创建不可变 `JobInput`：

```yaml
source_asset_id: src_...
references:
  - ordinal: 0
    kind: pdf_url
    url: https://...
    locator_semantics: mutable_remote_locator
  - ordinal: 1
    kind: text
    format: md
    display_name: notes.md
    text: "...frozen content..."
user_keywords: [Qwen3.8, C++]
```

本地文本正文是快照；PDF/Image URL 只冻结 locator 字符串。Targeted retry 复用原
Invocation inputs，无法保证 mutable Reference URL 背后仍是同一内容。

`user_keywords` 是唯一 ASR lexical/semantic prior。没有自动领域词库、术语提取、自然语言
ASR prompt 或 ASR 结果回灌。

## ASR 与证据

- `BaseAsr`：Qwen 全文件转写，文本即 Frozen BaseTranscript，并带 sentence timestamps；
- `PeerAsr`：豆包全文件转写，带 utterance timestamps；
- `AsrComparison`：两份全文按 Unicode code point 的机械差异；
- `AgreementResolution`：完整两臂 proposals 的 exact/projection 共识和剩余分歧；
- `AcousticWindowPlan`：只为纠错后的 lexical 分歧生成窗口或局部不可映射结果；
- `AcousticWindow`：由上述分歧映射出的全局毫秒音频窗；
- `GlmAdjudicationEvidence`：GLM 对该窗口的独立 multipart 音频转写；
- `AcousticResolution`：对该分歧的确定性候选选择或人工 review 结果。

`AcousticWindow`/`GlmAdjudicationEvidence` 使用 `window_id` scope。它们不是旧 whole-file ASR Chunk，
也不形成可拼接的第三份全文。

## Edit、Review 与 Transcript

Qwen 与 Kimi 各自产生一个 arm-specific edit proposal。每项 edit 的外部契约仅包含：

```json
{"source_sentence":"...", "original":"...", "replacement":"..."}
```

Resolver 在 Frozen Base 上确定性计算区间。两个模型同 span 的 lexical projection 相同，
即支持共同文字修改 + Base 格式；不同 span 不允许局部拼接共识。纯标点分歧被忽略。
`EditResolution` 汇总初次 agreement 和后置 GLM 结果；仅剩 unresolved 项进入 `ReviewQueue`。
稳定 review ID 与 exact queue artifact ID 避免过期决策。`ReviewResolution` 留存人工选择，
包括 keep。声学工作全部终结且 review 清零后封存 final，才创建 Transcript。Transcript 精确
绑定 Base 和 final，不允许全文模型输出或中间 preview 绕过 resolver。

## Alignment 与字幕

ATA Alignment 绑定精确 MediaObject 和 Transcript，使用全局整数毫秒。每个 Transcript Atom
必须恰好有一个 assignment，顺序一致、区间不重叠且不超过音频 duration。Subtitle 只能
切分/渲染 Atom，不改 Transcript；QA 检查身份、覆盖与时间结构，SrtRender 只消费通过 gate
的 current 对象。

## Run 与 Invocation

Run 的 `operation_kind` 是 `run` 或 `correct`，状态包括 `needs_review`。Invocation operation
闭集为：

```text
media_upload qwen_asr doubao_asr glm_asr
qwen_correction kimi_correction ata
```

Invocation 保存 requested/resolved model、local idempotency key、远端 job/status、可选 response
ID、elapsed/reasoning time、usage、prompt version/hash、结果 Artifact 和 retry ancestry。ordered
`invocation_inputs` 是 targeted retry 的输入事实；retry 不从最新 pointer 猜测原调用内容。
`run_checkpoints` 是同 run 的 stage/scope 恢复边界；新 `correct` 不复用旧 Correction 决定。
