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

资产的 ID、pack group、distribution path、size 与 SHA-256 只在 manifest 中维护，
README 不复制第二份完整性数据。同步时可用 `--asset` 或 `--pack` 限定下载范围；
不带选择参数时只同步 `layout`，避免首次操作隐式下载全部字体。
