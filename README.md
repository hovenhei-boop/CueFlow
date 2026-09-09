# CueFlow

CueFlow v0.5.3 从双路 ASR 恢复逐字稿，再由火山 ATA 对齐并输出 SRT。
Qwen ASR 是冻结 Base，豆包 ASR 是独立 Peer。千问与 Kimi 分别返回完整纠错文稿；
本地接受一致修改和单路修改，仅将两路修改不同的区间交给 GLM 在已有候选中选择。
GLM 使用单独提示词，允许按需联网，不能生成第三种文字。

```text
Source Media → MediaProbe → Frozen TimelineAudio → one TOS MediaObject
  ├─ Qwen ASR → Frozen Base
  └─ Doubao ASR → Peer
FULL Base + FULL Peer + References + UserKeywords + mechanical differences
  ├─ Qwen correction → corrected_text ┐
  └─ Kimi correction → corrected_text ┘ parallel requests; one database writer
                 ↓
           exact local merge
  ├─ same change / singleton → accept
  ├─ both keep → frozen Base
  └─ different changes → GLM candidate selection → existing text / KEEP
                 ↓
        sealed final, review cleared
                 ↓
          ATA (same MediaObject) → full raw response → utterances + diagnostics → SRT
```

GLM 只读取争议的文本候选及上下文，不读取关键词、辅助材料或音频。
默认前后各 400 个 Unicode 字符，可向外扩展到句界、每侧最多 500；每批最多 8 项。
原始 Base、Peer、千问全文、Kimi 全文均保留，候选按精确文字去重并保存来源。
KEEP 不是已验证真值；封存也不表示已经人工听音确认。

ATA 是字幕分句、文字与时间轴的唯一 authority。CueFlow 原样提交封存全文，以
sta_punc_mode=3 请求保留标点；接收 utterances 的句子文字和起止毫秒，按原序机械写 SRT。
不读取 words、不映射、不插值、不重新分句或美化文字。完整原始返回与规范化结果均落盘。

utterance_count、文字拼接比对、空文本、负时间、反向/零时长、重叠计数只是 diagnostics，
不产生质量 gate。导出只校验 current/sealed/stale 等状态真实性，并在负时间或 start>end
时抛 SrtSerializationError；重叠、零时长、空文本、空数组、长句和文字差异仍允许导出。
本地导出失败后可 resume 已完成的 ATA 结果，不重复调用；非法时间不会被本地修正。

## 安装

需要 Python 3.10+、`ffmpeg` 和 `ffprobe`：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[cloud,dev]"
```

运行时凭据：

- Qwen ASR / Qwen Max：`DASHSCOPE_API_KEY`，Correction 另需 `DASHSCOPE_BASE_URL`；
- 豆包 ASR：`DOUBAO_API_KEY`，或 `DOUBAO_APP_KEY` + `DOUBAO_ACCESS_KEY`；
- GLM 选择器：`ZHIPU_API_KEY`；可用 `ZHIPU_BASE_URL` 覆盖官方对话 API 地址；
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
    {"review_id": "rev_...c", "action": "replace", "replacement": "Groq"}
  ]
}
```

然后执行 `cueflow review PROJECT decisions.json`。`action` 可为 `keep`、`qwen`、`kimi`、`peer` 或
`replace`；ID 必须来自该 run 当前的真实队列，不能使用下标或本示例占位值。服务端从
ReviewItem 取出冻结的 Base `[start,end)`；replace 只提交 replacement，不接受
source_sentence、original 或调用方 offset。显式 keep 也会持久化；过期队列拒绝提交。
review 未清零前不会调用 ATA。

`resume` 继续指定 run 从未提交的步骤，复用已完成 checkpoint，不重发失败或交付不明的
付费请求。`retry` 仅针对指定失败 invocation，可能重复计费，必须由用户明确执行；已成功
的纠错臂/GLM 批次不重跑。GLM 单批失败不阻塞其他批次，相关区间转人工 review。

`cueflow correct` 可以在不重跑 ASR 的情况下替换整组 References，但必须传入与原
`run` 完全相同、同序的 UserKeywords。新 `correct` 会重新调用两个 Correction 模型，而不是
复用旧纠错结果。若要改变关键词，必须重新 `run`，以保证两路 ASR
收到同一组先验。

关键词最多 100 个，只执行首尾空白裁剪、空串拒绝和 exact 去重，并保持首次出现顺序、
Unicode、大小写及标点。`.NET`、`C++`、`GPT-5.6` 等不会被词法归一化。没有用户关键词时，
ASR 不接收任何领域 lexical prior；References 只进入 Correction。

PDF/Image URL 必须由 CLI 显式声明类型。CueFlow 不下载 URL 猜 MIME；本地文本在命令开始
时以 UTF-8 读取并把正文冻结进 `JobInput`。v0.5.3 不接受本地 PDF/图片，也不转换 Office
文件。

当前 Artifact Schema 为 **11.0.0**，Registry 为 **13**。旧项目只拒绝打开，不迁移或重写；
本版不提供旧版本兼容入口，请创建新项目。正常输出是
`PROJECT/output/subtitles.srt`，内容寻址 Artifact、blob 和 SQLite 状态位于
`PROJECT/.cueflow/`。

详细契约见 [Architecture](docs/architecture.md)、
[Reference Inputs](docs/reference-inputs.md)、[Schema Contracts](docs/schema-contracts.md)、
[Failure Model](docs/failure-model.md) 与 [Roadmap](docs/roadmap.md)。
