# CueFlow v0.5.4 Architecture

## 主链

源媒体 → MediaProbe → Frozen TimelineAudio → 单个 TOS MediaObject → 两路全文 ASR → 两路全文纠错 →
本地合并 → 按需 GLM 候选选择 → sealed Transcript → ATA 原始响应 →
规范化 utterances + diagnostics → SRT serializer。

orchestrator.py 编排业务步骤，run_runtime.py 管理 invocation、断点、原子提交与定向重试。
text_diff.py 负责精确文本映射，conflict_selection.py 负责合并、候选、上下文、批次和选择校验。
云端请求使用现有 OpenAI compatible SDK，不增加工作流框架或模型 SDK。

## 保持的底座

- TimelineAudio 为 16kHz mono PCM s16le，sample 0 对应 presentation timeline 0。
- 源媒体在付费 ASR 前必须满足 duration < 5h、byte_length < 512,000,000。
- Qwen、豆包和 ATA 共用由同一 Frozen TimelineAudio 上传一次得到的 MediaObject；
  领域不变量是同一云对象与完全相同的字节，不是各 Provider 使用的签名 URL 字符串相等。
  两路 ASR 使用同序冻结 UserKeywords；没有切块 fallback。
- UserKeywords 是 ASR 唯一领域先验：最多 100，trim、拒绝空串、exact 去重，保持顺序。
  References、其他 ASR 输出、纠错结果和搜索发现的术语均不回灌 ASR。
- References 从 Run 捕获的文件准备；文本保留 UTF-8 正文，PDF/Image 使用 TOS 对象身份，Office 可选转 PDF。
- Presigned URL 只在需要调用时生成，不进入 Artifact 或 Registry。TOS 对象保存稳定内容身份。
- 内容寻址、哈希校验、单 Run 写锁、结果与 checkpoint 原子发布、最终 SRT 原子替换保持有效。
- ATA 是字幕文字、分句及时间的唯一 authority。CueFlow 只读取句级字段并格式化 SRT，
  不重新分句、不加工文字、不制造新时间；导出只检查状态真实性和 SRT 可表示性。

## Provider 请求

| 步骤 | 模型或协议 | 关键输入 |
| --- | --- | --- |
| Qwen ASR | qwen-audio-3.0-asr-flash-filetrans | file_urls、channel_id=[0]、inline vocabulary weight=5 |
| Doubao ASR | bigmodel / volc.seedasr.auc | URL、show_utterances=true、enable_ddc=false、inline hotwords |
| Qwen 纠错 | qwen3.8-max-2026-09-02 | temperature=0，强制 search、search_strategy=max |
| Kimi 纠错 | kimi-k3 | temperature=1，enable_search=true |
| GLM 选择 | glm-5.2 | thinking disabled、temperature=0、max_tokens=1024、JSON object、auto web_search |
| ATA | 火山 URL alignment | caption_type=speech、sta_punc_mode=3、url + audio_text |

Qwen ASR 显式发送 special_word_filter.system_reserved_filter=false，不发送 context、
vocabulary_id 或 language_hints。豆包不发送 context_type/context_data、纠错表或额外过滤字段。
ATA 使用 /api/v1/vc/ata/submit 与 /api/v1/vc/ata/query；不发送 caption_category 或 cluster。
豆包 HTTP 失败优先于业务成功 header，错误诊断继续脱敏。

两路纠错共享 transcript_recovery_fulltext_zh_v1.txt，严格只返回 {"corrected_text":"..."}。
提示词来自用户提供版本，仅修复 JSON 字段名中 Markdown 转义。输入包括完整 Base、完整 Peer、
原始 References、关键词和机械 ASR 差异。允许有依据的大段恢复，不以修改长度或比例否决候选。

GLM 使用 transcript_candidate_selection_zh_v1.txt，只接收候选文段、上下文、case ID、候选 ID
和 KEEP ID。系统提供 web_search 工具并设置 tool_choice=auto，是否实际搜索由模型决定。
服务端返回的搜索条目随 Provider metadata 保存；开启搜索不等于实际执行搜索。

## 精确合并

Base 是 Qwen ASR 原文。每份纠错全文独立与 Base 做 Unicode codepoint diff，关闭 autojunk，
并用 patches 精确重建全文。没有 NFC、casefold、拼音、编辑距离得分或语义相似阈值。

重叠或相邻修改默认合并为一个组件。仅当两个争议之间存在非空 Base 区间 `[x,y)`、
`y>x`，且 `Base[x:y]` 在 Qwen 和 Kimi 投影中均连续、完整、原样保留时，
才能以该 stable anchor 拆分组件。SequenceMatcher 的 equal opcode 本身不是领域证据。
每一路候选都投影为完整公共 Base 区间上的原始文本。

| 两路在公共区间的结果 | 行为 |
| --- | --- |
| 都等于 Base | KEEP |
| 相同且不同于 Base | 接受一致修改 |
| 仅一路不同于 Base | 接受单路修改 |
| 两路都修改且结果不同 | 构建 GLM case |

候选来自 Base、Peer、Qwen 纠错、Kimi 纠错。精确相同的文字合并为一个候选，但保留各来源。
KEEP 始终是该区间的原始 Base，不是已经应用其他修改的预览。空字符串表示合法删除，
插入的 Base 区间可以为 [p,p)。Peer 边界落在不可细分的替换内部时，Peer 候选不可用，
不靠插值补出文本。无法定位纠错核心会触发结构错误，不能以模糊定位继续。

粗差异块中若没有共同锚点，保留整个争议块。Local merge 不得把 Qwen 左半和
Kimi 右半拼成一个没有任何完整来源的 component text。GLM 仍仅能选已有候选；
人工 replacement 可以在冻结区间上提出新文本。
重复文字的定位是确定性的文本对应，不构成语音位置或语义正确性的证明。

## 上下文与选择

默认 Base 目标区间前后各 400 字，排除目标本身；向外找句界最多扩展到每侧 500。
其他来源映射同一内容区间，不能套用 Base 的数字偏移。若上下文边缘落在来源的不可分替换中，
可以扩展到最近可映射边界，核心目标保持不变；总输入预算负责控制额外长度。

每批最多 8 项，候选 JSON 最多 48,000 UTF-8 bytes，另有固定系统提示词；输出最多 1,024 tokens。
超预算的单个 case 完整转人工 review，不截断目标、不假装 KEEP，也不以跨度大判定恢复错误。
候选和版本使用不含模型名称的 ID，按稳定哈希排序；真实来源、区间和排列落盘，可精确重试。

GLM 只能返回 {"decisions":[{"case_id":"...","candidate_id":"..."}]}。
每个输入 case 恰好一次，不得遗漏、重复、新增 ID 或返回新文字。本地按候选 ID 复制冻结文本。
KEEP 决策同样保存在最终 patches 中。选择不影响目标外的自动接受修改。

两路纠错的网络请求最多并行两个；worker 不接触 SQLite 或 ArtifactStore。主线程在每个结果
完成时提交，因此另一臂失败不会抹掉已完成结果。严格 JSON 的一次原样重试由编排层发起，
每次请求有独立 invocation、usage 和 retry ancestry；网络交付不明不自动重放。

## 封存与输出

失败的 GLM 批次转局部 review，其余批次继续。人工可 keep、qwen、kimi、peer，或仅以
`review_id + action=replace + replacement` 提交新文本。服务端使用 ReviewItem 中冻结的
`[start,end)`，不搜索 original 或 source_sentence。不存在的 Peer 不能选。队列 ID 过期即拒绝，
人工决定与 final 原子发布。
review 清零且所有选择终结后才封存，并创建 dual_fulltext_selection Transcript。

## ATA 句级结果与机械导出

请求保持 sealed Transcript 原文，以 audio_text 提交，音频仍为同一个 Frozen TimelineAudio
MediaObject。sta_punc_mode=3 保留输入标点；CueFlow 不增加标点、不预分句、不改换行。
官方描述句界包括句号、问号、叹号、分号及换行/回车；不把这些规则再实现为本地分句器。
参考：[ATA API](https://www.volcengine.com/docs/6561/149749)、
[官方常见问题](https://www.volcengine.com/docs/6561/111586)。
输入输出全文相等不是官方响应的本地强制契约。

query 使用 id 和 blocking=0，code=0 表示成功、2000 表示处理中，其余为明确失败。
成功响应的完整 HTTP response.content（解码传输压缩后的正文 bytes）先保存到内容寻址 blob，
再把 ata_response、invocation success 和 run checkpoint 原子提交。raw 不经 JSON 重编码、
不截断；不主动保存请求认证头或签名 URL。服务端正文保留原字段，raw 应按敏感项目数据管理。
字段缺失等本地规范化失败也不会丢失已成功返回的 raw。

ata_result.py 只读取顶层 utterances 数组中的 text/start_time/end_time，
规范化为 text/start_ms/end_ms。text 必须是字符串，时间必须是非 bool 的整数毫秒；
空数组、空字符串、负数、零时长、反向区间均可持久化。words 字段完全不消费。
没有 tokenizer 兼容分支、字符映射、时间插值、本地重分句或内容 QA 模块。

AtaResult 绑定 Run、Transcript、TimelineAudio、MediaObject、AtaResponse；保存句子原序列和收据：
utterance_count；text_comparison（relation、input_length、output_length、length_delta）；
empty_text_count、negative_time_count、reversed_interval_count、zero_duration_count、overlap_count。
比较对象是实际提交的 audio_text 与 utterance.text 原序拼接；按 Unicode codepoint 计长度，
没有 strip、NFC 或 casefold。空文本仅指 ""，负时间按任一端点为负的句子数统计；反向为 end<start，
零时长为 end==start；overlap 统计相邻句子在 Provider 原序中的严格非空时间交集。
这些字段永不触发 failed、needs_review 或 export blocked。

规范化结果通过本地 checkpoint 持久化后才进入 export。serializer 逐句编号、格式化毫秒，
原样写入 text，不清标点、归一化空白、merge/split 或从词级结果重建。
空数组输出空文件；空 text 和零时长按原值输出。长句、长时长、重叠、句序时间倒退、
超出媒体时长、输入输出文字差异都允许导出。
只有负端点或 start>end 在 serializer 抛 SrtSerializationError；不会 clamp、排序或修正时间。

Export Gate 保留 current/stale、在盘 schema/hash、当前 Run checkpoint、sealed resolution、
实际请求输入及精确依赖边、成功 invocation 和 raw blob 存在/hash 检查，防止串 Run 或串媒体。
这些属于工程一致性，不比较 ATA 输出和 Transcript 是否相等。
原子输出前完成 serialization；失败不覆盖已有字幕文件，也不会把旧文件报告为本次成功。
ATA raw 和本地 AtaResult 已提交后，resume 只重放剩余本地步骤；不重新计费。
不可解析的成功响应在 resume 时仍明确失败，不自动发起新的 ATA 请求。

Schema 12.0.0 / Registry 15 只接受当前契约；不提供任何旧 Artifact 转换、迁移或兼容入口。
旧项目不改写、不删除，应新建项目。纠错阶段的 review/needs_review 继续存在，与 ATA 无关。
成功生成 SRT 不是字幕准确率或播放器显示质量的证明；需要独立真实音频评估。

## 托管边界

Workspace/Project/Run、execution round、失败依赖恢复、取消、TOS 发布与 orphan 清理见 [0.5.4 设计](v0.5.4-design.md)。
本地 ArtifactStore 与 TOS ObjectStorage 分开，所有执行归属绑定明确 Run。
