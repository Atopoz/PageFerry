-- 区分 catalog 默认地址与用户显式 override，旧记录默认继续跟随 catalog。
ALTER TABLE provider_configs ADD COLUMN base_url_override TEXT;
