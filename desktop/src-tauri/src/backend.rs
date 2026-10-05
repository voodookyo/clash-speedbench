//! Owned backend runtime. Nothing here is a renderer-callable shell API.
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

pub const APP_ID: &str = "com.voodookyo.clash-speedbench";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const MANIFEST: &str = include_str!("../resources/manifest.json");

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema: u32,
    app_id: String,
    version: String,
    runtime_version: String,
    target: String,
    executable: String,
    files: BTreeMap<String, String>,
    source_revision: Option<String>,
    source_dirty: bool,
}

pub fn safe_relative(value: &str) -> bool {
    !value.is_empty()
        && !value.contains(['\\', ':'])
        && !value.starts_with('/')
        && value
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

pub fn verify_resources(root: &Path) -> Result<PathBuf, String> {
    let manifest: Manifest =
        serde_json::from_str(MANIFEST).map_err(|_| "Bundled manifest invalid")?;
    if manifest.schema != 1
        || manifest.app_id != APP_ID
        || manifest.version != VERSION
        || manifest.files.len() > 5000
        || !safe_relative(&manifest.executable)
    {
        return Err("Bundled manifest identity invalid".into());
    }
    let _ = (
        &manifest.runtime_version,
        &manifest.target,
        &manifest.source_revision,
        &manifest.source_dirty,
    );
    fn inventory(
        root: &Path,
        dir: &Path,
        result: &mut Vec<String>,
        depth: u32,
    ) -> Result<(), String> {
        if depth > 32 {
            return Err("Resource nesting exceeds limit".into());
        }
        for item in fs::read_dir(dir).map_err(|_| "Bundled resource directory is missing")? {
            let item = item.map_err(|_| "Bundled resource enumeration failed")?;
            let kind = item
                .file_type()
                .map_err(|_| "Bundled resource type unavailable")?;
            if kind.is_symlink() {
                return Err("Bundled resource links are not allowed".into());
            }
            if kind.is_dir() {
                inventory(root, &item.path(), result, depth + 1)?;
            } else if kind.is_file() {
                result.push(
                    item.path()
                        .strip_prefix(root)
                        .map_err(|_| "Resource escaped root")?
                        .to_string_lossy()
                        .replace('\\', "/"),
                );
            } else {
                return Err("Bundled resource type unsupported".into());
            }
            if result.len() > 5000 {
                return Err("Resource inventory exceeds limit".into());
            }
        }
        Ok(())
    }
    let mut actual = vec![];
    inventory(root, &root.join("app"), &mut actual, 0)?;
    inventory(root, &root.join("runtime"), &mut actual, 0)?;
    actual.sort();
    if actual != manifest.files.keys().cloned().collect::<Vec<_>>() {
        return Err(
            "Unexpected or missing runtime/source files; use the complete official package".into(),
        );
    }
    for (name, expected) in &manifest.files {
        if !safe_relative(name) || !name.starts_with("app/") && !name.starts_with("runtime/") {
            return Err("Bundled resource path invalid".into());
        }
        let path = root.join(name);
        let meta =
            fs::symlink_metadata(&path).map_err(|_| "Bundled runtime or source is missing")?;
        if !meta.is_file() || meta.len() > 100_000_000 {
            return Err("Bundled resource type invalid".into());
        }
        let bytes = fs::read(path).map_err(|_| "Bundled resource cannot be read")?;
        if format!("{:x}", Sha256::digest(bytes)) != *expected {
            return Err("Bundled runtime/source integrity check failed; reinstall the complete official package".into());
        }
    }
    if !manifest.files.contains_key(&manifest.executable) {
        return Err("Unlisted runtime executable".into());
    }
    Ok(root.join(manifest.executable))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Handshake {
    app_id: String,
    version: String,
    protocol: u32,
    instance_id: String,
    pid: u32,
    port: u16,
    nonce: String,
    token: String,
}

fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn validate_handshake(h: &Handshake, nonce: &str, pid: u32) -> Result<(), String> {
    if h.app_id != APP_ID
        || h.version != VERSION
        || h.protocol != 1
        || h.pid != pid
        || h.nonce != nonce
        || !hex(&h.nonce, 64)
        || !hex(&h.token, 32)
        || !hex(&h.instance_id, 32)
        || h.port == 0
    {
        return Err("Private backend handshake identity/challenge mismatch".into());
    }
    Ok(())
}

// The Windows kernel owns descendants through this handle, not a process
// name/PID scan. It is assigned BEFORE the private bootstrap permits work.
#[cfg(windows)]
pub struct Tree {
    handle: isize,
}
#[cfg(windows)]
impl Tree {
    pub fn assign(child: &Child) -> Result<Self, String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::{Foundation::CloseHandle, System::JobObjects::*};
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err("Cannot create owned process job".into());
            }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                std::mem::size_of_val(&limits) as u32,
            ) == 0
                || AssignProcessToJobObject(job, child.as_raw_handle()) == 0
            {
                CloseHandle(job);
                return Err("Cannot establish backend process ownership".into());
            }
            Ok(Self {
                handle: job as isize,
            })
        }
    }
    pub fn terminate(&self) {
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.handle as _, 2);
        }
    }
}
#[cfg(windows)]
impl Drop for Tree {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.handle as _);
        }
    }
}

#[cfg(unix)]
pub struct Tree {
    group: i32,
}
#[cfg(unix)]
impl Tree {
    pub fn assign(child: &Child) -> Result<Self, String> {
        Ok(Self {
            group: child.id() as i32,
        })
    }
    pub fn terminate(&self) {
        unsafe {
            libc::kill(-self.group, libc::SIGKILL);
        }
    }
}

pub struct Backend {
    child: Child,
    pipe: Option<ChildStdin>,
    tree: Tree,
    pub port: u16,
    token: String,
    instance_id: String,
}

impl Backend {
    pub fn spawn(resources: &Path, data: &Path) -> Result<Self, String> {
        Self::spawn_with_path(resources, data, None)
    }

    fn spawn_with_path(
        resources: &Path,
        data: &Path,
        restricted_path: Option<&std::ffi::OsStr>,
    ) -> Result<Self, String> {
        let python = verify_resources(resources)?;
        fs::create_dir_all(data).map_err(|_| "Application data directory is unavailable")?;
        let data =
            fs::canonicalize(data).map_err(|_| "Application data directory cannot be resolved")?;
        let mut command = Command::new(python);
        command
            .args(["-B", "-E", "-s", "-u"])
            .arg(resources.join("app/speedbench_desktop.py"))
            .env("SPEEDBENCH_HOME", &data)
            .current_dir(&data)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(test)]
        command.stderr(Stdio::inherit()); // Fixed startup stages only; never the private stdout frame.
        // -E protects this process; removing inherited Python overrides also
        // protects its worker descendants (without removing provider keys).
        for (key, _) in std::env::vars_os() {
            if key
                .to_string_lossy()
                .to_ascii_uppercase()
                .starts_with("PYTHON")
            {
                command.env_remove(key);
            }
        }
        if let Some(path) = restricted_path {
            command.env("PATH", path);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW, not a visible console.
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command
            .spawn()
            .map_err(|_| "Bundled Python could not start")?;
        let tree = match Tree::assign(&child) {
            Ok(tree) => tree,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        let mut backend = Self {
            child,
            pipe: None,
            tree,
            port: 0,
            token: String::new(),
            instance_id: String::new(),
        };
        backend.pipe = backend.child.stdin.take();
        let stdout = backend
            .child
            .stdout
            .take()
            .ok_or("Private bootstrap pipe unavailable")?;
        let (tx, rx) = mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout).take(4097);
            let mut frame = String::new();
            let ok = reader.read_line(&mut frame).is_ok()
                && frame.ends_with('\n')
                && frame.len() <= 4096;
            let _ = tx.send(if ok { Some(frame) } else { None });
        });
        let nonce = format!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        );
        let bootstrap =
            serde_json::json!({"protocol":1,"nonce":nonce,"parent_pid":std::process::id()});
        backend
            .pipe
            .as_mut()
            .ok_or("Private control pipe unavailable")?
            .write_all(format!("{bootstrap}\n").as_bytes())
            .map_err(|_| "Private bootstrap write failed")?;
        let frame=rx.recv_timeout(Duration::from_secs(20)).map_err(|_| "Backend readiness timed out")?
            .ok_or("Backend startup failed; close an existing SpeedBench using this data directory, or reinstall the full package")?;
        let handshake: Handshake =
            serde_json::from_str(&frame).map_err(|_| "Invalid private handshake frame")?;
        validate_handshake(&handshake, &nonce, backend.child.id())?;
        backend.port = handshake.port;
        backend.token = handshake.token;
        backend.instance_id = handshake.instance_id;
        let identity = backend.get_json("/api/desktop/identity")?;
        if identity.get("instance_id").and_then(|x| x.as_str()) != Some(&backend.instance_id)
            || identity.get("pid").and_then(|x| x.as_u64()) != Some(backend.child.id() as u64)
            || identity.get("app_id").and_then(|x| x.as_str()) != Some(APP_ID)
            || identity.get("version").and_then(|x| x.as_str()) != Some(VERSION)
        {
            return Err("Backend HTTP identity does not match private process handshake".into());
        }
        Ok(backend)
    }

    pub fn get_json(&self, path: &str) -> Result<serde_json::Value, String> {
        if !["/api/desktop/identity", "/api/desktop/state"].contains(&path) {
            return Err("Unsupported backend diagnostic".into());
        }
        let address = SocketAddr::from(([127, 0, 0, 1], self.port));
        let mut socket = TcpStream::connect_timeout(&address, Duration::from_secs(2))
            .map_err(|_| "Backend unavailable")?;
        socket
            .set_read_timeout(Some(Duration::from_secs(3)))
            .map_err(|_| "Backend timeout unavailable")?;
        socket
            .set_write_timeout(Some(Duration::from_secs(2)))
            .map_err(|_| "Backend timeout unavailable")?;
        write!(socket,"GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nX-SpeedBench-Token: {}\r\nConnection: close\r\n\r\n",self.port,self.token)
            .map_err(|_| "Backend diagnostic write failed")?;
        let mut response = String::new();
        socket
            .take(65_537)
            .read_to_string(&mut response)
            .map_err(|_| "Backend diagnostic failed")?;
        if response.len() > 65_536 || !response.starts_with("HTTP/1.0 200 ") {
            return Err("Backend rejected authenticated diagnostic".into());
        }
        let body = response
            .split_once("\r\n\r\n")
            .ok_or("Backend diagnostic invalid")?
            .1;
        serde_json::from_str(body).map_err(|_| "Backend diagnostic JSON invalid".into())
    }

    pub fn send(&mut self, command: &str) -> Result<(), String> {
        if !["cancel", "exit"].contains(&command) {
            return Err("Unsupported lifecycle command".into());
        }
        let value = serde_json::json!({"command":command});
        self.pipe
            .as_mut()
            .ok_or("Private control pipe closed")?
            .write_all(format!("{value}\n").as_bytes())
            .map_err(|_| "Private lifecycle command failed".into())
    }

    pub fn action_result(&mut self, request_id: &str, ok: bool) -> Result<(), String> {
        if !hex(request_id, 32) {
            return Err("Invalid desktop request identity".into());
        }
        let value = serde_json::json!({"command":"action_result","request_id":request_id,"ok":ok});
        self.pipe
            .as_mut()
            .ok_or("Private control pipe closed")?
            .write_all(format!("{value}\n").as_bytes())
            .map_err(|_| "Private action acknowledgement failed".into())
    }

    pub fn exited(&mut self) -> Result<Option<i32>, String> {
        self.child
            .try_wait()
            .map(|s| s.map(|s| s.code().unwrap_or(2)))
            .map_err(|_| "Backend exit check failed".into())
    }

    pub fn shutdown(&mut self) -> bool {
        let _ = self.send("exit");
        let deadline = Instant::now() + Duration::from_secs(40);
        while Instant::now() < deadline {
            match self.exited() {
                Ok(Some(code)) => return code == 0,
                Err(_) => break,
                _ => std::thread::sleep(Duration::from_millis(100)),
            }
        }
        self.tree.terminate();
        let _ = self.child.wait();
        false
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.pipe.take();
        self.tree.terminate();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn relative_paths_are_not_shell_or_traversal() {
        for path in [
            "../key",
            "/private",
            "runtime/../app",
            "C:/secret",
            "runtime\\key",
            "a//b",
        ] {
            assert!(!safe_relative(path));
        }
        assert!(safe_relative("runtime/python.exe"));
    }
    #[test]
    fn handshake_rejects_wrong_process_challenge_or_version() {
        let mut h = Handshake {
            app_id: APP_ID.into(),
            version: VERSION.into(),
            protocol: 1,
            instance_id: "a".repeat(32),
            pid: 4,
            port: 8951,
            nonce: "b".repeat(64),
            token: "c".repeat(32),
        };
        assert!(validate_handshake(&h, &"b".repeat(64), 4).is_ok());
        assert!(validate_handshake(&h, &"d".repeat(64), 4).is_err());
        assert!(validate_handshake(&h, &"b".repeat(64), 5).is_err());
        h.version = "unrelated".into();
        assert!(validate_handshake(&h, &"b".repeat(64), 4).is_err());
    }

    #[test]
    fn bundled_backend_starts_without_system_python_and_retains_raw() {
        let data =
            std::env::temp_dir().join(format!("speedbench 中文 空格 🧪-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&data).unwrap();
        struct Fixture(PathBuf);
        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let fixture = Fixture(data);
        let history = fixture.0.join("speedbench-history.jsonl");
        let raw = b"{\"ts\":\"fixture-only\",\"results\":[]}\n";
        fs::write(&history, raw).unwrap();
        let resources = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources");
        #[cfg(windows)]
        let path = PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join("System32");
        #[cfg(unix)]
        let path = PathBuf::from("/usr/bin:/bin");
        let mut backend =
            Backend::spawn_with_path(&resources, &fixture.0, Some(path.as_os_str())).unwrap();
        assert!(backend.port > 0);
        assert_eq!(
            backend.get_json("/api/desktop/identity").unwrap()["version"],
            VERSION
        );
        assert!(Backend::spawn_with_path(&resources, &fixture.0, Some(path.as_os_str())).is_err());
        assert!(backend.shutdown());
        assert_eq!(fs::read(history).unwrap(), raw);
        // Kernel lease is released, regardless of stale PID metadata.
        let mut restarted =
            Backend::spawn_with_path(&resources, &fixture.0, Some(path.as_os_str())).unwrap();
        assert!(restarted.shutdown());
    }

    #[cfg(windows)]
    #[test]
    fn job_close_kills_owned_grandchild_but_not_unrelated_process() {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::{
            Foundation::{CloseHandle, WAIT_OBJECT_0},
            System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE},
        };
        struct Owned(Child);
        impl Drop for Owned {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
        struct Handle(windows_sys::Win32::Foundation::HANDLE);
        impl Drop for Handle {
            fn drop(&mut self) {
                unsafe {
                    CloseHandle(self.0);
                }
            }
        }
        let resources = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources");
        let python = verify_resources(&resources).unwrap();
        let spawn = |code: &str| {
            Owned(
                Command::new(&python)
                    .args(["-B", "-E", "-s", "-u", "-c", code])
                    .creation_flags(0x08000000)
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::null())
                    .spawn()
                    .unwrap(),
            )
        };
        let mut unrelated = spawn("import time;time.sleep(60)");
        let mut parent=spawn("import sys,subprocess,time;sys.stdin.buffer.read(1);p=subprocess.Popen([sys.executable,'-B','-E','-s','-c','import time;time.sleep(60)']);print(p.pid,flush=True);time.sleep(60)");
        let tree = Tree::assign(&parent.0).unwrap();
        parent.0.stdin.as_mut().unwrap().write_all(b"x").unwrap();
        let pipe = parent.0.stdout.take().unwrap();
        let (tx, rx) = mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let mut line = String::new();
            let _ = BufReader::new(pipe).take(32).read_line(&mut line);
            let _ = tx.send(line);
        });
        let pid: u32 = rx
            .recv_timeout(Duration::from_secs(10))
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        let grandchild = Handle(unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) });
        assert!(!grandchild.0.is_null());
        assert_ne!(
            unsafe { WaitForSingleObject(grandchild.0, 0) },
            WAIT_OBJECT_0
        );
        drop(tree); // Simulate shell crash: last Job handle closes, no PID/name kill.
        assert_eq!(
            unsafe { WaitForSingleObject(grandchild.0, 5000) },
            WAIT_OBJECT_0
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        while parent.0.try_wait().unwrap().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(parent.0.try_wait().unwrap().is_some());
        assert!(unrelated.0.try_wait().unwrap().is_none());
    }
}
