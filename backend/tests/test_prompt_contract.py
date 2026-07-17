"""验证所有 runtime 共用的翻译 prompt 保持稳定、索引明确。"""

import json

import pytest

from modules.translation.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_translation_messages,
)


def test_prompt_keeps_system_and_task_context_stable_when_segments_change() -> None:
    """正文变化不能污染可缓存的 system 与 task context。"""

    first = build_translation_messages(
        texts=["First paragraph"],
        source_language="en",
        target_language="zh-CN",
        format_hint="docx",
    )
    second = build_translation_messages(
        texts=["A completely different paragraph"],
        source_language="en",
        target_language="zh-CN",
        format_hint="docx",
    )

    assert first.system == second.system == SYSTEM_PROMPT
    assert first.task_context == second.task_context
    assert first.segment_payload != second.segment_payload
    assert "First paragraph" not in first.system
    assert "First paragraph" not in first.task_context
    assert "heading" in first.system
    assert "Do not leave source-language" in first.system


def test_prompt_payload_is_deterministic_and_indexed() -> None:
    """相同输入应得到稳定 JSON, 每个 segment 都保留顺序 index。"""

    messages = build_translation_messages(
        texts=["alpha", "beta"],
        source_language=None,
        target_language="ja",
        format_hint="md",
    )

    task_context = json.loads(messages.task_context)
    assert task_context == {
        "format": "md",
        "format_constraints": [
            "Translate only visible natural-language text.",
            "Keep Markdown markers, table pipes, and every %%PROTECTED_*%% placeholder unchanged.",
            "Preserve heading, list, quote, table, and segment order without adding commentary.",
        ],
        "prompt_version": PROMPT_VERSION,
        "source_language": "auto",
        "source_language_label": "automatically identified language",
        "target_language": "ja",
        "target_language_label": "Japanese",
    }
    assert json.loads(messages.segment_payload) == {
        "segments": [
            {"index": 0, "text": "alpha"},
            {"index": 1, "text": "beta"},
        ]
    }


def test_prompt_keeps_markdown_context_out_of_translatable_payload() -> None:
    """受保护的 Markdown 内容只作为语境, 不得获得可写回的 index。"""

    messages = build_translation_messages(
        texts=["Read the setup guide"],
        source_language="en",
        target_language="zh-CN",
        format_hint="md",
        read_only_context=("title: setup", "pip install pageferry"),
    )

    assert json.loads(messages.task_context)["read_only_context"] == [
        "title: setup",
        "pip install pageferry",
    ]
    assert json.loads(messages.segment_payload) == {
        "segments": [{"index": 0, "text": "Read the setup guide"}]
    }


def test_docx_repair_prompt_separates_source_skeleton_and_bad_candidate() -> None:
    """DOCX repair 以 source skeleton 为准, bad candidate 按同一 index 传入。"""

    source = "[PARA_0001]<span>Receipt matters</span>"
    candidate = "<span>收据事项"
    messages = build_translation_messages(
        texts=[source],
        source_language="en",
        target_language="zh-CN",
        format_hint="docx_repair",
        repair_candidates=(candidate,),
    )

    context = json.loads(messages.task_context)
    assert "read_only_context" not in context
    assert "authoritative source skeleton" in " ".join(context["format_constraints"])
    assert json.loads(messages.segment_payload) == {
        "segments": [{"index": 0, "text": source, "candidate": candidate}]
    }


def test_repair_candidate_count_must_match_source_count() -> None:
    """repair candidate 与 source 无法按 index 对齐时必须在请求前拒绝。"""

    with pytest.raises(ValueError, match="candidate count"):
        build_translation_messages(
            texts=["first", "second"],
            source_language="en",
            target_language="zh-CN",
            format_hint="docx_repair",
            repair_candidates=("only one",),
        )
