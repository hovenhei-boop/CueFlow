# CueFlow

CueFlow v0.2.1 是面向已剪辑完成媒体的项目式字幕生成与检查引擎。v0.1.1 的 Source Media → Transcript → Alignment → Subtitle → QA → SRT 主链保持冻结；v0.2.1 只新增 Reference Material → 确定性提取/可选 Reference ASR、Vision、Cloud Document Parse → 带 provenance 的 Reference Evidence 旁路。Reference Evidence 不进入 Transcript、Alignment 或 SRT。

## 安装

Python 3.10 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[local,cloud,office,dev]"
```

运行媒体链需要可执行的 `ffmpeg` 与 `ffprobe`。可通过 PATH，或使用
`CUEFLOW_FFMPEG`、`CUEFLOW_FFPROBE` 指定绝对路径。Cloud Profile 只从
`DASHSCOPE_API_KEY` 与 `DASHSCOPE_BASE_URL` 读取凭据和地域 endpoint。

## CLI

```text
cueflow init PROJECT_DIR --name NAME --profile LOCAL_PROFILE|CLOUD_PROFILE
cueflow glossary set PROJECT_DIR GLOSSARY.json
cueflow asset add PROJECT_DIR FILE --kind auxiliary
cueflow run PROJECT_DIR MEDIA
cueflow status PROJECT_DIR
cueflow retry PROJECT_DIR INVOCATION_ID
cueflow reference add PROJECT_DIR FILE
cueflow reference extract PROJECT_DIR REF_ID [--pixel-subtitle-mode burned|none]
cueflow reference relocate PROJECT_DIR FOLDER
cueflow reference status PROJECT_DIR [REF_ID]
cueflow reference retry PROJECT_DIR WORK_ITEM_ID
```

`run` 与每次被接受的 `reference extract` 都在 Project 下创建全新 Run；Source `retry` 与 `reference retry` 都只在原 Run 内最小重放。Reference retry 以 work-item 为粒度，成功项永不重跑。项目内部状态位于 `PROJECT_DIR/.cueflow/`，Source 主链的唯一正常用户输出仍为 `PROJECT_DIR/output/subtitles.srt`。

CueFlow 分别以 `Path.name` 精确字符串作为 Source 与 Reference identity；二者都不保存或校验外部文件内容 hash、size 或 mtime。Reference 同名重复登记返回原对象且不更新 locator。Reference 缺失时不自动搜索；用户只能显式执行 `reference relocate`，它只检查指定文件夹的直接子项并按 filename 精确匹配。项目内 Artifact 继续保持内容寻址、不可变和 hash 校验。

CLI 执行失败时以结构化 JSON 报告可用的 `run_id`，并在存在失败 Invocation 时报告 `invocation_id`、当前状态和合法 `next_actions`。`delivery_ambiguous` 从不自动 retry；Source 使用显式 `cueflow retry`，Reference 使用显式 work-item `cueflow reference retry`。

Reference 的详细格式、视频路由、云端上传范围、模型常量、usage 双时长和限制见 [Reference Extraction](docs/reference-extraction.md)；版本边界见 [Roadmap](docs/roadmap.md)。
