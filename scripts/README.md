# Scripts

这里只放需要跨 `frontend/`、`backend/` 和 `tauri/` 协作的构建或发布脚本。单个 runtime 自己能完成的命令留在对应目录，不要把这里变成第二套业务代码入口。

`sync-pdf-assets.py` 按仓库内 canonical manifest 把大型 PDF 模型和字体显式安装到
app-data 的版本化目录；它支持单 asset 或 pack group、有序主源/fallback、size/SHA-256
校验和原子落盘。`publish-pdf-assets-github.py` 负责先把同一批文件发布到公开 GitHub Release，
作为 R2 失效后的备用源；它使用 draft 完成全量核验后才公开 Release。`publish-pdf-assets-r2.py`
再把已校验资源发布到 PageFerry 的 R2 版本目录。同版本内容冲突时两者都拒绝覆盖，license
后于二进制、manifest 最后上传。
`sync-layout-model.py` 仅保留旧开发命令兼容，不维护第二份下载逻辑。

`build-sidecar.py` 在当前 host 使用 PyInstaller 生成
`tauri/binaries/pageferry-backend/` onedir；该目录由 Git 忽略，包含当前平台的 Python 和 native libraries，不能跨平台复用。使用目录发布是为了避免 onefile 每次启动都把 native runtime 解压到新路径并重复接受 macOS 校验。
`make build-macos-smoke` 随后生成：

```text
tauri/target/release/bundle/macos/PageFerry.app
tauri/target/release/bundle/dmg/PageFerry_0.1.0-beta_aarch64.dmg
```

`finalize-macos-dmg.py` 会在 bundle 完成后清除 Tauri 默认写入的 `.VolumeIcon.icns` 与卷 custom-icon 标记，保留 `.DS_Store` 布局，重新压缩、签名并校验最终 DMG。安装窗口使用原生白底，不依赖 Finder 不稳定的背景缩放。`make build-macos-smoke` 和 `make build-macos-beta` 都做 ad-hoc 签名；后者只用于明确告知 Gatekeeper 限制的公开测试版，不会获得 Apple 信任。正式 macOS 构建必须让 PyInstaller 和 Tauri 共用 `APPLE_SIGNING_IDENTITY`；DMG 清理必须发生在最终 DMG 签名、公证和 stapling 之前，并通过 `DMG_SIGN_IDENTITY` 传入正式 identity。PDF ONNX 模型与字体始终由 resource-pack 安装，不进入 sidecar、`.app` 或 DMG。

`make build-macos-release APPLE_SIGNING_IDENTITY="Developer ID Application: ..." DMG_SIGN_IDENTITY="Developer ID Application: ..."` 会显式使用 `tauri.release.conf.json` 打包 sidecar resource，并生成已签名但未公证的 release candidate。该 target 不会读取 Apple notarization 凭据；必须对后处理完成的最终 DMG 另行 notarize 与 staple，不能把后处理前的 ticket 当作有效产物。

Windows x64 安装包必须在 Windows runner 上构建，不能复用 macOS 冻结出的 sidecar。
`.github/workflows/build-windows-release.yml` 只接收版本 tag，校验 tag 与应用版本一致，运行 Windows 后端测试和冻结 sidecar smoke test，再生成 NSIS `.exe` 并作为 Actions artifact 保留 7 天。发布前需下载该 artifact，与 DMG 一起计算 SHA-256 并上传 GitHub Release。
