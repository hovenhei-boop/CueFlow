# CueFlow v0.5.1 Lexicon

## 1. 用户模型与边界

用户看到的流程只有：

```text
添加并提取 Reference
→ 自动出现 Suggested Terms
→ Accept / Edit & Accept / Dismiss / Block
→ Project Lexicon / Neutral / Project Blacklist
```

内部为审计和 retry 使用 `runs.kind=lexicon`，但产品不提供“构建/重建 Lexicon Run”命令。每次新增或重新提取得到新的 Evidence，系统自动做增量术语发现；修改词库或 Project Blacklist 只改变本地项目状态，不重新扫描全部 Reference，也不调用模型。

v0.5.1 只构建词库。Project Lexicon 与 Official Packs 不修改现有 System/Project/Effective Glossary，不使 Source Artifact stale，不进入语义模型 prompt，也不修改 Transcript 或 SRT。

## 2. 自动发现与增量边界

Reference complete 或 partial bundle 发布后，系统读取其中全部成功 Evidence IDs，并只为没有 coverage 记录的 exact Artifact ID 建立 work item。Reference retry 继续使用原 Reference Run：旧成功 Evidence 因 ID 已覆盖而跳过，新成功 Evidence 才进入术语发现。另一次 Reference extract 产生新 Evidence，即使文本相同，也按新 Evidence 审计。

Evidence 文本按 `LEXICON_BATCH_MAX_CHARACTERS` 分为有界 batch；超长单元使用 128 字符重叠切片。一个内部 Lexicon Run 发布一个 `lexicon_input` manifest。Manifest 保存 field path、完整 Evidence offset、text hash 和 coordinates，不复制正文。每个成功 work item 发布一个 `term_candidate_set`；空候选数组同样是成功。

模型必须为每个候选返回它实际看到的 unit field path、零基半开 offset 和逐字 raw surface。客户端先校验 batch substring，再把 offset rebase 到完整 Evidence 并再次校验。不存在、越界、范围与 raw 不符、非法分类或伪造位置都会使 batch 明确失败。

每个模型 work item 最多两个 sent attempts。Preflight 失败是 `definitely_not_sent`，不占次数；`delivery_ambiguous` 占一次且禁止自动 retry。成功项永不重跑，失败项只能由显式 `lexicon suggestions retry` 最小重放。

## 3. Candidate 身份、provenance 与排序

Candidate identity 固定为：

```text
(normalization_version, NFC(term).strip())
```

不做 casefold，不折叠内部空格，不移除连字符。因此 `CueFlow`、`cueflow`、`Cue Flow` 与 `Cue-Flow` 是四个不同候选。模型建议的拼写保存在 `suggested_surface_form`，与原文 `raw_surface_form` 分开；它不能覆盖或伪装原文。

分类顺序：

1. `proper_noun`
2. `noun_or_term`
3. `verb`
4. `other`

专名 subtype 顺序为 `person`、`organization`、`location`、`event`、`project_or_program`、`product_brand_model_software`、`standard_protocol_code`、`work_or_title`、`other`。同类内按 normalized term 的 Unicode 长度降序，再按 UTF-8 binary 和 Candidate ID 排序。系统不做概率分数或不稳定“确定性评分”。

## 4. 审核与 Project Lexicon

Suggested Term 支持四个动作：

- Accept：以当前候选词面和分类创建 entry；
- Edit & Accept：用户先修改词面和/或分类，再创建 entry；
- Dismiss：结束当前审核，term 回到 Neutral；未来新 Evidence 仍可再次形成 Candidate；
- Block Temporarily / Permanently：结束当前审核并建立本项目抑制规则。

Project Lexicon 支持手工 Add、Edit、Disable、Remove 与 Block。Remove 只取消当前接受状态，使 term 进入 Neutral；Block 在同一事务中移除 entry 并建立 Project Blacklist rule。Candidate、entry 与可变 Blacklist rule 使用 revision 做显式并发检查。每次词库可见状态改变写入 decision，并发布新的不可变 `project_lexicon` revision；revision 包含全部活动 entries，包括 disabled entry 的 `enabled=false`。本版没有 alias、merge memory、replacement、实体聚类或自动同义词。

## 5. Neutral 与 Project Blacklist

Neutral 不是持久化资源表，而是一个逻辑状态：同一 exact normalized surface 当前既没有 Active Project Lexicon entry，也没有有效 Project Blacklist rule。Neutral term 可在未来新 Evidence 中再次成为 Suggested Term。

Project Blacklist 只阻止同一 exact term 出现在 Reference suggestion 和自动进入 Project Lexicon，不禁止模型或用户在 Source Transcript、Subtitle、SRT 中输出该词。它只作用于当前 Project：

- Temporary：创建或修改时必须选择 15、30、60 或 120 日；`now < expires_at` 时有效，`now >= expires_at` 时自动解除并进入 Neutral；
- Permanent：没有自动到期时间，但用户仍可主动 Unblock。

Temporary rule 可修改为另一合法期限或转为 Permanent；Permanent rule 只允许 Unblock。本版不提供 Permanent → Temporary。

同一 Project 内，同一 exact normalized surface 不能同时存在于 Active Project Lexicon 与有效 Project Blacklist。Candidate/Entry Block 必须原子完成状态转换；手工 Blacklist Add 命中 Active Entry 时失败，并要求调用方使用带 entry revision 的 Block 操作。

人工 Add、Edit & Accept 或 entry Edit 命中有效 Project Blacklist 时，第一次调用默认返回冲突。调用方必须显式选择：

- `unblock_and_add`：解除对应规则后写入；
- `cancel`：不改变任何状态。

Dismiss 和 Remove 都不表达持续抑制；只有 Block 才建立抑制规则。

## 6. Official Lexicon Packs

Official Packs 是应用级全局资源，位于：

- 设置 `CUEFLOW_DATA_HOME` 时：`$CUEFLOW_DATA_HOME/lexicon-packs`；
- Windows 默认：`%LOCALAPPDATA%/CueFlow/lexicon-packs`。

所有项目共享当前 installed set。Catalog 按领域列出 Pack；显式 `pack setup` 未传领域时默认全选，也可只选某些领域。以后 install/uninstall/update 仍是全局选择，不给 Project 设置领域、不建立 Project→Pack 绑定、不复制 Pack 到每个项目，也没有逐词 `pack select`。

Catalog entry 绑定 Pack ID、domain、numeric SemVer、local/HTTPS descriptor source 与 manifest hash。Descriptor 包含 manifest 和 terms；manifest 必须提供 schema、Pack identity、license name/URL、term count 与 terms hash。安装先完整校验 schema、identity、category/subtype、NFC+trim 后重复、license、manifest hash 和 terms hash/count，再通过应用目录独占锁、临时目录、原子 rename 与 current pointer 发布 immutable version。`repair` 只在用户显式调用时清理本存储的残留临时项并修复已安装版本。

`init`、Source Run 和普通 Reference 管理不会静默下载 Pack。v0.5.1 仅读取 installed terms 作为未来检索池，不把全部词直接塞给模型。

## 7. CLI 摘要

```text
cueflow lexicon suggestions list PROJECT
cueflow lexicon suggestions status PROJECT
cueflow lexicon suggestions retry PROJECT WORK_ITEM_ID
cueflow lexicon suggestions review PROJECT CANDIDATE_ID --action accept|edit_accept|dismiss|block_temporary|block_permanent --expected-revision N [--days 15|30|60|120]

cueflow lexicon entry list|add|edit|enable|disable|remove ...
cueflow lexicon entry block PROJECT ENTRY_ID (--temporary N | --permanent) --expected-revision N
cueflow lexicon blacklist list PROJECT [--kind all|temporary|permanent]
cueflow lexicon blacklist add PROJECT TERM (--temporary N | --permanent)
cueflow lexicon blacklist update PROJECT BLACKLIST_ID (--temporary N | --permanent) --expected-revision N
cueflow lexicon blacklist unblock PROJECT BLACKLIST_ID --expected-revision N

cueflow lexicon pack list [--catalog CATALOG]
cueflow lexicon pack setup CATALOG [--domain DOMAIN ...]
cueflow lexicon pack install [--catalog CATALOG] [--pack-id ID ... | --domain DOMAIN ...]
cueflow lexicon pack uninstall ID [ID ...]
cueflow lexicon pack update [--catalog CATALOG]
cueflow lexicon pack status
cueflow lexicon pack repair [--catalog CATALOG]
```

当前 CLI 代替尚未实现的 UI。Blacklist 冲突会以 JSON 返回 `conflicts` 与 `unblock_and_add/cancel`；调用方携带 `--blacklist-policy` 再次提交。

## 8. 后续消费契约

未来 Source 消费词库时，不能把全部已安装词包或全部项目词直接放入 prompt。消费阶段必须有明确容量上限、相关性检索和确定性顺序，只选择 enabled Project entries，并把所选 entry identity、Official Pack version、选择算法/version 与排序结果冻结为不可变 Effective Glossary Snapshot。Source Run 与 targeted retry 必须绑定原 snapshot，不能随着当前词库或 Pack 更新漂移。
