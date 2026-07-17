//! 生成 Tauri build-time context 与权限 schema。

/// 委托 `tauri-build` 生成当前 desktop bundle 所需资源。
fn main() {
    tauri_build::build()
}
