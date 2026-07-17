-- 持久化不敏感的 probe latency, 让应用重启后仍能显示最近检测结果。
ALTER TABLE provider_configs ADD COLUMN latency_ms INTEGER;
