use serde::Serialize;
use std::net::TcpListener;
use std::sync::Mutex;
use tauri::{Manager, State};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use uuid::Uuid;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct SidecarConnection {
    endpoint: String,
    token: String,
}

struct SidecarState {
    connection: SidecarConnection,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn sidecar_connection(state: State<'_, SidecarState>) -> SidecarConnection {
    state.connection.clone()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    if cfg!(feature = "source-check") {
        panic!("source-check is compilation-only; build the native app with its verified engine and runtime payload");
    }
    let builder = tauri::Builder::default();
    // Register before other plugins and setup so a second launch exits before
    // allocating another sidecar connection or engine process.
    #[cfg(desktop)]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
        }
    }));
    builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let listener = TcpListener::bind("127.0.0.1:0")?;
            let port = listener.local_addr()?.port();
            drop(listener);
            let port_string = port.to_string();
            let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
            let endpoint = format!("http://127.0.0.1:{port}");
            let sidecar = app.shell().sidecar("ai-powerbi-studio-engine")?.args([
                "--host",
                "127.0.0.1",
                "--port",
                &port_string,
                "--token",
                &token,
                "--parent-stdin-lifetime",
            ]);
            let (mut receiver, child) = sidecar.spawn()?;
            tauri::async_runtime::spawn(async move { while receiver.recv().await.is_some() {} });
            app.manage(SidecarState {
                connection: SidecarConnection { endpoint, token },
                child: Mutex::new(Some(child)),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![sidecar_connection])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<SidecarState>() {
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Modelcraft");
}
