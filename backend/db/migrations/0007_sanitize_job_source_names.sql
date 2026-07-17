-- 修复 0004 写入的绝对路径，只为历史任务保留跨平台 basename。
WITH RECURSIVE path_tail(id, value) AS (
    SELECT id, replace(source_path, char(92), '/')
    FROM translation_jobs
    WHERE source_name IS NULL OR source_name = source_path

    UNION ALL

    SELECT id, substr(value, instr(value, '/') + 1)
    FROM path_tail
    WHERE instr(value, '/') > 0
),
safe_name(id, value) AS (
    SELECT id, value
    FROM path_tail
    WHERE instr(value, '/') = 0
)
UPDATE translation_jobs
SET source_name = coalesce(
    nullif((SELECT value FROM safe_name WHERE safe_name.id = translation_jobs.id), ''),
    'document'
)
WHERE source_name IS NULL OR source_name = source_path;
