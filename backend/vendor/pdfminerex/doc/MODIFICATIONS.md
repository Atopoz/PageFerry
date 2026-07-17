# 源代码溯源改动说明

## 背景

为了解析 PDF 内容时能够追溯每个字符所在的完整文本指令块，对 `pdfminer.six` 的若干核心模块做了扩展，使字符对象在整个渲染链路中携带 `BT … ET` 范围内的原始指令序列。

## 改动概述

- `pdfminer/pdfinterp.py`
  - 新增 `TextBlockRecorder`，在 `BT … ET` 范围内收集并序列化操作符+参数。
  - `PDFTextState` 维护 `text_block` 引用，配合图形状态栈拷贝与恢复。
  - `PDFPageInterpreter.execute()` 在调用每个 `do_*` 前记录指令，`BT` 开启新记录器，`ET` 完成并复位。
  - `TextBlockRecorder.as_pdf_source()/as_pdf_bytes()` 提供完整的 BT…ET 序列化结果（保留原始 `bytes`，按 PDF 语法转义），用于回写内容流；`as_string()` 仍默认用于可读展示。
- `pdfminer/pdfdevice.py`
  - `PDFTextDevice.render_string` 读取当前 `text_block` 并向字符渲染链传递记录器引用。
- `pdfminer/converter.py`
  - `render_char` 将 `TextBlockRecorder` 传递给 `LTChar`，保持布局对象与文本块的关联。
- `pdfminer/layout.py`
  - `LTChar` 改为持有 `TextBlockRecorder` 引用；`source_operator`/`get_source_operator()` 动态返回完整 `BT … ET` 指令串，`__repr__` 同步展示。
  - 新增 `LTChar.source_text_block`/`get_source_text_block()`，可直接获取 recorder 实例供上层聚合使用。
  - `LTTextLine` 在 `add()` 时记录字符序号与对应的 recorder；通过 `iter_source_ranges()` 与 `get_source_ranges(unique=False)` 输出包含 `start`/`end` 范围的溯源段，`unique=True` 时按相同 `BT … ET` 去重聚合。
  - `LTTextBox` 在吸收 `LTTextLine` 时同步累积其段信息，提供等效的 `iter/get_source_ranges()` 接口；字符范围基于整箱的逻辑顺序（忽略 `LTAnno`），便于直接定位段落使用的全部 BT…ET。
- `charset_normalizer/__init__.py`
  - 添加最小化占位实现，满足示例脚本运行需求；如需完整特性，建议安装官方 `charset_normalizer` 包。

## 使用提示

- 从 `PDFPageAggregator` 或其他布局分析器获取的 `LTChar` 对象，可通过 `lt_char.source_operator`（或 `lt_char.get_source_operator()`）获得包含 `BT … ET` 的完整指令序列；序列中的参数按照 PDF 原始顺序格式化，便于还原文本对象上下文。
- 若需要在导出文本或调试日志中输出溯源信息，只需在遍历 `LTChar` 时读取该字段即可，也可根据需要再解析 `TextBlockRecorder` 中的指令。
- 遍历 `LTTextLine` 时，可调用 `line.get_source_ranges()` 获取 [(recorder, start, end)] 列表；若只关心唯一 BT…ET，可传 `unique=True`。`start`/`end` 以行内字符索引（忽略自动插入的 `LTAnno`）表示，可结合 `recorder.as_string()` 进一步定位源指令。
- 若需将操作符原封不动写回 PDF，调用 `recorder.as_pdf_source()`（或 `as_pdf_bytes()`）即可获得完整 `BT … ET` 指令串，包含合法转义后的字节序列。
- 若需要在段落/文本框层面判断引入了哪些 BT…ET，直接对 `LTTextBox` 调用 `get_source_ranges(unique=True)` 即可获得去重后的 recorder 列表；`unique=False` 时则保留整箱内的顺序与字符范围。

## 验证

- 使用 `python3 -m compileall pdfminer`，确认上述改动能够顺利编译通过。
