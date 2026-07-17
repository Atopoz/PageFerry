# pdfminerex 来源与改动链

`pdfminerex` 是 PageFerry 为 PDF 内容流追踪而保留的独立 vendor fork，不是一个
从 PyPI 安装的同名 package。

## 上游基线

- 项目：`pdfminer.six`
- 版本：`20250506`
- 上游仓库：<https://github.com/pdfminer/pdfminer.six>
- 上游许可证：MIT，见同目录 `LICENSE`

## JOTO 迁移链

1. `JOTO-Code/pdf-translate@d78fad2` 引入 `pdfminer-ex`，当时 lock 固定
   `pdfminer-six==20250506`。
2. `JOTO-Code/pdf-translate@33205e6` 将 fork 调整并命名为 `pdfminerex`。
3. `JOTO-Code/JOTO-Translation@a093c2cc135a938f6093e9d28eaa8796345476cf`
   将 fork 迁入 JOTO-Translation。
4. PageFerry 从
   `JOTO-Code/JOTO-Translation@e555aa15956b3ef15ff7b75d811a7820fe1de92f`
   导入当前快照。

fork 的核心行为是追踪 PDF `BT ... ET` 原始文本操作符及 Form XObject 路径，并把
来源信息传递到 `LTChar`、`LTTextLine`、`LTTextBox`，让 renderer 能只移除和回放
目标文本而不破坏页面中的图片与其他绘制对象。具体设计见
`doc/MODIFICATIONS.md` 与 `doc/SOURCE_TRACING.md`。

PageFerry 的适配把内部 import 改为 `vendor.pdfminerex` 相对路径，修复
`_saslprep.py` 对旧 `pdfminer` namespace 的残留引用，并让 interpreter 传播
ExtGState 的 `ca` / `CA` alpha，防止透明 OCR search layer 被 renderer 重绘成可见文字。
业务层不得把文件处理、layout 或翻译规则继续塞进本目录。
