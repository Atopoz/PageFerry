"""验证 jobs API 的路径/上传入口、background 执行与公开响应边界。"""

from collections.abc import Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from core.settings import Settings
from db.jobs import JobRepository
from main import create_app
from modules.translation.contracts import (
    BatchTranslator,
    DocumentKind,
    TranslationArtifact,
    TranslationBatchItem,
    TranslationBatchResult,
    TranslationProgress,
    TranslationProgressReporter,
    TranslationRequest,
    TranslationResult,
)
from modules.translation.jobs import TranslationJobService


class ApiIdentityTranslator:
    """提供 jobs API 测试所需的最小 translator。"""

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
    ) -> TranslationBatchResult:
        """保持 segment 不变并保留 index contract。"""

        del source_language, target_language, format_hint
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(index=index, text=text) for index, text in enumerate(texts)
            )
        )


class ApiResolver:
    """模拟已验证 provider 配置。"""

    def build_translator(self, provider_id: str, model_id: str) -> BatchTranslator:
        """接受测试模型并返回无网络 translator。"""

        assert provider_id == "deepseek"
        assert model_id == "deepseek-v4-flash"
        return ApiIdentityTranslator()


class ApiCopyPipeline:
    """把 source bytes 复制到输出目录, 让测试聚焦 HTTP 编排。"""

    def __init__(self, document_kind: DocumentKind) -> None:
        """保存当前请求对应的格式。"""

        self.document_kind = document_kind

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """创建一个可由 GET jobs 观察到的输出文件。"""

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
        output = request.output_dir / f"translated{request.source_path.suffix}"
        output.write_bytes(request.source_path.read_bytes())
        return TranslationResult(
            output_path=output,
            document_kind=self.document_kind,
            translated_segments=1,
        )


class ApiBilingualCopyPipeline(ApiCopyPipeline):
    """模拟一次执行同时生成译文版与双语版。"""

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """复用基础进度与译文文件, 再增加一个双语 artifact。"""

        translated_result = super().translate(request, report_progress=report_progress)
        bilingual = request.output_dir / "bilingual.docx"
        bilingual.write_bytes(request.source_path.read_bytes())
        return TranslationResult(
            output_path=translated_result.output_path,
            document_kind=self.document_kind,
            artifacts=(
                TranslationArtifact(kind="translated", path=translated_result.output_path),
                TranslationArtifact(kind="bilingual", path=bilingual),
            ),
            translated_segments=translated_result.translated_segments,
        )


class RecordingPipelineFactory:
    """记录 API 传入的格式选项, 并返回无网络 copy pipeline。"""

    def __init__(self) -> None:
        """创建尚未接收调用的 recorder。"""

        self.options = None

    def __call__(self, kind, _translator, options):
        """保存规范化选项, 并构造当前格式的测试 pipeline。"""

        self.options = options
        return ApiCopyPipeline(kind)


def _test_job_service(data_dir: Path) -> TranslationJobService:
    """构造使用真实 SQLite 与本地 stub runtime 的 API service。"""

    return TranslationJobService(
        JobRepository(data_dir / "pageferry.sqlite3"),
        ApiResolver(),
        workspace_dir=data_dir / "workspace",
        output_dir=data_dir / "outputs",
        pipeline_factory=lambda kind, _translator, _options: ApiCopyPipeline(kind),
    )


def test_pptx_options_are_snapshotted_and_reach_pipeline(tmp_path: Path) -> None:
    """PPTX 高级选项必须进入任务 snapshot, 并决定 pipeline 构造。"""

    source = tmp_path / "slides.pptx"
    source.write_bytes(b"presentation")
    data_dir = tmp_path / "app-data"
    app = create_app(Settings(data_dir=data_dir))
    factory = RecordingPipelineFactory()
    app.state.translation_job_service = TranslationJobService(
        JobRepository(data_dir / "pageferry.sqlite3"),
        ApiResolver(),
        workspace_dir=data_dir / "workspace",
        output_dir=data_dir / "outputs",
        pipeline_factory=factory,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs",
            json={
                "source_path": str(source),
                "source_language": "en",
                "target_language": "zh-CN",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "options": {
                    "kind": "pptx",
                    "translate_tables": False,
                    "translate_notes": True,
                },
            },
        )

    assert created.status_code == 202
    assert created.json()["options"] == {
        "kind": "pptx",
        "translate_tables": False,
        "translate_notes": True,
    }
    assert factory.options is not None
    assert factory.options.kind == "pptx"
    assert factory.options.translate_tables is False
    assert factory.options.translate_notes is True


def test_docx_bilingual_option_returns_two_artifacts(tmp_path: Path) -> None:
    """DOCX 双语选项必须冻结, 成功响应按稳定 kind 返回两个派生物。"""

    source = tmp_path / "source.docx"
    source.write_bytes(b"document")
    data_dir = tmp_path / "app-data"
    app = create_app(Settings(data_dir=data_dir))
    app.state.translation_job_service = TranslationJobService(
        JobRepository(data_dir / "pageferry.sqlite3"),
        ApiResolver(),
        workspace_dir=data_dir / "workspace",
        output_dir=data_dir / "outputs",
        pipeline_factory=lambda kind, _translator, _options: ApiBilingualCopyPipeline(kind),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs",
            json={
                "source_path": str(source),
                "source_language": "zh-CN",
                "target_language": "en",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "options": {
                    "kind": "docx",
                    "translate_tables": True,
                    "bilingual": True,
                },
            },
        )
        completed = client.get("/api/v1/jobs").json()[0]

    assert created.status_code == 202
    assert created.json()["options"] == {
        "kind": "docx",
        "translate_tables": True,
        "bilingual": True,
    }
    assert [artifact["kind"] for artifact in completed["artifacts"]] == [
        "translated",
        "bilingual",
    ]


def test_pdf_bilingual_path_option_is_snapshotted_and_reaches_pipeline(
    tmp_path: Path,
) -> None:
    """PDF path 入口必须冻结拼接式双语开关并交给 pipeline factory。"""

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    data_dir = tmp_path / "app-data"
    app = create_app(Settings(data_dir=data_dir))
    factory = RecordingPipelineFactory()
    app.state.translation_job_service = TranslationJobService(
        JobRepository(data_dir / "pageferry.sqlite3"),
        ApiResolver(),
        workspace_dir=data_dir / "workspace",
        output_dir=data_dir / "outputs",
        pipeline_factory=factory,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs",
            json={
                "source_path": str(source),
                "source_language": "en",
                "target_language": "zh-CN",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "options": {"kind": "pdf", "bilingual": True},
            },
        )

    assert created.status_code == 202
    assert created.json()["options"] == {"kind": "pdf", "bilingual": True}
    assert factory.options is not None
    assert factory.options.kind == "pdf"
    assert factory.options.bilingual is True


def test_pdf_bilingual_upload_option_is_snapshotted(tmp_path: Path) -> None:
    """PDF multipart 入口必须解析同一份拼接式双语 contract。"""

    data_dir = tmp_path / "app-data"
    app = create_app(Settings(data_dir=data_dir))
    factory = RecordingPipelineFactory()
    app.state.translation_job_service = TranslationJobService(
        JobRepository(data_dir / "pageferry.sqlite3"),
        ApiResolver(),
        workspace_dir=data_dir / "workspace",
        output_dir=data_dir / "outputs",
        pipeline_factory=factory,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs/upload",
            files={"file": ("source.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
            data={
                "source_language": "en",
                "target_language": "zh-CN",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "options": '{"kind":"pdf","bilingual":true}',
            },
        )

    assert created.status_code == 202
    assert created.json()["options"] == {"kind": "pdf", "bilingual": True}
    assert factory.options is not None
    assert factory.options.kind == "pdf"
    assert factory.options.bilingual is True


def test_pdf_options_reject_unexposed_layout_modes(tmp_path: Path) -> None:
    """path 与 multipart 都不能接收未公开的页内双语布局字段。"""

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    data_dir = tmp_path / "app-data"
    app = create_app(Settings(data_dir=data_dir))
    app.state.translation_job_service = _test_job_service(data_dir)
    payload = {
        "source_path": str(source),
        "source_language": "en",
        "target_language": "zh-CN",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-flash",
    }

    with TestClient(app) as client:
        path_response = client.post(
            "/api/v1/jobs",
            json={
                **payload,
                "options": {
                    "kind": "pdf",
                    "bilingual": True,
                    "inline_stack": True,
                },
            },
        )
        upload_response = client.post(
            "/api/v1/jobs/upload",
            files={"file": ("source.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
            data={
                "source_language": "en",
                "target_language": "zh-CN",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "options": '{"kind":"pdf","bilingual":true,"side_by_side":true}',
            },
        )

    assert path_response.status_code == 422
    assert upload_response.status_code == 400
    assert upload_response.json()["code"] == "invalid_document_options"


def test_path_job_api_runs_in_background_and_hides_source_path(tmp_path: Path) -> None:
    """POST 返回 queued snapshot, 随后 GET 可见成功且不泄露源路径。"""

    source = tmp_path / "outside.txt"
    source.write_text("private body", encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "app-data"))
    app.state.translation_job_service = _test_job_service(tmp_path / "app-data")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs",
            json={
                "source_path": str(source),
                "source_language": "en",
                "target_language": "zh-CN",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
            },
        )
        recent = client.get("/api/v1/jobs")

    assert created.status_code == 202
    assert created.json()["status"] == "queued"
    assert "source_path" not in created.json()
    assert str(source) not in created.text
    assert recent.status_code == 200
    completed = recent.json()[0]
    assert completed["status"] == "succeeded"
    assert completed["progress_stage"] == "formatting"
    assert completed["processed_segments"] == 1
    assert completed["total_segments"] == 1
    assert completed["translated_segments"] == 1
    assert Path(completed["output_path"]).read_text(encoding="utf-8") == "private body"


def test_upload_job_api_places_file_under_app_workspace(tmp_path: Path) -> None:
    """浏览器上传应复用同一 job contract 并成功发布输出。"""

    data_dir = tmp_path / "app-data"
    app = create_app(Settings(data_dir=data_dir))
    app.state.translation_job_service = _test_job_service(data_dir)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs/upload",
            files={"file": ("notes.md", b"# Notes", "text/markdown")},
            data={
                "source_language": "auto",
                "target_language": "en",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
            },
        )
        completed = client.get("/api/v1/jobs").json()[0]

    assert created.status_code == 202
    assert created.json()["source_name"] == "notes.md"
    assert completed["status"] == "succeeded"
    assert Path(completed["output_path"]).read_bytes() == b"# Notes"
    assert (data_dir / "workspace" / "jobs" / completed["id"] / "source.md").is_file()


def test_boot_token_protects_job_creation_but_not_status_reads(tmp_path: Path) -> None:
    """配置 boot token 后拒绝匿名 write, 同时保留本地只读状态查询。"""

    source = tmp_path / "source.txt"
    source.write_text("body", encoding="utf-8")
    data_dir = tmp_path / "app-data"
    app = create_app(Settings(data_dir=data_dir, boot_token="job-test-token"))
    app.state.translation_job_service = _test_job_service(data_dir)
    payload = {
        "source_path": str(source),
        "source_language": None,
        "target_language": "en",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-flash",
    }

    with TestClient(app) as client:
        denied = client.post("/api/v1/jobs", json=payload)
        listed = client.get("/api/v1/jobs")
        allowed = client.post(
            "/api/v1/jobs",
            headers={"X-PageFerry-Boot-Token": "job-test-token"},
            json=payload,
        )

    assert denied.status_code == 401
    assert listed.status_code == 200
    assert listed.json() == []
    assert allowed.status_code == 202
