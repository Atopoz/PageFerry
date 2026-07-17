# PDF translation module

本目录直接承载 PageFerry 的原生文本型 PDF 翻译实现，不再增加来源项目命名的中间层。

职责分成两组：

- `pipeline.py`、`layout.py`、`rasterizer.py`、`errors.py` 等文件负责 PageFerry 的任务编排、
  PP-DocLayoutV3 ONNX Runtime adapter、稳定错误和原子落盘。
- `extractor.py`、`formatter.py`、`renderer.py` 等文件负责已经由 parity tests 冻结的文本
  提取与内容流回写算法。后续只能在保持回归行为的前提下分片整理，不能顺手重写。

首版只处理带原生文本层的 PDF。内嵌图片保持原样，不做 OCR、图像翻译、涂抹或重绘；
扫描型 PDF 以 `pdf_no_text_layer` 明确失败。

PP-DocLayoutV3 与 Noto 字体不从源码目录读取，也不进入应用 bundle。`assets.py` 读取
`backend/resources/pdf_assets/manifest.json`，runtime 只消费 app-data 中已校验的版本化
resource pack；下载属于显式安装流程，不发生在翻译任务内部。

`vendor/pdfminerex/` 是带独立许可证和来源记录的 fork，不属于产品模块命名空间。
