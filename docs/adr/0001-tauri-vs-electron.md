# 0001 — Choose Tauri over Electron for the desktop shell

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

wlwl-ass ships as a desktop application that orchestrates local Python
processes (sessions, bots, scheduler). The previous Qt launcher
(`launcher/qt_launcher.py`) was functional but limited UI ceiling and update
velocity. We need a desktop shell that:

- Renders modern web UI (HTML/CSS/JS) with hot-reload during development.
- Bundles to a single user-facing binary per platform.
- Exposes OS integration (file dialogs, system tray, native menus, secure
  IPC) for features like screenshot, clipboard, and credential storage.
- Plays nicely with the existing Python codebase (we don't want to rewrite
  `agentmain.py` / `agent_loop.py` in another language).

The two viable contenders are **Electron** (Node.js + bundled Chromium) and
**Tauri 2** (Rust + system WebView).

## Decision

Use **Tauri 2.x**.

## Consequences

- ✅ Single binary ~5–15 MB per platform (Electron: 100–200 MB).
- ✅ Lower memory footprint (uses OS WebView, no bundled Chromium).
- ✅ The repository owner already has the Rust toolchain installed; no new
  language ramp-up.
- ✅ Native menu bar / tray / file system / IPC are first-class via Tauri
  plugins.
- ✅ Capabilities + CSP model is more locked-down by default than Electron's
  contextIsolation toggles.
- ⚠️ Smaller ecosystem than Electron — niche plugins (e.g. specialised media
  decoders) may need to be written in Rust ourselves.
- ⚠️ WebView differs across platforms (WebView2 on Windows, WKWebView on
  macOS, WebKitGTK on Linux); rare CSS/JS feature drift requires testing on
  all three.
- ⚠️ Tauri 2 is recent (released 2024) and breaks API from Tauri 1; we accept
  the more frequent ecosystem churn.

## Alternatives considered

### Electron

- ✅ Largest ecosystem; battle-tested by VSCode, Slack, Discord.
- ❌ 100 MB+ binary just for "hello world".
- ❌ Bundled Chromium = bigger memory + bigger attack surface.
- ❌ Adds Node.js as a runtime requirement on top of Python.

### Native Qt (status quo)

- ✅ Already shipped; mature widget set.
- ❌ UI iteration slow (no HMR, no DevTools); component ecosystem small.
- ❌ Cannot reuse web frontend talent / code.
- This is what we're moving away from.

### Webview-only (current `launch.pyw --legacy-shell`)

- ✅ Zero new dependencies (PyWebView already used).
- ❌ No native menus / tray; window chrome inconsistent across OSes.
- ❌ Cannot bundle as a single binary for distribution; users still need
  Python visible.
- ❌ Forces backend to serve the HTML, mixing concerns.

## Notes

- We deliberately defer the **packaging/signing** decisions (Apple notarisation,
  Windows code signing) to Phase 2. Phase 0 only needs the shell to boot
  locally during development.
- A future ADR may revisit this if Tauri 2.x ecosystem proves too thin — the
  exit ramp is "rebuild the React UI under Electron" since the React layer is
  framework-agnostic.
