-- 为任务历史补充安全展示名、翻译统计与 warning code。
ALTER TABLE translation_jobs ADD COLUMN source_name TEXT;
ALTER TABLE translation_jobs ADD COLUMN translated_segments INTEGER NOT NULL DEFAULT 0;
ALTER TABLE translation_jobs ADD COLUMN fallback_segments INTEGER NOT NULL DEFAULT 0;
ALTER TABLE translation_jobs ADD COLUMN warning_codes_json TEXT NOT NULL DEFAULT '[]';

UPDATE translation_jobs
SET source_name = source_path
WHERE source_name IS NULL;
