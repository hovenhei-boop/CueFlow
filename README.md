# CueFlow

CueFlow v0.1 是面向已剪辑完成媒体的项目式字幕生成与检查引擎。它忠实转写实际人声，建立本地 presentation timeline、强制对齐、字幕切分、QA，并导出唯一 SRT。产品契约以 `docs/` 中当前四份冻结规范为准。

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

CueFlow 只保存外部源媒体路径和完整内容身份，不复制、移动或删除源文件。源路径缺失时明确报告 `source_missing`。LOCAL/CLOUD Profile 使用同一个本地 Media Prep；Cloud 只影响后续 Semantic Transcription。
