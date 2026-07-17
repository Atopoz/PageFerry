-- 建立 provider metadata 表; API Key 只进入系统 Keychain, 此处仅保存 reference。
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    base_url TEXT,
    catalog_version TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    probe_status TEXT NOT NULL DEFAULT 'not_tested' CHECK (
        probe_status IN ('not_tested', 'succeeded', 'failed')
    ),
    probe_error_code TEXT,
    last_probed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_provider_configs_provider_id
ON provider_configs(provider_id);
