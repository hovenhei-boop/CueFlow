# CueFlow v0.5.2 Reference Inputs

## 当前支持

| CLI | JobInput kind | CueFlow 持有内容 | Correction 输入 |
|---|---|---|---|
| `--pdf-url HTTPS_URL` | `pdf_url` | URL 字符串 | 原始 PDF URL |
| `--image-url HTTPS_URL` | `image_url` | URL 字符串 | 原始图片 URL |
| `--text-file FILE` | `text` | UTF-8 正文快照 | 原始文本 |
| `--keyword VALUE` | `user_keywords` | 精确字符串 | 两个 Correction arm；同时在 T0 输入三路 ASR |

同一命令中三类 Reference 按选项出现顺序保存，不按类型重排。URL 类型由 CLI 明示，CueFlow
不依赖扩展名、Content-Type 或主动下载推断。

TXT/MD/CSV/JSON 必须是非空 UTF-8 文件。PDF 与图片必须是调用时可由 Correction Provider
访问的 HTTPS URL。v0.5.2 没有 Reference Upload API，因此不接受本地 PDF/图片。

## ASR 边界

References 完全不进入 ASR 前处理。没有 Hint Builder、Reference extraction、项目背景摘要、
内置领域包或自动术语层。只有用户显式提交的 UserKeywords 是 ASR lexical/semantic prior。

关键词规则：

1. 最多 100 个；
2. 只 strip 首尾 whitespace；
3. 空串拒绝；
4. exact duplicate 去重并保留首次出现顺序；
5. 保持原 Unicode、标点和大小写。

`correct` 可替换 References，但 UserKeywords 必须与初始 `run` 完全相同；改变关键词要求新
`run`，不能只重跑 Correction。

## Locator 语义

Reference URL 是 mutable locator，不是 immutable content snapshot：

- targeted retry 复用原 URL 字符串；
- signed/temporary URL 过期会明确失败；
- URL 原地换内容时 CueFlow 不检测；
- v0.5.2 不保存 Reference 内容 hash、size、page count 或 MIME；
- 不建立 SSRF 下载器、redirect 策略或 Reference 临时文件链。

媒体上传到 TOS 与 Reference URL 是两条不同的边界：MediaObject 有内容 hash，临时 presigned
媒体 URL 不持久化；外部 Reference URL 仍只有 locator 语义。

## 明确不支持

DOC/DOCX/PPT/PPTX/XLS/XLSX 不接受也不转换。用户必须自行导出 PDF 并提供 `--pdf-url`。
本版没有 Office COM、LibreOffice、conversion worker 或 PDF 本地预检。
