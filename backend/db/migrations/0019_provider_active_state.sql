-- 将“已配置”与“当前启用”拆开；升级前已经可用的 provider 保持启用。
ALTER TABLE provider_configs ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (
    active IN (0, 1)
);
