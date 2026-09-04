# CueFlow

CueFlow v0.5.2 是面向已剪辑媒体的字幕核心。它以 Qwen 全文件转写为冻结的
`BaseTranscript`，与豆包完整 Peer 一起提供给 Qwen Max 和 Kimi K3。两个纠错模型结合原始
辅助材料、用户关键词和实时互联网分别提交 `edits[]`。同区间的完全共识或共同 lexical
projection 自动应用，纯标点分歧保持 Base 格式。只有剩余 lexical 分歧才调用 GLM 局部复听；
唯一匹配既有候选则自动消解，否则交给人工 review。封存后的最终文本由火山 ATA 保留标点
打轴，再确定性生成字幕、QA 和 SRT。

```text
Media URL + UserKeywords
  ├─ Qwen whole-file ASR ───────────────→ Frozen BaseTranscript
  └─ Doubao whole-file ASR ─────────────→ FULL PeerTranscript

FULL Base + FULL Peer + References + UserKeywords + diagnostic differences + live Internet
  ├─ Qwen Max → edits[]
  └─ Kimi K3  → edits[]
                    ↓
       exact resolver + lexical projection
         ├─ agreement → patch; keep / pure prosody disagreement → Base
         ├─ invalid / contract issue → review
         └─ lexical disagreement → GLM window → unique candidate / review
                    ↓
       acoustic complete + review clear + sealed final
                    ↓
       ATA URL alignment → Subtitle → QA → SRT
```

whole-file Base/Peer ASR 不再切 Chunk；GLM 的 `AcousticWindow` 是独立概念，仍受每窗
`≤30s`、`≤25MB` 的硬限制。

## 安装

需要 Python 3.10+、`ffmpeg` 和 `ffprobe`：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[cloud,dev]"
```

运行时凭据：

- Qwen ASR / Qwen Max：`DASHSCOPE_API_KEY`，Correction 另需 `DASHSCOPE_BASE_URL`；
- 豆包 ASR：`DOUBAO_API_KEY`，或 `DOUBAO_APP_KEY` + `DOUBAO_ACCESS_KEY`；
- GLM ASR：`ZHIPU_API_KEY`；
- Kimi K3：`MOONSHOT_API_KEY` + `MOONSHOT_BASE_URL`；
- 火山 ATA：`VOLCENGINE_ATA_APPID` + `VOLCENGINE_ATA_ACCESS_TOKEN`；
- 火山 TOS：`TOS_ENDPOINT`、`TOS_REGION`、`TOS_BUCKET`、`TOS_ACCESS_KEY`、
  `TOS_SECRET_KEY`。

`CUEFLOW_FFMPEG` 与 `CUEFLOW_FFPROBE` 可以覆盖可执行文件路径。客户端不安装 PyTorch、
CUDA、本地 ASR 或本地 Forced Aligner。

## CLI

```powershell
cueflow init PROJECT --name NAME

cueflow run PROJECT MEDIA `
  --pdf-url https://example.com/report.pdf `
  --image-url https://example.com/slide.png `
  --text-file notes.md `
  --keyword "Qwen3.8" `
  --keyword "C++"

cueflow status PROJECT
cueflow resume PROJECT RUN_ID
cueflow retry PROJECT INVOCATION_ID
```

若结果为 `needs_review`，创建一个 UTF-8 JSON 文件，并一次覆盖队列中的所有项目：

```json
{
  "run_id": "run_...",
  "expected_review_queue_artifact_id": "art_...",
  "decisions": [
    {"review_id": "dis_...a", "action": "keep"},
    {"review_id": "dis_...b", "action": "qwen"},
    {"review_id": "rev_...c", "action": "replace", "edit": {
      "source_sentence": "Our partner is Grok.",
      "original": "Grok", "replacement": "Groq"
    }}
  ]
}
```

然后执行 `cueflow review PROJECT decisions.json`。`action` 可为 `keep`、`qwen`、`kimi` 或
`replace`；ID 必须来自该 run 当前的真实队列，不能使用下标或本示例占位值。定位/contract
问题可 keep 或提供重新 exact 定位的 edit，不强行选择不可定位的模型输出。显式 keep 也会
持久化；过期队列拒绝提交。review 未清零前不会调用 ATA。

`resume` 继续指定 run 从未提交的步骤，复用已完成 checkpoint，不重发失败或交付不明的
付费请求。`retry` 仅针对指定失败 invocation，可能重复计费，必须由用户明确执行；已成功
的纠错臂/GLM 窗口不重跑。GLM 单窗失败不阻塞其他窗口，相关 interval 转人工 review。

`cueflow correct` 可以在不重跑 ASR 的情况下替换整组 References，但必须传入与原
`run` 完全相同、同序的 UserKeywords。新 `correct` 会重新调用两个 Correction 模型，而不是
复用旧 proposals。若要改变关键词，必须重新 `run`，以保证三路 ASR
收到同一组先验。

关键词最多 100 个，只执行首尾空白裁剪、空串拒绝和 exact 去重，并保持首次出现顺序、
Unicode、大小写及标点。`.NET`、`C++`、`GPT-5.6` 等不会被词法归一化。没有用户关键词时，
ASR 不接收任何领域 lexical prior；References 只进入 Correction。

PDF/Image URL 必须由 CLI 显式声明类型。CueFlow 不下载 URL 猜 MIME；本地文本在命令开始
时以 UTF-8 读取并把正文冻结进 `JobInput`。v0.5.2 不接受本地 PDF/图片，也不转换 Office
文件。

当前 Artifact Schema 为 **7.0.0**，Registry 为 **9**。不迁移或重写旧项目，也不把 6.0.0 的
前置 GLM artifact 当作后置裁判证据；请创建新项目。正常输出是
`PROJECT/output/subtitles.srt`，内容寻址 Artifact、blob 和 SQLite 状态位于
`PROJECT/.cueflow/`。

详细契约见 [Architecture](docs/architecture.md)、
[Reference Inputs](docs/reference-inputs.md)、[Schema Contracts](docs/schema-contracts.md)、
[Failure Model](docs/failure-model.md) 与 [Roadmap](docs/roadmap.md)。
