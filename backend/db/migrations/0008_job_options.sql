-- 保存格式专属 pipeline 选项的不可变 snapshot，历史记录不依赖未来默认值。
ALTER TABLE translation_jobs ADD COLUMN options_json TEXT;
