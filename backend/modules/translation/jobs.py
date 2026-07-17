"""编排文件接收、格式 runtime、provider 与任务状态, 避免 HTTP 层承载业务逻辑。"""

import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from db.jobs import JobRecord, JobRepository
from modules.model_catalog.provider_config import (
    ProviderConfigError,
)
from modules.translation.contracts import (
    BatchTranslator,
    DocumentKind,
    DocumentPipeline,
    DocumentTranslationOptions,
    TranslationProgress,
    TranslationRequest,
)

logger = logging.getLogger(__name__)

SUPPORTED_DOCUMENTS: dict[str, DocumentKind] = {
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".txt": "txt",
    ".md": "md",
}
DEFAULT_MAX_SOURCE_BYTES = 200 * 1024 * 1024


class TranslatorResolver(Protocol):
    """定义任务编排层所需的最小 provider 配置能力。"""

    def build_translator(self, provider_id: str, model_id: str) -> BatchTranslator:
        """为一个已验证配置构造不泄露 credential 的 translator。"""
        ...


PipelineFactory = Callable[
    [DocumentKind, BatchTranslator, DocumentTranslationOptions | None],
    DocumentPipeline,
]


class JobServiceError(RuntimeError):
    """携带稳定错误码与 HTTP 建议状态的任务输入错误。"""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        """保存可安全展示的错误信息, 不保留正文或上游响应。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TranslationJobService:
    """创建并运行本地翻译任务, 同时保持每个 job 的文件边界独立。"""

    def __init__(
        self,
        repository: JobRepository,
        translator_resolver: TranslatorResolver,
        *,
        workspace_dir: Path,
        output_dir: Path,
        pipeline_factory: PipelineFactory | None = None,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    ) -> None:
        """注入持久化、provider 与可测试的 pipeline factory。"""

        self._repository = repository
        self._translator_resolver = translator_resolver
        self._workspace_dir = workspace_dir
        self._output_dir = output_dir
        self._pipeline_factory = pipeline_factory or _build_pipeline
        self._max_source_bytes = max_source_bytes

    def list_recent(self, limit: int = 30) -> list[JobRecord]:
        """返回数量受限的最近任务, 避免 UI 轮询无界读取历史。"""

        return self._repository.list_recent(limit)

    def get(self, job_id: str) -> JobRecord:
        """读取单个任务, 并把 repository 的 KeyError 转成公开错误。"""

        try:
            return self._repository.get(job_id)
        except KeyError:
            raise JobServiceError("job_not_found", "找不到该翻译任务。", status_code=404) from None

    def create_path_job(
        self,
        *,
        source_path: str,
        source_language: str | None,
        target_language: str,
        provider_id: str,
        model_id: str,
        options: DocumentTranslationOptions | None = None,
    ) -> JobRecord:
        """校验 Tauri 提供的本地路径, 并创建不复制原文件的 queued 任务。"""

        try:
            source = Path(source_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            raise JobServiceError("invalid_source", "文件路径无效。", status_code=400) from None
        document_kind = _validate_source(source, self._max_source_bytes)
        normalized_source, normalized_target = _validate_languages(
            source_language,
            target_language,
        )
        normalized_options = _normalize_document_options(document_kind, options)
        self._validate_provider(provider_id, model_id)
        return self._repository.create(
            job_id=str(uuid4()),
            source_path=source,
            source_name=source.name,
            document_type=document_kind,
            provider_id=provider_id,
            model_id=model_id,
            source_language=normalized_source,
            target_language=normalized_target,
            options=normalized_options,
        )

    def create_upload_job(
        self,
        *,
        file_name: str,
        stream: BinaryIO,
        source_language: str | None,
        target_language: str,
        provider_id: str,
        model_id: str,
        options: DocumentTranslationOptions | None = None,
    ) -> JobRecord:
        """把浏览器上传流原子写入 app workspace, 再创建 queued 任务。"""

        safe_name = _safe_upload_name(file_name)
        document_kind = _document_kind_for_name(safe_name)
        normalized_source, normalized_target = _validate_languages(
            source_language,
            target_language,
        )
        normalized_options = _normalize_document_options(document_kind, options)
        self._validate_provider(provider_id, model_id)

        job_id = str(uuid4())
        job_workspace = self._workspace_dir / "jobs" / job_id
        source = job_workspace / f"source{Path(safe_name).suffix.lower()}"
        job_workspace.mkdir(parents=True, exist_ok=False)
        try:
            _copy_upload_atomically(stream, source, self._max_source_bytes)
            return self._repository.create(
                job_id=job_id,
                source_path=source,
                source_name=safe_name,
                document_type=document_kind,
                provider_id=provider_id,
                model_id=model_id,
                source_language=normalized_source,
                target_language=normalized_target,
                options=normalized_options,
            )
        except Exception:
            # DB 与 filesystem 无法共享 transaction; 创建失败时删除这个 job 唯一拥有的目录。
            shutil.rmtree(job_workspace, ignore_errors=True)
            raise

    def run(self, job_id: str) -> JobRecord:
        """在 background thread 中执行一个 queued 任务并持久化最终状态。"""

        if not self._repository.mark_running(job_id):
            return self.get(job_id)
        job = self._repository.get(job_id)
        try:
            translator = self._translator_resolver.build_translator(
                job.provider_id,
                job.model_id,
            )
            pipeline = self._pipeline_factory(job.document_type, translator, job.options)
            result = pipeline.translate(
                TranslationRequest(
                    source_path=job.source_path,
                    # 每个任务独占目录, 避免同名输入或重复翻译覆盖旧结果。
                    output_dir=self._output_dir / job.id,
                    source_language=job.source_language,
                    target_language=job.target_language,
                    provider_id=job.provider_id,
                    model_id=job.model_id,
                ),
                report_progress=partial(self._report_progress, job_id),
            )
        except ProviderConfigError as error:
            self._repository.mark_failed(job_id, f"provider_{error.code.value}")
        except (FileNotFoundError, PermissionError):
            self._repository.mark_failed(job_id, "source_unavailable")
        except Exception:
            # 只记录 job id 和 traceback; 正文与 API Key 从未进入日志参数。
            logger.exception("Document pipeline failed for job %s", job_id)
            self._repository.mark_failed(job_id, "pipeline_failed")
        else:
            self._repository.mark_succeeded(job_id, result)
        return self._repository.get(job_id)

    def _report_progress(self, job_id: str, progress: TranslationProgress) -> None:
        """把同步 pipeline 的真实阶段 snapshot 写入当前 running job。"""

        if not self._repository.update_progress(job_id, progress):
            logger.debug("Ignored stale progress update for job %s", job_id)

    def _validate_provider(self, provider_id: str, model_id: str) -> None:
        """在写入任务前确认 provider 已配置, 避免制造必然失败的队列项。"""

        try:
            self._translator_resolver.build_translator(provider_id, model_id)
        except ProviderConfigError as error:
            raise JobServiceError(
                f"provider_{error.code.value}",
                error.message,
                status_code=error.status_code,
            ) from None


def _build_pipeline(
    document_kind: DocumentKind,
    translator: BatchTranslator,
    options: DocumentTranslationOptions | None,
) -> DocumentPipeline:
    """按 sibling runtime 选择格式实现, 不建立隐藏的万能文档模块。"""

    if document_kind == "docx":
        from modules.docx import DocxPipeline

        translate_tables = options.translate_tables if options is not None else True
        return DocxPipeline(translator, translate_tables=translate_tables)
    if document_kind == "pptx":
        from modules.pptx import PptxPipeline

        translate_tables = options.translate_tables if options is not None else True
        translate_notes = options.translate_notes if options is not None else True
        return PptxPipeline(
            translator,
            translate_tables=translate_tables,
            translate_notes=translate_notes,
        )
    if document_kind == "xlsx":
        from modules.xlsx import XlsxPipeline

        return XlsxPipeline(translator)
    if document_kind in {"txt", "md"}:
        from modules.plain_text import PlainTextPipeline

        return PlainTextPipeline(document_kind, translator)
    raise JobServiceError("unsupported_format", "尚不支持该文件格式。", status_code=400)


def _normalize_document_options(
    document_kind: DocumentKind,
    options: DocumentTranslationOptions | None,
) -> DocumentTranslationOptions | None:
    """补齐格式默认值, 并拒绝把另一种格式的选项误用于当前文件。"""

    if options is not None and options.kind != document_kind:
        raise JobServiceError(
            "invalid_document_options",
            "高级选项与当前文件格式不匹配。",
            status_code=400,
        )
    if document_kind == "docx":
        return DocumentTranslationOptions(
            kind="docx",
            translate_tables=(
                options.translate_tables
                if options is not None and options.translate_tables is not None
                else True
            ),
        )
    if document_kind == "pptx":
        return DocumentTranslationOptions(
            kind="pptx",
            translate_tables=(
                options.translate_tables
                if options is not None and options.translate_tables is not None
                else True
            ),
            translate_notes=(
                options.translate_notes
                if options is not None and options.translate_notes is not None
                else True
            ),
        )
    if document_kind == "xlsx":
        if options is not None:
            raise JobServiceError(
                "invalid_document_options",
                "当前文件格式没有可配置的高级选项。",
                status_code=400,
            )
        return None
    if options is not None:
        raise JobServiceError(
            "invalid_document_options",
            "当前文件格式没有可配置的高级选项。",
            status_code=400,
        )
    return None


def _validate_source(source: Path, max_bytes: int) -> DocumentKind:
    """确认本地输入的类型与大小都符合统一 source policy。"""

    if not source.is_file():
        raise JobServiceError("source_not_found", "找不到要翻译的文件。", status_code=404)
    try:
        source_size = source.stat().st_size
    except OSError:
        raise JobServiceError(
            "source_unavailable", "无法读取要翻译的文件。", status_code=400
        ) from None
    _validate_source_size(source_size, max_bytes)
    return _document_kind_for_name(source.name)


def _validate_source_size(size: int, max_bytes: int) -> None:
    """用同一错误 contract 拒绝 path 与 upload 入口的空文件或超限文件。"""

    if size <= 0:
        raise JobServiceError("empty_file", "不能翻译空文件。", status_code=400)
    if size > max_bytes:
        raise JobServiceError(
            "file_too_large",
            "文件超过 200 MB 限制。",
            status_code=413,
        )


def _document_kind_for_name(file_name: str) -> DocumentKind:
    """把安全文件名映射为已实现的 document kind。"""

    suffix = Path(file_name).suffix.lower()
    document_kind = SUPPORTED_DOCUMENTS.get(suffix)
    if document_kind is None:
        raise JobServiceError(
            "unsupported_format",
            "仅支持 DOCX、PPTX、XLSX、TXT 与 Markdown。",
            status_code=400,
        )
    return document_kind


def _safe_upload_name(file_name: str) -> str:
    """去掉浏览器可能附带的路径, 仅保留可展示的 basename。"""

    normalized = file_name.replace("\\", "/")
    safe_name = Path(normalized).name.strip()
    if not safe_name or safe_name in {".", ".."}:
        raise JobServiceError("invalid_file_name", "上传文件名无效。", status_code=400)
    return safe_name


def _validate_languages(
    source_language: str | None,
    target_language: str,
) -> tuple[str | None, str]:
    """规范化自动识别标记并拒绝空目标语言。"""

    source = source_language.strip() if source_language else None
    if source in {"", "auto"}:
        source = None
    target = target_language.strip()
    if not target:
        raise JobServiceError("invalid_target_language", "请选择目标语言。", status_code=400)
    return source, target


def _copy_upload_atomically(
    stream: BinaryIO,
    destination: Path,
    max_bytes: int,
) -> None:
    """分块限制上传大小, fsync 后再原子发布 workspace 源文件。"""

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".upload-",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                _validate_source_size(total, max_bytes)
                handle.write(chunk)
            _validate_source_size(total, max_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
