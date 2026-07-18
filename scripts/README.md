# Scripts

这里只放需要跨 `frontend/`、`backend/` 和 `tauri/` 协作的构建或发布脚本。单个 runtime 自己能完成的命令留在对应目录，不要把这里变成第二套业务代码入口。

`sync-pdf-assets.py` 按仓库内 canonical manifest 把大型 PDF 模型和字体显式安装到
app-data 的版本化目录；它支持单 asset 或 pack group、有序主源/fallback、size/SHA-256
校验和原子落盘。`publish-pdf-assets-github.py` 负责先把同一批文件发布到公开 GitHub Release，
作为 R2 失效后的备用源；它使用 draft 完成全量核验后才公开 Release。`publish-pdf-assets-r2.py`
再把已校验资源发布到 PageFerry 的 R2 版本目录。同版本内容冲突时两者都拒绝覆盖，license
后于二进制、manifest 最后上传。
`sync-layout-model.py` 仅保留旧开发命令兼容，不维护第二份下载逻辑。
