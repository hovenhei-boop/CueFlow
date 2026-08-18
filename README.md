# CueFlow

CueFlow v0.1 是本地项目式字幕生成与检查引擎。产品契约以 `docs/` 中当前四份冻结规范为准。

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

项目内部状态位于 `PROJECT_DIR/.cueflow/`，唯一正常用户输出为
`PROJECT_DIR/output/subtitles.srt`。CueFlow 不删除或修改外部源文件。
