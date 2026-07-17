"""暴露翻译任务的创建与只读状态 API, 不直接操作 SQLite 或文档 runtime。"""

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from api.security import require_boot_token
from db.jobs import JobRecord, JobStatus
from modules.translation.contracts import (
    DocumentKind,
    DocumentTranslationOptions,
    TranslationProgressStage,
)
from modules.translation.jobs import JobServiceError, TranslationJobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


class DocxOptionsRequest(BaseModel):
    """接收 DOCX runtime 当前真实支持的高级选项。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["docx"]
    translate_tables: bool = True
    bilingual: bool = False


class PptxOptionsRequest(BaseModel):
    """接收 PPTX runtime 当前真实支持的高级选项。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pptx"]
    translate_tables: bool = True
    translate_notes: bool = True


DocumentOptionsRequest = Annotated[
    DocxOptionsRequest | PptxOptionsRequest,
    Field(discriminator="kind"),
]


class PathJobRequest(BaseModel):
    """接收 Tauri 原生文件选择器返回的本地路径与翻译选项。"""

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1)
    source_language: str | None = None
    target_language: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    options: DocumentOptionsRequest | None = None


class DocxOptionsResponse(BaseModel):
    """返回创建任务时冻结的 DOCX 选项。"""

    kind: Literal["docx"]
    translate_tables: bool
    bilingual: bool


class PptxOptionsResponse(BaseModel):
    """返回创建任务时冻结的 PPTX 选项。"""

    kind: Literal["pptx"]
    translate_tables: bool
    translate_notes: bool


DocumentOptionsResponse = Annotated[
    DocxOptionsResponse | PptxOptionsResponse,
    Field(discriminator="kind"),
]


class JobArtifactResponse(BaseModel):
    """返回一次模型翻译生成的一个派生文件。"""

    kind: Literal["translated", "bilingual"]
    path: Path


class JobResponse(BaseModel):
    """返回任务 metadata, 不暴露本地源路径或文档正文。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    source_name: str
    output_path: Path | None
    artifacts: tuple[JobArtifactResponse, ...]
    document_type: DocumentKind
    status: JobStatus
    progress: int
    progress_stage: TranslationProgressStage
    processed_segments: int
    total_segments: int
    provider_id: str
    model_id: str
    source_language: str | None
    target_language: str
    error_code: str | None
    translated_segments: int
    fallback_segments: int
    warning_codes: tuple[str, ...]
    options: DocumentOptionsResponse | None
    created_at: str
    updated_at: str


class JobErrorResponse(BaseModel):
    """定义前端可稳定处理的任务错误。"""

    code: str
    message: str


def get_job_service(request: Request) -> TranslationJobService:
    """读取应用启动时创建的任务编排 service。"""

    return request.app.state.translation_job_service


@router.get("", response_model=list[JobResponse])
def list_jobs(
    service: Annotated[TranslationJobService, Depends(get_job_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[JobResponse]:
    """按时间倒序返回数量受限的最近任务。"""

    return [_job_response(job) for job in service.list_recent(limit)]


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    service: Annotated[TranslationJobService, Depends(get_job_service)],
) -> JobResponse | JSONResponse:
    """返回一个任务的最新持久化状态。"""

    try:
        return _job_response(service.get(job_id))
    except JobServiceError as error:
        return _error_response(error)


@router.post("", response_model=JobResponse, status_code=202)
def create_path_job(
    payload: PathJobRequest,
    background_tasks: BackgroundTasks,
    service: Annotated[TranslationJobService, Depends(get_job_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> JobResponse | JSONResponse:
    """创建本地路径任务, 并把同步 Office pipeline 放入 threadpool background task。"""

    try:
        values = payload.model_dump(exclude={"options"})
        job = service.create_path_job(
            **values,
            options=_document_options(payload.options),
        )
    except JobServiceError as error:
        return _error_response(error)
    background_tasks.add_task(service.run, job.id)
    return _job_response(job)


@router.post("/upload", response_model=JobResponse, status_code=202)
def create_upload_job(
    background_tasks: BackgroundTasks,
    service: Annotated[TranslationJobService, Depends(get_job_service)],
    _: Annotated[None, Depends(require_boot_token)],
    file: Annotated[UploadFile, File()],
    source_language: Annotated[str, Form()] = "auto",
    target_language: Annotated[str, Form()] = "zh-CN",
    provider_id: Annotated[str, Form()] = "deepseek",
    model_id: Annotated[str, Form()] = "deepseek-v4-flash",
    options: Annotated[str | None, Form()] = None,
) -> JobResponse | JSONResponse:
    """接收 Web 开发模式上传, 并使用与本地路径相同的任务执行逻辑。"""

    try:
        job = service.create_upload_job(
            file_name=file.filename or "",
            stream=file.file,
            source_language=source_language,
            target_language=target_language,
            provider_id=provider_id,
            model_id=model_id,
            options=_parse_upload_options(options),
        )
    except JobServiceError as error:
        return _error_response(error)
    background_tasks.add_task(service.run, job.id)
    return _job_response(job)


def _job_response(job: JobRecord) -> JobResponse:
    """显式投影 repository record, 确保未来字段不会意外暴露给 renderer。"""

    options = None
    if job.options is not None and job.options.kind in {"docx", "pptx"}:
        if job.options.kind == "docx":
            options = DocxOptionsResponse(
                kind="docx",
                translate_tables=job.options.translate_tables is not False,
                bilingual=job.options.bilingual is True,
            )
        else:
            options = PptxOptionsResponse(
                kind="pptx",
                translate_tables=job.options.translate_tables is not False,
                translate_notes=job.options.translate_notes is not False,
            )
    return JobResponse(
        id=job.id,
        source_name=job.source_name,
        output_path=job.output_path,
        artifacts=tuple(
            JobArtifactResponse(kind=artifact.kind, path=artifact.path)
            for artifact in job.artifacts
        ),
        document_type=job.document_type,
        status=job.status,
        progress=job.progress,
        progress_stage=job.progress_stage,
        processed_segments=job.processed_segments,
        total_segments=job.total_segments,
        provider_id=job.provider_id,
        model_id=job.model_id,
        source_language=job.source_language,
        target_language=job.target_language,
        error_code=job.error_code,
        translated_segments=job.translated_segments,
        fallback_segments=job.fallback_segments,
        warning_codes=job.warning_codes,
        options=options,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _document_options(
    options: DocxOptionsRequest | PptxOptionsRequest | None,
) -> DocumentTranslationOptions | None:
    """把 HTTP schema 转为 modules 层不依赖 Pydantic 的不可变 contract。"""

    if options is None:
        return None
    return DocumentTranslationOptions(
        kind=options.kind,
        translate_tables=options.translate_tables,
        translate_notes=(
            options.translate_notes if isinstance(options, PptxOptionsRequest) else None
        ),
        bilingual=(options.bilingual if isinstance(options, DocxOptionsRequest) else None),
    )


def _parse_upload_options(raw_options: str | None) -> DocumentTranslationOptions | None:
    """解析 multipart 中的 JSON 选项, 同时保持与 path endpoint 相同的校验规则。"""

    if raw_options is None or not raw_options.strip():
        return None
    try:
        options = TypeAdapter(DocumentOptionsRequest).validate_json(raw_options)
    except ValidationError:
        raise JobServiceError(
            "invalid_document_options",
            "高级选项格式无效。",
            status_code=400,
        ) from None
    return _document_options(options)


def _error_response(error: JobServiceError) -> JSONResponse:
    """把 service 错误序列化为不含内部异常的固定结构。"""

    payload = JobErrorResponse(code=error.code, message=error.message)
    return JSONResponse(status_code=error.status_code, content=payload.model_dump())
