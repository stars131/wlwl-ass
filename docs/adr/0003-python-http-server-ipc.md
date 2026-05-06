# 0003 — Python HTTP server as IPC mechanism

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

The Tauri shell needs to talk to the existing Python codebase (project
manager, bot manager, API config / profile system, scheduler). Three options:

1. **Tauri commands + Python subprocess (stdio JSON-RPC).** Rust defines
   `#[tauri::command]` functions that spawn Python and exchange JSON over
   stdin/stdout.
2. **Python HTTP server.** Python runs an HTTP server bound to a free
   localhost port; React fetches it directly. Rust just spawns the server
   and injects its base URL into the webview.
3. **Tauri Sidecar — bundled Python interpreter.** The Tauri bundle ships a
   Python runtime; users don't install Python.

## Decision

**Option 2: Python HTTP server.** The new module `launcher/api_server.py`
is implemented on stdlib `http.server` (matches the existing
`launcher/shell_server.py` pattern; no new dependency). React calls
`fetch('/api/...')` against a base URL that Tauri's Rust shell injects via
`window.__GA_API_BASE__` before the React app boots.

## Consequences

- ✅ **Debuggable.** Curl, browser DevTools, REST clients all work; we can
  exercise endpoints without Tauri running. Helps both unit tests and manual
  diagnosis.
- ✅ **Reuses existing scaffold.** `launcher/shell_server.py` already speaks
  HTTP; we extend that pattern.
- ✅ **Cross-process scaling.** If we ever want multiple webviews / mobile
  remote access, they all hit the same HTTP backend.
- ✅ **Schema enforced both sides.** React validates with Zod; Python uses
  explicit JSON shapes — clear contract.
- ⚠️ **Port allocation.** Each launch picks a free port; we rely on the
  ready signal (`__GA_READY__ port=...`) to inform the Rust parent. Race
  windows are tight but exist.
- ⚠️ **Authentication.** Localhost-only binding is the default trust
  boundary; no token. Acceptable on desktop. If we open to LAN access in
  Phase 2+, we add a per-session token to the URL (Streamlit-style).
- ⚠️ **CORS noise.** We must set permissive CORS headers for the Tauri
  webview origin. Default config allows all `http://127.0.0.1:*` origins;
  CSP narrows in production builds.

## Alternatives considered

### Tauri commands + stdio JSON-RPC

- ✅ Lowest latency (no HTTP serialisation, no socket round-trip).
- ❌ Doubles the Rust code volume (every backend call needs a Rust glue
  function in addition to the Python side).
- ❌ Hard to test from outside Tauri.
- ❌ Streaming responses (SSE / chunked) are awkward over stdio.

### Tauri Sidecar (bundled Python)

- ✅ Best end-user experience — no `pip install` required.
- ❌ Ballooning bundle size (~100–200 MB for CPython + scientific stack).
- ❌ Complex to set up cross-platform; conflicts with users' existing
  Python that may have site-packages we want to reuse.
- ❌ License/redistribution concerns for some packages.
- We may revisit this for Phase 2 packaging if user feedback demands a
  truly install-free experience.

## Implementation notes

- Server lifecycle is owned by `gui/src-tauri/src/python_runtime.rs`. The
  Rust parent kills the child on app exit (and on Drop, defensively).
- The Rust shell waits up to 15 s for the `__GA_READY__` line before
  surfacing errors to the user.
- Phase 0 ships only `/api/health`, `/api/version`, `/api/openapi.json`.
  Phase 1 adds the business endpoints under `ROUTES["GET"]` / `["POST"]` /
  etc. in `launcher/api_server.py`.
