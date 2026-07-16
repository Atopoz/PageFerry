# PageFerry 开发计划

> 状态：Active · 最后更新：2026-07-16

这里是当前仍在推进的产品决策、技术方案和迁移计划。已经成为长期约束的规则要同步到根目录 `AGENTS.md`；阶段结束或结论失效后，删除或归档对应草稿，不能让过期方案继续冒充 contract。

## 文档索引

| 文档                                    | 内容                                                    | 状态   |
| --------------------------------------- | ------------------------------------------------------- | ------ |
| [产品范围](./product-scope.md)          | 桌面端选择、MVP 边界、预览与存储决策                    | 已确认 |
| [技术架构](./technical-architecture.md) | 本地进程、模块边界、pipeline、SQLite、ONNX 与打包方案   | Draft  |
| [模型目录](./model-catalog.md)          | 随版本发布的 provider/model catalog 与 API Key 配置流程 | Draft  |
| [迁移路线](./roadmap.md)                | 从 JOTO-Translation 剥离核心能力的里程碑和验收门        | Draft  |

## 当前结论

- 产品形态：Tauri 桌面客户端，本地优先；Web 端不进入首版。
- 核心能力：DOCX、PPTX、原生文本型 PDF 的文件进、文件出。
- 本地服务：Python + FastAPI；界面：React + Vite + Tailwind CSS v4，按需使用 shadcn/ui；原生壳：Tauri 2。
- 数据：SQLite 只存任务和配置元数据，文件写入 PageFerry 专属用户数据目录，API Key 写入系统 Keychain。
- 提示词：固定翻译规则与待翻译文本分离，按稳定前缀组织请求，并记录 provider 返回的 prompt cache usage。
- 首版不做：预览、扫描 PDF、图像翻译、GPU、企业账号、租户、计费、PostgreSQL、Redis、Celery。
- 当前骨架没有打包 Python sidecar，也没有迁入翻译 pipeline，不能称为可分发版本。
