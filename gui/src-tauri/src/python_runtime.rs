//! Lifecycle of the embedded Python `launcher.api_server` child process.
//!
//! On startup we pick a free localhost port and spawn `python -m
//! launcher.api_server --port <P>`. The child writes `__GA_READY__\n` to stdout
//! once Bottle is listening; we wait up to a few seconds for that signal before
//! letting Tauri open its webview.
//!
//! On shutdown we send the child a graceful SIGTERM (or kill on Windows) so we
//! don't leave orphan listeners on developer machines.

use anyhow::{anyhow, bail, Context};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const READY_MARKER: &str = "__GA_READY__";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

pub struct PythonRuntime {
    child: Option<Child>,
    base: String,
}

impl PythonRuntime {
    pub fn start(project_root: &Path) -> anyhow::Result<Self> {
        let port = find_free_port().context("no free localhost port for api_server")?;
        let python = which_python().context("could not find a `python` interpreter on PATH")?;
        log::info!("spawning {} -m launcher.api_server --port {port}", python.display());

        let mut child = Command::new(python)
            .arg("-u")
            .arg("-m")
            .arg("launcher.api_server")
            .arg("--port")
            .arg(port.to_string())
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .current_dir(project_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("failed to spawn python launcher.api_server")?;

        wait_for_ready(&mut child)?;
        drain_pipe(child.stdout.take(), "api_server stdout");
        drain_pipe(child.stderr.take(), "api_server stderr");

        Ok(Self {
            child: Some(child),
            base: format!("http://127.0.0.1:{port}"),
        })
    }

    pub fn base_url(&self) -> String {
        self.base.clone()
    }

    pub fn shutdown(mut self) {
        if let Some(mut child) = self.child.take() {
            shutdown_child(&mut child, &self.base);
        }
    }
}

impl Drop for PythonRuntime {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            shutdown_child(&mut child, &self.base);
        }
    }
}

fn shutdown_child(child: &mut Child, base_url: &str) {
    let _ = request_api_shutdown(base_url);
    let deadline = Instant::now() + SHUTDOWN_TIMEOUT;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(_) => break,
        }
    }
    kill_process_tree(child.id());
    let _ = child.kill();
    let _ = child.wait();
}

fn request_api_shutdown(base_url: &str) -> std::io::Result<()> {
    let Some(port_text) = base_url.rsplit(':').next() else {
        return Ok(());
    };
    let Ok(port) = port_text.parse::<u16>() else {
        return Ok(());
    };
    let mut stream = std::net::TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(700),
    )?;
    stream.set_read_timeout(Some(Duration::from_millis(700)))?;
    let token = std::env::var("WLWL_API_AUTH_TOKEN").unwrap_or_default();
    let auth = if token.trim().is_empty() {
        String::new()
    } else {
        format!("Authorization: Bearer {}\r\n", token.trim())
    };
    let request = format!(
        "POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}Content-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;
    let mut sink = [0_u8; 512];
    let _ = stream.read(&mut sink);
    Ok(())
}

fn kill_process_tree(pid: u32) {
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
    }
}

fn find_free_port() -> anyhow::Result<u16> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

fn which_python() -> anyhow::Result<PathBuf> {
    if let Ok(path) = std::env::var("WLWL_PYTHON") {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Ok(path);
        }
        bail!("WLWL_PYTHON is set but does not point to a file: {}", path.display());
    }

    let candidates = if cfg!(windows) {
        vec!["py", "python", "python3"]
    } else {
        vec!["python3", "python"]
    };
    for name in candidates {
        if let Ok(path) = which::which(name) {
            return Ok(path);
        }
    }
    bail!("no python interpreter found on PATH")
}

fn wait_for_ready(child: &mut Child) -> anyhow::Result<()> {
    let stdout = child
        .stdout
        .as_mut()
        .context("python child has no stdout")?;
    let started = Instant::now();
    let mut reader = BufReader::new(stdout);
    let mut buf = Vec::new();
    loop {
        buf.clear();
        let n = reader
            .read_until(b'\n', &mut buf)
            .context("reading python stdout")?;
        if n == 0 {
            break;
        }
        let line = String::from_utf8_lossy(&buf);
        log::debug!("[api_server] {line}");
        if line.contains(READY_MARKER) {
            return Ok(());
        }
        if started.elapsed() >= STARTUP_TIMEOUT {
            break;
        }
    }
    Err(anyhow!("python api_server did not signal ready within {STARTUP_TIMEOUT:?}"))
}

fn drain_pipe(pipe: Option<impl Read + Send + 'static>, label: &'static str) {
    if let Some(pipe) = pipe {
        std::thread::spawn(move || {
            let mut reader = BufReader::new(pipe);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf) {
                    Ok(0) => break,
                    Ok(_) => log::debug!("[{label}] {}", String::from_utf8_lossy(&buf).trim_end()),
                    Err(_) => break,
                }
            }
        });
    }
}

/// Walk up from the current exe location until we find a directory with both
/// `launcher/api_server.py` and `agentmain.py`. Falls back to CWD in dev.
pub fn resolve_project_root() -> anyhow::Result<PathBuf> {
    if let Ok(root) = std::env::var("WLWL_PROJECT_ROOT") {
        let root = PathBuf::from(root);
        if root.join("launcher").join("api_server.py").exists() {
            return Ok(root);
        }
        bail!(
            "WLWL_PROJECT_ROOT is set but does not contain launcher/api_server.py: {}",
            root.display()
        );
    }

    if let Ok(cwd) = std::env::current_dir() {
        if cwd.join("launcher").join("api_server.py").exists() {
            return Ok(cwd);
        }
        if let Some(parent) = cwd.parent() {
            if parent.join("launcher").join("api_server.py").exists() {
                return Ok(parent.to_path_buf());
            }
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut cursor = exe.parent().map(Path::to_path_buf);
        while let Some(dir) = cursor {
            if dir.join("launcher").join("api_server.py").exists() {
                return Ok(dir);
            }
            cursor = dir.parent().map(Path::to_path_buf);
        }
    }
    bail!("could not locate launcher/api_server.py — set WLWL_PROJECT_ROOT?")
}
