# CueFlow v0.2.1 Reference Extraction

状态：冻结

## 1. 边界

v0.2.1 只实现以下旁路：

```text
Reference Material
→ deterministic inspection/extraction
→ optional Reference ASR / Vision / Cloud Document Parse
→ provenance-bearing Reference Evidence
```

ReferenceAsset 不是 auxiliary SourceAsset。Reference Evidence 不创建或修改 Source Transcript、Alignment、Subtitle、QA 或 SRT，也不被自动融合。v0.2.1 不包含 glossary、terminology mining、candidate、acceptance、UI、Supplementary PPT/板书/屏幕画面抽取或跨 Run cache。

## 2. 身份与 relocate

Reference identity 是 `Path.name` 的精确字符串。登记只保存 id、filename、locator、检测后的格式、媒体类别和登记时间；不保存或检查外部文件 hash、size 或 mtime。同项目同名重复登记返回原 ReferenceAsset，不更新 locator，也不报错。

缺失或不可读 locator 明确报告 `reference_missing`，不自动搜索。唯一恢复命令 `cueflow reference relocate PROJECT FOLDER` 只处理 locator 已失效的 Reference，只枚举 FOLDER 的可读普通直接子文件，不递归、不跟随 symlink、不扫描其他目录；filename 精确匹配才更新，未匹配项逐项报告。它不读取或修改 SourceAsset。

每次被接受的 `extract` 都在 Project 下创建新 Run，并读取 locator 当前内容；同一 ReferenceAsset 可有任意多个 Run。同名文件被原位覆盖后再次 extract 与 Source 的新 Run 语义一致。`retry` 永远留在原 Run，只重试指定失败 work item。

## 3. 视频冻结路由

| 视频状态 | Cloud | Local |
|---|---|---|
| 可读文本字幕轨 | 本地 cue 解析；零模型调用 | 同左 |
| PGS/VobSub 位图轨 | cue bitmap → Reference Vision；不调用 ASR | Local Reference ASR |
| 烧录硬字幕，调用方显式 `burned` | full-frame Vision + Cloud Reference ASR；证据独立 | Local Reference ASR |
| 无字幕，调用方显式 `none` | Cloud Reference ASR | Local Reference ASR |

存在字幕流但 codec 不受支持时明确失败，不降级为无字幕。无独立字幕流的视频必须由调用方给出 `pixel_subtitle_mode=burned|none`；系统不自动检测硬字幕。无音频时文本轨和 Cloud 位图仍可成功，ASR work item 明确失败；因此 Cloud burned 可形成 partial。

位图 cue 跳过 clear/end/empty，按解码后的原始 pixel bytes 精确去重，只把唯一位图交给 Vision，并为每个唯一位图保留全部 occurrence 起止时间。PGS 与 VobSub 都经过异常时长 gate；VobSub 使用 FFmpeg 官方独立 `reddwarf-vobsub.mkv` 样本验证了 60.248 秒媒体内 14 个正常 cue 和非空渲染，Phase 0 异常时长输入会明确停止。

## 4. 模型与执行常量

- Reference Vision：`qwen3.7-plus`，480p、4 fps、每 window 最多 30 秒。full-frame JPEG 由 FFmpeg `-q:v 8` 产生；Phase 0 manifest 的 `p480_4_j60` 中 `j60` 是误称，实测 command 和成本证据对应 `-q:v 8`，没有 Pillow 二次编码。
- Cloud Reference ASR：`qwen-audio-3.0-asr-flash`；16kHz mono PCM s16le WAV；每段不超过 225 秒；Provider segment 时间戳 rebase 回源时间轴。
- Local Reference ASR：复用本地 Qwen transport/load，但 context 为空，不接受 Glossary rework contract，不产生 Source Transcript。
- Cloud document：`qwen-doc-turbo`，Files API purpose=`file-extract`。

这些是当次运行配置常量，不是 ProfileV1/ProfileV2 领域体系。Invocation 保存实际 model/config。没有 Opus 默认、`--audio-upload-format`、压缩音频抽象、`--document-visual` 或 PDF 页面 Vision。

模型返回的 frame index 不作为时间证据。权威时间只来自 pipeline 保存的 `frame_id ↔ source_timestamp_ms`。full-frame JPEG 是临时数据，只持久化稳定 manifest、encoded hash、时间戳和实际 profile；唯一位图 blob 可作为项目内内容寻址 blob 持久化。

## 5. 文档和独立图片

Cloud/Local 共用确定性本地读取：UTF-8 TXT/MD、SRT/VTT/ASS cue、标准库 ZIP/XML 的 DOCX/PPTX/XLSX，以及限定版本 pypdf 的完整 text-layer PDF。混合 PDF 只要存在有内容但无可靠文本层的页面，整个文档即走 Cloud document parse；Local 明确 unsupported，不产生双套文本。

Cloud 对 legacy OLE DOC/PPT/XLS 和 scanned/无文本层 PDF 执行：Files API 上传 → `file_id` → `qwen-doc-turbo` → 状态轮询 → `finally DELETE file_id`。身份、权限与格式/provider 错误分别报告；usage 保存；删除失败也明确报告。tiny probe 只证明链路，不代表复杂版面或通用 OCR 质量。

Local legacy Office 由可选 `office` extra 的 extractous 0.3.x 提供，只调用本地 file API；不设置 OCR、不调用 URL API。缺包时提示安装 extra 或改用 Cloud。Local scanned PDF 没有 OCR。PyMuPDF、页面栅格化和页面 Vision 不在 v0.2.1。

独立 PNG/JPEG/WebP 在 Cloud 产生 `image_visual`；Local 明确 unsupported。该角色不用于 PDF 页面，也不与其他角色融合。损坏、加密、签名与扩展冲突或不能可靠识别的输入明确失败；扩展名只初筛，最终依靠 UTF-8/容器标志、OOXML 结构、PDF header、图像 magic 或 FFprobe。

## 6. 云端上传范围

在用户选择 `CLOUD_PROFILE` 并显式执行 Reference extract 时，CueFlow 可能上传：Reference legacy Office 或 scanned/mixed PDF 文档、去重后的位图 cue、临时 full-frame window 图片、独立图片，以及 PCM/WAV Reference 音频段。不会静默上传 Source 主链以外的其他文件。全帧 base64 仅存在于请求内，不写入 Artifact、日志或 Registry；Cloud document `file_id` 必须在 finally 删除。

## 7. Run、outcome、Invocation 和 retry

Reference Run 使用现有 `runs`，`reference_runs` 只是以 `run_id` 关联 ReferenceAsset、outcome 和当前 bundle 的扩展。聚合映射固定为：全 work item 成功 → `succeeded/complete`；有成功有失败 → `failed/partial`；无成功 → `failed/failed`。

只有真实 Provider/model work item 创建 Invocation；确定性解析不创建虚假调用。Invocation detail 保存 branch/work item、provider/model、实际 config、有序 input Artifact、response id、local/provider 双时长、usage、Provider 报告的 cost、retry parent/reason、failure、Cloud file id 和 cleanup status。Provider 未报告值为 null，不伪造 0。

Retry 只接受失败或 interrupted work item：成功项不重跑，成功 ASR 不因 Vision 失败而重跑，反之亦然。成功 retry 发布原 Run 的新 bundle，引用既有成功 evidence。每个模型 work item 每 Run 最多两个 sent attempt；`definitely_not_sent` 不占，`delivery_ambiguous` 占且不自动 retry。不提供“质量不满意”retry 参数。

Crash recovery 只在 Source `run/retry` 或 Reference `extract/retry` 入口执行，并分别只恢复对应 Run 类别。add、relocate、status 和打开项目不恢复。`project_status()` 分别报告 `latest_source_run` 与 `reference_runs`，Reference 状态不覆盖 Source 主链。

