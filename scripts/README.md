# Scripts

这里只放需要跨 `frontend/`、`backend/` 和 `tauri/` 协作的构建或发布脚本。单个 runtime 自己能完成的命令留在对应目录，不要把这里变成第二套业务代码入口。

`sync-pdf-assets.py` 按仓库内 canonical manifest 把大型 PDF 模型和字体显式安装到
app-data 的版本化目录；它支持单 asset 或 pack group、自有 CDN base URL、size/SHA-256
校验和原子落盘。`sync-layout-model.py` 仅保留旧开发命令兼容，不维护第二份下载逻辑。
