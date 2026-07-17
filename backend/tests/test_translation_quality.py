"""验证 unchanged translation 的高置信度 quality retry 判定。"""

from modules.translation.quality import should_retry_unchanged_translation


def test_english_heading_to_chinese_needs_retry() -> None:
    """纯英文多词标题原样返回时应触发一次定向 retry。"""

    text = "[PARA_3]<span>Receipt matters</span>"
    assert should_retry_unchanged_translation(
        source_text=text,
        translated_text=" [PARA_3] <span>Receipt   matters</span> ",
        source_language="en",
        target_language="zh-CN",
    )


def test_mixed_target_script_and_brand_name_does_not_retry() -> None:
    """已含目标脚本的品牌信息不应被误判为漏译。"""

    text = "<span>卖方: Example Tech (SH) Co., Ltd.</span>"
    assert not should_retry_unchanged_translation(
        source_text=text,
        translated_text=text,
        source_language="en",
        target_language="zh-CN",
    )


def test_chinese_body_to_english_needs_retry() -> None:
    """中文正文到英文却完全未变化时应触发 retry。"""

    text = "课题研究背景"
    assert should_retry_unchanged_translation(
        source_text=text,
        translated_text=text,
        source_language="zh-CN",
        target_language="en",
    )
