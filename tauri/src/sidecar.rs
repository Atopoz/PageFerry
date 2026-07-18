//! 启动、握手并回收随 PageFerry 发布的 Python sidecar。

use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

#[cfg(not(debug_assertions))]
use std::ffi::OsString;
#[cfg(any(not(debug_assertions), test))]
use std::fmt::Write as _;
#[cfg(not(debug_assertions))]
use std::path::PathBuf;

#[cfg(any(not(debug_assertions), test))]
use rand::RngCore;
#[cfg(any(not(debug_assertions), test))]
use serde::Deserialize;
use serde::Serialize;
#[cfg(not(debug_assertions))]
use tauri::AppHandle;
#[cfg(not(debug_assertions))]
use tauri::Manager;
use tauri::State;
use tauri_plugin_shell::process::CommandChild;
#[cfg(not(debug_assertions))]
use tauri_plugin_shell::process::CommandEvent;
#[cfg(not(debug_assertions))]
use tauri_plugin_shell::ShellExt;

#[cfg(not(debug_assertions))]
const SIDECAR_RESOURCE_DIRECTORY: &str = "backend";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(3);

/// 返回给 renderer 的最小本地连接信息。
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SidecarConnection {
    base_url: String,
    boot_token: Option<String>,
}

/// 解析 Python sidecar stdout 的单行 ready handshake。
#[derive(Debug, Deserialize)]
#[cfg(any(not(debug_assertions), test))]
struct ReadyHandshake {
    event: String,
    port: u16,
}

/// 保存 child 与握手结果，所有状态转换都在同一把锁内完成。
#[derive(Default)]
struct RuntimeState {
    child: Option<CommandChild>,
    connection: Option<SidecarConnection>,
    startup_error: Option<String>,
    running: bool,
}

/// 允许 Tauri setup、IPC command 与退出事件共享唯一 sidecar child。
struct ManagerInner {
    state: Mutex<RuntimeState>,
    changed: Condvar,
}

/// 管理 PageFerry 生命周期内唯一的 backend child。
#[derive(Clone)]
pub(crate) struct SidecarManager {
    inner: Arc<ManagerInner>,
}

impl Default for SidecarManager {
    fn default() -> Self {
        Self {
            inner: Arc::new(ManagerInner {
                state: Mutex::new(RuntimeState::default()),
                changed: Condvar::new(),
            }),
        }
    }
}

impl SidecarManager {
    /// debug 构建继续连接由 `make backend` 启动的固定开发端口。
    #[cfg(debug_assertions)]
    pub(crate) fn configure_development(&self) -> Result<(), String> {
        let mut state = self.lock_state()?;
        state.connection = Some(SidecarConnection {
            base_url: "http://127.0.0.1:8765".to_owned(),
            boot_token: None,
        });
        Ok(())
    }

    /// release 构建启动 resource 中的 frozen sidecar，并异步等待 stdout handshake。
    #[cfg(not(debug_assertions))]
    pub(crate) fn start(&self, app: &AppHandle) -> Result<(), String> {
        let data_dir = app
            .path()
            .data_dir()
            .map_err(|_| "系统无法解析 PageFerry 数据目录。".to_owned())?
            .join("PageFerry");
        let token = generate_boot_token();
        let arguments = vec![
            OsString::from("--port"),
            OsString::from("0"),
            OsString::from("--boot-token-stdin"),
            OsString::from("--data-dir"),
            data_dir.into_os_string(),
        ];
        let executable = sidecar_executable(app)?;
        let command = app.shell().command(executable).args(arguments);
        let (mut events, mut child) = command
            .spawn()
            .map_err(|_| "无法启动 PageFerry 本地服务。".to_owned())?;
        let mut token_line = token.as_bytes().to_vec();
        token_line.push(b'\n');
        if child.write(&token_line).is_err() {
            let _ = child.kill();
            return Err("无法初始化 PageFerry 本地服务。".to_owned());
        }

        {
            let mut state = self.lock_state()?;
            if state.child.is_some() || state.running {
                return Err("PageFerry 本地服务已经启动。".to_owned());
            }
            state.child = Some(child);
            state.running = true;
            state.startup_error = None;
        }

        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            while let Some(event) = events.recv().await {
                match event {
                    CommandEvent::Stdout(bytes) => {
                        if let Ok(line) = std::str::from_utf8(&bytes) {
                            if let Some(connection) = connection_from_handshake(line, &token) {
                                manager.mark_ready(connection);
                            }
                        }
                    }
                    CommandEvent::Stderr(_) => {
                        // Python/native loader 细节只保留在进程边界内，不能回显本机路径或参数。
                    }
                    CommandEvent::Error(_) => {
                        manager.mark_failed("PageFerry 本地服务启动失败。");
                        break;
                    }
                    CommandEvent::Terminated(_) => {
                        manager.mark_terminated();
                        break;
                    }
                    _ => {}
                }
            }
        });
        Ok(())
    }

    /// 在有界时间内等待 ready、启动失败或超时。
    fn wait_connection(&self) -> Result<SidecarConnection, String> {
        let state = self.lock_state()?;
        let (state, timeout) = self
            .inner
            .changed
            .wait_timeout_while(state, STARTUP_TIMEOUT, |current| {
                current.connection.is_none() && current.startup_error.is_none()
            })
            .map_err(|_| "PageFerry 本地服务状态不可用。".to_owned())?;

        if let Some(connection) = &state.connection {
            return Ok(connection.clone());
        }
        if let Some(error) = &state.startup_error {
            return Err(error.clone());
        }
        if timeout.timed_out() {
            return Err("PageFerry 本地服务启动超时。".to_owned());
        }
        Err("PageFerry 本地服务尚未就绪。".to_owned())
    }

    /// 在应用退出前先发送 TERM；超时后再强制回收 child。
    pub(crate) fn stop(&self) {
        let pid = self
            .inner
            .state
            .lock()
            .ok()
            .and_then(|state| state.child.as_ref().map(CommandChild::pid));
        let Some(pid) = pid else {
            return;
        };

        #[cfg(unix)]
        let graceful_signal_sent = unsafe { libc::kill(pid as i32, libc::SIGTERM) == 0 };
        #[cfg(not(unix))]
        let graceful_signal_sent = false;

        let Ok(state) = self.inner.state.lock() else {
            return;
        };
        let Ok((mut state, timeout)) =
            self.inner
                .changed
                .wait_timeout_while(state, SHUTDOWN_TIMEOUT, |current| {
                    graceful_signal_sent && current.running
                })
        else {
            return;
        };
        let force_kill = !graceful_signal_sent || timeout.timed_out();
        let child = state.child.take();
        state.connection = None;
        state.running = false;
        drop(state);

        if force_kill {
            if let Some(child) = child {
                let _ = child.kill();
            }
        }
    }

    /// 保存握手结果并唤醒等待中的 renderer command。
    #[cfg(not(debug_assertions))]
    fn mark_ready(&self, connection: SidecarConnection) {
        if let Ok(mut state) = self.inner.state.lock() {
            if state.running && state.connection.is_none() {
                state.connection = Some(connection);
                self.inner.changed.notify_all();
            }
        }
    }

    /// 在 ready 前记录稳定错误；原始 stderr 不进入 renderer。
    #[cfg(not(debug_assertions))]
    fn mark_failed(&self, message: &str) {
        if let Ok(mut state) = self.inner.state.lock() {
            if state.connection.is_none() {
                state.startup_error = Some(message.to_owned());
            }
            state.running = false;
            self.inner.changed.notify_all();
        }
    }

    /// 收敛 child 自然退出或被信号回收后的状态。
    #[cfg(not(debug_assertions))]
    fn mark_terminated(&self) {
        if let Ok(mut state) = self.inner.state.lock() {
            if state.connection.is_none() && state.startup_error.is_none() {
                state.startup_error = Some("PageFerry 本地服务在就绪前退出。".to_owned());
            }
            state.running = false;
            self.inner.changed.notify_all();
        }
    }

    /// 把 poison error 收敛成不泄漏内部实现的稳定文案。
    fn lock_state(&self) -> Result<std::sync::MutexGuard<'_, RuntimeState>, String> {
        self.inner
            .state
            .lock()
            .map_err(|_| "PageFerry 本地服务状态不可用。".to_owned())
    }
}

/// 从 Tauri resource 目录定位 PyInstaller onedir 的实际入口。
#[cfg(not(debug_assertions))]
fn sidecar_executable(app: &AppHandle) -> Result<PathBuf, String> {
    let executable_name = if cfg!(target_os = "windows") {
        "pageferry-backend.exe"
    } else {
        "pageferry-backend"
    };
    let executable = app
        .path()
        .resource_dir()
        .map_err(|_| "系统无法解析 PageFerry 资源目录。".to_owned())?
        .join(SIDECAR_RESOURCE_DIRECTORY)
        .join(executable_name);
    if !executable.is_file() {
        return Err("无法定位 PageFerry 本地服务。".to_owned());
    }
    Ok(executable)
}

/// 供 renderer 在首个 HTTP 请求前读取实际端口与一次性 token。
#[tauri::command]
pub(crate) fn sidecar_connection(
    state: State<'_, SidecarManager>,
) -> Result<SidecarConnection, String> {
    state.wait_connection()
}

/// 生成不会进入日志或 SQLite 的 256-bit boot token。
#[cfg(any(not(debug_assertions), test))]
fn generate_boot_token() -> String {
    let mut bytes = [0_u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    let mut token = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(&mut token, "{byte:02x}").expect("写入 String 不会失败");
    }
    token
}

/// 只接受 loopback 动态端口的固定 ready 事件。
#[cfg(any(not(debug_assertions), test))]
fn connection_from_handshake(line: &str, token: &str) -> Option<SidecarConnection> {
    let ready = serde_json::from_str::<ReadyHandshake>(line).ok()?;
    if ready.event != "ready" || ready.port == 0 {
        return None;
    }
    Some(SidecarConnection {
        base_url: format!("http://127.0.0.1:{}", ready.port),
        boot_token: Some(token.to_owned()),
    })
}

#[cfg(test)]
mod tests {
    //! 验证 token 与 ready handshake 不会把连接边界放宽到任意地址。

    use super::{connection_from_handshake, generate_boot_token, SidecarConnection};

    #[test]
    fn generates_256_bit_hex_boot_token() {
        let first = generate_boot_token();
        let second = generate_boot_token();

        assert_eq!(first.len(), 64);
        assert!(first.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_ne!(first, second);
    }

    #[test]
    fn parses_ready_handshake_into_loopback_connection() {
        let connection =
            connection_from_handshake(r#"{"event":"ready","port":43127}"#, "runtime-token");

        assert_eq!(
            connection,
            Some(SidecarConnection {
                base_url: "http://127.0.0.1:43127".to_owned(),
                boot_token: Some("runtime-token".to_owned()),
            })
        );
    }

    #[test]
    fn rejects_wrong_event_and_zero_port() {
        assert!(
            connection_from_handshake(r#"{"event":"starting","port":43127}"#, "runtime-token")
                .is_none()
        );
        assert!(
            connection_from_handshake(r#"{"event":"ready","port":0}"#, "runtime-token").is_none()
        );
    }
}
