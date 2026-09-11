# CueFlow v0.5.4 Failure Model

## 状态与恢复

Run 执行终止状态为 succeeded、failed、cancelled、interrupted；needs_review 为等待审核。
interrupted 表示异常中断，result.error.code 为 interrupted；调用方不得解析错误文本。
Invocation 状态：

- created：已落盘，尚未开始 Provider 交付；
- sending：可能已经交付；
- succeeded：该远端步骤结果、invocation 和 checkpoint 已原子提交；ATA 此处指完整原始响应；
- definitely_not_sent：凭据或客户端等交付前条件明确不可用；
- delivery_ambiguous：网络、流或进程中断，无法证明是否完成；
- explicit_failure：Provider 明确拒绝，或完成的响应违反输出契约。

崩溃恢复将遗留 created 收为 definitely_not_sent，sending 收为 delivery_ambiguous，run 标为
interrupted。磁盘上的未引用结果文件不能授权重发，也不能冒充成功断点。

## 重试

SDK 自动重试关闭。Qwen/Kimi 纠错与 GLM 选择只有在响应明确结束、但严格 JSON 或输出契约
无效时，才由编排层自动原样重试一次。两个请求分别保存 invocation、response ID、usage 和
retry ancestry；失败的首次付费请求不会从成本记录中消失。输入、模型、提示词和参数保持不变。
实时搜索结果可能变化，因此原样请求不代表整个互联网证据链可重放。

没有 completion marker、网络交付不明、timeout 不自动重试。用户显式 retry 只重发指定失败
invocation，复用原 ordered inputs、prompt hash、模型和 idempotency key。已经成功的臂或批次
不重跑；恢复不从最新 current pointer 猜测旧输入。旧 retry ancestor、成功结果和人工封存后的
GLM 决定拒绝再裁决。原 run 的配置或提示词变化也会在付费调用前拒绝恢复。

Qwen/Doubao ASR 在 submit 成功并取得 task/request ID 后立即建立 Provider metadata。
后续 query、结果下载或解析失败仍保留该 ID；query timeout 是已提交后的
delivery_ambiguous，不是 definitely_not_sent。

普通 resume 继续从未调用的阶段，并复用已提交 checkpoint，不刷新终结 review 队列。
retry_run 增加轮次，优先恢复失败/降级依赖；无失败时重跑 Correction 后半段。输入不能替换。
ATA 已提交后恢复导出，不重复调用 ATA。单 Run writer lock 排斥其他写者。

两路纠错并行执行网络请求，主线程单独写入。一个臂失败仍收集并保存另一个已完成结果；
required arm 不全时不生成合并稿。GLM 单批失败只让该批 case 进入 review，其他批次继续。

## Gate

- 源媒体 duration >= 5h 或 byte_length >= 512,000,000：ASR 前拒绝。
- 非 HTTPS PDF/Image、Office、非 UTF-8/空文本或无效关键词：输入契约错误。
- 任一 required Base、Peer、纠错臂、ATA 失败：不降级为单路成功。
- BaseTranscript 非空时 corrected_text 为空：已完成但输出契约无效，不当作删除全文。
- 纠错全文必须能从精确 diff 完整重建；不做 Unicode/大小写等价，不模糊修补。
- 单路修改和相同修改自动接受；两路修改不同才进入 GLM。
- Peer 核心边界不可映射：Peer 选项缺席，不截取猜测的文字补齐四选一。
- 超过完整 case 输入预算：整项 review，不截断目标，不把大改动直接判错。
- GLM 无效、缺项、重复或未知候选：失败；不伪造 KEEP 或部分有效的批次结果。
- GLM 返回合法 KEEP：正常决策，保留 Base 并记录；不视为系统失败。
- Artifact/hash/Registry 损坏：硬失败，不归类为模型不确定。
- review 未清零、选择未终结或 final 未封存：不能调用 ATA。
- ATA 响应无法读为 utterances 数组，或句子的 text/整数毫秒字段缺失：本地规范化失败，
  不生成 SRT；已成功返回的完整 raw 和 task ID 保留，成功 invocation 不改判为失败。
- ATA 句级负时间、start>end：AtaResult 与 diagnostics 正常落盘，在 SRT serializer 阶段
  抛 SrtSerializationError，Run failed；不进入 needs_review，不修正原始时间。
- ATA 重叠、时间倒序、超媒体范围、零时长、空 text、空数组、单句或超长句、文字差异：
  正常序列化。收据只记录，不产生 warning 级别、质量评分或 gate。
- 非当前 Run、stale、未封存、依赖/在盘 hash 损坏或 raw blob 不存在：工程一致性错误，
  拒绝导出。不会拿旧 Run 的字幕替代本次结果。
- ATA raw/normalized checkpoint 提交后的本地失败：resume 复用结果，不重新调用 ATA。
  确定性的字段缺失或非法 SRT 时间在 resume 后仍失败；不能用无限付费重试掩盖它。

## 质量边界

Base 和 Peer 都可能错，两路纠错可能共享同一错误来源。一致或单路修改也可能误改；GLM
只检查分歧，不检查自动接受的修改，也不能从四个都错的候选中创造正确答案。文本语境和
搜索只能帮助选择，不能证明实际发音。提示词要求保留真实口误、重复、自我修正和事实错误。

选择器允许自动联网，不强制每项联网；只保存服务端实际返回的搜索 metadata，不承诺完整
搜索快照。PDF/Image locator 在重试期间可能过期或换内容。定向重试可能再次计费。
已正常完成但格式/契约无效的返回在 invocation 中保存 response ID、usage 和受控诊断；
纠错和选择器的较大原文只保存 SHA-256、字节数与 truncated 标志，不把原文拼入错误消息。
ATA 成功结果不使用该截断机制：完整正文保存到内容寻址 blob，再链接到 AtaResponse；
JSON/HTTP/query 失败尚未得到成功结果时继续使用明确错误与 task ID 诊断。

结构测试和成功生成 SRT 不是字幕准确率的证明。精度、误改率、KEEP 率、人工负担
和单位媒体时长成本须用独立音频标注与相同输入实验测量，不能从候选去重数量推断投票置信度。

## 托管恢复

queued/running/needs_review/succeeded/failed/cancelled/interrupted 全链路统一。取消请求绑定轮次，
独立连接短事务写入，不获取执行锁；cancelled 不承诺远端撤销或零费用。
远端返回 task_id 后立即提交 Registry，已知任务恢复查询原 ID；未知交付不自动重新提交。
Reference 可预期准备失败产生 warning；媒体、Registry 和完整性失败仍阻断。
结果发布失败与模型失败分开，已完成的付费结果不因投影/TOS 暂时不可用而丢弃。
详细边界见 [0.5.4 设计](v0.5.4-design.md)。
