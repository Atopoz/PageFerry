-- 保存用户创建的 OpenAI-compatible provider 定义，凭据仍只进入系统 Keychain。
CREATE TABLE custom_provider_definitions (
    provider_id TEXT PRIMARY KEY CHECK (
        length(provider_id) BETWEEN 8 AND 64
        AND provider_id LIKE 'custom-%'
    ),
    display_name TEXT NOT NULL CHECK (
        length(trim(display_name)) BETWEEN 1 AND 80
    ),
    base_url TEXT NOT NULL CHECK (
        length(trim(base_url)) BETWEEN 1 AND 2048
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_custom_provider_definitions_created_at
ON custom_provider_definitions(created_at, provider_id);
