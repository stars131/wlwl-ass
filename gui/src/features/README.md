# Features

Each business area lives in its own folder under `src/features/<name>/`.
Phase 0 is empty — the structure is documented here so subsequent phases
have a uniform shape.

## Convention

```
src/features/<name>/
├── index.ts                # Public exports (components, hooks, types)
├── api/                    # Typed fetch wrappers (calls into ../../lib/api.ts)
│   └── <name>Api.ts
│   └── <name>Api.test.ts
├── components/             # React components (default export = container)
│   ├── <Name>Page.tsx
│   ├── <Name>Page.test.tsx
│   └── <SubComponent>.tsx
├── hooks/                  # useQuery/useMutation/useStore hooks
│   ├── use<Name>.ts
│   └── use<Name>.test.tsx
├── types.ts                # Zod schemas + inferred TS types
└── stores/                 # Zustand stores when local state needed
    └── <name>Store.ts
```

## Planned Phase 1 features

| name | scope |
|---|---|
| `sessions` | Multi-project list, start/stop/open Streamlit, per-session API picker |
| `bots` | 6-bot status table (Telegram/QQ/Feishu/WeCom/DingTalk/WeChat) + lifecycle |
| `api-configs` | Cred CRUD + Profile (cc-switch style) |
| `settings` | Global launch options (default LLM, permission mode, scheduler, etc.) |

Each new feature must:

1. Add Zod schemas in `types.ts` for every API payload it consumes.
2. Cover the API wrapper with vitest unit tests using `msw` or `vi.fn()`.
3. Pass `npm run lint` (zero warnings) and `npm run typecheck`.
4. Get a smoke test in `e2e/<name>.spec.ts`.
5. Document any non-obvious decision as a new ADR under `docs/adr/`.

## Naming

- Files: kebab-case (`api-configs/`, `<name>-page.tsx`).
- React components: PascalCase (`ApiConfigsPage`).
- Hooks: camelCase prefixed `use` (`useApiConfigs`).
- Stores: camelCase suffixed `Store` (`apiConfigsStore`).
