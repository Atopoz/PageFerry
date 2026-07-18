# PDF 翻译

> 状态：核心 pipeline 已接入，发布验收仍在推进

## 1. 能力边界

PageFerry 当前只翻译 PDF 中已有的原生文本层。正文、标题、表格文本、header 和 footer 都按同一套文本 contract 处理；header/footer 是模型标签，不是“页面顶部或底部就跳过”的同义词。

以下内容不进入当前 pipeline：

- 扫描件 OCR。
- 内嵌图片中的文字识别或翻译。
- 图片擦除、模糊、修补或重绘。
- 为某一份样例增加页高、坐标或固定页码启发式。

没有可见原生文本层的 PDF 以 `pdf_no_text_layer` 失败。混合文档若包含由全幅 Image XObject 构成且没有可见原生文字的扫描页，整次任务同样失败，不能把部分未翻译页面包装成完整成功。普通页面里的内嵌图片保持原样，不会被静默送入 OCR；`Tr 3/7` 或 ExtGState `ca` / `CA` 透明度隐藏的 OCR/search layer 也不会被重绘成可见文字。

## 2. Pipeline

PDF 使用独立 pipeline，处理顺序固定为：

1. 用 pikepdf 预检加密状态、页数和基本结构，并记录源文件 hash、页面几何与递归 Image XObject signature。
2. 确认 PP-DocLayoutV3 ONNX artifact 已存在且 size、SHA-256 与 manifest 一致。
3. 用 PDFium 以 144 DPI 逐页光栅化，仅作为 layout inference 输入；每页完成 inference 后立即释放，不为长文档保留整份 PIL 与 BGR 双缓存。
4. 用 ONNX Runtime CPU 执行布局检测，再由 `pdfminerex` 抽取文本、字体、坐标和阅读顺序。
5. 将文本按带结构 marker 的 chunk 有界并发翻译；候选必须完整解析且为每个源 span 提供唯一非空回填，marker 丢失、重排、嵌套、交叉、空值或 provider 失败时只 fallback 当前 chunk。
6. 字体子集和字体资源全部准备成功后才删除待替换文字；译文 stream 缺失或字体失败会中止任务，不发布空正文。随后只重写文本内容流，保留原页面几何和图片资源。
7. 重新读取临时 PDF，校验页面结构和每个递归 Image XObject 的 decoded hash，同时确认源文件 hash 未变。
8. `fsync` 临时文件后用 `os.replace` 原子发布；任何异常都删除未完成的临时结果。

布局 inference 在模型已经通过完整性检查后仍可能因单页或 session 异常失败。此时 extractor 使用无 layout 结果继续走 `pdfminerex` 文本框 fallback，并在成功结果中返回 `pdf_layout_fallback` warning。模型缺失或完整性不符则在光栅化前以稳定错误失败，不把“根本没有模型”伪装成一次普通 layout fallback。

## 3. 代码与资源边界

PDF 代码保持平铺，不建立带来源项目名称的业务子目录：

```text
backend/
  modules/pdf/                         # PageFerry PDF pipeline、layout adapter 与回写实现
  vendor/pdfminerex/                   # 有独立来源、许可证和 notices 的 vendor fork
  resources/pdf_assets/
    manifest.json                      # 模型与字体唯一 canonical manifest
    licenses/                          # 随代码发布的第三方许可证
scripts/
  sync-pdf-assets.py                   # 显式同步版本化 PDF resource pack
  sync-layout-model.py                 # 兼容旧开发命令，只选择 layout asset
```

`pdfminerex` 作为 vendor fork 管理是为了保留来源与许可证证据，不构成产品命名、runtime namespace 或额外业务层。普通 PDF 业务实现全部位于 `backend/modules/pdf/`。

## 4. Layout runtime 与模型交付

当前 baseline 是 PP-DocLayoutV3 官方 ONNX artifact，固定为 130,502,049 bytes，并由 manifest 固定 revision 与 SHA-256。adapter 只使用 `CPUExecutionProvider`：

- 输入为 800 × 800 RGB、NCHW、float32，按官方 export contract 同时传入 `im_shape` 与 `scale_factor`。
- 只读取矩形检测与候选数量 tensor，不把 instance mask 带入 PDF 文本 pipeline。
- ONNX session 为 app-scoped lazy singleton；单份文档按 `batch_size=1` 顺序推理，跨 job 使用有界 slot，默认单次 inference 最多使用 4 个 intra-op thread。
- 翻译任务 runtime 不联网，也不在后台静默下载缺失资源。

模型与字体二进制不进入 Git，也不放进 Tauri app bundle。canonical manifest、许可证和同步
contract 随代码发布；二进制安装到版本化 app-data：

```text
<app-data>/pdf/2026.07.18.2/
  layout/PP-DocLayoutV3/inference.onnx
  fonts/*.ttf
```

模型是独立 `layout` pack；字体拆成简中基础、繁台、繁港、日文、韩文、孟加拉文与高棉文
选择 group，避免为了一个目标语种下载全部 72 MiB 字体。简中基础 group 包含 Latin、简中、
粗体和数学字体，共约 22.29 MiB。标准应用更新不改写 app-data，因此这些稳定大文件不会随
每个 Tauri 更新包重复下载。

开发环境可以显式安装 layout：

```bash
uv run --directory backend python ../scripts/sync-pdf-assets.py --data-dir ../.data --pack layout
```

manifest 的默认来源是 PageFerry 自有 R2/CDN：
`https://assets.pageferry.download/pdf/2026.07.18.2/`。资源按 revision 使用不可变 object key，
全部携带一年 immutable cache header；下载后的可信边界仍是 manifest 中的 size 与 SHA-256，
不是 CDN cache 或 R2 ETag。可用 `--pack fonts-common-zh-cn` 等选择安装，也可用
`--base-url` 显式切换镜像。已有文件只有在 size 与 SHA-256 都通过时才复用，下载先进入同目录
临时文件，校验、`fsync` 后再原子替换。正式产品仍需补状态、进度、取消、重试和磁盘空间错误的
安装 API/UI；用户创建 PDF 任务前显式确认下载，不能让普通任务悄悄联网。

每个 asset 还固定 `Atopoz/PageFerry` 公开 GitHub Release 的独立 fallback URL，并记录官方
upstream。下载顺序为 R2/CDN、GitHub Release、官方 upstream；任一候选的网络、HTTP、size 或
SHA-256 校验失败后才切换，全部失败时不替换本地旧文件。这样即使 `pageferry.download` 未续费，
已经发布的客户端仍能从 GitHub 下载；官方 upstream 是第三层灾备，不替代自有版本管理。

发布时先运行 `scripts/publish-pdf-assets-github.py`，在 draft Release 内完成全量上传与 digest
核验后再公开；随后运行 `scripts/publish-pdf-assets-r2.py`。两个脚本都会先校验本地资源并复用
内容一致的同版本文件，冲突则停止；二进制先上传，许可证随后，版本化 manifest 最后上传作为
完成标志。Cloudflare 只对 `assets.pageferry.download/pdf/` 启用 cache eligibility，不开放
`r2.dev` 公共地址。

## 5. D950 对照实验

`source_D950_first10.pdf` 的前 10 页用于比较远程 V2 参考结果与本地 V3 ONNX CPU 结果。当前结论是：

| 指标                           | 结果             | 解释                                                             |
| ------------------------------ | ---------------- | ---------------------------------------------------------------- |
| 共同匹配 layout box 的平均 IoU | 0.947            | 说明两条路径在可匹配框上的几何位置接近，不代表 label 完全相同    |
| V2 `inline_formula` 误判       | 74 个            | 集中出现在第 9、10 页，各 37 个                                  |
| V3 `inline_formula` 误判       | 0 个             | V3 在该样例上消除了上述误判                                      |
| `footer` label                 | V2 为 0，V3 为 1 | 单份样例只能说明 label 边界变化，不能推出 V3 普遍产生更多 footer |

因此当前继续使用 V3，而不是因为一次 footer 分类差异退回 V2。V3 给出的 `header`、`footer` 按普通原生文本翻译；不根据 page height 改写 label，也不增加 D950 专用阈值。若扩充 corpus 后发现 V3 存在系统性回归，再用量化结果替换 baseline；runtime 不同时维护 V2/V3 双模型，也不按单文件猜测切换。

这组数据只覆盖一份 10 页文档，不是通用准确率报告。它固定的是当前 adapter 的对照基线和模型选择理由。

## 6. 稳定错误与 warning

| Code                         | 行为                                                             |
| ---------------------------- | ---------------------------------------------------------------- |
| `pdf_no_text_layer`          | 文档没有可见原生文本，或包含没有可见文字的全幅扫描页，不生成结果 |
| `pdf_encrypted`              | 输入需要密码或处于不支持的加密状态                               |
| `pdf_corrupt`                | PDF 无法安全解析、没有有效页面或结构损坏                         |
| `pdf_layout_model_missing`   | 模型缺失或完整性检查失败，任务在 inference 前停止                |
| `pdf_font_directory_missing` | 当前 PDF resource pack 的字体目录尚未安装                        |
| `pdf_font_resource_missing`  | 译文实际需要的某个字体 asset 缺失                                |
| `pdf_font_prepare_failed`    | 字体子集化或注册失败，不删除源文字、不发布结果                   |
| `pdf_unsupported`            | 输入不是受支持的 PDF                                             |
| `pdf_layout_fallback`        | warning；layout inference 失败后使用文本框 fallback，任务仍成功  |
| `pdf_chunk_fallback`         | warning；只保留失败 chunk 的原文，其余 chunk 正常翻译            |

## 7. 当前验证与剩余发布门

确定性测试已经覆盖模型输入 tensor、模型缺失、逐页 inference、layout fallback、marker 空值/嵌套/交叉后的 chunk fallback、字体与译文 stream fail-closed、混合扫描页、`Tr 3/7` 和 ExtGState alpha 隐藏的 OCR、图片 signature、源文件不变和原子落盘。D950 英译中端到端实验验证了 10 页输出可重新打开，页面几何与内嵌图片保持不变。

进入可分发版本前仍需完成：

- 扩充不同来源的 PDF golden corpus，覆盖旋转页面、竖排文字、复杂表格、字体缺失、坐标溢出和混合语言。
- 完成 PDF resource pack 的安装 API/UI、自有 CDN 地址与旧版本垃圾回收策略。
- 在目标 macOS clean-room 安装包中验证 ONNX Runtime、外置字体和模型路径。
- 固定 sidecar 打包策略，并测量冷启动、峰值内存和多 job CPU 争用。
- 对扫描件保持明确不支持；OCR 与图片翻译只能作为后续独立能力立项。
