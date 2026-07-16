# PageFerry frontend

PageFerry 的 React/Vite/Tailwind CSS v4 界面。这个目录只管理 WebView 中运行的 UI、前端测试和 Node 依赖；Python 服务与 Rust crate 分别位于 `backend/` 和 `tauri/`。

视觉 token 定义在 `src/styles/global.css`。shadcn/ui 组件只按需生成到 `src/components/ui/`，源码由本仓库直接维护，不把默认主题当作产品设计。

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
```

常规检查：

```bash
npm --prefix frontend run test
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run format:check
npm --prefix frontend run build
```
