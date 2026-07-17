-- 支持 XLSX，并把一次翻译产生的多个派生文件保存为独立 metadata。
DROP TABLE IF EXISTS translation_jobs_artifacts_next;

CREATE TABLE translation_jobs_artifacts_next (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    output_path TEXT,
    document_type TEXT NOT NULL CHECK (
        document_type IN ('docx', 'pptx', 'xlsx', 'pdf', 'txt', 'md')
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

INSERT INTO translation_jobs_artifacts_next (
    id, source_path, output_path, document_type, status, progress,
    provider_id, model_id, error_code, created_at, updated_at, source_name,
    translated_segments, fallback_segments, warning_codes_json,
    source_language, target_language, options_json, progress_stage,
    processed_segments, total_segments
)
SELECT
    id, source_path, output_path, document_type, status, progress,
    provider_id, model_id, error_code, created_at, updated_at, source_name,
    translated_segments, fallback_segments, warning_codes_json,
    source_language, target_language, options_json, progress_stage,
    processed_segments, total_segments
FROM translation_jobs;

DROP TABLE translation_jobs;
ALTER TABLE translation_jobs_artifacts_next RENAME TO translation_jobs;

CREATE INDEX idx_translation_jobs_created_at
ON translation_jobs(created_at DESC);

CREATE TABLE translation_job_artifacts (
    job_id TEXT NOT NULL REFERENCES translation_jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('translated', 'bilingual')),
    path TEXT NOT NULL,
    PRIMARY KEY (job_id, kind)
);

INSERT INTO translation_job_artifacts (job_id, kind, path)
SELECT id, 'translated', output_path
FROM translation_jobs
WHERE output_path IS NOT NULL;
