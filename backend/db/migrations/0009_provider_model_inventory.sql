-- 把 provider 连接状态与可启用的 model inventory 拆开，同时保留旧的单模型选择。
ALTER TABLE provider_configs ADD COLUMN default_model_id TEXT;
ALTER TABLE provider_configs ADD COLUMN last_synced_at TEXT;

CREATE TABLE provider_model_configs (
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('catalog', 'discovered')),
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

CREATE INDEX idx_provider_model_configs_enabled
ON provider_model_configs(provider_id, enabled, available);

UPDATE provider_configs
SET default_model_id = model_id,
    last_synced_at = last_probed_at
WHERE model_id IS NOT NULL;

INSERT INTO provider_model_configs (
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
    model_id,
    model_id,
    'catalog',
    1,
    1,
    probe_status,
    probe_error_code,
    latency_ms,
    last_probed_at,
    last_probed_at,
    created_at,
    updated_at
FROM provider_configs
WHERE model_id IS NOT NULL;
