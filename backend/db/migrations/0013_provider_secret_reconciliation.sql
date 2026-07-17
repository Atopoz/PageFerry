-- 固化 0012 前置 hook 使用的 Keychain reference reconciliation staging 表。
-- 已经执行过 0012 的数据库会得到空表; 未执行的数据库已在去重 transaction 中写入候选。
CREATE TABLE IF NOT EXISTS provider_secret_reconciliation_candidates (
    provider_id TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority > 0),
    staged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_id, secret_ref)
);

CREATE INDEX IF NOT EXISTS idx_provider_secret_candidates_priority
ON provider_secret_reconciliation_candidates(provider_id, priority);
