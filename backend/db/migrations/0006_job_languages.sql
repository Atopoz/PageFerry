-- 保存执行 queued job 所需的源语言与目标语言 metadata。
ALTER TABLE translation_jobs ADD COLUMN source_language TEXT;
ALTER TABLE translation_jobs ADD COLUMN target_language TEXT NOT NULL DEFAULT 'zh-CN';
