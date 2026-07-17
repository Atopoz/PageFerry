"""PDF CID 文本恢复测试。"""

from __future__ import annotations

from modules.pdf.entities import BBox, PageInfo, TextBlock, TextSpan
from modules.pdf.extractor import _check_span_is_preserved
from modules.pdf.formatter import build_tagged_text_blocks


def test_check_span_is_preserved_recovers_readable_line_with_single_cid() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    text = "trapped in the “Customer Management Industrial Complex” (cid:215) a persistent network of technology\n"  # noqa: E501

    should_preserve, processed_text = _check_span_is_preserved(text)

    assert should_preserve is False
    assert "(cid:" not in processed_text
    assert "Customer Management Industrial Complex" in processed_text
    assert "a persistent network of technology" in processed_text


def test_check_span_is_preserved_recovers_short_heading_with_single_cid() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    cases = [
        ("New (cid:1) York\n", "New", "York"),
        ("Q(cid:1)1 Revenue\n", "Q", "Revenue"),
        ("客户(cid:1)管理\n", "客户", "管理"),
    ]

    for text, left_fragment, right_fragment in cases:
        should_preserve, processed_text = _check_span_is_preserved(text)

        assert should_preserve is False
        assert "(cid:" not in processed_text
        assert left_fragment in processed_text
        assert right_fragment in processed_text


def test_check_span_is_preserved_keeps_ambiguous_short_cid_fragment() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    text = "A(cid:2)B\n"

    should_preserve, processed_text = _check_span_is_preserved(text)

    assert should_preserve is True
    assert processed_text == text


def test_check_span_is_preserved_keeps_cid_garbage_line() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    text = (
        "T(cid:76)(cid:77)(cid:87) (cid:86)(cid:73)(cid:87)(cid:73)(cid:69)(cid:86)(cid:71)(cid:76) "  # noqa: E501
        "(cid:77)(cid:87) (cid:84)(cid:69)(cid:86)(cid:88) (cid:83)(cid:74) (cid:69)(cid:82) "
        "(cid:83)(cid:82)(cid:75)(cid:83)(cid:77)(cid:82)(cid:75) (cid:81)(cid:89)l(cid:88)(cid:77)"
        "(cid:72)(cid:77)(cid:87)(cid:71)(cid:77)(cid:84)l(cid:77)(cid:82)(cid:69)(cid:86)(cid:93) "
        "(cid:87)(cid:73)(cid:86)(cid:77)(cid:73)(cid:87) (cid:73)x(cid:84)l(cid:83)(cid:86)(cid:77)"  # noqa: E501
        "(cid:82)(cid:75) (cid:88)(cid:76)(cid:73) (cid:83)(cid:84)(cid:84)(cid:83)(cid:86)(cid:88)"
        "(cid:89)(cid:82)(cid:77)(cid:88)(cid:77)(cid:73)(cid:87) (cid:88)(cid:83) "
        "(cid:86)(cid:73)-(cid:88)(cid:76)(cid:77)(cid:82)k\n"
    )

    should_preserve, processed_text = _check_span_is_preserved(text)

    assert should_preserve is True
    assert processed_text == text


def test_build_tagged_text_blocks_keeps_recovered_cid_span() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    original = "trapped in the “Customer Management Industrial Complex” (cid:215) a persistent network of technology\n"  # noqa: E501
    should_preserve, processed_text = _check_span_is_preserved(original)

    span = TextSpan(
        span_id=0,
        bbox=BBox(0, 0, 100, 12),
        text=processed_text,
        is_preserved=should_preserve,
    )
    block = TextBlock(
        block_id=0,
        bbox=BBox(0, 0, 100, 12),
        text=processed_text,
        spans=[span],
    )
    page = PageInfo(page_index=0, texts=[block], preserved_texts=[])

    tagged_blocks = build_tagged_text_blocks([page])

    assert len(tagged_blocks) == 1
    assert tagged_blocks[0].spans[0].text == (
        "trapped in the “Customer Management Industrial Complex” a persistent network of technology"
    )
