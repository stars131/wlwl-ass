// Prevents additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod python_runtime;

use anyhow::Context;
use python_runtime::PythonRuntime;
use std::sync::Mutex;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, RunEvent, WindowEvent,
};

/// Shared runtime guard that lives for the lifetime of the Tauri app.
struct AppState {
    python: Mutex<Option<PythonRuntime>>,
}

#[tauri::command]
fn get_api_base(state: tauri::State<'_, AppState>) -> Result<String, String> {
    let guard = state.python.lock().map_err(|e| e.to_string())?;
    guard
        .as_ref()
        .map(|rt| rt.base_url())
        .ok_or_else(|| "python runtime not started".to_string())
}

fn main() {
    if let Err(err) = run() {
        eprintln!("[ga-gui] fatal: {err:?}");
        std::process::exit(1);
    }
}

fn run() -> anyhow::Result<()> {
    let project_root = python_runtime::resolve_project_root()
        .context("failed to resolve project root containing launcher/api_server.py")?;
    let runtime = PythonRuntime::start(&project_root)
        .context("failed to start python launcher.api_server")?;
    let api_base = runtime.base_url();
    log::info!("python api server up at {api_base}");

    let state = AppState {
        python: Mutex::new(Some(runtime)),
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_log::Builder::default().build())
        .manage(state)
        .invoke_handler(tauri::generate_handler![get_api_base])
        .on_window_event(|window, event| {
            // Close-to-tray: clicking the window's X hides the window instead
            // of exiting. The tray's "Quit" item is the only path that closes
            // the process (and triggers RunEvent::ExitRequested → python cleanup).
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .setup(move |app| {
            // Inject API base into the webview before any user JS runs.
            let init = format!(
                "window.__GA_API_BASE__ = {};",
                serde_json::to_string(&api_base).expect("api base is valid json")
            );
            for (_label, window) in app.webview_windows() {
                window.eval(&init)?;
            }

            // Build system tray. Skip silently if no app icon is bundled
            // (icons/ ships placeholders only — packaged builds will have one).
            if let Some(icon) = app.default_window_icon().cloned() {
                let show_item = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
                let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

                let _tray = TrayIconBuilder::with_id("wlwl-ass-tray")
                    .icon(icon)
                    .tooltip("wlwl-ass")
                    .menu(&menu)
                    .show_menu_on_left_click(false)
                    .on_menu_event(|app, event| match event.id().as_ref() {
                        "show" => bring_to_front(app),
                        "quit" => app.exit(0),
                        _ => {}
                    })
                    .on_tray_icon_event(|tray, event| {
                        if let TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        } = event
                        {
                            toggle_main_window(tray.app_handle());
                        }
                    })
                    .build(app)?;
            } else {
                log::warn!("no default window icon — tray icon disabled");
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .context("failed to build Tauri application")?
        .run(|app_handle, event| {
            if let RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app_handle.try_state::<AppState>() {
                    if let Ok(mut guard) = state.python.lock() {
                        if let Some(rt) = guard.take() {
                            rt.shutdown();
                        }
                    }
                }
            }
        });

    Ok(())
}

fn bring_to_front(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn toggle_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        match w.is_visible() {
            Ok(true) => {
                let _ = w.hide();
            }
            _ => bring_to_front(app),
        }
    }
}
