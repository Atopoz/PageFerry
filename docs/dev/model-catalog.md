# 内置模型目录方案

> 状态：Active · bundled baseline、手动 model、显式同步与 model runtime settings contract 已确定

## 1. 目标

PageFerry 随每个应用版本携带一份经过核验的 provider/model catalog。五个内置 provider 始终陈列在模型服务页，bundled catalog 也为各 provider 提供不依赖 `/models` 的稳定 model baseline。普通用户直接选择其中一项、输入唯一 API Key；输入框右侧的「检测」只用当前草稿做一次临时 inference，backend 自动选择既有 default、catalog 推荐项或第一个候选并把实际 model 告知 UI，不要求用户先选择 model id。用户点击明确的「保存配置」后，PageFerry 才把当次合并出的整组 model 全部 probe，并在全部成功后原子保存、默认启用，用户只需关闭不想看到的模型。provider 配置后可以用已保存的 Key 显式同步远端 inventory，也可以手动登记未被 catalog 或 `/models` 列出的上游 model id；临时 inactive 不影响维护这组配置。已知服务默认使用 catalog 的 Base URL 和 protocol，不要求用户抄 endpoint；高级设置仍允许覆盖 Base URL，以兼容代理网关或私有部署。

第一阶段只向 UI 暴露 DeepSeek、Kimi、Zhipu GLM、MiniMax 与 Xiaomi MiMo。provider 的 `configured` 与 `active` 是两个边界：前者表示凭据和连接已经验证并保存，后者表示这组服务是否出现在翻译 runtime。停用 provider 只改变 `active`，不会删除 Keychain secret、model inventory 或 runtime settings；文件翻译页只展示 active provider 下已经启用并完成 probe 的模型，并按 provider 分组。

需要连接其他服务时，用户可以创建 OpenAI-compatible 自定义 provider。自定义定义只保存显示名和 Base URL，每个定义仍只有一个 Keychain 凭据；首版不提供同一 provider 下的多密钥 profile。

## 2. 不做什么

- 不让远端 `/models` 取代 bundled catalog，也不把用户手动登记的 model 伪装成 remote；合并后每个 model 仍保留 `catalog`、`manual` 或 `remote` 来源。
- 不在 v0.1 从远程地址静默热更新一份未签名 catalog。
- 不因为新版本出现推荐模型就自动改掉用户现有默认模型。
- 不把 API Key 写入磁盘 JSON 配置、SQLite、日志、崩溃报告或前端 localStorage；专用 reveal endpoint 的瞬时 JSON response 不得缓存或持久化。
- 不把 API Key 混进 provider 列表、通用状态响应或长期 frontend cache；只允许当前选中 provider 通过受保护的专用 endpoint 按需读取。
- 不因一次独立 model discovery 自动启用、停用或切换默认模型；首次验证保存是用户明确激活整组服务，因此会启用当次全部候选。
- 不因一次显式同步清空 enabled/default 选择、probe 结果或 model runtime override；远端额外模型消失时只更新 availability。
- 不为内置 provider 建立多个实例或多密钥 profile；首次配置成功默认激活，但用户可以非破坏地暂停并再次启用。

## 3. 数据分层

```text
bundled catalog ───────────────> stable catalog baseline
                                      │
remote /models discovery ──────> 当次合并预览（不持久化）
                                      │ 用户明确配置
                                      v
SQLite model inventory <─────── 显式 sync 幂等 merge
        ↑                             │
        └──── 手动添加 model ─────────┤
        │                             │
        │ enabled/default/runtime     └─ remote extras + availability
        v
file translation model picker
```

catalog baseline、用户手动登记项与 remote extras 会合成当前候选，但不互相伪装。catalog model 始终标记 `source: "catalog"`；用户通过 API 明确登记的 model 标记 `source: "manual"`；远端返回且 catalog 未登记的额外 model 标记 `source: "remote"`。同一 upstream identity 的归属优先级固定为 `catalog > manual > remote`：应用核验过的 catalog 定义优先，manual 不会被一次 remote discovery 降级为 remote。配置了可靠 `models_path` 的 provider 才调用远端 `/models`，没有可靠 model list endpoint 的 provider 只返回随应用发布的 baseline 与已经持久化的 manual model。

discovery 仍是当次预览：它可以使用尚未保存的 Key 和 draft Base URL，但不写 Keychain 或 SQLite，也不改变现有 inventory。provider 已配置后，显式 sync 只使用 Keychain 中的已保存凭据，并在一个事务内幂等 merge；inactive provider 也可以维护 inventory。新增远端 model 进入 inventory，重新出现的 model 恢复 availability，消失的 remote extra 保留原记录并标记 unavailable。manual model 由用户持有，即使 `/models` 没有返回也保持 available；sync 不自动启用新 model，不改默认 model，也不清除已有 probe 与 runtime settings。

## 4. Schema

内置文件位置：`backend/resources/model_catalog/catalog.json`。

顶层拆成三组，避免把“模型能力”和“某个服务如何暴露该模型”混在一起：

```json
{
  "schema_version": 1,
  "catalog_version": "0.2.0-dev",
  "released_at": null,
  "providers": [],
  "models": [],
  "provider_models": []
}
```

- `providers`：服务名、protocol、默认 Base URL、认证方式和是否允许编辑 URL。
- `models`：规范化模型身份、显示名、上下文和能力标签。
- `provider_models`：provider 上游 model id、初始候选状态、参数差异和映射关系。

bundled catalog 不保存用户是否启用模型。用户 inventory 写入 SQLite：provider config 保存非破坏启停字段 `active`、`default_model_id`、最近一次 probe 的 effective Base URL snapshot、可空的 `base_url_override` 与 `last_synced_at`；model 行保存取值为 `catalog`、`manual` 或 `remote` 的 `source`、`enabled`、`available`、probe 状态，以及可空的 `reasoning_policy_override`、`per_job_concurrency_override`、`global_concurrency_override`。override 为 `NULL` 表示跟随 catalog 或应用默认值。`base_url_override IS NULL` 同样表示继续跟随当前应用版本的 catalog 默认值，而不是把旧 snapshot 误当成用户选择。默认模型必须同时属于 enabled inventory；后续 sync 不能静默改掉已有选择。

当前 bundled catalog 要求 `model_id` 与 `upstream_model_id` 相同。SQLite 以 `model_id` 作为 inventory 主键；以后如果需要引入稳定 alias，必须先配套 migration 改写已有远端行，不能只改 catalog 映射。

Provider status 的 `base_url` 始终返回当前 effective URL，`base_url_overridden` 明确区分 preset 正在使用用户 override 还是 catalog 默认值。custom 的地址属于定义本身，因此该标记固定为 `false`，且 `base_url_editable` 也保持 `false`。

用户创建的 OpenAI-compatible 定义写入 `custom_provider_definitions`，只包含稳定 id、显示名、Base URL 与时间戳。API Key 仍只进入系统 Keychain，自定义定义不扩张 bundled catalog，也不会伪装成官方 preset。

当前 catalog 已登记 `deepseek-v4-flash`、`deepseek-v4-pro`、`kimi-k2.6`、`glm-5.2`、`MiniMax-M2.7`、`MiniMax-M2.7-highspeed`、`mimo-v2.5` 与 `mimo-v2.5-pro`。没有经过核验的模型名不进入 bundled baseline；账号额外返回的 model 可以通过 discovery、显式 sync 或手动登记增补，但不会因此获得未核验的 reasoning options。manual model 没有 catalog reasoning policy，runtime 使用 provider 默认行为；只有未来被 bundled catalog 正式收录后，才获得该 catalog 定义声明的 reasoning 选项。

## 5. 初始 provider 范围

| Provider    | 默认 Base URL                          | remote sync   | bundled baseline                         |
| ----------- | -------------------------------------- | ------------- | ---------------------------------------- |
| DeepSeek    | `https://api.deepseek.com`             | `GET /models` | `deepseek-v4-flash`、`deepseek-v4-pro`   |
| Kimi        | `https://api.moonshot.cn/v1`           | `GET /models` | `kimi-k2.6`                              |
| Zhipu GLM   | `https://open.bigmodel.cn/api/paas/v4` | 不支持        | `glm-5.2`                                |
| MiniMax     | `https://api.minimaxi.com/v1`          | `GET /models` | `MiniMax-M2.7`、`MiniMax-M2.7-highspeed` |
| Xiaomi MiMo | `https://api.xiaomimimo.com/v1`        | 不支持        | `mimo-v2.5`、`mimo-v2.5-pro`             |

上述五个 provider 首版都使用 OpenAI-compatible chat adapter。`models_path` 与 `chat_path` 必须分别配置，不能靠 Base URL 字符串猜 endpoint。DeepSeek 翻译 batch 使用 JSON Output；effective reasoning policy 为 `off` 时发送 `{"thinking":{"type":"disabled"}}`。响应仍要检查 index 是否完整、唯一，不能因 JSON 可解析就放弃业务 contract 校验。

bundled catalog 只向已核验 model 暴露受支持的 reasoning policy，当前 contract 为：

| Model                    | 可选 policy                              | catalog 默认值     |
| ------------------------ | ---------------------------------------- | ------------------ |
| DeepSeek V4 Flash / Pro  | `provider_default`、`off`、`high`、`max` | `off`              |
| Kimi K2.6                | `provider_default`、`off`、`on`          | `off`              |
| GLM 5.2                  | `provider_default`、`off`、`high`、`max` | `provider_default` |
| MiniMax M2.7 / Highspeed | `provider_default`                       | `provider_default` |
| MiMo V2.5 / Pro          | `provider_default`、`off`、`on`          | `provider_default` |

每个已启用 model 还可以单独覆盖单 job 并发与应用级共享并发。应用默认值分别为 6 和 15；有效单 job 上限不能超过有效共享上限，两个值当前都限制在 1–32。override 清空后恢复应用默认值，不把默认值复制进每一行配置。

自定义 provider 同样使用 OpenAI-compatible adapter，创建时由用户提供 Base URL，固定使用 `/models` 与 `/chat/completions`。自定义地址与 preset override 共用一套安全校验：拒绝 userinfo、query、fragment、空白、反斜杠与非法端口；公网和 LAN 必须使用 HTTPS，HTTP 仅允许 `localhost`、`*.localhost`、`127.0.0.0/8` 与 `::1` 这类明确 loopback，显式端口仍可用于本地 runtime。所有 loopback HTTP 连接都绕过 `HTTP_PROXY` / `ALL_PROXY`，避免 Bearer Key 离机。

## 6. 配置与检测流程

1. 模型服务页固定陈列五个 preset；未配置项也可以直接选择，不再先执行“添加 provider”。
2. UI 输入该 provider 唯一的 API Key；「检测」紧邻 password input，但只做不持久化的临时 inference。已配置 provider 被选中时，UI 通过专用 endpoint 从 Keychain 读取真实值，默认以 password 黑点显示，用户可以用眼睛按钮临时查看。preset 默认展示当前 effective Base URL；底部「保存配置」常驻，API Key 为空、字段无效或正在执行其他操作时禁用。Key、Base URL 或默认模型出现未保存更改时，字段附近继续给出提示，但不再通过按钮闪现表达状态。
3. `POST /api/v1/providers/{id}/models` 接收 `model_id` 和可选 `display_name`，把用户明确登记的上游 model 写为 `source: "manual"`。新记录默认 `available: true`、`enabled: false`、`probe_status: "not_tested"`，该请求不读取 Key，也不发网络请求；添加成功不等于模型已激活。
4. `POST /api/v1/providers/{id}/models/discover` 使用当次 Key，或在未提交新 Key 时读取既有 Keychain secret；request 可携带尚未保存的 preset `base_url` 直接预览 draft endpoint，省略时使用已存 override，传 `null` 或空白时仅本次恢复 catalog 默认值。该临时 URL 不写 SQLite。
5. discovery 先加入 `source: "catalog"` 的 bundled baseline，再为有可靠 `models_path` 的 provider 合并 `source: "remote"` 的账号额外 model，并保留已经持久化的 `source: "manual"` model。该请求不写 Keychain 或 SQLite，日志也不记录当次 Key。
6. `POST /api/v1/providers/{id}/probe` 接收当次尚未保存的 Key、preset Base URL 与可选 `model_id`。backend 会重新解析 catalog、manual 与 remote inventory；未指定 model 时优先选择既有 default、catalog 推荐项，否则选择第一个候选。它只对所选 model 做最小 inference 并返回 model identity 与 latency，不写 Keychain、SQLite、inventory、enabled/default 或 active 状态。
7. 点击「保存配置」时，首次配置由 UI 在 `PUT /api/v1/providers/{id}` 显式发送 `enable_all_models: true`；已配置 provider 则沿用当前 enabled/default 集合。backend 对所有待启用模型执行最小 inference，仅全部通过后才更新 Keychain secret、provider metadata 与 model inventory。首次配置同时写入 `active: true`；既有 provider 再次保存时保留原有 active 状态，不能把用户暂停的服务意外打开。即使 `/models` 请求失败，只要待启用项都属于 catalog 或 manual inventory，manual model 仍会直接进入真实 inference probe，不能因手动登记而绕过验证。网络 probe 不持有进程锁，提交时则对 probe 前读取的 provider record、secret、已选 model runtime rows 与 manual inventory 做 CAS；配置、manual inventory 或 reasoning/concurrency settings 已被其他操作更新时返回 `409 conflict`，不能用过期结果回滚新 Key 或覆盖新加的 manual model。preset 的 `base_url` 字段省略时保留既有 override，传 `null` 或空白时恢复 catalog 默认值，传非空值时先走安全校验再参与 discovery 与 inference。
8. 失败时保留上一次可用配置；新加的 manual model 仍保持 disabled，不会因失败的 probe 进入文件翻译页。错误区分 Key、endpoint、model、rate limit、network 与 protocol，且不回传上游 response body。
9. `PUT /api/v1/providers/{id}/active` 非破坏地切换 provider runtime 可用性。关闭时只写 `active: false`；重新打开前必须确认 Keychain secret 仍存在，并至少有一个 enabled、available、probe succeeded 的 model。
10. `PUT /api/v1/providers/{id}/models/{model_id}/enabled` 即时切换单个模型。启用会对该模型执行最小 inference，并以 CAS 防止迟到 probe 覆盖新配置；禁用不发网络请求。系统禁止关闭最后一个 enabled model；若关闭当前 default，则在同一事务中把 default 移到下一个 enabled model。
11. `DELETE /api/v1/providers/{id}` 表示“移除配置”：preset 会删除 metadata、model inventory 与 Keychain item，但入口仍留在列表；custom provider 还会删除其持久化定义。它不是日常暂停服务的入口。
12. provider 配置完成后，`POST /api/v1/providers/{id}/models/sync` 只使用同一份状态 snapshot 中已保存的 Key 与 effective Base URL 执行幂等 merge；远端请求返回后再次校验 provider record 与 secret，旧 endpoint 的迟到结果不能写进新配置。sync 只会把消失的 remote extra 标记 unavailable，不改变 manual model 的 availability。新增 remote extra 默认 disabled，用户按行启用时再执行 probe。不支持 `models_path` 的 provider 不显示同步能力。
13. `PUT /api/v1/providers/{id}/models/{model_id}/settings` 只允许修改已启用 model。字段省略表示保留当前 override，显式 `null` 表示恢复默认；reasoning policy 必须属于该 catalog model 的支持集合，manual model 因没有 catalog reasoning policy 而只能跟随 provider 默认行为；单 job 并发不能大于全局并发。

自定义入口使用 `POST /api/v1/providers/custom` 创建定义，返回 backend 生成的稳定 `custom-<uuid>` id，之后复用相同的 discovery、配置、translator 与 `DELETE` 流程。首版没有修改自定义定义的 endpoint：custom 的 `PUT` 只更新 Key 与 model inventory，若携带 `base_url` 会明确拒绝；需要换地址时删除后重建，API 不向 UI 谎称该字段可编辑。

仅 `/models` 成功、临时检测通过或手动写入一个 model id 都不能证明整组模型可用于翻译，因此保存配置和后续单模型启用仍必须包含最小 inference。重复 discovery 或临时检测不能覆盖 enabled/default inventory；首次保存的显式“整组启用”是唯一例外。显式 sync 也只维护 remote inventory identity 与 availability，不替用户启用、切换默认、修改 manual model 或修改 runtime settings。

## 7. 密钥与进程边界

- `GET /api/v1/providers/{id}/api-key` 是唯一返回明文 Key 的 endpoint：它要求 boot token，只读取当前指定 provider，并对成功和错误响应设置 `Cache-Control: no-store, private`、`Pragma: no-cache` 与立即过期头。
- React 只在用户选中一个已配置 provider 时把该 Key 读入当前页面内存；password input 默认显示黑点，眼睛按钮只改变可见性。切换 provider 会清掉上一项的明文，不写 localStorage、SQLite、日志或通用 provider 状态。
- `GET /api/v1/providers` 永远不读取 Keychain，也不返回任何 secret；不能为了少一次请求把所有 provider Key 批量带到 renderer。
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

| 能力                                     | 目录                                                             |
| ---------------------------------------- | ---------------------------------------------------------------- |
| catalog schema / merge                   | `backend/modules/model_catalog/`                                 |
| bundled JSON                             | `backend/resources/model_catalog/`                               |
| endpoint probes                          | `backend/modules/model_catalog/providers/`                       |
| Keychain                                 | `backend/modules/model_catalog/secrets/`                         |
| provider 配置编排                        | `backend/modules/model_catalog/provider_config.py`               |
| custom provider metadata                 | `backend/db/migrations/0011_custom_openai_providers.sql`         |
| 单 provider 单配置约束                   | `backend/db/migrations/0012_unique_provider_configs.sql`         |
| legacy Keychain reference reconciliation | `backend/db/migrations/0013_provider_secret_reconciliation.sql`  |
| preset Base URL override                 | `backend/db/migrations/0014_provider_base_url_overrides.sql`     |
| model runtime overrides                  | `backend/db/migrations/0017_provider_model_runtime_settings.sql` |
| manual model source                      | `backend/db/migrations/0018_manual_provider_models.sql`          |
| provider 非破坏启停                      | `backend/db/migrations/0019_provider_active_state.sql`           |
| provider 配置 API                        | `backend/api/v1/providers.py`                                    |
| 模型设置 UI                              | `frontend/src/features/providers/`                               |
