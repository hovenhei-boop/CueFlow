# CueFlow v0.1 Schema 契约

状态：设计基线

本文示例定义语义，不提前决定最终序列化类名。JSON 是 Artifact payload 和交换格式；SQLite 保存控制平面字段与引用。

## 1. Artifact Envelope

每个 Artifact 文件拥有统一 Envelope：

```json
{
  "schema_version": "1.0.0",
  "artifact_id": "art_...",
  "artifact_kind": "transcript",
  "scope_key": "chunk_0007",
  "content_hash": "sha256:...",
  "created_at": "2026-08-15T00:00:00Z",
  "producer": {
    "component": "cueflow.transcription",
    "component_version": "0.1.0",
    "processing_profile": "LOCAL_PROFILE",
    "provider": "explicit-provider-id",
    "model": "explicit-model-id",
    "config_hash": "sha256:..."
  },
  "inputs": [
    {"role": "media_chunk", "artifact_id": "art_..."},
    {"role": "effective_glossary", "artifact_id": "art_..."}
  ],
  "payload": {}
}
```

内容哈希使用 RFC 8785 JSON Canonicalization Scheme（JCS）生成 UTF-8 bytes，再计算 SHA-256。Canonicalization 输入固定为：

```json
{
  "artifact_kind": "transcript",
  "scope_key": "chunk_0007",
  "schema_semantics": {"major": 1, "minor": 0},
  "producer": {
    "component": "cueflow.transcription",
    "component_version": "0.1.0",
    "processing_profile": "LOCAL_PROFILE",
    "provider": "explicit-provider-id",
    "model": "explicit-model-id",
    "config_hash": "sha256:..."
  },
  "inputs": [],
  "payload": {}
}
```

`inputs` 保持声明顺序，引用精确 Artifact 或 SourceAsset 身份。哈希不覆盖 `artifact_id`、`created_at`、文件路径和其他存储元数据。JCS 输入必须满足 I-JSON；禁止 NaN、Infinity、重复 object key 和无效 Unicode。`content_hash` 格式为 `sha256:` 加 64 位小写十六进制。实现必须用跨进程 Golden Test 验证。

遇到未知 major version 时，仍允许定位、复制和备份 Artifact；如果已有渲染完成的 SRT，也允许直接访问该 SRT。需要解释未知 payload 的 Core 操作必须拒绝。未知 minor 字段不得静默改变已知字段语义。

`scope_key` 对 Chunk 级 `media_chunk`、`transcript` 和 `alignment` 使用 `chunk_id`；`media_probe`、`chunk_plan`、`timeline_audio`、`video_proxy`、Glossary、Subtitle、QA、`filler_review` 与 `srt_render` 使用 `global`。

## 2. SourceAsset 契约

视频 SourceAsset 使用 `external_reference`：

```json
{
  "source_asset_id": "src_...",
  "asset_kind": "media",
  "media_kind": "video",
  "format": "mp4",
  "content_hash": "sha256:...",
  "byte_length": 123456789,
  "storage_mode": "external_reference",
  "storage_locator": "D:/media/input.mp4",
  "registered_at": "2026-08-16T00:00:00Z"
}
```

登记必须读取完整文件计算哈希。CueFlow 不把原始大视频复制到项目中，也不得删除或修改外部文件。重新执行 Media Prep 前必须重新校验定位器内容哈希；已登记的 Timeline Audio、Video Proxy 等 Artifact 可以在外部文件不可访问时继续用于精确下游恢复。

## 3. 时间、工作音频与视频代理契约

- 所有 Core 时间都是整数毫秒。
- 时间字段名以 `_ms` 结尾。
- 合法区间满足 `0 <= start_ms < end_ms`。
- 区间语义是 `[start_ms, end_ms)`。
- 浮点秒和帧号不是 Core 权威时间。

视频 `media_probe` 示例：

```json
{
  "timeline_status": "corrected",
  "timeline_tolerance_ms": 10,
  "presentation_duration_ms": 10000,
  "video_stream": {
    "start_time_ms": 0,
    "duration_ms": 10000,
    "time_base": "1/90000"
  },
  "audio_stream": {
    "start_time_ms": 42,
    "duration_ms": 9958,
    "time_base": "1/48000"
  },
  "timeline_issues": ["audio_starts_after_video"],
  "timeline_actions": [
    {"action": "pad_silence_before", "duration_ms": 42}
  ]
}
```

`timeline_status` 只允许 `normal`、`corrected` 或 `unverified`。Media Probe 必须保存探测事实和修正动作，不能只保存修正后的结果。`timeline_tolerance_ms` 是版本化 Media Prep 配置，示例值不是领域常量。

`timeline_audio` 必须满足：

- 0ms 对应源视频呈现 0ms；
- `duration_ms` 等于视频呈现总时长；
- 输入音轨尚未开始或已经结束的区域渲染为静音；
- 下游不能再次应用源流 start time 或 edit-list offset。

直接音频输入的 `timeline_audio` 使用音频文件自己的 0ms 与总时长。

v0.1 Timeline Audio 固定为 16kHz、mono、PCM s16le；Media Prep 配置固定 `timeline_tolerance_ms = 20`。

视频 `video_proxy` payload 至少包含：

```json
{
  "source_asset_id": "src_...",
  "media_probe_artifact_id": "art_...",
  "timeline_audio_artifact_id": "art_...",
  "video_blob": {
    "content_hash": "sha256:...",
    "byte_length": 1234567,
    "media_type": "video/mp4"
  },
  "max_width": 640,
  "max_height": 360,
  "target_video_bitrate_bps": 1000000,
  "authoritative_for_audio_processing": false
}
```

Proxy 保持宽高比，任何维度不得超出 640×360 边界。审阅音轨只能从 Timeline Audio 派生，不能成为下游音频权威。

Chunk 示例：

```json
{
  "chunk_id": "chunk_0007",
  "global_start_ms": 1440000,
  "global_end_ms": 1678500,
  "timeline_audio_artifact_id": "art_timeline_audio_..."
}
```

Provider 局部时间必须在 Adapter 边界转换。除隔离保存的原始 Provider Attachment 外，它不应出现在 Alignment payload 中。

ChunkPlan 配置固定：`target_duration_ms = 180000`、`hard_limit_ms = 225000`、`silence_min_duration_ms = 500`、`silence_threshold_db = -40`。所有 Chunk 必须连续覆盖 Timeline Audio、无重叠、无间隙，且除最后一个短尾块外满足正时长。

## 4. Glossary 契约

```json
{
  "terms": [
    "Heriot-Watt University",
    "赫瑞-瓦特大学",
    "赫瓦"
  ],
  "normalization_version": "0.1.0"
}
```

`terms[]` 中每项都是可能被实际说出的独立词形。只允许最小 Unicode 规范化、Trim、精确去重和确定性排序；不得保存或推断 `aliases`、实体对应、首选名称或替换方向。Payload 不得包含无类型 `note`、任意 Prompt 片段或嵌入的源文档正文。

## 5. Transcript 契约

```json
{
  "chunk_id": "chunk_0007",
  "source_text": "Hello, 世界！",
  "leading_decoration": "",
  "atomizer_version": "0.1.0",
  "language": "English",
  "atoms": [
    {
      "atom_id": "a0001",
      "position": 0,
      "text": "Hello",
      "atom_class": "word",
      "decoration_after": ", "
    },
    {
      "atom_id": "a0002",
      "position": 1,
      "text": "世",
      "atom_class": "cjk_character",
      "decoration_after": ""
    },
    {
      "atom_id": "a0003",
      "position": 2,
      "text": "界",
      "atom_class": "cjk_character",
      "decoration_after": "！"
    }
  ],
  "semantic_confidence": null
}
```

`atom_class` 只允许 `word`、`cjk_character`、`number` 和 `pronounceable_symbol`。空格和标点只能出现在 `leading_decoration` 或 `decoration_after` 中，不能成为 Atom。

`leading_decoration + concat(atom.text + atom.decoration_after)` 必须等于 `source_text`，否则 Artifact 无效。Provider Token 不是 Transcript Atom；装饰字符不生成 Alignment Assignment，也不产生 unaligned warning。

进入 Forced Alignment 的 Transcript 必须声明当前固定 Aligner Snapshot 支持的语言名称。Cloud Semantic Adapter 的同一次受限响应只允许返回 `source_text` 与 `language`；缺失或不受支持的语言明确失败，不得默认按中文对齐。

## 6. Alignment 契约

```json
{
  "chunk_id": "chunk_0007",
  "transcript_artifact_id": "art_transcript_...",
  "media_chunk_artifact_id": "art_chunk_...",
  "assignments": [
    {
      "atom_id": "a0001",
      "status": "aligned",
      "global_start_ms": 1441250,
      "global_end_ms": 1441390,
      "acoustic_confidence": null
    },
    {
      "atom_id": "a0002",
      "status": "unaligned",
      "reason": "provider_returned_no_boundary"
    }
  ]
}
```

Assignment 必须引用精确 Transcript Artifact 中的实音 Atom。一个 Alignment payload 只能对应一个 `chunk_id` 和一个 Transcript Artifact，不存在 Global Alignment Schema。已对齐区间必须位于相应 MediaChunk 内，唯一例外是显式版本化的边界容差。遇到实质性非法 Provider 输出时，Adapter 必须校验失败，不能无记录地强行截断时间。

## 7. Subtitle 契约

```json
{
  "transcript_artifact_ids": ["art_transcript_..."],
  "alignment_artifact_ids": ["art_alignment_..."],
  "segmenter_config_hash": "sha256:...",
  "cues": [
    {
      "cue_id": "cue_0001",
      "global_start_ms": 1441250,
      "global_end_ms": 1443520,
      "atom_spans": [
        {
          "transcript_artifact_id": "art_transcript_...",
          "first_atom_id": "a0001",
          "last_atom_id": "a0012"
        }
      ],
      "text": "我们使用卡尔曼滤波",
      "display_unit_count": 9,
      "protected_overflow": false
    }
  ]
}
```

Cue 必须严格有序且不重叠。Cue 的实音文字序列必须与 `atom_spans` 一致；`text` 可以按 `segmenter_config_hash` 指定的风格删除句号、分号、问号、感叹号和破折号，并把需要表达停顿的逗号转换为空格，但不能改写 Transcript。Cue 起止时间分别取第一个和最后一个 Aligned Atom。

显示单位规则集中在版本化 Segmenter Config：每个 `cjk_character`、完整 `word`、完整 `number` 和完整 `pronounceable_symbol` Atom 各计 1。普通 Cue 的 `display_unit_count <= 10`。只有当一个至少 2 个 Atom 的 Effective Glossary 精确匹配保护单元本身超过 10 时，才允许 `protected_overflow = true`，并且 QA 必须产生 `protected_unit_exceeds_display_limit` warning。

## 8. QA 契约

```json
{
  "subject_artifact_ids": ["art_subtitle_..."],
  "qa_ruleset_version": "0.1.0",
  "result": "warnings",
  "issues": [
    {
      "issue_id": "issue_0001",
      "severity": "warning",
      "code": "possible_chunk_boundary_duplication",
      "resolution_status": "unresolved",
      "semantic_attempts": 4,
      "replacement_artifact_ids": ["art_transcript_retry_..."],
      "locations": [
        {"artifact_id": "art_transcript_a", "atom_span": [98, 104]},
        {"artifact_id": "art_transcript_b", "atom_span": [0, 6]}
      ],
      "observed": {"normalized_overlap": "卡尔曼"}
    }
  ]
}
```

`result` 只允许 `passed`、`warnings` 和 `blocked`。`resolution_status` 只允许 `detected`、`rework_requested`、`resolved` 和 `unresolved`。QA 输出不得包含可自动执行的文字替换。

时间戳非法、Cue 重叠或越界、实音 Atom 未正确对齐、Transcript/Alignment/Chunk 引用错位和 Artifact 依赖身份不一致必须产生 `blocking_error`。Orchestrator 对对应确定性阶段或本地 Alignment 进行一次修复性重算；仍非法时 Run 失败，不能导出。

`glossary_single_atom_conflict` 只允许至少 2 个 Atom 的 Glossary term 参与。term 和候选 Transcript 窗口必须 Atom 数量、class 序列完全相同，NFC/casefold 后恰好一个 Atom 不同。单 Atom term 不参与，且 v0.1 不使用 Levenshtein、拼音/音素或 NER 扩展候选。

每个 Chunk、每次 Run 的 `semantic_attempts` 最大为 4。连续两次候选 Atom 序列完全相同后：与 Glossary term 一致则 `resolved`；仍为同一个冲突则停止返工并生成 warning code `stable_glossary_conflict`。4 次内没有连续一致结果则生成 warning code `unstable_glossary_conflict`。两者都是非阻塞 ReviewIssue。Glossary 不得直接覆盖 Transcript。

v0.1 不冻结 `0.90/0.95` confidence 阈值。Provider confidence 只能按原 Provider、metric name 和 scale 保存；缺失时为 `unavailable`，不得伪造。

## 9. Filler Review 契约

```json
{
  "subtitle_artifact_id": "art_subtitle_...",
  "review_config_hash": "sha256:...",
  "mode": "deterministic_local",
  "status": "completed",
  "candidates": [
    {
      "cue_id": "cue_0001",
      "transcript_artifact_id": "art_transcript_...",
      "atom_id": "a0017",
      "text": "啊",
      "evidence": {
        "cue_terminal": true,
        "sentence_terminal_decoration": true,
        "following_pause_ms": 950,
        "stream_terminal": false
      }
    }
  ],
  "suppressions": [
    {
      "cue_id": "cue_0001",
      "transcript_artifact_id": "art_transcript_...",
      "atom_id": "a0017",
      "text": "啊",
      "reason": "terminal_filler"
    }
  ],
  "warnings": []
}
```

`mode` 只允许 `deterministic_local` 或 `cloud_atom_review`；`status` 只允许 `completed` 或 `unavailable`。候选只能是 Cue 最后一个实音 Atom，文字只能是 `啊`、`呀`、`哦`、`嗯`、`呃`，每 Cue 最多一个，不允许 `呢`。`suppressions` 必须是 `candidates` 的子集，不能包含替代文字。

Cloud Adapter 返回未知 Atom、非候选 Atom、改写文本或多个同 Cue suppression 时必须拒绝其语义结果。Cloud 调用失败或 `delivery_ambiguous` 时生成 `status = unavailable`、空 suppressions 和 warning；不自动重试，也不阻塞 SRT。被 suppression 的 Atom 仍在 Subtitle span 中，SRT Render 只省略其显示并保留原 Cue 时间包络。

## 10. SQLite 最小控制平面

最小逻辑表：

```text
projects
source_assets
artifacts
artifact_dependencies
current_pointers
runs
invocations
```

关键完整性规则：

- Current Pointer 必须引用同一项目内已经登记的 Artifact。
- `artifacts` 保存 `scope_key`；`current_pointers` 主键为 `(project_id, artifact_kind, scope_key)`。
- Chunk Artifact 的 `scope_key` 必须等于 payload 的 `chunk_id`；全局 Artifact 必须使用 `global`。
- Dependency Edge 必须引用已经登记的不可变输入。
- Artifact Row 保存 kind、Schema Version、Content Hash 和 Storage Locator。
- 成功 Invocation 可以引用它生成的 Artifact。
- Artifact 文件持久化前，数据库不得声称它已经 active。
- 同一 Chunk 的 Transcript/Alignment 指针和相关全局 stale 状态必须在同一个写事务中更新。

大型 payload 与媒体字节保存在文件存储中。v0.1 不需要为 QA Issue 单独建表，因为不可变 QaArtifact 是它们的权威记录。

## 11. 公开文件与内部文件

正常用户可见输出：

```text
output/
└── subtitles.srt
```

内部实现可以拥有项目数据库和受管 Artifact Store，但其目录布局在 v0.1 中不是公开 API。用户可以备份或复制它们，正常产品界面不提供历史 Artifact 选择。
