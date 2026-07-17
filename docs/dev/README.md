# PageFerry 开发计划

> 状态：Active · 最后更新：2026-07-16

这里是当前仍在推进的产品决策、技术方案和开发计划。已经成为长期约束的规则要同步到根目录 `AGENTS.md`；阶段结束或结论失效后，删除或归档对应草稿，不能让过期方案继续冒充 contract。

## 文档索引

| 文档                                    | 内容                                                    | 状态   |
| --------------------------------------- | ------------------------------------------------------- | ------ |
| [产品范围](./product-scope.md)          | 桌面端选择、MVP 边界、预览与存储决策                    | 已确认 |
| [技术架构](./technical-architecture.md) | 本地进程、模块边界、pipeline、SQLite、ONNX 与打包方案   | Active |
| [模型目录](./model-catalog.md)          | 随版本发布的 provider/model catalog 与 API Key 配置流程 | Active |
| [开发路线](./roadmap.md)                | 轻量格式 pipeline 的里程碑与验收门                     | Active |

## 当前结论

- 产品形态：Tauri 桌面客户端，本地优先；Web 端不进入首版。
- 当前可用格式：DOCX、PPTX、TXT、Markdown；界面和文件选择器不展示尚未接通的 PDF、XLSX。
- 本地服务：Python + FastAPI；界面：React + Vite + Tailwind CSS v4；原生壳：Tauri 2，macOS 使用 native overlay titlebar 和可拖动顶部区域。
- 前端主路径：文件翻译、历史记录、模型供应商三个独立页面；文件页只显示当前 renderer session 的任务，provider 页使用二栏布局。
- Provider：首发只提供 DeepSeek、Kimi、Zhipu GLM、MiniMax、Xiaomi MiMo；彩色品牌 icon、remote discovery/catalog fallback 与 enabled/default model inventory 使用明确 contract。
- 数据：SQLite 只存任务和配置元数据，并为每个 `translation_jobs` 保存创建时的格式 options snapshot；文件写入 PageFerry 专属用户数据目录，API Key 写入系统 Keychain。
- 提示词：固定翻译规则与待翻译文本分离，按稳定前缀组织请求，并记录 provider 返回的 prompt cache usage。
- Module：`backend/modules/plain_text/`、`docx/`、`pptx/` 独立维护；PPTX speaker notes 是必须完成并验收的新增能力。
- 首版不做：预览、扫描 PDF、图像翻译、GPU、企业账号、租户、计费、PostgreSQL、Redis、Celery。
- DOCX、PPTX（含 speaker notes）、TXT、Markdown pipeline、五个常驻 preset 与 OpenAI-compatible 自定义 provider contract 正在收口；Python sidecar 尚未打包，仍不能称为可分发版本。
