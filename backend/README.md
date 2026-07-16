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
