# PageFerry backend

PageFerry 的 Python 本地服务。当前只提供应用启动、目录初始化、SQLite 建库、健康检查和内置模型目录读取；文档翻译 pipeline 会按 `docs/dev/roadmap.md` 分阶段迁入。

```bash
uv sync --directory backend --frozen
make backend
```

常规检查：

```bash
uv run --directory backend pytest
uv run --directory backend ruff check .
uv run --directory backend ruff format --check .
```

Provider 安全边界：自定义 OpenAI-compatible endpoint 与 preset Base URL override
只有显式 loopback 可以使用 HTTP，且该连接会忽略 `HTTP_PROXY` / `ALL_PROXY`；
HTTPS endpoint 仍保留系统代理行为。旧数据库升级到 provider 单配置约束时，migration 只暂存
Keychain reference，不保存 Key；startup 会保留可用 reference、清理孤立条目。临时
Keychain 故障不会阻止 sidecar 启动，staging 会留待下次启动幂等重试。
