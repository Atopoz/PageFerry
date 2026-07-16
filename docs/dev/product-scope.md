# 产品范围

> 状态：已确认 · 目标版本：v0.1

## 1. 产品定义

PageFerry 是面向个人用户的本地文档翻译客户端：选择原文件，配置语言与模型，等待任务完成，得到一个新文件。它不是 JOTO-Translation 企业平台的缩小皮肤，而是把可复用的文档翻译能力剥离后，重新建立一套更短、更适合桌面端的产品边界。

首版的核心承诺只有三个：

1. 原文件不被覆盖。
2. 文档不上传到 PageFerry 自建服务器。
3. 输出文件尽量保留原文档的结构与版式。

## 2. 为什么先做桌面端

桌面端与当前需求更匹配：

- 文档、临时文件、OCR/ONNX 模型都在本机，避免服务端上传和存储成本。
- 可以使用系统文件选择器、Keychain、通知和“用默认应用打开结果”。
- Python pipeline 可以作为 sidecar 运行，不需要先建设多用户任务基础设施。
- 用户自己提供模型服务 API Key，PageFerry 不承担代付费和账号系统。

React 和本地 HTTP contract 仍保持独立，将来若要做 Web 版可以复用部分 UI 与 API schema；但 v0.1 不同时维护桌面与 Web 两套运行环境。

## 3. v0.1 范围

### 进入首版

- DOCX 翻译。
- PPTX 翻译。
- 原生文本型 PDF 翻译。
- 源语言自动识别和目标语言选择。
- 随应用版本发布的 provider/model catalog。
- 用户输入 API Key、连通性检测、选择默认模型。
- 本地任务状态、进度、失败原因和历史记录。
- 输出文件定位与“用系统默认应用打开”。
- macOS Apple Silicon 安装包作为第一个发布目标。

### 明确不进入首版

- 扫描件或纯图片 PDF。
- 文档内嵌图片文字翻译。
- 原项目图像翻译 pipeline。
- 基于生图模型的图片重绘方案。
- GPU、CUDA、Paddle runtime。
- DOCX/PPTX/PDF 的内置高保真预览。
- PostgreSQL、Redis、Celery、对象存储。
- 登录、组织、租户、权限、计费、审计和企业管理页面。
- 远程任务服务、云端文件同步和多人协作。

## 4. 预览与本地 Office

v0.1 砍掉内置预览，先把“文件进、文件出”做可靠。完成后由系统默认应用打开结果，用户可以使用已安装的 Word、PowerPoint、Preview、WPS 或 LibreOffice 自己查看。

原因很直接：

- DOCX 和 PPTX 没有跨平台、轻量且稳定的高保真预览组件。
- 转 PDF 会引入 LibreOffice/Office 自动化、字体和平台差异，明显扩大安装包与故障面。
- 调用用户本机 Office 可以作为未来的可选增强，例如 Windows COM 或 macOS AppleScript/JXA 导出 PDF；它不能成为核心翻译成功的前置条件，也不能假设用户一定安装了 Office。

后续若重新评估预览，优先级为：结果文件系统打开 > 低保真结构摘要 > 可选 Office 导出 > 内置完整 renderer。

## 5. 本地数据与文件边界

“存到软件所属目录”在桌面端定义为 PageFerry 专属的用户数据目录，而不是可执行文件或 `.app` 安装目录。安装目录可能只读，也可能在升级时被整体替换，不能保存用户数据。

默认位置：

| 平台 | 目录 |
| --- | --- |
| macOS | `~/Library/Application Support/PageFerry/` |
| Windows | `%LOCALAPPDATA%\PageFerry\` |
| Linux | `~/.local/share/PageFerry/` |

目录内容：

```text
PageFerry/
  pageferry.sqlite3      # 任务与配置元数据
  workspace/<job-id>/   # 可回收的任务中间文件
  outputs/<job-id>/     # 完成后的输出文件
  models/               # ONNX 模型及版本清单
  cache/                # 可安全重建的缓存
  logs/                 # 本地诊断日志，不记录文档正文和 API Key
```

其他规则：

- 源文件按只读输入处理，不复制回源路径、不原地覆盖。
- 输出先写临时文件，校验完成后再原子改名。
- SQLite 不存文档正文或二进制文件。
- API Key 进入系统 Keychain；SQLite 只保存 secret reference。
- 用户可以选择“导出到指定目录”，但内部 workspace 仍归 PageFerry 管理。

## 6. 最小用户流程

1. 首次启动选择模型服务，输入 API Key，执行检测并保存默认模型。
2. 拖入一个受支持文档。
3. 选择目标语言和模型，可选调整少量高级参数。
4. 创建任务，查看按文档阶段表达的进度和明确错误。
5. 完成后打开结果或导出到用户指定位置。

首版不塞入聊天、智能体、知识库或模型广场。那些功能与“可靠翻译一个文件”没有因果关系，只会污染主路径。

## 7. v0.1 验收标准

- 三种格式各有一组 golden corpus，覆盖表格、页眉页脚、批注/备注、复杂字体和长文本等代表性结构。
- 同一输入与固定 translator stub 能得到结构稳定、可重复比较的输出。
- 任意失败都不会损坏原文件，并能清理或标识残留 workspace。
- 应用重启后可以恢复任务历史，运行中任务会转成可解释的中断状态。
- 除用户选择的 LLM endpoint 外，核心翻译不依赖 PageFerry 远程服务。
- 在目标 macOS 机器上通过安装、首次启动、翻译、打开结果和卸载 smoke test。
