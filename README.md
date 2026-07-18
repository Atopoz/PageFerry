<div align="center">
  <img src="frontend/src/assets/logo.svg" width="112" alt="PageFerry Logo" />

  <h1>PageFerry</h1>

  <p><strong>让文档跨越语言，也保留原来的模样。</strong></p>

  <p>
    面向个人用户的本地优先文档翻译客户端<br />
    专注 DOCX、PPTX、XLSX、TXT、Markdown 和 PDF
  </p>
</div>

---

## 关于 PageFerry

PageFerry 将文档翻译收敛为一条简单的个人工作流：选择文档、目标语言和模型，得到一份新的译文文件。

它不只关心文字是否被翻译，也尽可能保留段落、表格、幻灯片、排版和其他原始结构。文件处理在个人客户端完成，模型服务由用户自行选择和配置。

## 下载 `0.1.0-beta`

请从 [`v0.1.0-beta` 应用发布页](https://github.com/Atopoz/PageFerry/releases/tag/v0.1.0-beta) 下载安装包。`pdf-assets-*` Release 只是 PDF 运行资源 fallback，不是应用安装包。

| 系统 | 要求 | 安装包 |
| --- | --- | --- |
| macOS | macOS 14 及以上，Apple Silicon | `.dmg` |
| Windows | Windows 10/11，x64 | NSIS `.exe` |

这是未签名、未公证的公开测试版。macOS 首次打开若被 Gatekeeper 拦截，请前往「系统设置 → 隐私与安全性」，对 PageFerry 选择「仍要打开」。Windows 可能显示 SmartScreen 提示；请核对 Release 页公布的 SHA-256 后再选择继续运行。

## 当前能力

- 支持 DOCX、PPTX、XLSX、TXT、Markdown 与原生文本型 PDF。
- DOCX 和 PDF 可在同一次翻译中同时产出标准译文与双语版。PDF 双语版按页左原文、右译文拼接，不使用页内双语重排。
- PDF 布局检测在本机使用 PP-DocLayoutV3 ONNX Runtime CPU；首次使用 PDF 时，由用户确认下载约 146.7 MiB 模型与字体资源。
- 原文始终只读，结果写入新文件；API Key 保存在系统 Keychain / Credential Manager，不写入 SQLite。

## 已知限制

- 不支持扫描型 PDF、OCR、图片文字翻译或图片重绘。
- 调用用户配置的模型服务时，待翻译文本会发送到对应 provider；PageFerry 本身不提供账号或中转服务。
- beta 版尚未完成 Apple Developer ID、notarization 或 Windows code signing。

## 开发

仓库使用 React / Vite 界面、Python / FastAPI sidecar 与 Tauri 2 原生壳。完整的本地验证可运行：

```bash
make setup
make check
```

架构、产品边界和 PDF pipeline contract 见 [`docs/dev/`](docs/dev/README.md)。

## License

[Apache License 2.0](LICENSE)
