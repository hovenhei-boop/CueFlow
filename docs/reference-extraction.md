# CueFlow v0.3.0 Reference Extraction

## 1. 边界

Reference Material 经确定性识别与提取，或远端 ASR、Vision、Document Parse，生成带 provenance 的 Reference Evidence。

ReferenceAsset 与 SourceAsset 独立。Evidence 不创建或修改 Source Transcript、Alignment、Subtitle、QA 或 SRT，也不自动融合。词库、术语挖掘、candidate、acceptance、UI、PPT/板书/屏幕画面补充抽取及跨 Run cache 不在当前实现内。

## 2. 身份与 relocate

Reference identity 是 `Path.name` 的精确字符串。登记保存 id、filename、locator、检测后的格式、媒体类别和登记时间；不保存或检查外部文件 hash、size 或 mtime。同项目同名重复登记返回原 ReferenceAsset，不更新 locator。

缺失或不可读 locator 明确报告 `reference_missing`，不自动搜索。`cueflow reference relocate PROJECT FOLDER` 只处理 locator 已失效的 Reference，只枚举 FOLDER 的可读普通直接子文件，不递归、不跟随 symlink、不扫描其他目录；filename 精确匹配才更新，未匹配项逐项报告。它不读取或修改 SourceAsset。

每次被接受的 `extract` 都在 Project 下创建新 Run，读取 locator 当前内容；同名文件可原位覆盖后重新 extract。`retry` 永远留在原 Run，只重试指定失败 work item。

## 3. 输入路由

| 输入 | 处理路径 |
|---|---|
| 可读文本字幕轨、SRT/VTT/ASS | 本地 cue 解析，零模型调用 |
| PGS/VobSub 位图字幕轨 | cue bitmap → Reference Vision，不调用 ASR |
| 显式 `burned` 视频 | full-frame Vision + Reference ASR，证据独立 |
| 显式 `none` 视频、纯音频 | Reference ASR |
| UTF-8 TXT/MD、DOCX/PPTX/XLSX | 本地确定性解析 |
| 完整文本层 PDF | 本地 pypdf 解析 |
| 扫描/混合 PDF、OLE DOC/PPT/XLS | Cloud Document Parse |
| 独立 PNG/JPEG/WebP | Reference Vision，产生 `image_visual` |

存在字幕流但 codec 不受支持时明确失败，不降级为无字幕。无独立字幕流的视频必须由调用方给出 `pixel_subtitle_mode=burned|none`；系统不自动检测硬字幕。无音频时文本轨和位图轨仍可成功，ASR work item 明确失败，因此 burned 视频可形成 partial。

位图 cue 跳过 clear/end/empty，按解码后的原始 pixel bytes 精确去重，只把唯一位图交给 Vision，并保留全部 occurrence 起止时间。PGS/VobSub 经过异常时长 gate；无法可靠解释的时间信息明确失败。

OOXML 使用标准库 ZIP/XML。PDF 只要存在有内容但无可靠文本层的页面，整个文档即走 Document Parse，不产生双套文本，不栅格化页面或调用页面 Vision。`image_visual` 只用于独立图片。

损坏、加密、签名与扩展冲突或不能可靠识别的输入明确失败。扩展名只初筛，最终依靠 UTF-8/容器标志、OOXML 结构、PDF header、图像 magic 或 FFprobe。

## 4. 模型与执行参数

- Reference Vision：`qwen3.7-plus`；full-frame 为 480p、4 fps、每 window 最多 30 秒；JPEG 由 FFmpeg `-q:v 8` 产生。位图字幕传入去重后的 PNG。
- Reference ASR：`qwen-audio-3.0-asr-flash`；16kHz mono PCM s16le WAV，每段不超过 225 秒；Provider segment 时间戳 rebase 回源时间轴。
- Cloud Document Parse：`qwen-doc-turbo`，Files API purpose=`file-extract`。

Invocation 保存实际 model/config。权威图像时间来自 pipeline 的 `frame_id ↔ source_timestamp_ms`，不信任模型返回的 frame index。源时长 `local_measured_duration` 与账单时长 `provider_usage_duration` 独立保存；缺失 usage/cost 为 null，不伪造为 0。

## 5. 云端上传范围

显式执行 `reference extract` 时，按路由可能上传指定 Reference 的 OLE Office 或扫描/混合 PDF 文档、去重位图 cue、临时 full-frame 图片、独立图片和 PCM/WAV 音频段。确定性提取不上传文件，不创建模型 Invocation；不扫描或上传其他未指定文件。

full-frame JPEG 为临时数据，仅持久化 manifest、encoded hash、时间戳和执行参数；base64 请求正文不写入 Artifact、日志或 Registry。唯一位图可作为内容寻址 blob 持久化。

Document Parse 使用 Files API 上传、状态轮询和解析，取得 `file_id` 后在 finally 请求删除。身份、权限、格式/provider 错误分别报告，删除未确认也明确报告。该链路不保证任意复杂版面的 OCR 质量。

## 6. Run、outcome、Invocation 和 retry

Reference Run 使用现有 `runs`，`reference_runs` 是关联 ReferenceAsset、outcome 和当前 bundle 的扩展。全 work item 成功 → `succeeded/complete`；有成功有失败 → `failed/partial`；无成功 → `failed/failed`。只有 complete/partial 发布 bundle。

只有真实 Provider/model work item 创建 Invocation。Detail 保存 branch/work item、provider/model、实际 config、有序 input Artifact、response id、双时长、usage/cost、retry parent/reason、failure、remote file id 和 cleanup status。

Retry 只接受失败或 interrupted work item，成功项不重跑。成功 retry 发布原 Run 的新 bundle，引用既有成功 Evidence。每模型 work item 每 Run 最多两个 sent attempt；`definitely_not_sent` 不占，`delivery_ambiguous` 占且不自动 retry。不提供质量重试参数。

Crash recovery 只在 Source `run/retry` 或 Reference `extract/retry` 入口执行，分别只恢复对应 Run 类别。add、relocate、status 和打开项目不恢复。`project_status()` 分别报告 `latest_source_run` 与 `reference_runs`。
