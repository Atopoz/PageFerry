-- 增加用户手动登记的 model 来源，同时完整保留现有 probe 与 runtime override。
CREATE TABLE provider_model_configs_next (
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('catalog', 'remote', 'manual')),
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
    reasoning_policy_override TEXT CHECK (
        reasoning_policy_override IS NULL OR reasoning_policy_override IN (
            'provider_default', 'off', 'on', 'low', 'medium', 'high', 'max'
        )
    ),
    per_job_concurrency_override INTEGER CHECK (
        per_job_concurrency_override IS NULL
        OR per_job_concurrency_override BETWEEN 1 AND 32
    ),
    global_concurrency_override INTEGER CHECK (
        global_concurrency_override IS NULL
        OR global_concurrency_override BETWEEN 1 AND 32
    ),
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
    updated_at,
    reasoning_policy_override,
    per_job_concurrency_override,
    global_concurrency_override
)
SELECT
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
    updated_at,
    reasoning_policy_override,
    per_job_concurrency_override,
    global_concurrency_override
FROM provider_model_configs;

DROP TABLE provider_model_configs;

ALTER TABLE provider_model_configs_next RENAME TO provider_model_configs;

CREATE INDEX idx_provider_model_configs_enabled
ON provider_model_configs(provider_id, enabled, available);
