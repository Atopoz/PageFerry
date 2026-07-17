# LT 布局对象指令溯源指南

本文档总结了 `LTChar`、`LTTextLine` 与 `LTTextBox` 如何访问各自关联的 `BT … ET` 指令块，并给出常见的读取与回写示例。

## 1. 基础概念

- **TextBlockRecorder**：由 `PDFPageInterpreter` 在 `BT`/`ET` 之间创建，顺序记录操作符及其参数。任何 `LTChar` 只要来自某个文本块，就会持有该 recorder。
- **可读输出 vs. 原始输出**：`recorder.as_string()` 侧重可读性，会尽量把 `bytes` 解析成字符串；`recorder.as_pdf_source()`/`as_pdf_bytes()` 则保持合法 PDF 语法，可直接写回内容流。

## 2. LTChar 级别

```python
for lt_char in lt_line:
    if not isinstance(lt_char, LTChar):
        continue
    recorder = lt_char.source_text_block
    if recorder is None:
        continue
    print(lt_char.get_text(), recorder.as_pdf_source())
```

- `LTChar.source_text_block` / `get_source_text_block()`：返回 `TextBlockRecorder` 实例。
- `LTChar.source_operator` / `get_source_operator()`：等价于 `recorder.as_string()`，适合调试打印。
- 写回 PDF：优先使用 `recorder.as_pdf_source()`（字符串）或 `recorder.as_pdf_bytes()`（`bytes`）。

## 3. LTTextLine 级别

每次 `LTTextLine.add(LTChar)` 会记录“行内字符索引 + recorder”。通过 `line.get_source_ranges()` 可以拿到一组 `LTTextLineSource`：

```python
for seg in lt_line.get_source_ranges(unique=False):
    print(f"chars[{seg.start}:{seg.end}) -> id={id(seg.text_block)}")
    print(seg.text_block.as_pdf_source())

unique_segments = lt_line.get_source_ranges(unique=True)
```

- `start` / `end`：行内字符偏移（忽略 `LTAnno` 自动插入的空格/换行）。
- `unique=True`：若多段字符共享同一个 recorder，则只返回一次，便于判断一行引用了多少 `BT … ET`。
- 常见用途：调试跨 `TJ`/`Tj` 的换行、统计文本块使用情况、批量回写时维持行级顺序。

## 4. LTTextBox 级别

文本框在接收每一行时，会将行级段落映射到整个盒子的字符序列：

```python
for seg in lt_box.get_source_ranges(unique=False):
    print(f"box chars[{seg.start}:{seg.end})")
    # 直接输出 BT…ET，或落盘复用
    print(seg.text_block.as_pdf_source())

dedup = lt_box.get_source_ranges(unique=True)
```

- 盒内 `start` / `end`：以“文本框中的第 n 个 `LTChar`”为基准，保证跨行连续。
- `unique=True`：按 recorder 实例去重，可快速判断一个段落引用了哪些文本块。
- 与行级 API 组合：若需要精确位置，再回到该文本框包含的 `LTTextLine` 并使用行级 `start`/`end` 细化。

## 5. 综合示例

```python
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar, LTTextLine, LTTextBox

for page in extract_pages("sample.pdf"):
    for obj in page:
        if isinstance(obj, LTTextBox):
            print("== TextBox ==")
            for seg in obj.get_source_ranges(unique=False):
                print(f"box[{seg.start}:{seg.end})\n{seg.text_block.as_pdf_source()}\n")

        if isinstance(obj, LTTextLine):
            print("-- TextLine --", obj.get_text().strip())
            for seg in obj.get_source_ranges():
                print(f"line[{seg.start}:{seg.end}) ->" f" id={id(seg.text_block)}")

            for child in obj:
                if isinstance(child, LTChar):
                    recorder = child.source_text_block
                    if recorder:
                        print("char:", child.get_text(), recorder.as_pdf_source())
            break
    break
```

运行该脚本可以同时查看文本框、文本行以及单个字符对应的 `BT … ET` 指令串，验证溯源链条是否符合预期。

## 6. 回写建议

1. **保持原顺序**：按 `LTTextBox` → `LTTextLine` → `LTChar` 的顺序输出 recorder，可最大程度还原原始内容流。
2. **直接使用 `as_pdf_source()`**：避免对字符串再编码，免得破坏 `TJ` 数组或非 ASCII 字节；若需要 `bytes`，调用 `as_pdf_bytes()`。
3. **跨块合并**：若想将多个 `BT … ET` 合并重写，可利用行/盒子的 `unique=True` 结果，以 recorder 实例或 `as_pdf_source()` 的值作为 key 进行分组。

> 提示：若希望访问 recorder 中的原始 `(operator, args)` 序列，可遍历 `recorder._ops`（内部结构为 `List[Tuple[str, Tuple[object, ...]]]`）。如需稳定 API，可以在必要时包装一个辅助函数来返回该列表。
