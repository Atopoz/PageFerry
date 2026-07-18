"""验证任务 metadata 的状态迁移、语言选择和结果统计。"""

from pathlib import Path

from db.jobs import JobRepository
from db.sqlite import initialize_database
from modules.translation.contracts import (
    DocumentTranslationOptions,
    TranslationArtifact,
    TranslationProgress,
    TranslationResult,
)


def test_job_repository_roundtrips_pdf_bilingual_option(tmp_path: Path) -> None:
    """PDF 双语开关必须通过现有 options_json 原样往返。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    repository = JobRepository(database)

    job = repository.create(
        job_id="job-pdf-bilingual",
        source_path=tmp_path / "source.pdf",
        source_name="source.pdf",
        document_type="pdf",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        source_language="en",
        target_language="zh-CN",
        options=DocumentTranslationOptions(kind="pdf", bilingual=True),
    )

    assert job.options == DocumentTranslationOptions(kind="pdf", bilingual=True)


def test_job_repository_persists_result_warnings_and_fallback_count(tmp_path) -> None:
    """任务成功后应完整保存结果路径、fallback 与语言 metadata。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    repository = JobRepository(database)
    job = repository.create(
        job_id="job-1",
        source_path=tmp_path / "source.md",
        source_name="source.md",
        document_type="md",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        source_language="en",
        target_language="zh-CN",
    )

    assert job.status == "queued"
    assert job.progress_stage == "extracting"
    assert job.processed_segments == 0
    assert job.total_segments == 0
    assert repository.mark_running(job.id)
    repository.mark_succeeded(
        job.id,
        TranslationResult(
            output_path=tmp_path / "output.md",
            document_kind="md",
            translated_segments=8,
            fallback_segments=2,
            warning_codes=("segment_fallback",),
        ),
    )

    completed = repository.get(job.id)
    assert completed.status == "succeeded"
    assert completed.progress == 100
    assert completed.progress_stage == "formatting"
    assert completed.processed_segments == 10
    assert completed.total_segments == 10
    assert completed.output_path == tmp_path / "output.md"
    assert completed.artifacts == (
        TranslationArtifact(kind="translated", path=tmp_path / "output.md"),
    )
    assert completed.translated_segments == 8
    assert completed.fallback_segments == 2
    assert completed.warning_codes == ("segment_fallback",)
    assert completed.source_language == "en"
    assert completed.target_language == "zh-CN"


def test_job_repository_persists_multiple_artifacts_in_stable_order(tmp_path: Path) -> None:
    """同一 DOCX 任务的译文版与双语版必须作为独立 metadata 保存。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    repository = JobRepository(database)
    job = repository.create(
        job_id="job-bilingual",
        source_path=tmp_path / "source.docx",
        source_name="source.docx",
        document_type="docx",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        source_language="zh-CN",
        target_language="en",
    )
    translated = tmp_path / "translated.docx"
    bilingual = tmp_path / "bilingual.docx"

    repository.mark_succeeded(
        job.id,
        TranslationResult(
            output_path=translated,
            document_kind="docx",
            artifacts=(
                TranslationArtifact(kind="translated", path=translated),
                TranslationArtifact(kind="bilingual", path=bilingual),
            ),
            translated_segments=2,
        ),
    )

    assert repository.get(job.id).artifacts == (
        TranslationArtifact(kind="translated", path=translated),
        TranslationArtifact(kind="bilingual", path=bilingual),
    )


def test_job_repository_persists_real_batch_progress_without_stage_regression(
    tmp_path,
) -> None:
    """running job 只接受真实 segment 比例与向前推进的三阶段 snapshot。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    repository = JobRepository(database)
    job = repository.create(
        job_id="job-progress",
        source_path=tmp_path / "source.docx",
        source_name="source.docx",
        document_type="docx",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        source_language=None,
        target_language="zh-CN",
    )

    assert repository.mark_running(job.id)
    assert repository.get(job.id).progress == 0
    assert repository.update_progress(
        job.id,
        TranslationProgress(stage="translating", total_segments=5),
    )
    assert repository.update_progress(
        job.id,
        TranslationProgress(
            stage="translating",
            processed_segments=2,
            total_segments=5,
        ),
    )

    translating = repository.get(job.id)
    assert translating.progress_stage == "translating"
    assert translating.progress == 40
    assert translating.processed_segments == 2
    assert translating.total_segments == 5
    assert not repository.update_progress(
        job.id,
        TranslationProgress(
            stage="translating",
            processed_segments=1,
            total_segments=5,
        ),
    )
    assert not repository.update_progress(
        job.id,
        TranslationProgress(stage="extracting"),
    )

    assert repository.update_progress(
        job.id,
        TranslationProgress(
            stage="formatting",
            processed_segments=5,
            total_segments=5,
        ),
    )
    formatting = repository.get(job.id)
    assert formatting.progress_stage == "formatting"
    assert formatting.progress == 100


def test_job_repository_marks_unrecoverable_jobs_as_interrupted(tmp_path) -> None:
    """应用重启时应把无法恢复的 queued 与 running 任务改为明确失败。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    repository = JobRepository(database)
    repository.create(
        job_id="queued-job",
        source_path=Path("/tmp/source.docx"),
        source_name="source.docx",
        document_type="docx",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        source_language=None,
        target_language="en",
    )
    repository.create(
        job_id="running-job",
        source_path=Path("/tmp/running.docx"),
        source_name="running.docx",
        document_type="docx",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        source_language=None,
        target_language="en",
    )
    repository.mark_running("running-job")

    assert repository.mark_interrupted_jobs() == 2
    assert repository.get("queued-job").status == "failed"
    assert repository.get("queued-job").error_code == "process_interrupted"
    assert repository.get("running-job").status == "failed"
    assert repository.get("running-job").error_code == "process_interrupted"
