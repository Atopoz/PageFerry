"""验证任务 service 的文件边界、独立输出目录与失败状态。"""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path

import pytest

from db.jobs import JobRepository
from db.sqlite import initialize_database
from modules.translation.contracts import (
    BatchTranslator,
    DocumentKind,
    DocumentPipelineError,
    TranslationBatchItem,
    TranslationBatchResult,
    TranslationProgress,
    TranslationProgressReporter,
    TranslationRequest,
    TranslationResult,
)
from modules.translation.jobs import (
    DEFAULT_MAX_SOURCE_BYTES,
    JobServiceError,
    TranslationJobService,
    _build_pipeline,
)


class IdentityTranslator:
    """提供无需网络的 translator, 供任务编排测试使用。"""

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
    ) -> TranslationBatchResult:
        """按原 index 返回输入文本。"""

        del source_language, target_language, format_hint
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(index=index, text=text) for index, text in enumerate(texts)
            )
        )


class StubResolver:
    """记录 provider 解析并返回固定 translator。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.calls: list[tuple[str, str]] = []

    def build_translator(self, provider_id: str, model_id: str) -> BatchTranslator:
        """返回 identity translator 并记录模型选择。"""

        self.calls.append((provider_id, model_id))
        return IdentityTranslator()


class CopyPipeline:
    """用小文本复制模拟格式 runtime, 专注测试 job orchestration。"""

    document_kind: DocumentKind = "txt"

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """把输入复制到 job 独占目录并返回成功统计。"""

        if report_progress is not None:
            report_progress(TranslationProgress(stage="extracting"))
            report_progress(TranslationProgress(stage="translating", total_segments=1))
            report_progress(
                TranslationProgress(
                    stage="translating",
                    processed_segments=1,
                    total_segments=1,
                )
            )
            report_progress(
                TranslationProgress(
                    stage="formatting",
                    processed_segments=1,
                    total_segments=1,
                )
            )
        request.output_dir.mkdir(parents=True)
        output = request.output_dir / "translated.txt"
        output.write_bytes(request.source_path.read_bytes())
        return TranslationResult(
            output_path=output,
            document_kind="txt",
            translated_segments=1,
        )


class StableFailurePipeline:
    """模拟 PDF runtime 在执行期返回一个可安全持久化的稳定错误码。"""

    document_kind: DocumentKind = "pdf"

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """不接触正文, 直接返回扫描件边界错误。"""

        del request, report_progress
        raise DocumentPipelineError("pdf_no_text_layer")


class RecordingJobRepository(JobRepository):
    """记录 service 交付的 progress snapshot, 同时复用真实 SQLite 更新。"""

    def __init__(self, database: Path) -> None:
        """绑定测试数据库并初始化 snapshot 列表。"""

        super().__init__(database)
        self.progress_snapshots: list[TranslationProgress] = []

    def update_progress(self, job_id: str, snapshot: TranslationProgress) -> bool:
        """记录 snapshot 后执行真实的阶段与计数约束。"""

        self.progress_snapshots.append(snapshot)
        return super().update_progress(job_id, snapshot)


def test_default_pdf_factory_passes_app_data_font_directory(tmp_path: Path) -> None:
    """默认 PDF factory 必须把 app-data 字体目录交给 pipeline。"""

    from modules.pdf.layout import LayoutDetector
    from modules.pdf.pipeline import PdfPipeline

    font_directory = tmp_path / "pdf" / "revision" / "fonts"
    detector = LayoutDetector(tmp_path / "model.onnx", verify_checksum=False)

    pipeline = _build_pipeline(
        "pdf",
        IdentityTranslator(),
        None,
        pdf_layout_detector=detector,
        pdf_font_directory=font_directory,
    )

    assert isinstance(pipeline, PdfPipeline)
    assert pipeline._font_directory == font_directory.resolve()


def _service(
    tmp_path: Path,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
) -> TranslationJobService:
    """创建使用临时数据库与 stub runtime 的任务 service。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    return TranslationJobService(
        JobRepository(database),
        StubResolver(),
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "outputs",
        pipeline_factory=lambda _kind, _translator, _options: CopyPipeline(),
        max_source_bytes=max_source_bytes,
    )


def test_path_job_runs_in_an_id_scoped_output_directory(tmp_path: Path) -> None:
    """同名输入也必须落到 job id 目录, 且源文件保持只读不变。"""

    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    original = source.read_bytes()
    service = _service(tmp_path)

    job = service.create_path_job(
        source_path=str(source),
        source_language="auto",
        target_language="zh-CN",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
    )
    completed = service.run(job.id)

    assert completed.status == "succeeded"
    assert completed.output_path == tmp_path / "outputs" / job.id / "translated.txt"
    assert completed.source_language is None
    assert source.read_bytes() == original


def test_job_service_persists_pipeline_progress_snapshots(tmp_path: Path) -> None:
    """service 必须把 pipeline callback 接到 repository, 不能只在成功时补结果。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    repository = RecordingJobRepository(database)
    service = TranslationJobService(
        repository,
        StubResolver(),
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "outputs",
        pipeline_factory=lambda _kind, _translator, _options: CopyPipeline(),
    )
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    job = service.create_path_job(
        source_path=str(source),
        source_language=None,
        target_language="zh-CN",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
    )

    service.run(job.id)

    assert repository.progress_snapshots == [
        TranslationProgress(stage="extracting"),
        TranslationProgress(stage="translating", total_segments=1),
        TranslationProgress(
            stage="translating",
            processed_segments=1,
            total_segments=1,
        ),
        TranslationProgress(
            stage="formatting",
            processed_segments=1,
            total_segments=1,
        ),
    ]


def test_upload_is_atomic_and_keeps_only_a_safe_display_name(tmp_path: Path) -> None:
    """上传路径片段不得逃出 workspace, 最终源文件只在完整写入后出现。"""

    service = _service(tmp_path)
    job = service.create_upload_job(
        file_name=r"C:\\fakepath\\notes.md",
        stream=BytesIO(b"# Notes"),
        source_language=None,
        target_language="en",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
    )

    assert job.source_name == "notes.md"
    assert job.source_path == tmp_path / "workspace" / "jobs" / job.id / "source.md"
    assert job.source_path.read_bytes() == b"# Notes"
    assert not list(job.source_path.parent.glob(".upload-*.tmp"))


def test_oversized_upload_cleans_its_private_workspace(tmp_path: Path) -> None:
    """超限流应删除 partial 文件和 job 目录, 也不写入 metadata。"""

    service = _service(tmp_path, max_source_bytes=4)

    with pytest.raises(JobServiceError, match="200 MB"):
        service.create_upload_job(
            file_name="large.txt",
            stream=BytesIO(b"12345"),
            source_language=None,
            target_language="en",
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
        )

    jobs_root = tmp_path / "workspace" / "jobs"
    assert not jobs_root.exists() or not list(jobs_root.iterdir())


def test_empty_upload_keeps_empty_file_contract_and_cleans_workspace(tmp_path: Path) -> None:
    """upload 的 0-byte 输入仍应返回 empty_file, 且不留下 job workspace。"""

    service = _service(tmp_path)

    with pytest.raises(JobServiceError) as raised:
        service.create_upload_job(
            file_name="empty.txt",
            stream=BytesIO(),
            source_language=None,
            target_language="zh-CN",
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
        )

    assert raised.value.code == "empty_file"
    assert raised.value.status_code == 400
    jobs_root = tmp_path / "workspace" / "jobs"
    assert not jobs_root.exists() or not list(jobs_root.iterdir())


def test_oversized_local_path_is_rejected_before_job_creation(tmp_path: Path) -> None:
    """本地 path 入口也必须在写入 job 前拒绝 201 MB 的 sparse 文件。"""

    source = tmp_path / "oversized.txt"
    with source.open("wb") as handle:
        handle.truncate(DEFAULT_MAX_SOURCE_BYTES + 1)
    service = _service(tmp_path)

    with pytest.raises(JobServiceError) as raised:
        service.create_path_job(
            source_path=str(source),
            source_language=None,
            target_language="zh-CN",
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
        )

    assert raised.value.code == "file_too_large"
    assert raised.value.status_code == 413
    assert service.list_recent() == []


def test_empty_local_path_is_rejected_before_job_creation(tmp_path: Path) -> None:
    """本地 path 入口必须与 upload 一样以 empty_file 拒绝 0-byte 文件。"""

    source = tmp_path / "empty.txt"
    source.touch()
    service = _service(tmp_path)

    with pytest.raises(JobServiceError) as raised:
        service.create_path_job(
            source_path=str(source),
            source_language=None,
            target_language="zh-CN",
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
        )

    assert raised.value.code == "empty_file"
    assert raised.value.status_code == 400
    assert service.list_recent() == []


def test_unsupported_extension_is_rejected_before_job_creation(tmp_path: Path) -> None:
    """尚未接入的格式不能靠改扩展名绕过 runtime registry。"""

    source = tmp_path / "sheet.csv"
    source.write_bytes(b"heading,value")
    service = _service(tmp_path)

    with pytest.raises(JobServiceError, match="仅支持"):
        service.create_path_job(
            source_path=str(source),
            source_language=None,
            target_language="zh-CN",
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
        )


def test_pdf_extension_is_registered_without_format_options(tmp_path: Path) -> None:
    """PDF 输入应进入 job registry, 且首版不制造图片翻译或预览选项。"""

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    service = _service(tmp_path)

    job = service.create_path_job(
        source_path=str(source),
        source_language="en",
        target_language="zh-CN",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
    )

    assert job.document_type == "pdf"
    assert job.options is None


def test_pipeline_error_code_is_persisted_without_becoming_generic_failure(tmp_path: Path) -> None:
    """格式 runtime 的稳定错误不能被任务层压成 pipeline_failed。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    service = TranslationJobService(
        JobRepository(database),
        StubResolver(),
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "outputs",
        pipeline_factory=lambda _kind, _translator, _options: StableFailurePipeline(),
    )
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    job = service.create_path_job(
        source_path=str(source),
        source_language="en",
        target_language="zh-CN",
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
    )

    completed = service.run(job.id)

    assert completed.status == "failed"
    assert completed.error_code == "pdf_no_text_layer"
