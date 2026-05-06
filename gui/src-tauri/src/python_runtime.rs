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
use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const READY_MARKER: &str = "__GA_READY__";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(15);

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
            .current_dir(project_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("failed to spawn python launcher.api_server")?;

        wait_for_ready(&mut child)?;

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
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for PythonRuntime {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn find_free_port() -> anyhow::Result<u16> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

fn which_python() -> anyhow::Result<PathBuf> {
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
    let stdout = child.stdout.take().context("python child has no stdout")?;
    let started = Instant::now();
    let reader = BufReader::new(stdout);
    for line in reader.lines() {
        let line = line.context("reading python stdout")?;
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

/// Walk up from the current exe location until we find a directory with both
/// `launcher/api_server.py` and `agentmain.py`. Falls back to CWD in dev.
pub fn resolve_project_root() -> anyhow::Result<PathBuf> {
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
