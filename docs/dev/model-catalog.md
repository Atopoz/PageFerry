# 内置模型目录方案

> 状态：Draft · v0.1 核心能力

## 1. 目标

PageFerry 随每个应用版本携带一份经过核验的 provider/model catalog。普通用户的配置路径应当是：选择模型服务，输入 API Key，点击检测，选择模型。已知服务的 Base URL 和 protocol 由应用提供，不要求用户抄 endpoint。

同时保留“OpenAI-compatible”高级入口，供用户配置自定义 Base URL 和 model id。

## 2. 不做什么

- 不把 `/models` 返回值当成产品目录的唯一来源；很多兼容服务不完整或根本不实现它。
- 不在 v0.1 从远程地址静默热更新一份未签名 catalog。
- 不因为新版本出现推荐模型就自动改掉用户现有默认模型。
- 不把 API Key 写入 JSON、SQLite、日志、崩溃报告或前端 localStorage。
- 不复制 Cherry Studio 的 AGPL 代码、目录数据和品牌资产。

## 3. 数据分层

```text
bundled catalog           # 应用版本自带，经过发布验证
        ↓ merge
user provider override    # 自定义 URL、显示开关、模型别名
        ↓ merge
runtime discovery         # /models 临时发现，只补充当前可见模型
        ↓
effective catalog         # UI 与任务创建实际使用
```

优先级为 user override > runtime discovery > bundled catalog。删除或弃用只改变 catalog 标记，不静默重写用户选择；任务历史始终保留当时使用的 provider id、model id 与 catalog version。

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
- `provider_models`：provider 上游 model id、默认开关、参数差异和映射关系。

仓库初始文件只放 provider 骨架，`models` 与 `provider_models` 暂时为空。没有经过真实 endpoint 检测的模型名不进入正式目录，免得第一版就自带一份会撒谎的清单。

## 5. 初始 provider 范围

根据原项目已有适配边界，先为以下 provider 建立身份：

- OpenAI
- Google Gemini
- DeepSeek
- SiliconFlow
- Zhipu GLM
- MiniMax
- Qwen / DashScope
- Custom OpenAI-compatible

Anthropic 等新增服务要等协议 adapter 和真实检测通过后再进入 catalog，不能只添加一个 Logo 和 Base URL 就宣称支持。

## 6. 配置与检测流程

1. 用户选择 provider。
2. UI 只显示 API Key；Base URL 默认隐藏在“高级设置”。
3. API Key 写入系统 Keychain，SQLite 保存 `secret_ref`。
4. 先请求 provider 的模型列表 endpoint（如果该服务支持）。
5. 再用用户选择的模型执行最小推理，例如要求只返回固定短字符串。
6. 同时验证鉴权、model id、请求协议、响应解析和最小输出。
7. 成功后保存 provider 配置；失败返回可执行的错误，如“Key 无效”“模型不存在”“endpoint 不兼容”，而不是笼统的“连接失败”。

仅 `/models` 成功不能证明推理可用，因此检测必须包含最小 inference。这一点属于 contract，不是 UI 细节。

## 7. 密钥与进程边界

- React 永久不读取已保存 Key 明文；它只提交新 Key 或显示掩码状态。
- Python sidecar 通过平台 adapter 读写 Keychain。
- 创建任务时以 provider config id 查找 secret，不把 Key 放进任务表。
- 子进程环境、命令行参数和日志中不传 Key。
- 删除 provider 配置时同时删除 Keychain item；失败要提示残留，不能假装成功。

## 8. 随版本更新

每次 catalog 更新执行：

1. 对官方文档和真实 endpoint 核验 Base URL、model id、协议与能力。
2. 跑 `/models` 和最小 inference probe，保存不含密钥的测试证据。
3. 更新 catalog version 和 changelog。
4. 校验 schema、provider/model 引用完整性和重复 id。
5. 在升级测试中验证旧用户选择仍可解析；被弃用模型只提示迁移，不自动切换。

未来若需要独立于应用发版更新 catalog，必须增加签名、版本回滚、兼容范围和本地缓存验证。在这些机制完成前，只随安装包发布更可靠。

## 9. 实现落点

| 能力 | 目录 |
| --- | --- |
| catalog schema / merge | `backend/modules/model_catalog/` |
| bundled JSON | `backend/resources/model_catalog/` |
| endpoint probes | `backend/modules/model_catalog/providers/`（迁移阶段创建） |
| Keychain | `backend/modules/model_catalog/secrets/`（迁移阶段创建） |
| provider 配置 API | `backend/api/v1/` |
| 模型设置 UI | `frontend/src/`，稳定后再拆 feature |

不要在还只有一个页面时预建十几层 feature 目录。等设置页、任务页和共享组件真正出现，再按依赖边界拆分。
