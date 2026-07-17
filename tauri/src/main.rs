//! 组装 PageFerry 原生壳及 renderer 所需的最小 Tauri plugin。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod output;

/// 启动主窗口，并只注册文件选择与受限结果打开能力。
fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![output::open_output])
        .run(tauri::generate_context!())
        .expect("error while running PageFerry");
}
