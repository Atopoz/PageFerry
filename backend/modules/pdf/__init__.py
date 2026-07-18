# ruff: noqa: RUF003 -- 中文说明保留自然标点。
"""提供原生文本型 PDF 的布局检测、翻译与结构保真回写。"""

# package import 不能顺带加载 pikepdf、pdfium、fontTools 等 native runtime；
# API 启动只需要资源状态，完整 PDF pipeline 在创建 PDF job 时再显式 import。
