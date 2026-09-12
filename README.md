# CueFlow

CueFlow v0.6.1 在 v0.5.4 字幕主链上加入完整 Phone + Password Authentication；字幕算法与
Artifact ID 保持不变。
字幕主链从双路 ASR 恢复逐字稿，再由火山 ATA 对齐并输出 SRT。
Qwen ASR 是冻结 Base，豆包 ASR 是独立 Peer。千问与 Kimi 分别返回完整纠错文稿；
本地接受一致修改和单路修改，仅将两路修改不同的区间交给 GLM 在已有候选中选择。
GLM 使用单独提示词，允许按需联网，不能生成第三种文字。

```text
Source Media → MediaProbe → Frozen TimelineAudio → one TOS MediaObject
  ├─ Qwen ASR → Frozen Base
  └─ Doubao ASR → Peer
FULL Base + FULL Peer + References + UserKeywords + mechanical differences
  ├─ Qwen correction → corrected_text ┐
  └─ Kimi correction → corrected_text ┘ parallel requests; one database writer
                 ↓
           exact local merge
  ├─ same change / singleton → accept
  ├─ both keep → frozen Base
  └─ different changes → GLM candidate selection → existing text / KEEP
                 ↓
        sealed final, review cleared
                 ↓
          ATA (same MediaObject) → full raw response → utterances + diagnostics → SRT
```

GLM 只读取争议的文本候选及上下文，不读取关键词、辅助材料或音频。
默认前后各 400 个 Unicode 字符，可向外扩展到句界、每侧最多 500；每批最多 8 项。
原始 Base、Peer、千问全文、Kimi 全文均保留，候选按精确文字去重并保存来源。
KEEP 不是已验证真值；封存也不表示已经人工听音确认。

ATA 是字幕分句、文字与时间轴的唯一 authority。CueFlow 原样提交封存全文，以
sta_punc_mode=3 请求保留标点；接收 utterances 的句子文字和起止毫秒，按原序机械写 SRT。
不读取 words、不映射、不插值、不重新分句或美化文字。完整原始返回与规范化结果均落盘。

utterance_count、文字拼接比对、空文本、负时间、反向/零时长、重叠计数只是 diagnostics，
不产生质量 gate。导出只校验 current/sealed/stale 等状态真实性，并在负时间或 start>end
时抛 SrtSerializationError；重叠、零时长、空文本、空数组、长句和文字差异仍允许导出。
本地导出失败后可 resume 已完成的 ATA 结果，不重复调用；非法时间不会被本地修正。

## 安装

需要 Python 3.10+、`ffmpeg` 和 `ffprobe`：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[cloud,dev]"
```

运行时凭据：

- Qwen ASR / Qwen Max：`DASHSCOPE_API_KEY`，Correction 另需 `DASHSCOPE_BASE_URL`；
- 豆包 ASR：`DOUBAO_API_KEY`，或 `DOUBAO_APP_KEY` + `DOUBAO_ACCESS_KEY`；
- GLM 选择器：`ZHIPU_API_KEY`；可用 `ZHIPU_BASE_URL` 覆盖官方对话 API 地址；
- Kimi K3：`MOONSHOT_API_KEY` + `MOONSHOT_BASE_URL`；
- 火山 ATA：`VOLCENGINE_ATA_APPID` + `VOLCENGINE_ATA_ACCESS_TOKEN`；
- 火山 TOS：`TOS_ENDPOINT`、`TOS_REGION`、`TOS_BUCKET`、`TOS_ACCESS_KEY`、
  `TOS_SECRET_KEY`。

`CUEFLOW_FFMPEG` 与 `CUEFLOW_FFPROBE` 可以覆盖可执行文件路径。客户端不安装 PyTorch、
CUDA、本地 ASR 或本地 Forced Aligner。

## Core 入口

```powershell
cueflow init WORKSPACE
cueflow project-create WORKSPACE "Sony A6700"
cueflow run WORKSPACE MEDIA --reference notes.md --reference manual.docx --keyword "Sony A6700"
cueflow status WORKSPACE RUN_ID
cueflow retry-run WORKSPACE RUN_ID
cueflow retry-invocation WORKSPACE INVOCATION_ID
cueflow resume WORKSPACE RUN_ID
cueflow cancel WORKSPACE RUN_ID --round 1
```

Project 可选，使用 `--project PROJECT_ID` 将 Run 加入项目。每个 Run 拥有独立执行目录和锁。
媒体、Reference、Keywords 在创建时固定；更换任何输入都必须创建新 Run。
`retry-run` 优先恢复失败/降级节点及其下游；无失败时重跑两路 Correction 与后半段。
同 Run 复用成功 ASR，跨 Run 不复用。

Reference 只接受文件。TXT/MD/CSV/JSON 保留原始 UTF-8 文本；PDF/图片保存在 TOS，按调用生成 URL；
Office 通过可选的 LibreOffice headless 转 PDF。无法准备的 Reference 记录 warning、排除并继续；
SQLite/完整性/取消错误不会被降级吞掉。失败的 Office 原件保留可恢复对象。

媒体沿用现有 preparation，标准为 16 kHz mono PCM WAV；TOS 保存与 TimelineAudio 相同的字节。
本地持久执行区保存 artifacts、blobs、checkpoint 和完整 ATA raw。Core 不删除调用方的媒体原文件。
服务器上传适配器可在标准媒体持久化后删除自己拥有的原视频临时文件。

结果契约是 `contract_version: "1.0"`，状态统一使用 `succeeded`。
每轮输出位于 `WORKSPACE/runs/RUN_ID/attempts/N/final.srt` 与 `result.json`，成功输出同时持久化 TOS；
Run 根目录 `result.json` 是当前轮次投影。失败轮次不会暴露上一轮 SRT 作为自己的成功结果。

review 文件必须包含 run_id、expected_review_queue_artifact_id 和完整 decisions[]；
`cueflow review WORKSPACE RUN_ID decisions.json` 提交决策。允许 keep/qwen/kimi/peer/replace，
replace 仅提供 replacement，区间取自已冻结 ReviewItem。未清零 review 不调用 ATA。

Python 接口见 `cueflow.api.Workspace`。`run()` 是同步便捷入口；需要 Worker 调度时，使用
`create_run()` 获得 queued handle，再调用 `execute_run(run_id)`。`retry_run()` 会直接执行新轮次。
`get_result(run_id, execution_round=N)` 可读取历史轮次，`events` 命令可读取持久进度事件。
取消是协作式停止，不保证撤销远端任务或免除费用。未知 usage 始终为 null。

## Account 与 Authentication

v0.6.1 中一个正式 User 必须同时具有 active E.164 phone identity、Argon2id password
credential 和长期 Phone Reputation。手机号是主登录标识但不是数据库主键；密码和短信只是
两种认证方法。Account 数据库由服务器使用绝对路径显式注入，不属于任何 Workspace：

```python
from pathlib import Path

from cueflow.account import AccountService
from cueflow.account_migrations import migrate_account_database
from cueflow.account_store import AccountStore

database = Path("D:/cueflow-data/account.sqlite3")
backups = Path("D:/cueflow-data/account-backups")
migrate_account_database(database, backups)
accounts = AccountService(AccountStore(database))
```

Migration 002 对非空 v1 开发库明确拒绝并保留已校验备份；空库可以升级。Session 支持 30 天
滑动 Refresh、180 天 Family 绝对上限、reuse 撤销和最多 5 个 active families。数据库只保存
密码 PHC 与 token HMAC digest，不保存 raw password/token/code。

Session 有效性及 180 天边界只有一份 Account/Auth 共用的权威实现。Phone Reputation service
构造时自动校验数据库 key ids；正常按手机号查询只尝试 current/compatible stable keys，跨 key
重复命中会 fail closed。单手机号 deep validation 批量读取并重放该号码自己的事件历史，不扫描
全体手机号。仅当事务准备创建一条全新 reputation 时，才强制绕过启动缓存再次检查数据库 key
ids，并在 INSERT 前按 known keys 二次确认，防止新旧进程并存时绕过手机号封禁。

每个正式使用过的手机号拥有长期 Phone Reputation。账户注销删除 User、Identity、Password、
Session 和 Account Audit，但不会删除稳定假名化手机号、AEAD 加密 E.164 或手机号处罚账本；
这些记录不是匿名数据。Phone block 是明确公开的业务状态，会阻止认证并撤销现有 Session。

当前 Artifact Schema 为 **12.0.0**，Registry 为 **15**。非当前数据库（包括开发期 Registry 14）拒绝打开且不改写，不提供迁移。
上述“不迁移”仅适用于可重建的 Workspace Registry；Account schema 独立向前迁移。
本版提供 SmsProvider 边界和 Starlette ASGI Auth 接口，但不绑定短信厂商或 ASGI Server；仍不
包含 OAuth callback、支付、RBAC 或分布式 Worker。内置 password blocklist 仅有 3 条机制验证用
seed 数据，不是完整 NIST compromised-password blocklist。`starlette<1` 是有意的兼容上界；
升级 Starlette major version 或 httpx 2.x 必须重跑 Auth HTTP compatibility tests。
真实 Provider/TOS 与长媒体发布验收仍须单独通过。

完整边界见 [v0.6.1 Authentication 冻结设计](docs/v0.6.1-phone-password-authentication-design.md)、
[0.6.0 Account Core 设计](docs/v0.6.0-account-core-design.md)、
[0.5.4 字幕主链设计](docs/v0.5.4-design.md)、[Architecture](docs/architecture.md)、
[Reference Inputs](docs/reference-inputs.md)、[Schema Contracts](docs/schema-contracts.md)、
[Failure Model](docs/failure-model.md) 与 [Roadmap](docs/roadmap.md)。
