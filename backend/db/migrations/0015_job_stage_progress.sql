-- 为任务补充三阶段状态与可由真实 batch 完成数计算的 segment 进度。
ALTER TABLE translation_jobs ADD COLUMN progress_stage TEXT NOT NULL DEFAULT 'prepare'
CHECK (progress_stage IN ('prepare', 'translate', 'finalize'));

ALTER TABLE translation_jobs ADD COLUMN processed_segments INTEGER NOT NULL DEFAULT 0
CHECK (processed_segments >= 0);

ALTER TABLE translation_jobs ADD COLUMN total_segments INTEGER NOT NULL DEFAULT 0
CHECK (total_segments >= 0 AND processed_segments <= total_segments);
