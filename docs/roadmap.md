# CueFlow Roadmap

## v0.5.2 — Architecture migration

本版只交付并测试新的核心真相：

- Qwen/豆包 whole-file ASR 与纠错后 GLM adjudication windows；
- UserKeywords 唯一 ASR lexical prior；
- 双 Correction 全文 Base/Peer + `edits[]`、exact locator、可分离 lexical projection；
- GLM 移至纠错后 lexical 分歧，局部失败转人工；final 封存后才进入 ATA；
- Schema 7.0.0 / Registry 9、run checkpoints、原子 final/review 和定向恢复；
- TOS MediaObject、URL-only ATA、Artifact/Registry/retry 必要变更；
- 删除或断开会形成第二条运行路径的旧 Correction、Chunk 和默认 VocaSync 行为；
- 新主链的单元、集成替身和契约测试。

本版不顺手统一目录、命名、fixture、helper 或所有依赖。真实凭据环境仍需完成 Qwen
`special_word_filter` object 序列化、豆包 100 个 inline hotwords、Kimi/Qwen live search 和
ATA submit/query 响应形状的受控集成验证。

## v0.5.3 — Cleanup / consolidation

新架构完成至少一个真实视频端到端后，本版不新增功能，只做集中清理：

- 删除不可达 legacy provider/helper/config/dependency；
- 删除旧 schema/registry 和 compatibility shim；
- 文件、类、变量命名与 provider abstraction 收敛；
- 重复 fixture/逻辑整理；
- 文档、Ruff、MyPy、TODO 和依赖全量审计；
- 必要的大范围目录重组。

判断标准：旧代码若留下会改变 v0.5.2 运行行为或形成第二条路径，应在 v0.5.2 删除；只是
脏、丑、重复或不可达的内容留到 v0.5.3。

## v0.5.4～v0.5.x — Debug / calibration / stabilization

v0.5.3 后原则上不再主动大重构，重点是实际视频和边界数据：

```text
v0.5.4  real-media Debug 与校准
v0.5.5  failure / retry / 边界情况
v0.5.6  字幕质量、ATA、segmentation
v0.5.7  性能、成本、超时
v0.5.8  安装、环境、配置
v0.5.9  0.6 RC 级稳定化
```

首轮校准至少统计：

- agreement overall precision；
- lexical projection agreement precision；
- GLM-resolved disagreement precision 与自动消解覆盖率；
- 无 GLM agreement precision（本版 agreement 均不调用 GLM）；
- singleton precision；
- conflict distribution。

另记纯标点忽略数、人工负载、单窗错误率与调用成本。比较“有/无 GLM agreement”需另行
批准带标签的对照实验；本版 GLM 只处理分歧，不能用生产分组直接推断它降低了共同误改。
不在 v0.5.2 提前冻结白名单或额外付费探针。
