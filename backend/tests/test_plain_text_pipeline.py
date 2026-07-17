"""验证 TXT/Markdown runtime 的编码、结构保护与显式 fallback。"""

from collections.abc import Callable, Sequence

import pytest

from modules.plain_text import PlainTextPipeline
from modules.translation.contracts import (
    TranslationBatchItem,
    TranslationBatchResult,
    TranslationProgress,
    TranslationRequest,
)


class UpperTranslator:
    """把 segment 转成大写, 用于观察正文是否被替换。"""

    def __init__(self) -> None:
        """初始化每个 batch 收到的只读语境记录。"""

        self.read_only_contexts: list[tuple[str, ...]] = []

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """按输入 index 返回大写文本。"""

        del source_language, target_language, format_hint
        self.read_only_contexts.append(tuple(read_only_context))
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(index=index, text=text.upper())
                for index, text in enumerate(texts)
            )
        )


class BrokenTranslator:
    """返回空 batch, 模拟 provider 破坏 index contract。"""

    def translate_batch(self, **_: object) -> TranslationBatchResult:
        """故意省略所有 index。"""

        return TranslationBatchResult(items=())


class ConstantTranslator:
    """为每个 segment 返回同一候选文本, 用于验证 pipeline 的拒绝规则。"""

    def __init__(self, candidate: str) -> None:
        """保存测试所需的固定候选文本。"""

        self._candidate = candidate

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """为每个输入 index 返回固定候选文本。"""

        del source_language, target_language, format_hint, read_only_context
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(index=index, text=self._candidate)
                for index, _text in enumerate(texts)
            )
        )


class TransformTranslator:
    """记录输入并用回调生成候选文本, 便于验证 Markdown 结构边界。"""

    def __init__(self, transform: Callable[[str], str]) -> None:
        """保存转换回调并初始化输入记录。"""

        self._transform = transform
        self.seen_texts: list[str] = []

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """按输入 index 返回回调结果并记录模型实际看到的文本。"""

        del source_language, target_language, format_hint, read_only_context
        self.seen_texts.extend(texts)
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(index=index, text=self._transform(text))
                for index, text in enumerate(texts)
            )
        )


def _request(source, output_dir) -> TranslationRequest:
    """创建测试使用的固定翻译请求。"""

    return TranslationRequest(
        source_path=source,
        output_dir=output_dir,
        source_language="en",
        target_language="zh-CN",
        provider_id="stub",
        model_id="stub",
    )


def test_txt_pipeline_preserves_bom_and_crlf_and_never_overwrites_source(tmp_path) -> None:
    """TXT 输出应保留 BOM/CRLF, 且源文件字节完全不变。"""

    source = tmp_path / "sample.txt"
    original = "\ufefffirst line\r\n\r\nsecond line\r\n".encode("utf-8")
    source.write_bytes(original)

    result = PlainTextPipeline("txt", UpperTranslator()).translate(
        _request(source, tmp_path / "outputs")
    )

    assert source.read_bytes() == original
    assert result.output_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert result.output_path.read_bytes().decode("utf-8-sig") == (
        "FIRST LINE\r\n\r\nSECOND LINE\r\n"
    )
    assert result.fallback_segments == 0


def test_txt_pipeline_reports_three_stages_with_real_segment_counts(tmp_path) -> None:
    """TXT 只在 batch 完整处理后推进 segment 计数, 最后进入 formatting。"""

    source = tmp_path / "progress.txt"
    source.write_text("first paragraph\n\nsecond paragraph\n", encoding="utf-8")
    updates: list[TranslationProgress] = []

    PlainTextPipeline("txt", UpperTranslator()).translate(
        _request(source, tmp_path / "outputs"),
        report_progress=updates.append,
    )

    assert updates[0] == TranslationProgress(stage="extracting")
    assert updates[1] == TranslationProgress(stage="translating", total_segments=2)
    assert updates[-1] == TranslationProgress(
        stage="formatting",
        processed_segments=2,
        total_segments=2,
    )
    assert [update.processed_segments for update in updates if update.stage == "translating"] == [
        0,
        2,
    ]


def test_txt_pipeline_preserves_gb18030_bytes_and_crlf(tmp_path) -> None:
    """按既有 GB18030 解码顺序读取, 输出继续使用源编码与 CRLF。"""

    def prefix_translation(text: str) -> str:
        """用 GB18030 可编码前缀证明译文经过实际写回。"""

        return f"EN_{text}"

    source = tmp_path / "legacy.txt"
    original_text = "第一段。\r\n\r\n第二段。\r\n"
    source.write_bytes(original_text.encode("gb18030"))

    result = PlainTextPipeline("txt", TransformTranslator(prefix_translation)).translate(
        _request(source, tmp_path / "outputs")
    )

    assert source.read_bytes() == original_text.encode("gb18030")
    assert result.output_path.read_bytes() == (
        "EN_第一段。\r\n\r\nEN_第二段。\r\n".encode("gb18030")
    )
    assert result.fallback_segments == 0


def test_markdown_pipeline_protects_code_front_matter_and_nested_link_destination(tmp_path) -> None:
    """Markdown 只翻译正文, 不改 front matter、代码与嵌套链接目标。"""

    source = tmp_path / "guide.md"
    source.write_text(
        "---\ntitle: Keep me\n---\n\n"
        "# Hello\n\n"
        "Visit [docs](https://example.com/a_(b)).\n\n"
        "```py\nprint('Keep me')\n```\n",
        encoding="utf-8",
    )

    translator = UpperTranslator()
    result = PlainTextPipeline("md", translator).translate(_request(source, tmp_path / "outputs"))
    output = result.output_path.read_text(encoding="utf-8")

    assert "title: Keep me" in output
    assert "# HELLO" in output
    assert "https://example.com/a_(b)" in output
    assert "print('Keep me')" in output
    assert "%%PROTECTED_" not in output
    assert translator.read_only_contexts == [
        ("--- title: Keep me ---", "```py print('Keep me') ```"),
    ]


def test_plain_text_pipeline_reports_fallback_instead_of_silent_partial_output(tmp_path) -> None:
    """batch contract 被破坏时应保留原文并报告 fallback。"""

    source = tmp_path / "sample.txt"
    source.write_text("Keep this text", encoding="utf-8")

    result = PlainTextPipeline("txt", BrokenTranslator()).translate(
        _request(source, tmp_path / "outputs")
    )

    assert result.output_path.read_text(encoding="utf-8") == "Keep this text"
    assert result.fallback_segments == 1
    assert result.warning_codes == ("segment_fallback",)


@pytest.mark.parametrize(
    ("document_kind", "source_text", "candidate"),
    [
        ("txt", "Keep this text", ""),
        ("txt", "Keep this text", " \t "),
        ("md", "# Keep this text\n", ""),
        ("md", "# Keep this text\n", " \t "),
    ],
)
def test_non_empty_segment_rejects_empty_or_whitespace_candidate(
    tmp_path,
    document_kind,
    source_text,
    candidate,
) -> None:
    """TXT 与 Markdown 的非空 segment 都不能被空白候选静默清除。"""

    source = tmp_path / f"sample.{document_kind}"
    source.write_text(source_text, encoding="utf-8")

    result = PlainTextPipeline(document_kind, ConstantTranslator(candidate)).translate(
        _request(source, tmp_path / "outputs")
    )

    assert result.output_path.read_text(encoding="utf-8") == source_text
    assert result.translated_segments == 0
    assert result.fallback_segments == 1
    assert result.warning_codes == ("segment_fallback",)


def test_markdown_table_rejects_candidate_that_destroys_row_skeleton(tmp_path) -> None:
    """普通文本不能替换 table row, alignment row 也不得送入模型。"""

    source_text = "| Name | Note |\n| :--- | ---: |\n| Alpha | A \\| B |\n"
    source = tmp_path / "table.md"
    source.write_text(source_text, encoding="utf-8")
    translator = ConstantTranslator("这只是一段普通文本")

    result = PlainTextPipeline("md", translator).translate(_request(source, tmp_path / "outputs"))

    assert result.output_path.read_text(encoding="utf-8") == source_text
    assert result.translated_segments == 0
    assert result.fallback_segments == 2


def test_markdown_table_counts_only_unescaped_pipe_as_cell_delimiter(tmp_path) -> None:
    """cell 内 escaped pipe 数量可变化, 但未转义 delimiter 数必须保持一致。"""

    source_text = "| Name | Note |\n| --- | --- |\n| Alpha | A \\| B |\n"
    source = tmp_path / "escaped-pipe.md"
    source.write_text(source_text, encoding="utf-8")

    def translate_table_row(text: str) -> str:
        """保持 table cell 数, 同时让 cell 内 escaped pipe 数发生变化。"""

        if "Alpha" in text:
            return "| 阿尔法 | 甲 \\| 乙 \\| 丙 |"
        return "| 名称 | 备注 |"

    translator = TransformTranslator(translate_table_row)
    result = PlainTextPipeline("md", translator).translate(_request(source, tmp_path / "outputs"))

    assert result.output_path.read_text(encoding="utf-8") == (
        "| 名称 | 备注 |\n| --- | --- |\n| 阿尔法 | 甲 \\| 乙 \\| 丙 |\n"
    )
    assert "| --- | --- |" not in translator.seen_texts
    assert result.fallback_segments == 0


def test_markdown_table_supports_optional_outer_pipe_and_validates_each_row(tmp_path) -> None:
    """无 outer pipe 的 GFM table 也应逐行翻译, 结构破坏时只 fallback 该行。"""

    source_text = "Name | Value\n--- | ---\nAlpha | Beta\nGamma | Delta\n"
    source = tmp_path / "optional-outer-pipe.md"
    source.write_text(source_text, encoding="utf-8")

    def translate_table_row(text: str) -> str:
        """返回两个合法 row 和一个破坏 delimiter 的普通文本。"""

        translations = {
            "Name | Value": "名称 | 值",
            "Alpha | Beta": "阿尔法 | 贝塔",
            "Gamma | Delta": "这只是一段普通文本",
        }
        return translations[text]

    translator = TransformTranslator(translate_table_row)
    result = PlainTextPipeline("md", translator).translate(_request(source, tmp_path / "outputs"))

    assert result.output_path.read_text(encoding="utf-8") == (
        "名称 | 值\n--- | ---\n阿尔法 | 贝塔\nGamma | Delta\n"
    )
    assert "--- | ---" not in translator.seen_texts
    assert result.translated_segments == 2
    assert result.fallback_segments == 1
