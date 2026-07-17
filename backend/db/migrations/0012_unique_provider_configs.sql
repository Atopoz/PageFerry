-- 清理旧 schema 允许的重复 provider 配置，并把单 Key 语义固化为唯一索引。
DELETE FROM provider_configs
WHERE rowid IN (
    SELECT rowid
    FROM (
        SELECT
            rowid,
            row_number() OVER (
                PARTITION BY provider_id
                ORDER BY updated_at DESC, created_at DESC, rowid DESC
            ) AS duplicate_rank
        FROM provider_configs
    )
    WHERE duplicate_rank > 1
);

DROP INDEX IF EXISTS idx_provider_configs_provider_id;

CREATE UNIQUE INDEX idx_provider_configs_provider_id
ON provider_configs(provider_id);
