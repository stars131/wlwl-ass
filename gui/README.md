# wlwl-ass GUI

Tauri 2 + React 19 + TypeScript desktop client for wlwl-ass.

> **Status: Phase 0 — scaffolding only.** Business features (sessions / bots /
> API configs / settings / per-session API selection) are NOT implemented yet.
> The current Qt launcher in `launcher/qt_launcher.py` remains the day-to-day
> entry point until the Web UI reaches feature parity (Phase 1+).

## Stack

| Layer | Choice | Why |
|---|---|---|
| Desktop shell | **Tauri 2.x** | Single ~5 MB binary, system WebView |
| UI framework | **React 19 + TypeScript strict** | Largest ecosystem, type safety |
| Build / HMR | **Vite 6** | Tauri-native dev experience |
| Styling | **Tailwind 4 + shadcn/ui** | Copy-paste components, no lock-in |
| Server state | **TanStack Query v5** | Caching, SWR, mutations |
| Local state | **Zustand 5** | Minimal, hookable |
| Validation | **Zod** | Runtime + compile-time types |
| Tests | **Vitest + Testing Library + Playwright** | Standard JS testing pyramid |
| IPC | **Python HTTP server** ↔ webview | See ADR 0003 |

## Quick Start

Pre-requisites: Node ≥ 20.9, Rust stable (any version supporting Tauri 2),
Python ≥ 3.10 in repo root for the backend.

```bash
# From this directory:
npm install
npm run typecheck    # TS strict mode
npm run lint         # ESLint flat config
npm run test         # Vitest unit
npm run tauri:dev    # Launch the desktop app
```

The Tauri wrapper will spawn the Python `launcher.api_server` on a free local
port and open the React UI in a webview.

## Project Layout

```
gui/
├── src/                         # React app
│   ├── main.tsx                 # ReactDOM root
│   ├── App.tsx                  # Top-level shell + routing
│   ├── lib/
│   │   ├── api.ts               # Typed HTTP client (zod-validated)
│   │   ├── api.test.ts
│   │   └── env.ts               # Resolves backend URL from window/env
│   ├── features/                # Feature-based folders (one dir per business area)
│   │   └── README.md            # Convention: features/<name>/{components,hooks,api,types,*.test.tsx}
│   ├── components/ui/           # shadcn/ui copy-paste sink
│   ├── styles/globals.css       # Tailwind base + CSS variables
│   └── test/setup.ts            # Vitest global setup
├── src-tauri/                   # Rust shell
│   ├── src/main.rs              # Spawn Python server + open window
│   ├── src/python_runtime.rs    # Lifecycle of the Python child process
│   ├── tauri.conf.json          # Tauri config
│   └── Cargo.toml
├── e2e/                         # Playwright suites (none yet)
├── package.json                 # npm scripts + deps
├── tsconfig.json                # Strict TS
├── vite.config.ts
├── tailwind.config.ts
├── eslint.config.js             # Flat config, type-aware
└── playwright.config.ts
```

## Development Workflow

1. Make a feature branch: `git checkout -b feat/<feature>`
2. Write code under `src/features/<feature>/`
3. Validate: `npm run lint && npm run typecheck && npm run test`
4. Commit using Conventional Commits (commitlint enforces this).
5. Push; CI runs lint / typecheck / tests on web + rust + python sides.

## Standards Reference

- ADRs live under `../docs/adr/` — read these before adding new infra.
- Architectural overview: `../docs/architecture/overview.md`.
- Per-session API selection design: `../docs/adr/0006-per-session-api-config.md`.

## What Phase 0 Delivers

- Tauri shell that boots and opens an empty React page
- React app that fetches `/api/health` from the Python server and shows status
- Strict TS, ESLint, Prettier, Vitest, Playwright config
- ADRs documenting every architectural choice
- CI workflows for web + rust + python

## What Phase 0 Does NOT Deliver

- Actual UI for sessions / bots / configs / settings (use the Qt launcher meanwhile)
- Packaged binary or auto-updater
- Mobile responsiveness, i18n framework, dark-mode toggle
