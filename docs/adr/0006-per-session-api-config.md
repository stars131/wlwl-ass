# 0006 — Per-session API config selection

- **Status:** Proposed (design predicate; implementation lands in Phase 1)
- **Date:** 2026-05-02
- **Update (2026-05):** mykey.py / mykey_local_override.py / profile mechanism
  retired. Configs now live in `temp/launcher_api_configs.json` (full library;
  all entries active simultaneously) plus `~/.wlwl-ass/config.json` (single-provider
  defaults, bot tokens, settings). The per-session **`config_name`** selection
  described below works unchanged — the selector still resolves by name from
  the merged credential view returned by `llmcore.reload_mykeys()`.

## Context

wlwl-ass lets users configure multiple LLM endpoints — historically
through `mykey.py` + `mykey_local_override.py`, now through
`temp/launcher_api_configs.json` (Qt launcher) and `~/.wlwl-ass/config.json`
(launcher.config_store). Originally:

- **At launcher level**: a "Profile" (cc-switch style) selects which configs
  are active for the whole process.  *(Retired — see Update note above.)*
- **At session level**: each project carries an integer `llm_no` saved in
  `temp/projects.json` and passed to its Streamlit subprocess via
  `WLWL_LLM_NO`. The agent inside picks `self.llmclients[llm_no]` at startup.

This is fragile:

1. `llm_no` is an index into a list whose order depends on config scan
   order. Same number means different things in different sessions.
2. Renaming a config or adding one above shifts every existing session's
   LLM choice silently.
3. The user has no first-class UI to say "session X uses `gpt-native`,
   session Y uses `claude-relay-1`" — they have to remember the index.

The reference project (`desktop-cc-gui`, see `src/features/threads/` and
`src/features/models/`) addresses this by associating each thread with a
**provider id + model id** rather than an integer index.

## Decision (proposed)

Add a new field to each session record:

```ts
// In temp/projects.json
{
  "id": "p_xxx",
  "name": "demo",
  "llm_config_name": "gpt-native",   // NEW — references the `name` field of a config in mykey
  "llm_no": 0,                        // KEPT for back-compat (deprecated)
  ...
}
```

When the agent subprocess starts, it receives `WLWL_LLM_CONFIG_NAME=<name>`
(in addition to the legacy `WLWL_LLM_NO`). On startup the agent:

1. If `WLWL_LLM_CONFIG_NAME` is set and matches a loaded client by `name`,
   select it.
2. Else fall back to `WLWL_LLM_NO`.
3. Else default to `0`.

The UI (Phase 1, Sessions tab):

- Each session card has a dropdown listing config names available in the
  active Profile (or all configs if no Profile is active).
- Changing the dropdown:
  - Calls `PUT /api/projects/<id>/llm` with `{ "config_name": <name> }`.
  - The backend writes the new value to `projects.json`. If the session is
    running, the user is offered a "restart to apply" affordance — the
    agent reads env vars at startup, so live switching means restarting the
    subprocess.

## Consequences

- ✅ Stable references — renaming/reordering configs no longer silently
  changes session behavior.
- ✅ UI obviousness — users see their LLM choice as a name, not a number.
- ✅ Per-session **and** per-Profile composition: one session can use a
  config from Profile A while another uses Profile B (we just pin by name).
- ⚠️ Must handle the case where the named config no longer exists (was
  deleted). UI shows a warning, agent falls back to first available client.
- ⚠️ Migration: existing `projects.json` entries lack `llm_config_name`.
  The first time the new code reads a project, it backfills based on
  `llm_no` if possible.

## Implementation contract (Phase 1)

### Backend

- `launcher/project_manager.py`: store and surface `llm_config_name` in
  `_normalize_project()` and `update_options()`.
- `launcher/api_server.py`: new endpoint
  `PUT /api/projects/{id}/llm  body: {"config_name": str}`.
- `launcher/project_manager.py::_spawn`: pass
  `env["WLWL_LLM_CONFIG_NAME"] = project.get("llm_config_name") or ""`.
- `agentmain.py`: `next_llm` accepts a name as an alternative to an index;
  startup logic prefers `WLWL_LLM_CONFIG_NAME` over `WLWL_LLM_NO`.

### Frontend

- `features/sessions/components/SessionApiPicker.tsx`:
  `<Select>` listing `useApiConfigsForActiveProfile()` results.
- `features/sessions/api/sessionsApi.ts::setSessionLlm(id, configName)`.
- Test plan: `SessionApiPicker.test.tsx` covers loading state, mutation,
  error toast on missing config.

## Alternatives considered

### Keep `llm_no` only

- Status quo. Already painful; explicitly the reason we're writing this
  ADR.

### Drop `llm_no` immediately

- Cleaner schema but breaks any external script reading `projects.json`.
  We deprecate gradually instead.

### Profile-level only (no per-session)

- Less flexible — users can't run two simultaneous sessions on different
  models from the same Profile. Reject.

## Status notes

This ADR is **Proposed**, not Accepted, until the Phase 1 implementation
goes in. We document it now so the data model is decided up-front and the
Phase 0 scaffolding (Zod schemas in `src/lib/api.ts`, Python schemas in
`launcher/api_server.py`) anticipates these fields rather than retrofitting.
