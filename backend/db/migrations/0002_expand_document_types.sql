-- 用 SQLite table rebuild 扩展首期可接受的 TXT 与 Markdown document kind。
DROP TABLE IF EXISTS translation_jobs_next;

CREATE TABLE translation_jobs_next (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    output_path TEXT,
    document_type TEXT NOT NULL CHECK (
        document_type IN ('docx', 'pptx', 'pdf', 'txt', 'md')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    provider_id TEXT,
    model_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO translation_jobs_next (
    id,
    source_path,
    output_path,
    document_type,
    status,
    progress,
    provider_id,
    model_id,
    error_code,
    created_at,
    updated_at
)
SELECT
    id,
    source_path,
    output_path,
    document_type,
    status,
    progress,
    provider_id,
    model_id,
    error_code,
    created_at,
    updated_at
FROM translation_jobs;

DROP TABLE translation_jobs;
ALTER TABLE translation_jobs_next RENAME TO translation_jobs;

CREATE INDEX idx_translation_jobs_created_at
ON translation_jobs(created_at DESC);
