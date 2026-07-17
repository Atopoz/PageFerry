-- 将旧的 discovered 来源名统一为 API contract 使用的 remote。
CREATE TABLE provider_model_configs_next (
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('catalog', 'remote')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0, 1)),
    probe_status TEXT NOT NULL DEFAULT 'not_tested' CHECK (
        probe_status IN ('not_tested', 'succeeded', 'failed')
    ),
    probe_error_code TEXT,
    latency_ms INTEGER,
    last_seen_at TEXT,
    last_probed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider_id, model_id),
    UNIQUE (provider_id, upstream_model_id)
);

INSERT INTO provider_model_configs_next (
    provider_id,
    model_id,
    upstream_model_id,
    display_name,
    source,
    enabled,
    available,
    probe_status,
    probe_error_code,
    latency_ms,
    last_seen_at,
    last_probed_at,
    created_at,
    updated_at
)
SELECT
    provider_id,
    model_id,
    upstream_model_id,
    display_name,
    CASE source WHEN 'discovered' THEN 'remote' ELSE source END,
    enabled,
    available,
    probe_status,
    probe_error_code,
    latency_ms,
    last_seen_at,
    last_probed_at,
    created_at,
    updated_at
FROM provider_model_configs;

DROP TABLE provider_model_configs;

ALTER TABLE provider_model_configs_next RENAME TO provider_model_configs;

CREATE INDEX idx_provider_model_configs_enabled
ON provider_model_configs(provider_id, enabled, available);
