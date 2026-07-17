# 内置模型目录方案

> 状态：Active · 五个首发 provider 与 model inventory contract 已确定

## 1. 目标

PageFerry 随每个应用版本携带一份经过核验的 provider/model catalog。五个内置 provider 始终陈列在模型服务页，普通用户直接选择其中一项、输入唯一 API Key、获取模型列表，再启用需要的模型并指定默认模型。已知服务默认使用 catalog 的 Base URL 和 protocol，不要求用户抄 endpoint；高级设置仍允许覆盖 Base URL，以兼容代理网关或私有部署。

第一阶段只向 UI 暴露 DeepSeek、Kimi、Zhipu GLM、MiniMax 与 Xiaomi MiMo。provider 连接配置与 model inventory 是两个边界：配置 provider 不等于启用它返回的全部模型，文件翻译页也只能选择已经启用并完成 probe 的模型。

需要连接其他服务时，用户可以创建 OpenAI-compatible 自定义 provider。自定义定义只保存显示名和 Base URL，每个定义仍只有一个 Keychain 凭据；首版不提供同一 provider 下的多密钥 profile。

## 2. 不做什么

- 不把远端 `/models` 与 bundled catalog 混成一个来源不明的列表；每个 model 必须保留 `remote` 或 `catalog` 来源。
- 不在 v0.1 从远程地址静默热更新一份未签名 catalog。
- 不因为新版本出现推荐模型就自动改掉用户现有默认模型。
- 不把 API Key 写入 JSON、SQLite、日志、崩溃报告或前端 localStorage。
- 不因一次 model discovery 自动启用、停用或切换默认模型。
- 不为内置 provider 建立多个实例或多密钥 profile；配置成功就是该 provider 的激活态。

## 3. 数据分层

```text
bundled catalog ───────────────> catalog candidates / fallback
                                      │
remote /models discovery ──────> remote candidates
                                      │ 用户明确选择
                                      v
SQLite model inventory ────────> enabled models + default model
                                      │
                                      v
file translation model picker
```

remote discovery 与 catalog fallback 不互相伪装。配置了可靠 `models_path` 的 provider 才调用远端 `/models`；没有可靠 model list endpoint 的 provider 直接返回随应用发布的 catalog 候选，并标记 `source: "catalog"`。discovery 结果只是当次候选，不写 SQLite，也不改变现有 inventory；只有后续 `PUT` 成功才保存用户选择。

## 4. Schema

内置文件位置：`backend/resources/model_catalog/catalog.json`。

顶层拆成三组，避免把“模型能力”和“某个服务如何暴露该模型”混在一起：

```json
{
  "schema_version": 1,
  "catalog_version": "0.1.0",
  "released_at": "2026-07-16",
  "providers": [],
  "models": [],
  "provider_models": []
}
```

- `providers`：服务名、protocol、默认 Base URL、认证方式和是否允许编辑 URL。
- `models`：规范化模型身份、显示名、上下文和能力标签。
- `provider_models`：provider 上游 model id、初始候选状态、参数差异和映射关系。

bundled catalog 不保存用户是否启用模型。用户 inventory 写入 SQLite：provider config 保存 `default_model_id`、最近一次 probe 的 effective Base URL snapshot 与可空的 `base_url_override`，model 行保存 `source`、`enabled`、`available` 与 probe 状态。`base_url_override IS NULL` 表示继续跟随当前应用版本的 catalog 默认值，而不是把旧 snapshot 误当成用户选择。默认模型必须同时属于 enabled inventory；同步到新列表时不能静默改掉已有选择。

Provider status 的 `base_url` 始终返回当前 effective URL，`base_url_overridden` 明确区分 preset 正在使用用户 override 还是 catalog 默认值。custom 的地址属于定义本身，因此该标记固定为 `false`，且 `base_url_editable` 也保持 `false`。

用户创建的 OpenAI-compatible 定义写入 `custom_provider_definitions`，只包含稳定 id、显示名、Base URL 与时间戳。API Key 仍只进入系统 Keychain，自定义定义不扩张 bundled catalog，也不会伪装成官方 preset。

当前 catalog 已登记 `deepseek-v4-flash`、`glm-5.2` 与 `mimo-v2.5-pro`；其中 DeepSeek 通过默认 request options 关闭 thinking。没有经过官方文档和真实 endpoint 检测的模型名不进入 catalog fallback。

## 5. 初始 provider 范围

| Provider | 默认 Base URL | model inventory 来源 | 初始 catalog 候选 |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | remote `GET /models` | `deepseek-v4-flash` |
| Kimi | `https://api.moonshot.cn/v1` | remote `GET /models` | 无；以账号实际返回为准 |
| Zhipu GLM | `https://open.bigmodel.cn/api/paas/v4` | bundled catalog fallback | `glm-5.2` |
| MiniMax | `https://api.minimaxi.com/v1` | remote `GET /models` | 无；以账号实际返回为准 |
| Xiaomi MiMo | `https://api.xiaomimimo.com/v1` | bundled catalog fallback | `mimo-v2.5-pro` |

上述五个 provider 首版都使用 OpenAI-compatible chat adapter。`models_path` 与 `chat_path` 必须分别配置，不能靠 Base URL 字符串猜 endpoint。DeepSeek 翻译 batch 使用 JSON Output，并固定发送 `{"thinking":{"type":"disabled"}}`；响应仍要检查 index 是否完整、唯一，不能因 JSON 可解析就放弃业务 contract 校验。

自定义 provider 同样使用 OpenAI-compatible adapter，创建时由用户提供 Base URL，固定使用 `/models` 与 `/chat/completions`。自定义地址与 preset override 共用一套安全校验：拒绝 userinfo、query、fragment、空白、反斜杠与非法端口；公网和 LAN 必须使用 HTTPS，HTTP 仅允许 `localhost`、`*.localhost`、`127.0.0.0/8` 与 `::1` 这类明确 loopback，显式端口仍可用于本地 runtime。所有 loopback HTTP 连接都绕过 `HTTP_PROXY` / `ALL_PROXY`，避免 Bearer Key 离机。

## 6. 配置与检测流程

1. 模型服务页固定陈列五个 preset；未配置项也可以直接选择，不再先执行“添加 provider”。
2. UI 输入该 provider 唯一的 API Key；preset 默认展示当前 effective Base URL，但 override 收在高级设置，不与普通用户争夺视觉层级。
3. `POST /api/v1/providers/{id}/models/discover` 使用当次 Key，或在未提交新 Key 时读取既有 Keychain secret；request 可携带尚未保存的 preset `base_url` 直接同步 draft endpoint，省略时使用已存 override，传 `null` 或空白时仅本次预览 catalog 默认值。该临时 URL 不写 SQLite。
4. 有可靠 `models_path` 时返回 `source: "remote"` 的 `/models` 结果；否则返回 `source: "catalog"` 的 bundled fallback。该请求不写 Keychain 或 SQLite，日志也不记录当次 Key。
5. 用户从候选中选择 `enabled_model_ids`，并指定其中一个 `default_model_id`。未启用模型不进入文件翻译页的模型选择器。
6. `PUT /api/v1/providers/{id}` 会重新解析 inventory，并对所有待启用模型执行最小 inference；仅全部通过后才更新 Keychain secret、provider metadata 与 model inventory。preset 的 `base_url` 字段省略时保留既有 override，传 `null` 或空白时恢复 catalog 默认值，传非空值时先走安全校验再参与 discovery 与 inference。
7. 失败时保留上一次可用配置，并区分 Key、endpoint、model、rate limit、network 与 protocol；不回传上游 response body。
8. `DELETE /api/v1/providers/{id}` 对 preset 表示停用：删除 metadata、model inventory 与 Keychain item，但 preset 仍留在列表；对 custom provider 还会删除其持久化定义。

自定义入口使用 `POST /api/v1/providers/custom` 创建定义，返回 backend 生成的稳定 `custom-<uuid>` id，之后复用相同的 discovery、配置、translator 与 `DELETE` 流程。首版没有修改自定义定义的 endpoint：custom 的 `PUT` 只更新 Key 与 model inventory，若携带 `base_url` 会明确拒绝；需要换地址时删除后重建，API 不向 UI 谎称该字段可编辑。

仅 `/models` 成功不能证明模型可用于翻译，因此保存配置必须包含最小 inference。重复 discovery 也不能自动覆盖 enabled/default inventory；列表差异先呈现给用户，再由 `PUT` 提交明确选择。

## 7. 密钥与进程边界

- React 永久不读取已保存 Key 明文；它只提交新 Key 或显示掩码状态。
- Python sidecar 通过平台 adapter 读写 Keychain。
- production 使用 `com.pageferry.provider-secrets` 作为 Keychain service namespace；`make backend` 显式使用 `com.pageferry.provider-secrets.dev`，避免本地调试读写正式应用凭据。
- namespace 由 `PAGEFERRY_SECRET_SERVICE_NAME` 注入，只隔离 Keychain item，不改变 SQLite 中稳定的 `secret_ref` contract。
- 创建任务时以 provider config id 查找 secret，不把 Key 放进任务表。
- 子进程环境、命令行参数和日志中不传 Key。
- 删除 provider 配置时同时删除 Keychain item；失败要提示残留，不能假装成功。

## 8. 随版本更新

每次 catalog 更新执行：

1. 对官方文档和真实 endpoint 核验 Base URL、model id、协议与能力。
2. 跑 `/models` 和最小 inference probe，保存不含密钥的测试证据。
3. 更新 catalog version 和 changelog。
4. 校验 schema、provider/model 引用完整性和重复 id。
5. 在升级测试中验证旧用户选择仍可解析；被弃用模型只提示用户改选，不自动切换。

未来若需要独立于应用发版更新 catalog，必须增加签名、版本回滚、兼容范围和本地缓存验证。在这些机制完成前，只随安装包发布更可靠。

## 9. 实现落点

| 能力 | 目录 |
| --- | --- |
| catalog schema / merge | `backend/modules/model_catalog/` |
| bundled JSON | `backend/resources/model_catalog/` |
| endpoint probes | `backend/modules/model_catalog/providers/` |
| Keychain | `backend/modules/model_catalog/secrets/` |
| provider 配置编排 | `backend/modules/model_catalog/provider_config.py` |
| custom provider metadata | `backend/db/migrations/0011_custom_openai_providers.sql` |
| 单 provider 单配置约束 | `backend/db/migrations/0012_unique_provider_configs.sql` |
| legacy Keychain reference reconciliation | `backend/db/migrations/0013_provider_secret_reconciliation.sql` |
| preset Base URL override | `backend/db/migrations/0014_provider_base_url_overrides.sql` |
| provider 配置 API | `backend/api/v1/providers.py` |
| 模型设置 UI | `frontend/src/features/providers/` |
