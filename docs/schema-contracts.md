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

内容哈希使用规范化 JSON 序列化，覆盖以下语义内容：

```text
artifact_kind + scope_key + schema major/minor 语义 + 影响结果的 Producer 配置
+ 有序输入身份 + payload
```

哈希不覆盖 `artifact_id`、`created_at`、文件路径和其他存储元数据。具体 Canonicalization 算法必须在实现前冻结，并用跨进程 Golden Test 验证。

遇到未知 major version 时，仍允许定位、复制和备份 Artifact；如果已有渲染完成的 SRT，也允许直接访问该 SRT。需要解释未知 payload 的 Core 操作必须拒绝。未知 minor 字段不得静默改变已知字段语义。

`scope_key` 对 Chunk 级 `media_chunk`、`transcript` 和 `alignment` 使用 `chunk_id`；`media_probe`、`chunk_plan`、`timeline_audio`、Glossary、Subtitle、QA 与 `srt_render` 使用 `global`。

## 2. 时间与工作音频契约

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

## 3. Glossary 契约

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

## 4. Transcript 契约

```json
{
  "chunk_id": "chunk_0007",
  "source_text": "Hello, 世界！",
  "leading_decoration": "",
  "atomizer_version": "0.1.0",
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

## 5. Alignment 契约

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

## 6. Subtitle 契约

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
      "text": "我们使用卡尔曼滤波"
    }
  ]
}
```

Cue 必须严格有序且不重叠，除非未来 Schema 显式声明其他字幕格式策略。Cue 的实音文字序列必须与 `atom_spans` 一致；`text` 可以按 `segmenter_config_hash` 指定的风格减少标点或调整空格，但不能改写实音内容。Cue 起止时间分别取第一个和最后一个 Aligned Atom。SRT Render 只格式化已经确定的 Cue。

## 7. QA 契约

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

普通低 confidence 可以形成内部 `detected` Issue，但只有版本化白名单内的 QA rule code 可以触发自动返工。每个 Chunk、每次 Run 的 `semantic_attempts` 最大为 4，表示首次 Semantic Attempt 加最多 3 次自动返工；达到上限后停止返工并生成 ReviewIssue。该列表仍来自内部 QaArtifact，不产生用户侧 JSON 报告。

## 8. SQLite 最小控制平面

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

## 9. 公开文件与内部文件

正常用户可见输出：

```text
output/
└── subtitles.srt
```

内部实现可以拥有项目数据库和受管 Artifact Store，但其目录布局在 v0.1 中不是公开 API。用户可以备份或复制它们，正常产品界面不提供历史 Artifact 选择。
