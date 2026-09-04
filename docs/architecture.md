# CueFlow v0.5.2 Architecture

## 1. 版本目标

v0.5.2 只完成字幕核心的大架构迁移和阻塞性清理，不做仓库级美化。它删除会形成第二条
运行路径的 whole-file Chunk ASR、全文 `corrected_text` Correction、passthrough Correction
和默认 VocaSync Alignment；不可达的旧 helper、目录重组和命名统一留到 v0.5.3。

```text
Source Media
  → MediaProbe → TimelineAudio → TOS MediaObject → presigned HTTPS URL
      ├─ Qwen Audio 3.0 whole-file ASR → Base ASR / Frozen BaseTranscript
      └─ Doubao whole-file ASR         → Peer ASR
                 ↓
        character-level mechanical comparison (diagnostic only)

FULL Frozen BaseTranscript + FULL PeerTranscript + raw References + UserKeywords
  + mechanical differences + each model's live Internet
      ├─ Qwen Max → edits[]
      └─ Kimi K3  → edits[]
                 ↓
        exact resolver + separable lexical projection
          ├─ agreement / lexical agreement → accept
          ├─ keep / pure prosodic disagreement → Base
          ├─ invalid locator / contradictory arm overlap → human review
          └─ lexical disagreement → local GLM WAV transcription
                                      ├─ unique exact candidate → accept / keep
                                      └─ failure / ambiguity → human review
                 ↓
        all acoustic work terminal + review clear → sealed Corrected Transcript
                 ↓
        Volcengine ATA → Alignment → Subtitle → QA → SRT
```

Qwen 是唯一 Base，豆包是完整 Peer，GLM 只转写疑点音频窗。删除的是 whole-file Provider
chunking / fallback chunking，不是 GLM `EvidenceWindow`。

## 2. 核心不变量

1. TimelineAudio 是 16kHz mono PCM s16le，sample 0 对应 presentation timeline 0。
2. 原媒体在付费 ASR 前必须满足 `duration < 5h` 且 `byte_length < 512,000,000`；不提供
   Chunk fallback。
3. UserKeywords 是唯一允许进入 ASR 的 lexical/semantic prior。每路 ASR 的输入只能是
   音频、T0 时刻冻结的同一组 UserKeywords，以及必要的非语义 Provider 控制参数。
4. References、内置领域包、Reference 提取词、其他 ASR 输出、机械候选、Correction 输出、
   联网发现词和自动术语都不得进入任何 ASR。
5. UserKeywords 最多 100 个；只 trim 首尾空白、拒绝空串、exact 去重、保留首次顺序，
   不改 Unicode、大小写或标点。
6. Qwen/豆包全文结果按原 Unicode code point 机械比较，不做 canonicalization、ITN 等价或
   本地语义判断；原始 ASR diff 不生成 GLM 窗口，也不要求时间映射成功。
7. 只有独立句法标点和空白变化可标记为 `prosodic_format_only`。处于字母/数字 lexical
   token 内部或连接 lexical token 的 `. - / + # &` 等符号不得跳过 GLM。
8. 合并后的每个 GLM 窗口必须再次满足 `duration ≤ 30s`；落盘 WAV 必须再次满足
   `byte_length ≤ 25MB`，否则不合并或 fail closed。
9. Correction 两臂看到相同的静态 CueFlow 输入，但各自积极使用实时互联网；搜索路径、
   结果和 retry 结果允许不同，不建立搜索快照或可重放证据层。
10. 每个 edit 只有 `source_sentence`、`original`、`replacement`。`source_sentence` 必须是
    Frozen Base 中唯一子串，`original` 必须在该句中唯一出现；offset 只由本地 exact resolver
    计算。
11. 同一 Base 区间、相同 replacement 直接接受；同区间但仅句法标点不同，可按下节的
    lexical projection 接受共同文字修改。不同区间不提取局部共识；应用时从右向左。
12. 只有纠错后的 lexical singleton/conflict 才调用 GLM。GLM 不是最终兜底，失败或不确定
    进入对应 interval 的人工 review。全部声学工作终结、review 清零且 final sealed 后才允许 ATA。
13. Artifact 不可变且内容寻址；Registry current pointer 表示当前投影。旧 Schema 不迁移。
14. Presigned URL 只在调用时生成，不写 Artifact、Registry 或日志；MediaObject 只保存稳定
    bucket/key/hash/version 身份。

## 3. Provider 请求闭集

### Qwen Base ASR

- 模型固定为 `qwen-audio-3.0-asr-flash-filetrans`；
- `input.file_urls=[media_url]`，不发送 `input.context`；
- `parameters.channel_id=[0]`；
- UserKeywords 映射为 inline `parameters.vocabulary`，每项 weight=5；
- 不发送 `vocabulary_id`、`language_hints`；
- 显式发送
  `parameters.special_word_filter.system_reserved_filter=false`，防止服务商把命中内容替换为
  `*`。当前 HTTP JSON 路径按 object 序列化，真实请求测试负责封印接口表现。

### Doubao Peer ASR

- URL whole-file submit，`model_name=bigmodel`；
- `show_utterances=true`，`enable_ddc=false`；
- UserKeywords 只映射到 `request.corpus.context.hotwords`；
- 不发送 `context_type/context_data`、boosting/correct/regex table、POI/Music FC、
  `sensitive_words_filter` 等字段。

### GLM acoustic evidence

- 模型固定为 `glm-asr-2512`；
- 只以 multipart `file` 上传本地窗口 WAV，不使用 URL 或 `file_base64`；
- 发送与另两路相同的 UserKeywords 作为 `hotwords`；不发送自然语言 `prompt`；
- 每个最终窗口 `≤30s` 且 `≤25MB`。

### Correction

- Qwen 固定 `qwen3.8-max-2026-09-02`，不静默改用浮动别名；
- Qwen Max 强制搜索并使用最大搜索强度；Kimi K3 开启自动搜索；
- 两臂分别提交严格 `{"edits":[...]}`，禁止全文 `corrected_text`；
- Prompt 以人类可读 `prompt_version` 和 SHA-256 记录；
- 不保存 search query/result snapshot/citation。Provider 天然返回的 response ID 或引用可以
  顺手记录，但不是正确性契约。

### ATA

- 只使用 URL 音频；submit/query 固定为 `/api/v1/vc/ata/submit` 与
  `/api/v1/vc/ata/query`；
- query：`appid`、`caption_type=speech`、`sta_punc_mode=3`；
- payload：`url`、`audio_text`；
- 不发送已废弃的 `caption_category`、`cluster` 或空的过滤字段；
- 不在本地硬编码未由当前文档给出的 200MB 上限。

## 4. Correction 输入边界

每个模型一次读取完整 Frozen BaseTranscript、完整 PeerTranscript、原始 References、
UserKeywords 和机械疑点。`CorrectionRequest` 不含 GLM evidence；Peer 只是有噪声的第二份
证据，所有三字段 edit 都必须锚定 Base，而不是 Peer。缺少 References 也执行双 Correction。

生效 Prompt 为 `transcript-recovery-edits-zh-v2`，保留完整恢复口播规则和三字段输出契约，
补充全文 Peer 的角色。禁止全文 `corrected_text` 输出，也不把纠错 Prompt 发送给 ASR。
两个模型允许提出必要的局部标点修正；完全一致的标点修改可以应用，标点分歧不参与声学裁决。

`correct` 可以替换 References，但不能改变 ASR 时冻结的 UserKeywords；要改变关键词必须新建
`run`，使 Qwen、豆包和 GLM 都接收同一集合。

## 5. 产物链与导出 Gate

```text
SourceAsset → MediaProbe → TimelineAudio → MediaObject
                                      ├→ BaseAsr
                                      └→ PeerAsr
BaseAsr + PeerAsr → AsrComparison (diagnostic)
both full texts + references + keywords → QwenEditProposal + KimiEditProposal
proposals → AgreementResolution → AcousticWindowPlan
lexical disputes → AcousticWindow[n] → GlmAdjudicationEvidence[n] → AcousticResolution[n]
agreements + acoustic outcomes → EditResolution + ReviewQueue
human decisions if needed → ReviewResolution → sealed EditResolution
sealed EditResolution → Transcript → Alignment → Subtitle → QA → SrtRender
```

SRT Export 要求 Transcript、Alignment、Subtitle、QA 全部 current 且非 stale，身份链一致，
QA 不为 blocked；Transcript 必须精确绑定已封存的最终 resolution。

## 6. Lexical projection：粗 diff 只是起点

`separable-runs-v1` 在每个 edit 内计算字符 diff；不把 opcode 整块二选一。每个非 equal
块进一步划分为可判定的 lexical 与 prosodic runs，使用完整 Base/proposal 邻接字符判断
token 内部符号（包括 `.NET` 的前导点）。两边只按相同类别的有序 runs 配对；仅允许边界
prosody 与空边界配对。
内部结构无法可靠配对就返回 `projection_unresolved`，进入 lexical disagreement → GLM。

每个可分离变化独立判定：lexical 使用 proposal，prosodic 使用 Base。白名单仅为
`，,。.！!？?；;：:` 和空白，且连接非汉字的字母/数字/组合字符时仍属 lexical；其他符号
保守保留为 lexical。不 strip 标点、不统一大小写/Unicode、不做语义等价。

| Base | Qwen | Kimi | 结果 |
| --- | --- | --- | --- |
| 英为达， | 英伟达， | 英伟达。 | 英伟达， |
| 英为达 | 英伟达， | 英伟达。 | 英伟达 |
| 今天很好， | 今天很好。 | 今天很好！ | 保持 Base，不调用 GLM |
| H264 | H.264 | keep | lexical disagreement → GLM |
| Black well | Blackwell, | Blackwell. | Blackwell |

同 span 指同一个冻结 Base artifact 上的同一 `[start,end)`，不是两个 source_sentence
字面相等。两臂 span 不同，即使共享部分文字，也不得拆出“共同答案”；只为复听建立重叠
区间并集和各臂原候选。单臂自相矛盾的重叠 edit 则直接进入 contract review。

`lexical_agreement_ignore_prosody` 保存原 span、两臂原 edit、两个 projection、被忽略的
prosodic changes 与 policy。Schema 会重算支持关系，禁止本地创造新的 lexical 内容。

## 7. 后置 GLM 和人工兜底

Planner 只消费 `AgreementResolution.lexical_disagreements`，包含 lexical singleton、
replacement conflict、有效的不同 span 及不可分离的 mixed diff。依据冻结 Qwen timed units
顺序 exact 映射；失败或核心区间超长只生成该项 review，不反查全文猜位置。

默认前后各 3s padding，间隔 ≤2s 可合并，但 union 必须 ≤30s；必要时缩减 padding，
不拆分超过上限的核心争议。窗口落盘后再验证 ≤25MB。GLM 只看窗口音频和冻结的用户关键词。

`ascii-case-insensitive-v1` 只折叠 ASCII A–Z 大小写，匹配 Base/Qwen/Kimi 的原始局部候选，
加上 Base 两侧至多各 32 字符的 exact context。只有唯一候选在 GLM 文本中唯一命中才自动
选取；不去标点/空白、不做拼音、编辑距离、语义评分，也不调用第四个 LLM。上下文超出窗口、
第三路标点不同、同音异写或新答案都会保守转 review，不能误报为已消解。

单窗 timeout、5xx、无效返回、证据不足不会终止其他窗；存储损坏/输入 hash 不匹配等
完整性错误仍然硬失败。人工可 keep、选某臂，或提交一个重新 exact 定位的三字段 edit。

## 8. 生命周期与版本

Artifact Schema 7.0.0 / Registry 9：GLM 改为后置裁判，不能把旧 6.0.0 证据重新解释成新
语义；旧库只拒绝，不迁移或改写。新 `correct` 复用匹配的 Base/Peer，但重新调用两臂。
同 run 使用持久 checkpoint 恢复；成功 paid result、invocation 状态和 checkpoint 原子发布。
最终 resolution 和 review queue 同事务发布，单项目写锁防止多个命令同时写入。
详细 resume/retry 和 review 乐观并发契约见 Failure Model 与 Schema Contracts。
