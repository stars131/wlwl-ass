# 0002 — React + TypeScript strict mode

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

We need a UI framework + language combination for the new GUI that:

- Has the largest pool of contributors and prior art (so the project can
  attract help / reuse components).
- Encodes invariants in the type system to prevent the kinds of regressions
  that have hit the Python frontends (e.g. shape-drift between
  `projects.json` schema and Streamlit consumers).
- Plays well with the chosen build tooling (Vite) and the chosen UI library
  (shadcn/ui copy-paste components).

## Decision

- **React 19** as the UI framework.
- **TypeScript ~5.7** with `tsconfig.json` set to `strict: true` plus extra
  flags listed below.
- Lint configuration forbids `any` (`@typescript-eslint/no-explicit-any:
  error`) and unfloated promises (`no-floating-promises: error`).

### Strict TS config (canonical)

```jsonc
{
  "strict": true,
  "noImplicitAny": true,
  "strictNullChecks": true,
  "noImplicitReturns": true,
  "noFallthroughCasesInSwitch": true,
  "noUncheckedIndexedAccess": true,
  "noUnusedLocals": true,
  "noUnusedParameters": true,
  "exactOptionalPropertyTypes": true,
  "noImplicitOverride": true
}
```

## Consequences

- ✅ Type-driven refactors — schema changes show up as compile errors instead
  of silent runtime issues.
- ✅ Tooling (autocomplete, jump-to-definition, rename) is best-in-class.
- ✅ Onboarding is well-trod; almost any frontend developer can contribute.
- ⚠️ `noUncheckedIndexedAccess` makes array/object access more verbose
  (`arr[i]` is `T | undefined`). We accept the verbosity for the catch.
- ⚠️ `exactOptionalPropertyTypes` rejects `{ x: undefined }` for `x?: string`
  — must omit the property. Harder to write, easier to reason about.

## Alternatives considered

### Vue 3 + TypeScript

- ✅ Solid TS support, single-file components are ergonomic.
- ❌ Smaller component ecosystem than React; shadcn/ui is React-only.
- ❌ Reference project (desktop-cc-gui) uses React, so we lose easy
  cross-pollination.

### Svelte / SolidJS

- ✅ Compiler-driven, smaller runtime, excellent DX.
- ❌ Smaller talent pool; ecosystem still maturing for desktop-shell apps.
- ❌ Component libraries less mature than React's.

### JavaScript (no TS)

- Rejected outright. We are explicitly building "by enterprise standards" —
  static types are non-negotiable for any module crossing IPC boundaries.

## Enforcement

- `npm run typecheck` (CI) runs `tsc -b --noEmit` and must be zero errors.
- `npm run lint` runs ESLint with the rules above and `--max-warnings=0`.
- Pre-commit / CI rejects PRs that fail either check.
