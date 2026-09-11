# CueFlow v0.5.4 Reference Inputs

用户只提交本地/上传文件，不提交 URL。Reference 成员、顺序和原始字节在 Run 创建时固定。
关键词最多 100 个，只裁剪首尾空白、拒绝空串并 exact 去重，保持 Unicode、大小写和标点。
关键词直接进入 ASR/Correction；Reference 仅用于 Correction，不构造词库、摘要或强制替换表。

| 文件 | 准备方式 | Correction 输入 |
|---|---|---|
| TXT/MD/CSV/JSON | 原件保存 TOS，UTF-8-sig 读取，正文保留 | 内联原文 |
| PDF | 原件保存 TOS，检查基本完整性 | 本次调用生成的 PDF URL |
| PNG/JPG/JPEG/WebP | 原件保存 TOS | 本次调用生成的 image URL |
| DOC/DOCX/PPT/PPTX/XLS/XLSX | 可选 LibreOffice headless 转 PDF | 本次调用生成的 PDF URL |

Office 各次转换使用独立临时目录与 UserInstallation，默认超时 120 秒。
损坏、密码保护、不支持、缺少转换器、超时、无输出或 PDF 基本完整性检查失败，都记录
reference_unavailable warning 并排除，不阻断媒体转写。全部 Reference 不可用也允许继续。
SQLite/数据完整性/取消错误不降级为 warning。PDF 检查不是完整 PDF 渲染器或内容解析器。

失败 Office 原件保留可恢复对象；retry_run 可重新准备相同字节。恢复后有效集合扩大，
两路 Correction 与下游重算；原件不能用调用方后来修改的文件替换。
没有失败时 Reference 准备复用。签名 URL 不落 Registry/Artifact，每次真正调用模型时再生成。
无需 ProviderUpload/file_id 缓存。真实 Kimi/Qwen PDF URL 内容读取仍是发布验收门槛，
不能把模拟请求成功当作已通过真实平台验证。
