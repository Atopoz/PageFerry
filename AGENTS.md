# AGENTS.md

PageFerry 仓库入口。这里只放跨模块规则；修改具体目录时，先看相邻代码和该目录 README，不要凭文件名猜职责。

## Project Map

```text
.
|-- frontend/               # React / Vite / Tailwind UI；独立 package.json 与 lockfile
|   |-- src/                # WebView 中运行的界面代码
|   `-- tests/              # React 单元与交互测试
|-- backend/                # Python / FastAPI sidecar；独立 pyproject 与 uv.lock
|   |-- core/               # settings、paths、logging、errors 等应用底座
|   |-- api/                # HTTP 边界，只做参数与响应编排
|   |-- modules/            # 功能模块及其专属模型、文件和平台实现
|   |-- db/                 # SQLite schema、migration 与持久化代码
|   `-- tests/              # Python 测试
|-- tauri/                  # Rust / Tauri 2 原生壳；独立 Cargo files 与 toolchain
|   `-- binaries/           # 按 target triple 生成的 sidecar；只跟踪 .gitkeep
|-- scripts/                # 仅放跨 runtime 的构建与发布脚本
|-- .data/                  # 开发期本地数据；生产环境不使用仓库目录
`-- docs/
    `-- dev/                # 当前计划、调研和 handoff
```

## Product Boundary

- 首版只做文件进、文件出：DOCX、PPTX、TXT、Markdown、原生文本型 PDF。
- 首版不做扫描件 PDF、图像翻译、内置 Office 高保真预览、GPU、PostgreSQL、Redis、Celery、租户、计费或账号体系。
- 原始文件只读，绝不覆盖；中间文件写入应用数据目录，结果通过临时文件完成后原子落盘。
- SQLite 只保存任务与用户配置元数据；文档内容和 API Key 不写入数据库。API Key 使用系统 Keychain。
- 内置 provider/model catalog 随应用版本发布；已知 provider 默认隐藏 Base URL，用户通常只需输入 API Key。实时 `/models` 只能补充可用性，不能替代最小推理检测。
- 不要把预览、图像翻译或远程 Web 服务偷偷混进核心 pipeline。需要新增时，先明确独立模块和验收边界。

## Architecture

```text
api -> modules -> db
  \       \----> core
   \-----------> core
```

- `api` 只处理 HTTP、schema、状态码和进度传输，不直接写 SQLite 或调用第三方 SDK。
- `modules` 按功能纵向组织业务流程及其专属实现；LLM、ONNX、文件系统或 Keychain 代码先放进实际使用它的 module，不预建共享层。
- `db` 只放 SQLite schema、migration、连接和持久化实现，不接收模型、文件处理或其他杂项。
- `modules` 不 import `api`；只有出现第二个真实消费者时，才把重复实现提取为明确的共享组件。
- `core` 不放文档翻译业务规则。
- 迁移 JOTO-Translation 时先保持可观察行为，再替换运行时或目录结构；不要在同一批改动里同时重写算法、模型和渲染。
- `pdfminerex` 作为有来源说明的独立 vendor fork 管理，不埋进普通业务模块。

## Working Rules

- 先理解相邻代码、现有测试和真实运行边界，再动手。
- 保持改动聚焦，不顺手重构无关模块，不为未来假设预造万能抽象。
- 优先做小而可独立验证的切片；测试通过后再扩大范围。
- Python 文件必须有说明职责的模块 docstring；每个 class、函数和方法都要有直接的人话 docstring。docstring 与注释尽可能使用中文句式，`dry run`、`runtime`、`pipeline`、`fallback`、`endpoint` 等专业术语保留英文，不做生硬翻译。pipeline 中涉及结构标记、逆序回填、修复、fallback、原子落盘等非直观逻辑时，补充解释原因与边界的行内注释，不写逐行复述语法的废话注释。
- 前端使用 Tailwind CSS v4 与 CSS variables 管理视觉 token；shadcn/ui 组件按需加入并保留源码，不整库安装，也不接受默认 SaaS 风格覆盖 PageFerry 品牌。
- 涉及 API、SQLite schema、文件布局、桌面权限、模型 catalog 或 frontend contract 时，同步更新 `docs/dev/`。
- 发现危险设计、数据丢失风险、密钥泄漏、来源许可证问题或不可维护抽象时，立即停下并给出更稳方案。
- 文档、脚本和配置统一 UTF-8。

## Communication

- 默认使用中文沟通；`contract`、`runtime`、`endpoint`、`payload`、`schema`、`migration`、`token` 等术语保留英文。
- 结论先行，复杂结构优先用小表格、流程图或目录树说明。
- 不确定时明确标注，并给出可复现的验证方式；不要用“应该可以”代替证据。
- 注释和文档用直接的人话，不混写僵硬的中英文架构口号。

## Verification

后端常规验证：

```bash
uv run --directory backend pytest
uv run --directory backend ruff check .
uv run --directory backend ruff format --check .
```

前端常规验证：

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run format:check
npm --prefix frontend run test
npm --prefix frontend run build
```

Tauri 或打包改动要在 `tauri/` 内运行 `cargo fmt --check`、`cargo clippy` 和 `cargo check`，再做对应平台的安装包 smoke test。可以用 `make check` 跑当前骨架的全量静态检查和测试。只跑了局部验证就明确说局部，不要声称 full pass。

## Git

- 只有操作依赖远端状态时才先 `git fetch`；不要默认自动 `pull`、`rebase` 或 `merge`。
- 提交前先检查完整 worktree，保留用户已有改动，只暂存当前任务文件。
- Commit message 使用中文，格式为 `type(scope): 中文描述`；scope 取最小稳定能力名。
- 较大改动的 commit body 用 2-5 条短列表说明新增、删除、迁移、文档和验证内容。

## Documentation

- `docs/dev/` 只放当前仍在推进的计划、调研和 handoff，并维护 `docs/dev/README.md` 索引。
- 稳定规则沉淀到本文件或正式用户文档；阶段完成后删除或归档草稿，不要把过期计划伪装成长期 contract。
