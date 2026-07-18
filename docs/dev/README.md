# PageFerry 开发计划

> 状态：Active · 最后更新：2026-07-18

这里是当前仍在推进的产品决策、技术方案和开发计划。已经成为长期约束的规则要同步到根目录 `AGENTS.md`；阶段结束或结论失效后，删除或归档对应草稿，不能让过期方案继续冒充 contract。

## 文档索引

| 文档                                    | 内容                                                    | 状态   |
| --------------------------------------- | ------------------------------------------------------- | ------ |
| [产品范围](./product-scope.md)          | 桌面端选择、MVP 边界、预览与存储决策                    | 已确认 |
| [技术架构](./technical-architecture.md) | 本地进程、模块边界、pipeline、SQLite、ONNX 与打包方案   | Active |
| [PDF 翻译](./pdf-translation.md)        | 文本型 PDF pipeline、V3 layout 实验、错误与发布验收边界 | Active |
| [模型目录](./model-catalog.md)          | bundled baseline、显式同步与 model runtime settings     | Active |
| [开发路线](./roadmap.md)                | 文档 pipeline、桌面 runtime 与发布闭环的里程碑和验收门  | Active |

## 当前结论

- 产品形态：Tauri 桌面客户端，本地优先；Web 端不进入首版。
- 当前可用格式：DOCX、PPTX、XLSX、TXT、Markdown、原生文本型 PDF。PDF 不做 OCR 或图片翻译；没有可用文本层时返回稳定错误。
- 本地服务：Python + FastAPI；界面：React + Vite + Tailwind CSS v4；原生壳：Tauri 2，macOS 使用 native overlay titlebar 和可拖动顶部区域。
- 前端主路径：文件翻译、历史记录、模型供应商三个独立页面；文件页只显示当前 renderer session 的任务，provider 页使用二栏布局。
- Provider：首发只提供 DeepSeek、Kimi、Zhipu GLM、MiniMax、Xiaomi MiMo；API Key 行内检测只做临时 inference，显式保存才会验证并原子写入配置。首次保存默认启用当次完整 model inventory，之后可逐项关闭；provider 暂停与“移除配置”分离，前者保留 Key、inventory 和 runtime settings。用户也可手动登记默认 disabled 的 model；已有 Key 可显式幂等 sync remote extras，但不会把 manual model 标为 unavailable，也不改 enabled/default、probe 或 runtime settings。
- 模型选择：文件翻译页按 active provider 分组，只展示 enabled、available 且 probe 成功的 model，避免跨供应商扁平列表造成识别负担。
- Model runtime：每个已启用 model 可以覆盖 catalog 支持的 reasoning policy、单 job 并发和同 provider + upstream model 的应用级共享并发；两层默认值为 6 与 15。
- Pipeline 并发：DOCX、PPTX、XLSX、TXT、Markdown、PDF 使用单 job 有界滑动窗口，worker 完整收敛 provider、repair 与 fallback 后再按原 group 顺序 commit/progress；retry 必须先释放共享 slot 再 backoff。
- 数据：SQLite 只存任务、artifact 和配置元数据，并为每个 `translation_jobs` 保存创建时的格式 options snapshot；文件写入 PageFerry 专属用户数据目录，API Key 写入系统 Keychain。
- 提示词：固定翻译规则与待翻译文本分离，按稳定前缀组织请求，并记录 provider 返回的 prompt cache usage。
- Module：`backend/modules/plain_text/`、`docx/`、`pptx/`、`xlsx/`、`pdf/` 独立维护；PDF 业务代码在 `modules/pdf/` 平铺，`pdfminerex` 作为有来源与许可证记录的 vendor fork 单独管理。
- PDF Layout：PP-DocLayoutV3 通过 ONNX Runtime CPU 运行；模型由 manifest 固定，版本化模型与字体以 `assets.pageferry.download` 为主源、公开 GitHub Release 为 fallback 显式安装，runtime 不自动下载。header/footer 按普通原生文本翻译，不使用样例专属坐标启发式。
- 首版不做：预览、扫描 PDF、图像翻译、GPU、企业账号、租户、计费、PostgreSQL、Redis、Celery。
- 六种文档 pipeline、五个常驻 preset、OpenAI-compatible 自定义 provider、catalog sync 与 model runtime settings contract 正在收口；PDF 的核心路径已经通过 D950 端到端验证，R2/CDN 与 GitHub fallback 资源交付已接通，但安装 UI 与 Python sidecar 尚未打包，仍不能称为可分发版本。
