# 0004 — Feature-based folder structure

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

Two common organising principles for React apps:

1. **By technical role** — `components/`, `hooks/`, `services/`, `stores/`,
   `types/` at the top level.
2. **By business feature** — `features/sessions/`, `features/bots/`, etc.,
   each containing its own components / hooks / API / types / store.

We expect ~5 distinct business features in Phase 1 (sessions, bots,
api-configs, settings, per-session-api) and more later. The reference
project (`desktop-cc-gui`) ships ~43 features under the same convention.

## Decision

Adopt **feature-based** organisation.

```
src/
├── App.tsx                  # routes only
├── lib/                     # cross-cutting infrastructure (api client, env)
├── components/ui/           # shadcn/ui copy-paste (no business logic)
├── features/
│   └── <name>/
│       ├── index.ts         # public exports
│       ├── api/             # typed fetch wrappers + tests
│       ├── components/      # React components + tests
│       ├── hooks/           # useQuery / useMutation / Zustand hooks
│       ├── types.ts         # Zod schemas + inferred types
│       └── stores/          # Zustand stores when needed
├── styles/
└── test/
```

`App.tsx` only knows feature **routes**, not internals. A feature exposes a
single `<Page>` component plus type definitions; everything else stays
encapsulated.

## Consequences

- ✅ Cohesive change sets — adding/removing a feature touches one folder.
- ✅ Easy to grep `src/features/sessions/` and see everything sessions does.
- ✅ Encourages small, well-scoped features. If a folder grows past ~15
  files, split into sub-features.
- ⚠️ Requires discipline to avoid cross-feature imports (use `lib/` or
  `components/ui/` instead). ESLint `import/no-cycle` flags accidental
  coupling.
- ⚠️ Top-level `lib/` becomes a magnet for "doesn't fit anywhere else"
  utilities; we mitigate by reviewing additions to it strictly.

## Cross-feature dependencies

If feature A genuinely needs something from feature B's internals, that's a
sign the shared piece belongs in `lib/` or its own feature. The rule:
**features may not import from other features' subdirectories.** They may
only import from a sibling feature's `index.ts` (its public surface).

## Alternatives considered

### By technical role (components/hooks/services/...)

- ✅ Familiar from older React projects.
- ❌ Doesn't scale — adding a feature requires touching every folder.
- ❌ Hard to delete features cleanly.

### Hybrid (features/ + shared/)

- We essentially do this — `lib/` and `components/ui/` are the shared
  layers. We just call them by their roles, not "shared".

## Naming

- Folders: kebab-case (`api-configs/`, `per-session-api/`).
- React components: PascalCase (`SessionsPage`, `BotRow`).
- Hooks: `use<Subject>` (`useSessions`, `useBotStatus`).
- Stores: `<subject>Store` (`apiConfigsStore`).
- Tests: co-located, `*.test.ts(x)`; e2e in top-level `gui/e2e/`.
