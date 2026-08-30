# CueFlow

CueFlow v0.5.1 是面向已剪辑完成媒体的项目式字幕生成与检查引擎。Source 主链为 Media Prep → 远端语义转写与纠错 → 本地 Forced Alignment → Subtitle → QA → SRT。独立的 Reference 旁路通过确定性提取或远端 ASR、Vision、Document Parse 生成带 provenance 的 Evidence，并自动生成等待人工处理的 Suggested Terms。Project Lexicon 与全局 Official Packs 在本版不进入 Source Transcript、Effective Glossary、Alignment 或 SRT。

## 安装

Python 3.10 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[alignment,cloud,dev]"
```

运行媒体链需要可执行的 `ffmpeg` 与 `ffprobe`。可通过 PATH，或使用
`CUEFLOW_FFMPEG`、`CUEFLOW_FFPROBE` 指定绝对路径。远端 Provider 从
`DASHSCOPE_API_KEY` 与 `DASHSCOPE_BASE_URL` 读取凭据和地域 endpoint。
`alignment` extra 提供本地 Forced Aligner 所需的 `qwen-asr`；设备与 dtype 由运行时检测，
模型缓存可由 `CUEFLOW_MODEL_CACHE` 指定。完整 Source 主链需要 `alignment` 和 `cloud`，确定性 Reference 提取不加载模型。

## CLI

```text
cueflow init PROJECT_DIR --name NAME
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
cueflow lexicon suggestions list|status|retry|review ...
cueflow lexicon entry list|add|edit|enable|disable|remove|block ...
cueflow lexicon blacklist list|add|update|unblock ...
cueflow lexicon pack list|setup|install|uninstall|update|status|repair ...
```

Registry 只接受当前精确契约。全新空库初始化当前结构；版本、表或列不符合当前契约时明确拒绝且不修改已有数据。

`run` 与每次被接受的 `reference extract` 都在 Project 下创建全新 Run；Source `retry` 与 `reference retry` 都只在原 Run 内最小重放。Reference retry 以 work-item 为粒度，成功项永不重跑。项目内部状态位于 `PROJECT_DIR/.cueflow/`，Source 主链的唯一正常用户输出仍为 `PROJECT_DIR/output/subtitles.srt`。

CueFlow 分别以 `Path.name` 精确字符串作为 Source 与 Reference identity；二者都不保存或校验外部文件内容 hash、size 或 mtime。Reference 同名重复登记返回原对象且不更新 locator。Reference 缺失时不自动搜索；用户只能显式执行 `reference relocate`，它只检查指定文件夹的直接子项并按 filename 精确匹配。项目内 Artifact 继续保持内容寻址、不可变和 hash 校验。

CLI 执行失败时以结构化 JSON 报告可用的 `run_id`，并在存在失败 Invocation 时报告 `invocation_id`、当前状态和合法 `next_actions`。`delivery_ambiguous` 从不自动 retry；Source 使用显式 `cueflow retry`，Reference 使用显式 work-item `cueflow reference retry`。

执行 `run` 会将音频 Chunk 发送到远端语义 Provider；`reference extract` 按输入格式使用确定性提取或上传指定 Reference 的文档、音频段和图像。Reference Evidence 生成后会自动触发术语发现，因此即使 Reference 内容由本地确定性提取得到，其 Evidence 文本仍可能发送到词库云端 Provider。`reference add`、状态查看、词库人工管理和词包本地读取不触发该上传。

Reference 的详细格式、视频路由、云端上传范围、模型常量、usage 双时长和限制见 [Reference Extraction](docs/reference-extraction.md)；候选审核、Project Lexicon、Project Blacklist 与 Official Packs 见 [Lexicon](docs/lexicon.md)；后续范围见 [Roadmap](docs/roadmap.md)。
