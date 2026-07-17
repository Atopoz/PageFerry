"""持久化 provider 连接与可启用 model inventory metadata."""

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class ProviderConfigRepositoryError(RuntimeError):
    """已经脱敏的 SQLite repository 错误."""


@dataclass(frozen=True, slots=True)
class ProviderConfigRecord:
    """持久化在 SQLite 中且不含凭据内容的 provider metadata."""

    provider_id: str
    model_id: str | None
    default_model_id: str | None
    base_url: str | None
    base_url_override: str | None
    catalog_version: str
    secret_ref: str
    probe_status: str
    probe_error_code: str | None
    latency_ms: int | None
    last_probed_at: str | None
    last_synced_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProviderModelRecord:
    """一个 provider 下已发现或 catalog 预置的 model metadata."""

    provider_id: str
    model_id: str
    upstream_model_id: str
    display_name: str
    source: str
    enabled: bool
    available: bool
    probe_status: str
    probe_error_code: str | None
    latency_ms: int | None
    last_seen_at: str | None
    last_probed_at: str | None


@dataclass(frozen=True, slots=True)
class ProviderInventoryItem:
    """写入 repository 的规范化 model 身份."""

    model_id: str
    upstream_model_id: str
    display_name: str
    source: str


@dataclass(frozen=True, slots=True)
class CustomProviderRecord:
    """一个持久化且不含凭据的 OpenAI-compatible provider 定义。"""

    provider_id: str
    display_name: str
    base_url: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProviderSecretCandidateRecord:
    """0012 去重前保存的一个 Keychain reference 候选。"""

    provider_id: str
    secret_ref: str
    priority: int


class SQLiteProviderConfigRepository:
    """在应用 SQLite 数据库中读写 provider 与 model metadata."""

    def __init__(self, database_path: Path) -> None:
        """把 repository 绑定到一个应用数据库路径."""

        self._database_path = database_path

    def list(self) -> tuple[ProviderConfigRecord, ...]:
        """返回全部已配置 provider metadata."""

        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM provider_configs ORDER BY provider_id"
                ).fetchall()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError("Could not read provider configurations.") from None
        return tuple(_record_from_row(row) for row in rows)

    def list_custom_providers(self) -> tuple[CustomProviderRecord, ...]:
        """按创建顺序返回所有用户自定义 provider 定义。"""

        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_CUSTOM_PROVIDER_COLUMNS}
                    FROM custom_provider_definitions
                    ORDER BY created_at, provider_id
                    """
                ).fetchall()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not read custom provider definitions."
            ) from None
        return tuple(_custom_provider_from_row(row) for row in rows)

    def get_custom_provider(self, provider_id: str) -> CustomProviderRecord | None:
        """返回指定自定义 provider 定义, 不存在时返回空值。"""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_CUSTOM_PROVIDER_COLUMNS}
                    FROM custom_provider_definitions
                    WHERE provider_id = ?
                    """,
                    (provider_id,),
                ).fetchone()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not read the custom provider definition."
            ) from None
        return _custom_provider_from_row(row) if row is not None else None

    def create_custom_provider(
        self,
        *,
        provider_id: str,
        display_name: str,
        base_url: str,
        created_at: str,
    ) -> CustomProviderRecord:
        """持久化一个尚未配置凭据的自定义 provider 定义。"""

        try:
            with self._connection() as connection, connection:
                connection.execute(
                    """
                    INSERT INTO custom_provider_definitions (
                        provider_id, display_name, base_url, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (provider_id, display_name, base_url, created_at, created_at),
                )
                row = connection.execute(
                    f"""
                    SELECT {_CUSTOM_PROVIDER_COLUMNS}
                    FROM custom_provider_definitions
                    WHERE provider_id = ?
                    """,
                    (provider_id,),
                ).fetchone()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not create the custom provider definition."
            ) from None
        if row is None:
            raise ProviderConfigRepositoryError("The custom provider definition was not created.")
        return _custom_provider_from_row(row)

    def list_secret_reconciliation_candidates(
        self,
    ) -> tuple[ProviderSecretCandidateRecord, ...]:
        """按 provider 与 legacy 优先级返回待 reconciliation 的 reference。"""

        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT provider_id, secret_ref, priority
                    FROM provider_secret_reconciliation_candidates
                    ORDER BY provider_id, priority, secret_ref
                    """
                ).fetchall()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not read provider secret reconciliation candidates."
            ) from None
        return tuple(
            ProviderSecretCandidateRecord(
                provider_id=row["provider_id"],
                secret_ref=row["secret_ref"],
                priority=row["priority"],
            )
            for row in rows
        )

    def replace_secret_reference(
        self,
        *,
        provider_id: str,
        expected_reference: str,
        replacement_reference: str,
    ) -> bool:
        """仅在当前 reference 未变化时切换到可用 legacy Keychain 条目。"""

        try:
            with self._connection() as connection, connection:
                cursor = connection.execute(
                    """
                    UPDATE provider_configs
                    SET secret_ref = ?
                    WHERE provider_id = ? AND secret_ref = ?
                    """,
                    (replacement_reference, provider_id, expected_reference),
                )
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not reconcile the provider secret reference."
            ) from None
        return cursor.rowcount == 1

    def secret_reference_is_in_use(self, reference: str) -> bool:
        """判断 reference 是否仍被任一 surviving provider 配置引用。"""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM provider_configs
                    WHERE secret_ref = ?
                    LIMIT 1
                    """,
                    (reference,),
                ).fetchone()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not verify provider secret reference ownership."
            ) from None
        return row is not None

    def clear_secret_reconciliation_candidates(self, provider_id: str) -> None:
        """在 Keychain cleanup 全部成功后清除一个 provider 的 staging 记录。"""

        try:
            with self._connection() as connection, connection:
                connection.execute(
                    """
                    DELETE FROM provider_secret_reconciliation_candidates
                    WHERE provider_id = ?
                    """,
                    (provider_id,),
                )
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not finish provider secret reconciliation."
            ) from None

    def get(self, provider_id: str) -> ProviderConfigRecord | None:
        """返回指定 provider 配置, 不存在时返回空值."""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM provider_configs WHERE provider_id = ?",
                    (provider_id,),
                ).fetchone()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not read the provider configuration."
            ) from None
        return _record_from_row(row) if row is not None else None

    def list_models(self, provider_id: str) -> tuple[ProviderModelRecord, ...]:
        """返回 provider 的已持久化 model inventory."""

        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_MODEL_COLUMNS}
                    FROM provider_model_configs
                    WHERE provider_id = ?
                    ORDER BY enabled DESC, model_id
                    """,
                    (provider_id,),
                ).fetchall()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError("Could not read provider models.") from None
        return tuple(_model_record_from_row(row) for row in rows)

    def get_model(self, provider_id: str, model_id: str) -> ProviderModelRecord | None:
        """返回一个 provider-model 设置."""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_MODEL_COLUMNS}
                    FROM provider_model_configs
                    WHERE provider_id = ? AND model_id = ?
                    """,
                    (provider_id, model_id),
                ).fetchone()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError("Could not read the provider model.") from None
        return _model_record_from_row(row) if row is not None else None

    def save_successful_probe(
        self,
        *,
        provider_id: str,
        default_model_id: str,
        base_url: str,
        base_url_override: str | None,
        catalog_version: str,
        secret_ref: str,
        probe_latencies: Mapping[str, int],
        inventory: Sequence[ProviderInventoryItem],
        probed_at: str,
    ) -> tuple[ProviderConfigRecord, tuple[ProviderModelRecord, ...]]:
        """原子更新 metadata, 并返回同一 transaction 内读取的完整 snapshot."""

        enabled_ids = set(probe_latencies)
        latency_ms = probe_latencies[default_model_id]
        try:
            with self._connection() as connection, connection:
                connection.execute(
                    """
                    INSERT INTO provider_configs (
                        id, provider_id, model_id, default_model_id, base_url,
                        base_url_override, catalog_version, secret_ref, probe_status,
                        probe_error_code, latency_ms, last_probed_at, last_synced_at,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', NULL, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id) DO UPDATE SET
                        model_id = excluded.model_id,
                        default_model_id = excluded.default_model_id,
                        base_url = excluded.base_url,
                        base_url_override = excluded.base_url_override,
                        catalog_version = excluded.catalog_version,
                        secret_ref = excluded.secret_ref,
                        probe_status = 'succeeded',
                        probe_error_code = NULL,
                        latency_ms = excluded.latency_ms,
                        last_probed_at = excluded.last_probed_at,
                        last_synced_at = excluded.last_synced_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        provider_id,
                        provider_id,
                        default_model_id,
                        default_model_id,
                        base_url,
                        base_url_override,
                        catalog_version,
                        secret_ref,
                        latency_ms,
                        probed_at,
                        probed_at,
                        probed_at,
                        probed_at,
                    ),
                )
                # 不删除旧 model, 避免远端下架后连历史选择都无法解释。
                connection.execute(
                    """
                    UPDATE provider_model_configs
                    SET available = 0, enabled = 0, updated_at = ?
                    WHERE provider_id = ?
                    """,
                    (probed_at, provider_id),
                )
                for item in inventory:
                    enabled = item.model_id in enabled_ids
                    connection.execute(
                        """
                        INSERT INTO provider_model_configs (
                            provider_id, model_id, upstream_model_id, display_name,
                            source, enabled, available, probe_status, probe_error_code,
                            latency_ms, last_seen_at, last_probed_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, ?, ?, ?)
                        ON CONFLICT(provider_id, model_id) DO UPDATE SET
                            upstream_model_id = excluded.upstream_model_id,
                            display_name = excluded.display_name,
                            source = excluded.source,
                            enabled = excluded.enabled,
                            available = 1,
                            probe_status = CASE
                                WHEN excluded.enabled = 1 THEN 'succeeded'
                                ELSE provider_model_configs.probe_status
                            END,
                            probe_error_code = CASE
                                WHEN excluded.enabled = 1 THEN NULL
                                ELSE provider_model_configs.probe_error_code
                            END,
                            latency_ms = CASE
                                WHEN excluded.enabled = 1 THEN excluded.latency_ms
                                ELSE provider_model_configs.latency_ms
                            END,
                            last_seen_at = excluded.last_seen_at,
                            last_probed_at = CASE
                                WHEN excluded.enabled = 1 THEN excluded.last_probed_at
                                ELSE provider_model_configs.last_probed_at
                            END,
                            updated_at = excluded.updated_at
                        """,
                        (
                            provider_id,
                            item.model_id,
                            item.upstream_model_id,
                            item.display_name,
                            item.source,
                            int(enabled),
                            "succeeded" if enabled else "not_tested",
                            probe_latencies.get(item.model_id),
                            probed_at,
                            probed_at if enabled else None,
                            probed_at,
                            probed_at,
                        ),
                    )
                row = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM provider_configs WHERE provider_id = ?",
                    (provider_id,),
                ).fetchone()
                model_rows = connection.execute(
                    f"""
                    SELECT {_MODEL_COLUMNS}
                    FROM provider_model_configs
                    WHERE provider_id = ?
                    ORDER BY enabled DESC, model_id
                    """,
                    (provider_id,),
                ).fetchall()
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not save the provider configuration."
            ) from None
        if row is None:
            raise ProviderConfigRepositoryError("The provider configuration was not saved.")
        return (
            _record_from_row(row),
            tuple(_model_record_from_row(model_row) for model_row in model_rows),
        )

    def delete(self, provider_id: str) -> bool:
        """删除 provider metadata 与其 model inventory."""

        try:
            with self._connection() as connection, connection:
                connection.execute(
                    "DELETE FROM provider_model_configs WHERE provider_id = ?",
                    (provider_id,),
                )
                cursor = connection.execute(
                    "DELETE FROM provider_configs WHERE provider_id = ?",
                    (provider_id,),
                )
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not delete the provider configuration."
            ) from None
        return cursor.rowcount > 0

    def delete_custom_provider(self, provider_id: str) -> bool:
        """原子删除自定义定义及其 provider 配置和 model inventory。"""

        try:
            with self._connection() as connection, connection:
                # definition 没有外键绑定旧 schema, 因此在同一事务内显式清理三类记录。
                connection.execute(
                    "DELETE FROM provider_model_configs WHERE provider_id = ?",
                    (provider_id,),
                )
                connection.execute(
                    "DELETE FROM provider_configs WHERE provider_id = ?",
                    (provider_id,),
                )
                cursor = connection.execute(
                    "DELETE FROM custom_provider_definitions WHERE provider_id = ?",
                    (provider_id,),
                )
        except sqlite3.Error:
            raise ProviderConfigRepositoryError(
                "Could not delete the custom provider definition."
            ) from None
        return cursor.rowcount > 0

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """打开短生命周期 connection, lock timeout 与应用启动保持一致."""

        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()


_RECORD_COLUMNS = """
provider_id,
model_id,
default_model_id,
base_url,
base_url_override,
catalog_version,
secret_ref,
probe_status,
probe_error_code,
latency_ms,
last_probed_at,
last_synced_at,
created_at,
updated_at
"""

_MODEL_COLUMNS = """
provider_id,
model_id,
upstream_model_id,
display_name,
source,
enabled,
available,
probe_status,
probe_error_code,
latency_ms,
last_seen_at,
last_probed_at
"""

_CUSTOM_PROVIDER_COLUMNS = """
provider_id,
display_name,
base_url,
created_at,
updated_at
"""


def _record_from_row(row: sqlite3.Row) -> ProviderConfigRecord:
    """把 SQLite row 转换为不可变 provider record."""

    return ProviderConfigRecord(
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        default_model_id=row["default_model_id"] or row["model_id"],
        base_url=row["base_url"],
        base_url_override=row["base_url_override"],
        catalog_version=row["catalog_version"],
        secret_ref=row["secret_ref"],
        probe_status=row["probe_status"],
        probe_error_code=row["probe_error_code"],
        latency_ms=row["latency_ms"],
        last_probed_at=row["last_probed_at"],
        last_synced_at=row["last_synced_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _model_record_from_row(row: sqlite3.Row) -> ProviderModelRecord:
    """把 SQLite row 转换为不可变 model record."""

    return ProviderModelRecord(
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        upstream_model_id=row["upstream_model_id"],
        display_name=row["display_name"],
        source=row["source"],
        enabled=bool(row["enabled"]),
        available=bool(row["available"]),
        probe_status=row["probe_status"],
        probe_error_code=row["probe_error_code"],
        latency_ms=row["latency_ms"],
        last_seen_at=row["last_seen_at"],
        last_probed_at=row["last_probed_at"],
    )


def _custom_provider_from_row(row: sqlite3.Row) -> CustomProviderRecord:
    """把 SQLite row 转换为不可变自定义 provider record。"""

    return CustomProviderRecord(
        provider_id=row["provider_id"],
        display_name=row["display_name"],
        base_url=row["base_url"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "CustomProviderRecord",
    "ProviderConfigRecord",
    "ProviderConfigRepositoryError",
    "ProviderInventoryItem",
    "ProviderModelRecord",
    "ProviderSecretCandidateRecord",
    "SQLiteProviderConfigRepository",
]
