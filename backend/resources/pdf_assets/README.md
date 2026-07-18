# PDF resource pack

`manifest.json` 是 PageFerry PDF 大型二进制的唯一 canonical manifest。仓库只保存
manifest、许可证和同步代码；发布时的模型与字体安装到：

```text
<app-data>/pdf/<pack_revision>/
  layout/
  fonts/
```

`layout` pack 当前包含固定 revision 的 PP-DocLayoutV3 ONNX 模型，采用
Apache-2.0，完整条款见 `licenses/Apache-2.0.txt`。字体按目标语种拆成
`fonts-common-zh-cn`、`fonts-zh-tw`、
`fonts-zh-hk`、`fonts-ja`、`fonts-ko`、`fonts-bn` 与 `fonts-km` 选择 group，落盘时
仍统一进入 `fonts/`。17 个 Noto 字体文件均采用 SIL Open Font License 1.1；
完整条款见 `licenses/OFL-1.1.txt`。CJK 字体 metadata
中的 Reserved Font Name 约束仍然有效，修改版不得冒充原始字体发布。
每个字体同时固定经过逐字节核验的 Google Fonts 官方 `fonts.gstatic.com` artifact 与
Noto source revision，不再依赖无法追溯的本地文件来源。

资产的 ID、pack group、distribution path、size 与 SHA-256 只在 manifest 中维护，
README 不复制第二份完整性数据。同步时可用 `--asset` 或 `--pack` 限定下载范围；
不带选择参数时只同步 `layout`，避免首次操作隐式下载全部字体。

正式资源发布在 Cloudflare R2 bucket `pageferry-assets`，通过
`https://assets.pageferry.download/pdf/<pack_revision>/` 对外提供。版本目录一旦发布就视为
不可变：同一个 object key 已存在但 size 或 SHA-256 不一致时必须停止，不能覆盖。
`scripts/publish-pdf-assets-r2.py` 会先校验全部本地二进制，再依次发布二进制、许可证，最后
发布版本化 manifest 作为完成标志。所有对象都写入
`Cache-Control: public, max-age=31536000, immutable`；完整性仍以 canonical manifest 的
SHA-256 为准，不依赖 R2 ETag。

每个二进制还在公开仓库 `Atopoz/PageFerry` 的
`pdf-assets-<pack_revision>` GitHub Release 保存同名 asset。runtime 的固定候选顺序是
PageFerry R2/CDN、GitHub Release、官方 upstream；主源出现网络、HTTP、size 或 SHA-256
错误时才尝试下一项。三层都使用 canonical manifest 的同一组 size/SHA-256，不把 fallback
降级成“下载到什么就信什么”。

发布当前 revision：

```bash
npx --yes wrangler@4.112.0 login
uv run --directory backend python ../scripts/publish-pdf-assets-github.py \
  --repo Atopoz/PageFerry \
  --tag pdf-assets-2026.07.18.2
uv run --directory backend python ../scripts/publish-pdf-assets-r2.py \
  --bucket pageferry-assets \
  --wrangler-command "npx --yes wrangler@4.112.0"
```

先发布 GitHub Release，再发布 R2。这样 R2 最后写入 manifest 完成标志时，里面声明的 fallback
已经可用。两个发布脚本都会校验并复用内容完全一致的既有文件；同 revision 可以安全重跑，
内容冲突则拒绝覆盖。`default_base_url` 必须先指向本次即将发布的版本目录，保证最后上传的远端
manifest 自包含同一 CDN 地址。
