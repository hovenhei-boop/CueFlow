# CueFlow v0.4.1 Failure Model

## 1. 原则

不掩盖 structural error，不删除或修改外部源，不猜测 retry 输入，不自动重试 delivery ambiguous 请求，不伪造 confidence，不让新 Run 自动沿用其他 Run 的结果，不让 QA 修改 Transcript，也不生成与实际人声不一致的 SRT。

## 2. Source failure

新 Run 或重新 Media Prep 时，SourceAsset locator 不存在、不是普通文件或不可读取，抛 `SourceMissingError`。系统不比较外部 Source 内容 hash/长度，不猜路径、不搜索同名文件、不 relink，也不从项目内备用媒体恢复；原 locator 上覆盖后的同名文件可用于新 Run。

若原 Run 已完成 Media Prep，targeted retry 使用 Invocation 绑定的项目内 Artifact 和 blob，不因外部源之后缺失而重新做 Media Prep。

## 3. Media timing failure

Opening analysis 与全文件 continuity scan 都读取原媒体。Evidence 可靠时任何非零 offset 都量化为 16kHz sample correction；offset 为零记录 origin unchanged；timestamp 缺失或矛盾时记录 origin unverified；中段 gap/backward jump/discontinuity 无法确定性修复时 `timeline_status = unverified`。

Unverified 是非阻塞 warning。Render 执行已记录 action，不猜补偿。未知 action、缺少 origin/duration action、负 sample count或 Timeline Audio 格式/sample length 不符是 ContractError。全文件 scan 必须流式处理；ffprobe 失败、输出无法解析或没有可用 packet 时不得伪装 normal。

## 4. Artifact publication 与恢复

Artifact 发布顺序：临时写入、flush/fsync、原子替换到内容路径、read-back validation、SQLite 事务登记 Artifact/dependency/current pointer。中途崩溃最多留下 temp/orphan；现有 current 状态保持有效。

恢复只信任已登记、文件/hash 完整、Schema 可解释且依赖匹配的 Artifact。不同 Run 的 Artifact 即使内容相同，也不能使新 Run 跳过 Provider/阶段执行。

## 5. Provider 与 Invocation

每次 operation 在发送前创建 Invocation 和 ordered InvocationInputs。状态流为：

```text
created → sending → succeeded
                  → definitely_not_sent
                  → delivery_ambiguous
                  → explicit_failure
```

凭据/依赖/模型在发送前不可用时为 definitely_not_sent。请求可能已送达但无确定结果时为 delivery_ambiguous，禁止自动重试。Provider 明确错误或非法契约为 explicit_failure。每个阶段在 finally 中关闭已创建 Provider；阶段无工作时不实例化。

真正开始 `run`/`retry` 时执行单 Orchestrator recovery：遗留 `created` 原子收口为 `definitely_not_sent`，遗留 `sending` 原子收口为 `delivery_ambiguous`，对应 `running` Run 收口为 `interrupted`。本次 Ctrl+C 使 Run 进入 `interrupted`，其他未预期异常进入 `failed`，并在同一事务中收口对应 in-flight Invocation。普通项目打开、`status`、glossary 和 asset 操作不恢复或中断 Run。

## 6. Semantic budget 与 retry reset

新 Run 每 Chunk 的 window 0 最多 4 个 Semantic Attempt。进入 sending 即消耗 slot；definitely_not_sent 不消耗。成功 Attempt 产生 Transcript；失败/ambiguous Invocation 仍保留审计。

只有用户执行 `cueflow retry INVOCATION_ID` 才能 reset 目标 Chunk：第一次创建 window 1，第二次创建 window 2，第三次拒绝；两次上限是固定 SQLite 数据模型约束，不是 Ruleset 配置。每个 window 最多 4 个已发送 Attempt，同一 Run/Chunk总量最多 12。Reset 持久化并绑定触发 Invocation，自动返工不能 reset，retry 不创建隐藏 Run。

## 7. Semantic stability failure

Glossary conflict 与 Provider uncertainty 只能请求重听当前 Chunk。每个成功 Attempt 创建新 Transcript/Invocation，并对当前 Attempt 的完整 Chunk 文本重新执行 conflict scan；下一轮以当前实际 conflict 为准。Rejected Transcript 不进入 Alignment。连续两次稳定但仍冲突时接受实际结果并产生 stable warning；4-attempt window 用尽仍不稳定时产生 unstable warning并接受最后实际结果。Glossary 不覆盖文字，QA 不额外重扫整篇 Transcript。

## 8. Alignment execution repair

Alignment Stage 只处理 accepted Transcript。一次执行返回未对齐 Atom、token mismatch、非法或越界 timestamp 时，允许最多一次 structural repair。第二次仍非法则阶段失败并禁止 Subtitle/SRT。ProviderUnavailable 与 delivery ambiguous 按 Invocation failure 处理，不伪装为结构修复成功。

## 9. QA Alignment Repair Wave

QA 发现 alignment-related blocking issues 时，可独立执行最多一个 Wave：汇总全部受影响 Chunk、一次加载 Aligner、批量重建、切换 current、重建 Subtitle、重跑 QA。Wave 与 execution repair 不共享预算。第二轮仍 blocked 时失败。Wave 不建独立表，通过本 Run 的 `qa_alignment_repair` Invocations 审计。

## 10. Export gate

缺少 Chunk Transcript/Alignment、精确 Artifact identity 不一致、未完整对齐、Cue 时间非法、Subtitle inputs 不一致、QA subject/dependency 不一致、blocked QA、Envelope/Registry dependency 不一致均阻塞 Export。

Glossary stability、Provider uncertainty、Chunk 边界疑似重复、保护单元超限和 timeline unverified 是 warning。SrtRender Artifact 成功发布后才原子替换 `output/subtitles.srt`。

## 11. Targeted retry 与 Run reopen

只有 definitely_not_sent、delivery_ambiguous 或 explicit_failure Invocation 可显式 retry。原 Run 必须为 failed 或 interrupted；系统将同一 Run reopen 为 running。

Retry 读取原 InvocationInputs，重放相同 operation，使用同一 Run 已成功且 exact upstream 匹配的其他 Chunk Artifact，处理尚未完成的必要阶段，重建必要全局下游，再次进入 succeeded/failed。它不读取外部源重建已有 MediaChunk，不使用其他 Run 输出，也不重跑无关成功 Chunk。

## 12. 必测失败路径

Source missing/unreadable 与同名覆盖；不兼容 Registry version rejection；Invocation `created`/`sending` crash recovery；opening timestamp 缺失、AAC priming、negative PTS、edit-list；中段 discontinuity；unknown action；4-attempt window、两次 reset和12次硬上限；新 glossary conflict；rejected Transcript 无 Alignment；两个独立 repair 预算；多 Chunk QA batch；targeted retry exact inputs/Run reopen；structural QA 阻止 SRT；Artifact/Registry dependency/current pointer 不完整。

Reference 必测：filename identity/duplicate no-op；relocate 只匹配直接子项；格式/signature 损坏和加密；无字幕时缺少 pixel mode；不支持字幕 codec 不降级；无音频的 success/partial/failed 聚合；PGS/VobSub clear/empty、原始像素去重和全部 occurrence；独立有效 VobSub 与异常时长 stop gate；mixed PDF 整体 Document Parse；Cloud document 身份/权限/格式区分与 finally delete；本地测量/Provider usage 双时长不混用；两个 sent attempt；retry 不建新 Run且不重跑成功项；Reference status 不覆盖 Source。

## 13. Reference failure 与 partial

Reference locator 缺失明确报错并要求用户显式 relocate；系统不猜测、不搜索、不从 Source 恢复。损坏、加密、无法可靠识别、不支持的字幕 codec，以及所需 Provider 凭据或依赖缺失均明确失败，不静默选择其他路线。

Reference work item 相互隔离。全成功使 Run `succeeded/complete`；至少一个成功与至少一个失败使 Run `failed/partial` 并发布 partial bundle；没有成功使 Run `failed/failed`。单资产失败不改变其他资产的 Run。

只有真实模型调用创建 Invocation。每个模型 work item 每 Run 最多两个 sent attempt；`definitely_not_sent` 不占，`delivery_ambiguous` 占且不会自动重放。显式 `reference retry WORK_ITEM_ID` 只在原 Run 追加 attempt；成功项不重跑。不能通过质量参数无限返工。

Cloud document 在上传得到 file_id 后，无论 poll、Provider、响应解析或本地流程如何结束都在 finally 请求删除。身份 401、权限 403、格式/provider 400/415/422 分别报告；删除未确认是独立 cleanup failure。Provider 未返回 usage/cost 时存 null。

Source recovery 只由 Source run/retry 触发；Reference recovery 只由 Reference extract/retry 触发。add、relocate、status 和打开项目不会改变 running Run。

## 14. Lexicon failure、retry 与抑制冲突

Lexicon model work item 每个最多两个 sent attempt；`definitely_not_sent` 不占次数，`delivery_ambiguous` 占一次且不自动 retry。成功 work item 永不重跑；空 candidate array 是成功。无法重新绑定到本次 batch 和完整 Evidence 的 field path/offset、raw substring 不匹配、非法分类或响应结构均为 explicit failure，不接受模型臆造的 provenance。

Reference complete/partial bundle 已发布后才触发 Lexicon，因此 Lexicon 失败不能回滚或掩盖 Reference 成功 Evidence。Lexicon Run complete/partial/failed 独立聚合；显式 suggestion retry 留在原 Run，只重放目标失败 batch。Lexicon recovery 只处理 `kind=lexicon`，不改变 Source/Reference Run。

Project Blacklist 冲突不是自动失败恢复。人工写入命中有效规则时默认抛 `SuppressionConflictError`，结构化返回 normalized term、冲突种类与 `unblock_and_add/cancel` 两个选择。只有调用方再次显式给出选择才变更状态。Entry Block 在一个事务中移出 Active Project Lexicon 并建立规则；revision 冲突、非法期限或任一写入失败都不得留下半状态。

Official Pack 安装在应用数据目录加独占锁，下载/读取到临时位置，完成 schema、identity、license、manifest hash、terms hash/count 校验后原子 rename 并更新 current pointer。失败保留旧 current；`repair` 只在用户显式调用时清理自身临时项并按已持久化 catalog 修复当前版本。运行 Source 或初始化 Project 不隐式下载 Pack。
