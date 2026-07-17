-- 建立最初的 translation job metadata 表与时间索引。
CREATE TABLE IF NOT EXISTS translation_jobs (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    output_path TEXT,
    document_type TEXT NOT NULL CHECK (document_type IN ('docx', 'pptx', 'pdf')),
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

CREATE INDEX IF NOT EXISTS idx_translation_jobs_created_at
ON translation_jobs(created_at DESC);
