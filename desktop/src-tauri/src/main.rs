#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod backend;
mod desktop_ui;
use backend::Backend;
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

#[derive(Clone)]
struct Shell {
    backend: Arc<Mutex<Option<Backend>>>,
    quitting: Arc<AtomicBool>,
    origin: Arc<Mutex<Option<String>>>,
    error: Arc<Mutex<Option<String>>>,
    ready: Arc<AtomicBool>,
}

fn data_home(_app: &tauri::AppHandle) -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("SPEEDBENCH_HOME") {
        return Ok(PathBuf::from(path));
    }
    // Keep the existing launcher history location; testing explicitly supplies
    // a temporary SPEEDBENCH_HOME, never the user's real data directory.
    #[cfg(windows)]
    {
        return std::env::var_os("APPDATA")
            .map(|x| PathBuf::from(x).join("ClashSpeedBench"))
            .ok_or("APPDATA is unavailable".into());
    }
    #[cfg(target_os = "macos")]
    {
        return _app
            .path()
            .home_dir()
            .map(|x| x.join("Library/Application Support/ClashSpeedBench"))
            .map_err(|_| "Home data directory unavailable".into());
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        return _app
            .path()
            .data_dir()
            .map(|x| x.join("ClashSpeedBench"))
            .map_err(|_| "Application data directory unavailable".into());
    }
}

fn show(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn quit(app: tauri::AppHandle, shell: Shell) {
    if shell.quitting.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(move || {
        let ok = shell
            .backend
            .lock()
            .unwrap()
            .as_mut()
            .map(|b| b.shutdown())
            .unwrap_or(true);
        app.exit(if ok { 0 } else { 2 });
    });
}

fn main() {
    // Read-only package diagnostic: runs before WebView creation and cannot
    // choose a command, data directory, URL or resource path from arguments.
    #[cfg(windows)]
    if std::env::args().collect::<Vec<_>>()
        == [std::env::args().next().unwrap(), "--verify-package".into()]
    {
        let result = std::env::current_exe()
            .map_err(|_| "Executable location unavailable".to_string())
            .and_then(|exe| {
                backend::verify_resources(exe.parent().ok_or("Package root unavailable")?)
            });
        std::process::exit(if result.is_ok() { 0 } else { 2 });
    }
    let shell = Shell {
        backend: Arc::new(Mutex::new(None)),
        quitting: Arc::new(AtomicBool::new(false)),
        origin: Arc::new(Mutex::new(None)),
        error: Arc::new(Mutex::new(None)),
        ready: Arc::new(AtomicBool::new(false)),
    };
    let state = shell.clone();
    let app=tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app,_,_|show(app)))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::Builder::new().open_js_links_on_click(false).build())
        .manage(shell.clone())
        .setup(move |app| {
            let handle=app.handle().clone();
            let nav=state.clone();let loaded=state.clone();
            WebviewWindowBuilder::new(app,"main",WebviewUrl::App("index.html".into()))
                .title("Clash SpeedBench").inner_size(1280.,900.).min_inner_size(720.,600.)
                .initialization_script("Object.defineProperty(window,'SPEEDBENCH_ENV',{value:Object.freeze({client:'webview'}),writable:false});")
                .on_navigation(move |url| {
                    if (url.scheme()=="tauri" && url.host_str()==Some("localhost"))
                        || (url.scheme()=="http" && url.host_str()==Some("tauri.localhost")) {return true;}
                    nav.origin.lock().unwrap().as_ref().is_some_and(|origin|
                        url.origin().ascii_serialization()==*origin && ["/","/index.html"].contains(&url.path())
                            && url.query().is_none() && url.username().is_empty() && url.password().is_none())
                })
                .on_new_window(|_,_|tauri::webview::NewWindowResponse::Deny)
                .on_page_load(move |window,_| {
                    if let Some(error)=loaded.error.lock().unwrap().as_ref() {
                        let value=serde_json::to_string(error).unwrap();
                        let _=window.eval(format!("document.getElementById('startup-message')?.replaceChildren(document.createTextNode({value}));"));
                    }
                }).build()?;
            let open=tauri::menu::MenuItem::with_id(app,"open","打开 SpeedBench",true,None::<&str>)?;
            let cancel=tauri::menu::MenuItem::with_id(app,"cancel","取消当前任务",true,None::<&str>)?;
            let exit=tauri::menu::MenuItem::with_id(app,"exit","退出（先取消并清理）",true,None::<&str>)?;
            let status_item=tauri::menu::MenuItem::with_id(app,"status","启动中…",false,None::<&str>)?;
            let menu=tauri::menu::Menu::with_items(app,&[&status_item,&open,&cancel,&exit])?;
            let tray_shell=state.clone();
            let mut tray=tauri::tray::TrayIconBuilder::with_id("main").menu(&menu).tooltip("Clash SpeedBench")
                .on_menu_event(move |app,event|match event.id.as_ref() {
                    "open"=>show(app),
                    "cancel"=>{let s=tray_shell.clone();std::thread::spawn(move || {if let Some(b)=s.backend.lock().unwrap().as_mut(){let _=b.send("cancel");}});},
                    "exit"=>quit(app.clone(),tray_shell.clone()),_=>{}
                });
            if let Some(icon)=app.default_window_icon(){tray=tray.icon(icon.clone());}
            tray.build(app)?;
            let startup=state.clone();
            std::thread::spawn(move || {
                // Serialize startup with quit. Closing before readiness must
                // not let the quit thread exit ahead of a late backend spawn.
                let mut owned=startup.backend.lock().unwrap();
                if startup.quitting.load(Ordering::SeqCst){return;}
                let result=handle.path().resource_dir().map_err(|_|"Resource directory unavailable".to_string())
                    .and_then(|root|data_home(&handle).and_then(|data|Backend::spawn(&root,&data)));
                match result {
                    Ok(mut backend)=>{
                        if startup.quitting.load(Ordering::SeqCst){let _=backend.shutdown();return;}
                        let origin=format!("http://127.0.0.1:{}",backend.port);
                        *startup.origin.lock().unwrap()=Some(origin.clone());
                        let port=backend.port;
                        *owned=Some(backend);
                        startup.ready.store(true,Ordering::SeqCst);
                        drop(owned);
                        if let Some(window)=handle.get_webview_window("main") {let _=window.navigate(format!("{origin}/").parse().unwrap());}
                        let mut notifications=desktop_ui::NotificationGate::default();
                        loop {
                            if startup.quitting.load(Ordering::SeqCst){return;}
                            let status=startup.backend.lock().unwrap().as_mut().and_then(|b|b.exited().ok()).flatten();
                            if let Some(code)=status {startup.backend.lock().unwrap().take();startup.quitting.store(true,Ordering::SeqCst);handle.exit(code);return;}
                            let diagnostic=startup.backend.lock().unwrap().as_ref().and_then(|b|b.get_json("/api/desktop/state").ok());
                            if let Some(frame)=diagnostic.and_then(|v|serde_json::from_value::<desktop_ui::Status>(v).ok()).filter(|v|v.valid()) {
                                let label=frame.label();let _=status_item.set_text(&label);
                                if let Some(tray)=handle.tray_by_id("main"){let _=tray.set_tooltip(Some(&label));}
                                for action in &frame.actions {
                                    if let Some(url)=desktop_ui::external_url(&action.action,port) {
                                        let ok=handle.opener().open_url(url,None::<&str>).is_ok();
                                        if let Some(b)=startup.backend.lock().unwrap().as_mut(){let _=b.action_result(&action.request_id,ok);}
                                    }
                                }
                                if let Some(body)=notifications.observe(&frame.job,frame.notifications) {
                                    let _=handle.notification().builder().title("Clash SpeedBench").body(body).show();
                                }
                            }
                            std::thread::sleep(std::time::Duration::from_secs(1));
                        }
                    },
                    Err(error)=>{
                        *startup.error.lock().unwrap()=Some(error.clone());
                        if let Some(window)=handle.get_webview_window("main") {
                            let value=serde_json::to_string(&error).unwrap();
                            let _=window.eval(format!("document.getElementById('startup-message')?.replaceChildren(document.createTextNode({value}));"));
                        }
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window,event| {
            if let WindowEvent::CloseRequested {api,..}=event {
                let shell=window.state::<Shell>();
                if !shell.ready.load(Ordering::SeqCst) {api.prevent_close();quit(window.app_handle().clone(),shell.inner().clone());}
                else if !shell.quitting.load(Ordering::SeqCst){api.prevent_close();let _=window.hide();}
            }
        })
        .build(tauri::generate_context!()).expect("SpeedBench desktop initialization failed");
    app.run(move |handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !shell.quitting.load(Ordering::SeqCst) {
                api.prevent_exit();
                quit(handle.clone(), shell.clone());
            }
        }
    });
}
