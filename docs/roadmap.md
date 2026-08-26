# CueFlow Roadmap

## v0.3.0 当前能力

Source 主链提供本地媒体准备、远端语义转写与纠错、本地 Forced Alignment、字幕切分、QA 和 SRT 导出。

Reference 旁路提供登记/relocate、确定性文档和字幕提取、远端 ASR/Vision/Document Parse、provenance、独立 Run/outcome、work-item retry 和 bundle。Evidence 不自动进入 Source 主链。

## 待独立设计

术语/词库、terminology mining、candidate、acceptance、证据融合、Reference Evidence 的产品化消费、PPT/板书/屏幕画面补充抽取和 UI 均不在当前实现内。

Forced Aligner 的部署与依赖策略需另行实验和批准；当前使用本地实现。

### CueFlow Server 接入与执行 provenance

未来正式客户端通过自有 CueFlow Server 使用远端语义与 Reference 能力，不直接接入阿里云。上游 Provider 接入、模型路由、灰度与降级由服务端负责。

Server 响应必须携带本次产物实际执行的模型及 revision 标识，客户端必须将实际值持久化到对应 Invocation / Artifact provenance。请求模型名、客户端常量或路由别名不能替代实际执行标识；请求值如需保留，应与执行事实区分。实际标识缺失时不得回填请求值冒充实际值，具体契约校验与失败行为在 Server 设计中确定。

验收需覆盖请求模型与实际执行模型不同的场景，包括换模、灰度和降级，确认客户端记录的是实际执行值。本项为后续 Server 契约待办，不修改当前直连适配器、run config 或 schema，也不决定 Forced Aligner 的部署位置。
