"""持久化翻译任务的轻量 metadata, 不接触正文、文件内容或 API Key。"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from modules.translation.contracts import (
    DocumentKind,
    DocumentTranslationOptions,
    TranslationProgress,
    TranslationProgressStage,
    TranslationResult,
)

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class JobRecord:
    """表示 SQLite 中一条完整的翻译任务快照。"""

    id: str
    source_path: Path
    source_name: str
    output_path: Path | None
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
    options: DocumentTranslationOptions | None
    error_code: str | None
    translated_segments: int
    fallback_segments: int
    warning_codes: tuple[str, ...]
    created_at: str
    updated_at: str


class JobRepository:
    """提供小事务的任务创建、状态迁移和结果查询。"""

    def __init__(self, database: Path) -> None:
        """绑定一个已完成 migration 的 PageFerry SQLite 文件。"""

        self._database = database

    def create(
        self,
        *,
        job_id: str,
        source_path: Path,
        source_name: str,
        document_type: DocumentKind,
        provider_id: str,
        model_id: str,
        source_language: str | None,
        target_language: str,
        options: DocumentTranslationOptions | None = None,
    ) -> JobRecord:
        """创建 queued 任务并返回数据库中的规范化记录。"""

        now = _now()
        with self._connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO translation_jobs (
                    id, source_path, source_name, document_type, status, progress,
                    provider_id, model_id, source_language, target_language, options_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(source_path),
                    source_name,
                    document_type,
                    provider_id,
                    model_id,
                    source_language,
                    target_language,
                    _options_json(options),
                    now,
                    now,
                ),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> JobRecord:
        """按 id 读取任务, 不存在时抛出 KeyError。"""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM translation_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _to_job(row)

    def list_recent(self, limit: int = 30) -> list[JobRecord]:
        """按创建时间倒序读取有限数量的最近任务。"""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM translation_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_to_job(row) for row in rows]

    def mark_running(self, job_id: str) -> bool:
        """仅把 queued 任务推进为 running。"""

        return self._transition(
            job_id,
            from_status="queued",
            status="running",
            # extracting 尚未得到任何可测量工作量, 不能用固定值伪装进度。
            progress=0,
        )

    def update_progress(self, job_id: str, snapshot: TranslationProgress) -> bool:
        """持久化阶段与真实 segment 完成数, 并拒绝倒退或无效计数。"""

        _validate_progress_snapshot(snapshot)
        progress = _compatibility_progress(snapshot)
        stage_order = {"extracting": 0, "translating": 1, "formatting": 2}
        with self._connection() as connection, connection:
            cursor = connection.execute(
                """
                UPDATE translation_jobs
                SET progress_stage = ?, processed_segments = ?, total_segments = ?,
                    progress = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                  AND (
                      CASE progress_stage
                          WHEN 'extracting' THEN 0
                          WHEN 'translating' THEN 1
                          WHEN 'formatting' THEN 2
                      END < ?
                      OR (
                          progress_stage = ?
                          AND processed_segments <= ?
                          AND total_segments = ?
                      )
                  )
                """,
                (
                    snapshot.stage,
                    snapshot.processed_segments,
                    snapshot.total_segments,
                    progress,
                    _now(),
                    job_id,
                    stage_order[snapshot.stage],
                    snapshot.stage,
                    snapshot.processed_segments,
                    snapshot.total_segments,
                ),
            )
        return cursor.rowcount == 1

    def mark_succeeded(self, job_id: str, result: TranslationResult) -> None:
        """保存成功输出以及 fallback/warning 统计。"""

        processed_segments = result.translated_segments + result.fallback_segments
        with self._connection() as connection, connection:
            connection.execute(
                """
                UPDATE translation_jobs
                SET status = 'succeeded', progress = 100, progress_stage = 'formatting',
                    processed_segments = ?, total_segments = ?,
                    output_path = ?, error_code = NULL,
                    translated_segments = ?, fallback_segments = ?, warning_codes_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    processed_segments,
                    processed_segments,
                    str(result.output_path),
                    result.translated_segments,
                    result.fallback_segments,
                    json.dumps(result.warning_codes, ensure_ascii=False),
                    _now(),
                    job_id,
                ),
            )

    def mark_failed(self, job_id: str, error_code: str) -> None:
        """保存可面向 UI 的稳定错误码, 但不保存异常正文。"""

        with self._connection() as connection, connection:
            connection.execute(
                """
                UPDATE translation_jobs
                SET status = 'failed', error_code = ?, updated_at = ?
                WHERE id = ? AND status != 'cancelled'
                """,
                (error_code, _now(), job_id),
            )

    def cancel_queued(self, job_id: str) -> bool:
        """只取消尚未开始的任务, 避免伪装成可中断运行中 pipeline。"""

        return self._transition(
            job_id,
            from_status="queued",
            status="cancelled",
            progress=0,
        )

    def mark_interrupted_jobs(self) -> int:
        """启动时终止无法由当前进程恢复的 queued 与 running 任务。"""

        with self._connection() as connection, connection:
            cursor = connection.execute(
                """
                UPDATE translation_jobs
                SET status = 'failed', error_code = 'process_interrupted', updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (_now(),),
            )
        return cursor.rowcount

    def _transition(
        self,
        job_id: str,
        *,
        from_status: JobStatus,
        status: JobStatus,
        progress: int,
    ) -> bool:
        """用当前 status 作为乐观锁完成一次合法状态迁移。"""

        with self._connection() as connection, connection:
            cursor = connection.execute(
                """
                UPDATE translation_jobs
                SET status = ?, progress = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (status, progress, _now(), job_id, from_status),
            )
        return cursor.rowcount == 1

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """创建并可靠关闭带 Row 映射、外键与短等待的 SQLite connection。"""

        connection = sqlite3.connect(self._database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()


def _to_job(row: sqlite3.Row) -> JobRecord:
    """把 SQLite Row 转为不暴露存储细节的 JobRecord。"""

    raw_warnings = json.loads(row["warning_codes_json"] or "[]")
    warning_codes = tuple(item for item in raw_warnings if isinstance(item, str))
    return JobRecord(
        id=row["id"],
        source_path=Path(row["source_path"]),
        source_name=_safe_source_name(row["source_name"], row["source_path"]),
        output_path=Path(row["output_path"]) if row["output_path"] else None,
        document_type=row["document_type"],
        status=row["status"],
        progress=row["progress"],
        progress_stage=row["progress_stage"],
        processed_segments=row["processed_segments"],
        total_segments=row["total_segments"],
        provider_id=row["provider_id"] or "",
        model_id=row["model_id"] or "",
        source_language=row["source_language"],
        target_language=row["target_language"] or "zh-CN",
        options=_options_from_json(row["options_json"], row["document_type"]),
        error_code=row["error_code"],
        translated_segments=row["translated_segments"],
        fallback_segments=row["fallback_segments"],
        warning_codes=warning_codes,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_progress_snapshot(snapshot: TranslationProgress) -> None:
    """拒绝负数、超出总数或与阶段语义冲突的 progress snapshot。"""

    if snapshot.processed_segments < 0 or snapshot.total_segments < 0:
        raise ValueError("translation progress counts must be non-negative")
    if snapshot.processed_segments > snapshot.total_segments:
        raise ValueError("processed segments cannot exceed total segments")
    if snapshot.stage == "extracting" and (
        snapshot.processed_segments != 0 or snapshot.total_segments != 0
    ):
        raise ValueError("extracting progress cannot report segment counts")
    if snapshot.stage == "formatting" and snapshot.processed_segments != snapshot.total_segments:
        raise ValueError("formatting progress requires all segments to be processed")


def _compatibility_progress(snapshot: TranslationProgress) -> int:
    """为旧 client 计算真实翻译处理率, 不给 extracting 分配虚构权重。"""

    if snapshot.stage == "extracting":
        return 0
    if snapshot.stage == "formatting":
        return 100
    if snapshot.total_segments == 0:
        return 0
    return snapshot.processed_segments * 100 // snapshot.total_segments


def _options_json(options: DocumentTranslationOptions | None) -> str | None:
    """把经过 service 校验的格式选项编码为稳定 JSON。"""

    if options is None:
        return None
    payload: dict[str, str | bool] = {"kind": options.kind}
    if options.translate_tables is not None:
        payload["translate_tables"] = options.translate_tables
    if options.translate_notes is not None:
        payload["translate_notes"] = options.translate_notes
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _options_from_json(
    raw_options: str | None,
    document_type: DocumentKind,
) -> DocumentTranslationOptions | None:
    """读取任务创建时的选项 snapshot, 损坏的历史值安全退回空值。"""

    if not raw_options:
        return None
    try:
        payload = json.loads(raw_options)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != document_type:
        return None
    translate_tables = payload.get("translate_tables")
    translate_notes = payload.get("translate_notes")
    if translate_tables is not None and not isinstance(translate_tables, bool):
        return None
    if translate_notes is not None and not isinstance(translate_notes, bool):
        return None
    return DocumentTranslationOptions(
        kind=document_type,
        translate_tables=translate_tables,
        translate_notes=translate_notes,
    )


def _safe_source_name(source_name: str | None, source_path: str) -> str:
    """只返回 basename, 防止历史或异常 metadata 把绝对路径带到 API。"""

    candidate = source_name or source_path
    # Windows 路径在 macOS/Linux 的 Path 下不会识别反斜杠, 先统一分隔符再取 basename。
    basename = Path(candidate.replace("\\", "/")).name.strip()
    return basename or "document"


def _now() -> str:
    """返回带 UTC 时区的 ISO 时间戳。"""

    return datetime.now(UTC).isoformat()
