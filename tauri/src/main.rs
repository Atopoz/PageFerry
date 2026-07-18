//! 组装 PageFerry 原生壳及 renderer 所需的最小 Tauri plugin。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod output;
mod sidecar;

use sidecar::SidecarManager;
use tauri::{Manager, RunEvent};

/// 启动主窗口，并只注册文件选择与受限结果打开能力。
fn main() {
    let sidecar_manager = SidecarManager::default();
    let setup_manager = sidecar_manager.clone();
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .manage(sidecar_manager)
        .setup(move |_app| {
            #[cfg(debug_assertions)]
            setup_manager.configure_development()?;
            #[cfg(not(debug_assertions))]
            setup_manager.start(_app.handle())?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            output::open_output,
            sidecar::sidecar_connection
        ])
        .build(tauri::generate_context!())
        .expect("error while building PageFerry");

    application.run(|app, event| {
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            app.state::<SidecarManager>().stop();
        }
    });
}
