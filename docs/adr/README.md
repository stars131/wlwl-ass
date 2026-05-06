# Architecture Decision Records

This directory captures the **why** behind significant technical choices in the
wlwl-ass GUI rebuild. Each ADR is small and immutable: when a decision is
revisited, write a new ADR that supersedes the old one (don't edit history).

## Index

| # | Title | Status |
|---|---|---|
| [0001](./0001-tauri-vs-electron.md) | Choose Tauri over Electron for the desktop shell | Accepted |
| [0002](./0002-react-typescript-strict.md) | React + TypeScript strict mode | Accepted |
| [0003](./0003-python-http-server-ipc.md) | Python HTTP server as IPC mechanism | Accepted |
| [0004](./0004-feature-based-folder-structure.md) | Feature-based folder structure for the React app | Accepted |
| [0005](./0005-shadcn-tailwind.md) | shadcn/ui + Tailwind for components | Accepted |
| [0006](./0006-per-session-api-config.md) | Per-session API config selection | Accepted |
| [0007](./0007-release-strategy.md) | Tauri release / signing / updater strategy | Accepted |
| [0008](./0008-kernel-api-delegation-and-forum.md) | Kernel API + Delegated Workers + Forum/Moderator | Proposed |
| [0009](./0009-worker-plugin-interface.md) | Worker Plugin Interface (智能体即插即拔) | Proposed |
| [0010](./0010-voice-conversation-loop.md) | Voice Conversation Loop (语音对话回路) | Proposed |

## Format

Each ADR follows a lightweight template:

- **Status** — Proposed / Accepted / Superseded by ADR-XXXX
- **Context** — what problem motivated this decision
- **Decision** — the choice we made
- **Consequences** — trade-offs we accept
- **Alternatives considered** — what we rejected and why

## When to write an ADR

- Adopting or replacing a major dependency (UI lib, state lib, build tool)
- Defining a cross-cutting convention (folder layout, naming, error handling)
- Introducing a system boundary (IPC protocol, schema versioning, auth model)
- Reversing a prior ADR

Bug fixes, individual features, and refactors that don't change conventions
do **not** need an ADR — write a PR description.
