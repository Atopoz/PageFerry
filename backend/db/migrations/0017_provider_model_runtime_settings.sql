-- 为每个已激活 model 保存可选 runtime override；NULL 始终表示跟随应用或 catalog 默认值。
ALTER TABLE provider_model_configs ADD COLUMN reasoning_policy_override TEXT CHECK (
    reasoning_policy_override IS NULL OR reasoning_policy_override IN (
        'provider_default', 'off', 'on', 'low', 'medium', 'high', 'max'
    )
);

ALTER TABLE provider_model_configs ADD COLUMN per_job_concurrency_override INTEGER CHECK (
    per_job_concurrency_override IS NULL
    OR per_job_concurrency_override BETWEEN 1 AND 32
);

ALTER TABLE provider_model_configs ADD COLUMN global_concurrency_override INTEGER CHECK (
    global_concurrency_override IS NULL
    OR global_concurrency_override BETWEEN 1 AND 32
);
