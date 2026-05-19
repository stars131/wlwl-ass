# wlwl-ass Architecture Overview

This document orients new contributors. For the **why** behind specific
choices read the [ADRs](../adr/README.md). For day-to-day commands read
`gui/README.md` and the project root `README.md`.

## High-level picture

```
┌──────────────────────────────────────────────────────────────────┐
│                    wlwl-ass Desktop App                          │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────────┐         ┌────────────────────────────────┐  │
│  │  Tauri shell   │ spawn   │   Python launcher.api_server   │  │
│  │  (Rust)        │────────▶│   (stdlib http.server)         │  │
│  │                │ http://127.0.0.1:<port>                  │  │
│  │  ┌──────────┐  │  ◀───── │   ┌──────────────────────────┐ │  │
│  │  │ WebView  │  │   JSON  │   │ ProjectManager           │ │  │
│  │  │  React   │──┼─────────┼──▶│ BotManager               │ │  │
│  │  │   UI     │  │  fetch  │   │ ApiConfig+Profile        │ │  │
│  │  └──────────┘  │         │   │ launch_options (settings)│ │  │
│  └────────────────┘         │   └──────────────────────────┘ │  │
│                             │                                  │  │
│                             │   spawn bot subprocesses         │  │
│                             │      ┌─────────────────────┐     │  │
│                             └────▶ │ frontends/fsapp.py  │     │  │
│                                    │ frontends/tgapp.py  │     │  │
│                                    │ ...                 │     │  │
│                                    └─────────────────────┘     │  │
│                                              │                  │  │
│                                              ▼                  │  │
│                                    ┌─────────────────────┐     │  │
│                                    │ agentmain.py        │     │  │
│                                    │ wlwl_ass.py / agent_loop  │     │  │
│                                    │ llmcore             │     │  │
│                                    │ tools/* skills      │     │  │
│                                    └─────────────────────┘     │  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

## Process model

| Process | Role | Lifetime |
|---|---|---|
| **Tauri shell** | Rust process, hosts WebView + IPC | App lifetime |
| **api_server** | Python HTTP server, business logic facade | Spawned by Tauri; killed on app exit |
| **Bot subprocesses** | One per enabled bot (Telegram, Feishu, …) | Owned by BotManager |
| **L4 scheduler** | Background reflect loop | Spawned by api_server when settings.scheduler=true |

## Layer responsibilities

### 1. Tauri shell (`gui/src-tauri/`)

- Spawn `python -m launcher.api_server --port <free>`, wait for the
  `__GA_READY__` line.
- Inject `window.__GA_API_BASE__` into the WebView before React boots.
- Provide native menu bar / system tray / global shortcuts (Phase 2).
- Kill the Python child cleanly on app exit.

The Rust code is intentionally minimal — almost no business logic.

### 2. React UI (`gui/src/`)

- Fetches everything from the local Python API (`fetch('/api/...')`).
- Validates every response with [Zod](https://zod.dev/) schemas.
- Caches server state with [TanStack Query](https://tanstack.com/query).
- Local UI state lives in [Zustand](https://github.com/pmndrs/zustand)
  stores per feature.
- Components: shadcn/ui copy-paste in `components/ui/`, business components
  per feature in `features/<name>/components/`.

See [ADR 0004](../adr/0004-feature-based-folder-structure.md) for the
folder convention.

### 3. Python API server (`launcher/api_server.py`)

- Stdlib `http.server` — no Bottle/Flask runtime dependency.
- `ROUTES` dict maps `(method, path) → callable`. Each callable returns a
  JSON-serialisable dict.
- Phase 0 endpoints: `/api/health`, `/api/version`, `/api/openapi.json`.
- Phase 1+ wires in `ProjectManager`, `BotManager`, `api_config` (profiles),
  and `launch_config` (settings).

### 4. Existing Python business layer (`launcher/`, `frontends/`,
`agentmain.py`, `wlwl_ass.py`, `agent_loop.py`)

**Untouched by Phase 0.** The new GUI is a new caller of these modules; it
does not replace them. The CLI entry point `wlwl` (see `launcher/cli_repl.py`)
and `python agentmain.py` continue to work for headless / scripted use.

## Testing pyramid

```
                e2e (Playwright, Phase 2+)
              ╱
        integration (vitest with msw for HTTP mocks)
       ╱
unit (vitest for TS, pytest for Python, cargo test for Rust)
```

- Every TS module that touches IPC has a unit test that uses a mocked
  `fetch` (no live server).
- Every Python `api_server` route has a pytest test against
  `serve_threaded()`.
- Every Rust module exposing IPC behaviour has a `cargo test` (Phase 1+).
- One Playwright smoke test per feature route, asserting the page renders
  and the first user action works (Phase 2).

## Build & release

| Stage | Command | Output |
|---|---|---|
| Dev | `cd gui && npm run tauri:dev` | HMR React + live Python backend |
| Lint | `npm run lint && npm run typecheck` | CI gate |
| Test | `npm run test` (web) + `cargo test` + `pytest tests/` | CI gate |
| Bundle | `npm run tauri:build` | Per-OS installers (Phase 2) |

## Open questions tracked in ADRs

- [ADR 0006](../adr/0006-per-session-api-config.md) — per-session API
  config selection. **Proposed**; lands in Phase 1.

## Document hygiene

- This file describes **the system as designed**, not as currently built.
  When Phase 0 ships, "as built" matches "as designed" only for the
  scaffolding. Subsequent phases tick off the dotted boxes above.
- When a major architectural decision changes, write a new ADR; this file
  links to it; this file's diagrams are updated in the same PR.
