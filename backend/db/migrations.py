"""按文件名顺序执行带 checksum 的单向 SQLite migration。"""

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_NAME_PATTERN = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
PROVIDER_CONFIG_UNIQUE_MIGRATION = "0012_unique_provider_configs.sql"


@dataclass(frozen=True, slots=True)
class Migration:
    """保存一个 migration 的文件名、校验和与 SQL 正文。"""

    name: str
    checksum: str
    sql: str


def _load_migrations() -> list[Migration]:
    """读取并验证所有 migration 文件名、版本唯一性与 checksum。"""

    migrations: list[Migration] = []
    versions: set[str] = set()

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = MIGRATION_NAME_PATTERN.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"Invalid migration filename: {path.name}")

        version = match.group("version")
        if version in versions:
            raise RuntimeError(f"Duplicate migration version: {version}")
        versions.add(version)

        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                name=path.name,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )

    if not migrations:
        raise RuntimeError(f"No database migrations found in {MIGRATIONS_DIR}")
    return migrations


def _iter_statements(script: str) -> Iterator[str]:
    """按 SQLite 自己的 complete_statement 规则安全切分 SQL。"""

    buffer: list[str] = []

    for line in script.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()

    remainder = "".join(buffer).strip()
    if remainder:
        raise RuntimeError("Migration contains an incomplete SQL statement")


def _stage_provider_secret_candidates(connection: sqlite3.Connection) -> None:
    """在 0012 去重前保存所有可安全重试的 Keychain reference。"""

    # 0012 已发布且不能改 checksum, 因此 staging 必须与它处于同一 transaction:
    # migration 失败时两者一起 rollback, 成功时 0013 再接管长期 schema。
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_secret_reconciliation_candidates (
            provider_id TEXT NOT NULL,
            secret_ref TEXT NOT NULL,
            priority INTEGER NOT NULL CHECK (priority > 0),
            staged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (provider_id, secret_ref)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_provider_secret_candidates_priority
        ON provider_secret_reconciliation_candidates(provider_id, priority)
        """
    )
    connection.execute(
        """
        WITH ranked AS (
            SELECT
                provider_id,
                secret_ref,
                row_number() OVER (
                    PARTITION BY provider_id
                    ORDER BY updated_at DESC, created_at DESC, rowid DESC
                ) AS priority,
                count(*) OVER (PARTITION BY provider_id) AS duplicate_count
            FROM provider_configs
        ),
        safe_candidates AS (
            SELECT provider_id, secret_ref, min(priority) AS priority
            FROM ranked
            WHERE duplicate_count > 1
              AND secret_ref LIKE 'keychain:provider/%'
              AND length(trim(secret_ref)) > length('keychain:provider/')
            GROUP BY provider_id, secret_ref
        )
        INSERT INTO provider_secret_reconciliation_candidates (
            provider_id, secret_ref, priority
        )
        SELECT provider_id, secret_ref, priority
        FROM safe_candidates
        WHERE 1
        ON CONFLICT(provider_id, secret_ref) DO UPDATE SET
            priority = min(
                provider_secret_reconciliation_candidates.priority,
                excluded.priority
            )
        """
    )


def apply_migrations(connection: sqlite3.Connection) -> None:
    """逐个事务应用未执行 migration, 并拒绝被篡改的历史文件。"""

    # migration 表先独立提交, 后续每个版本才能拥有清晰的事务边界。
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()

    for migration in _load_migrations():
        connection.execute("BEGIN IMMEDIATE")
        try:
            applied = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE name = ?",
                (migration.name,),
            ).fetchone()
            if applied is not None:
                if applied[0] != migration.checksum:
                    raise RuntimeError(f"Applied migration checksum changed: {migration.name}")
                connection.commit()
                continue

            if migration.name == PROVIDER_CONFIG_UNIQUE_MIGRATION:
                _stage_provider_secret_candidates(connection)
            for statement in _iter_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (name, checksum) VALUES (?, ?)",
                (migration.name, migration.checksum),
            )
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
