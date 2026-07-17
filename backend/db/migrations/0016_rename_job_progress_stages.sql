-- 将三阶段内部值调整为与客户端文案一致的 extracting/translating/formatting。
DROP TABLE IF EXISTS translation_jobs_progress_next;

CREATE TABLE translation_jobs_progress_next (
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
    updated_at TEXT NOT NULL,
    source_name TEXT,
    translated_segments INTEGER NOT NULL DEFAULT 0,
    fallback_segments INTEGER NOT NULL DEFAULT 0,
    warning_codes_json TEXT NOT NULL DEFAULT '[]',
    source_language TEXT,
    target_language TEXT NOT NULL DEFAULT 'zh-CN',
    options_json TEXT,
    progress_stage TEXT NOT NULL DEFAULT 'extracting' CHECK (
        progress_stage IN ('extracting', 'translating', 'formatting')
    ),
    processed_segments INTEGER NOT NULL DEFAULT 0 CHECK (processed_segments >= 0),
    total_segments INTEGER NOT NULL DEFAULT 0 CHECK (total_segments >= 0),
    CHECK (processed_segments <= total_segments)
);

INSERT INTO translation_jobs_progress_next (
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
    updated_at,
    source_name,
    translated_segments,
    fallback_segments,
    warning_codes_json,
    source_language,
    target_language,
    options_json,
    progress_stage,
    processed_segments,
    total_segments
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
    updated_at,
    source_name,
    translated_segments,
    fallback_segments,
    warning_codes_json,
    source_language,
    target_language,
    options_json,
    CASE progress_stage
        WHEN 'prepare' THEN 'extracting'
        WHEN 'translate' THEN 'translating'
        WHEN 'finalize' THEN 'formatting'
    END,
    processed_segments,
    total_segments
FROM translation_jobs;

DROP TABLE translation_jobs;
ALTER TABLE translation_jobs_progress_next RENAME TO translation_jobs;

CREATE INDEX idx_translation_jobs_created_at
ON translation_jobs(created_at DESC);
