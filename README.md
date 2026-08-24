# CueFlow

CueFlow v0.1.1 是面向已剪辑完成媒体的项目式字幕生成与检查引擎。它忠实转写实际人声，建立本地 presentation timeline、强制对齐、字幕切分、QA，并导出唯一 SRT。产品契约以 `docs/` 中当前四份冻结规范为准。

## 安装

Python 3.10 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[local,cloud,dev]"
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
```

`run` 总是创建全新 Run，不沿用其他 Run 的处理结果；`retry` 在原 Run 内从失败 Invocation 绑定的精确 Artifact 输入做最小重放。项目内部状态位于 `PROJECT_DIR/.cueflow/`，唯一正常用户输出为 `PROJECT_DIR/output/subtitles.srt`。

CueFlow 以 `Path.name` 精确字符串作为 Source identity，只保存文件名和外部 locator，不保存或校验 Source 内容 hash/长度。用户可在原 locator 直接覆盖同名文件后重新运行；路径缺失、不是普通文件或不可读取时明确报告 `source_missing`。CueFlow 不搜索其他同名文件，v0.1.1 不提供 relink。项目内 Artifact 继续保持内容寻址、不可变和 hash 校验。

CLI 执行失败时以结构化 JSON 报告可用的 `run_id`，并在存在失败 Invocation 时报告 `invocation_id`、当前状态和合法 `next_actions`。`delivery_ambiguous` 从不自动 retry，只能由用户显式执行 `cueflow retry`。
