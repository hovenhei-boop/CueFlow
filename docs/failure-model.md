# CueFlow v0.5.2 Failure Model

## 原则

CueFlow 不自动重放可能产生重复计费的请求，不对严格 JSON 做宽松修复，不静默替换模型，
也不把部分结果伪装为成功。Run 终态包括 `succeeded`、`needs_review`、`failed` 和
`interrupted`。

Invocation 状态：

- `created`：已落盘但尚未开始 Provider 交付；
- `sending`：交付已经开始；
- `succeeded`：Provider 结果已校验并绑定 Artifact；
- `definitely_not_sent`：凭据、client、输入或交付前链路明确失败；
- `delivery_ambiguous`：请求可能已到达，或已有远端 job 但状态无法确认；
- `explicit_failure`：Provider 明确拒绝，或响应违反本地 Contract。

崩溃恢复把遗留 `created` 收口为 `definitely_not_sent`，把遗留 `sending` 收口为
`delivery_ambiguous`，并把 Run 标记为 `interrupted`。

## Retry

Provider transport 和 TOS SDK 自动 retry 关闭。Correction 只有在模型已明确结束但严格 JSON/
schema 无效时，允许一次语义输入完全相同的原样 retry；不改模型、Prompt、References、
Keywords 或静态证据。因为 live search 在环，这次调用的互联网结果仍可能变化。

传输歧义绝不自动重放。用户可以显式执行 `cueflow retry PROJECT INVOCATION_ID`；这是一次有
重复计费风险的明确用户动作。Targeted retry 绑定原 Run、原 ordered Invocation inputs、原
idempotency key，并记录 `retry_of_invocation_id`，不读取新的 UserKeywords 或其他 run 的结果；
重发 Correction 时仍会联网，搜索结果不保证相同。

`cueflow resume PROJECT RUN_ID` 只复用该 run 的已提交 checkpoint，继续从未提交的工作；
不会自动重试 failed / ambiguous 调用。Correction 目标重试只重发失败臂，成功臂保持原样；
尚未调用的另一臂可以首次调用。GLM 定向重试只重发该窗，其他终结结果和 Correction 保留；
旧 retry ancestor、已成功调用、已经人工封存的 final 均不能再次裁决。普通 resume 不刷新
终结窗口结果或 review 队列版本。与此相反，新 `correct` 明确重新调用两个 Correction 模型。

配置/Prompt 身份漂移直接拒绝原 run 恢复，要求新 run；用户需要改变 References 时使用
`correct`。操作持有单项目 writer lock，失败即提示已有写者，不并发生成两个 final。
ATA 完成而导出未完成的 run，从 alignment checkpoint 恢复，不重复调用 ATA。

## 输入与 Provider Gate

- 源媒体 `duration >= 5h` 或 `byte_length >= 512,000,000`：付费 ASR 前 fail closed；
- 非 HTTPS PDF/Image、非 UTF-8/空文本、Office 文件、未知 Reference：本地 ContractError；
- 超过 100 个、空白-only 或非字符串 Keyword：本地 ContractError；
- GLM 合并窗超过 30s：不合并；最终 WAV 超过 30s 或 25MB：调用前失败；
- 任一 required Base/Peer ASR、Correction 或 ATA 失败：不降级到单路成功；
- malformed JSON/schema：Correction 原样 retry 一次后仍无效则该臂失败，不生成假 keep；
- 流未正常结束、没有 completion marker：delivery ambiguous，不自动重试；
- edit 无法 exact locate、定位不唯一、单臂矛盾重叠：contract review，不调用 GLM；
- 纯句法标点分歧：保持 Base 格式，不调用 GLM、不制造 review；
- 同 span 的共同 lexical projection：自动接受，不因附带标点不同丢失共同纠错；
- projection 无法可靠拆分、lexical singleton/conflict、有效的不同 span：GLM；
- 单个 GLM timeout/5xx/provider failure/返回无效：仅该窗关联项转 review，继续其他窗口；
- GLM 没有唯一 exact contextual match，或候选/位置不唯一：人工 review；
- 本地 Artifact/hash/Registry 损坏：硬失败，不能伪装成普通 GLM 不可用；
- 声学工作尚未终结、review 未清零或 final 未封存：不产生付费 ATA 调用；
- ATA word token 与 Transcript Atom 不一致：ContractError，不导出 SRT；
- QA blocked 或 identity/stale 不一致：ExportBlockedError。

Presigned media URL 默认有效 7 天，以覆盖异步轮询和显式重试窗口；每次需要时重新签发，URL
本身不持久化。若 URL 在 Provider 取回媒体前失效，上游调用明确失败。

## 已接受的残余风险

1. Qwen 与豆包可能以相同方式听错，Correction 两臂也可能都未发现；错误将随 Frozen Base
   出厂，架构无法自愈。
2. 两个 Correction 模型都使用实时互联网，可能受同一错误网页或相关搜索结果影响并提交
   相同的错误 replacement，尤其是纯音频无法区分拼写的专名。CueFlow 接受这一相关性风险。
3. 搜索证据不持久化，因此错误 agreement 无法完整重构当时的互联网证据链；retry 也不
   保证看到相同外部信息。
4. GLM 不检查双模型 agreement 或共同 keep，不能发现两臂共同误改或共同漏改。
5. GLM exact match 是保守工程门槛，不是正确性的声学证明；固定上下文、大小写以外的细微
   差异、同音异写、超长 utterance 都可能增加人工负载。不得为提高自动率添加模糊匹配。
6. PDF/Image 是 mutable URL locator，retry 期间可能过期或原地换内容，v0.5.2 不做内容
   hash 或快照。

没有用户关键词时不注入领域词库是有意的质量取舍，见 Architecture，不是运行失败。
