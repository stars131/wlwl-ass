# 0012 — Multi-Bot Process Isolation

- **Status:** Accepted (implementation landed alongside this ADR, 2026-05-17 / 2026-05-18)
- **Date:** 2026-05-18
- **Builds on:** [ADR-0011 Feishu Concierge Bot](./0011-feishu-concierge-bot.md), [ADR-0008 Kernel + Workers](./0008-kernel-api-delegation-and-forum.md)

---

## 0. TL;DR

The repo now hosts two cooperating Feishu bots (owner: `fsapp.py`; concierge:
`fsapp_concierge.py`) plus five additional IM-bot frontends. They share process
spawn / lifecycle code in `launcher.bot_manager`, share `temp/`, and (until
this ADR) shared a `process_registry.json` that any one bot's agent could read
and write. The 2026-05-17 turn-23 incident — owner agent ran
`process kill bot:feishu` while it WAS `bot:feishu`, then auto-restart failed
on an unrelated `tokenjuice.py` SyntaxError — exposed three thin spots:

1. The agent didn't know which bot it was running inside, so guards like
   "don't kill yourself" had nothing to compare against.
2. Storage paths weren't namespaced per bot, so an agent could (and did)
   reach into another bot's runtime state by accident.
3. Bot startup failures were invisible until someone tailed a log file —
   `BotManager.start()` returned `(True, "已启动")` for a process that died
   200 ms after spawn.

This ADR formalises the boundary: **per-bot identity, per-bot owned paths,
fail-loud crash detection, and self-repair preflight.** The implementation
is incremental — no breaking changes to existing bots — and is fully landed.

---

## 1. Context

### 1.1 What blew up

The 2026-05-17 incident timeline (from `temp/fsapp.log`):

| Turn | Action | Effect |
|---|---|---|
| 1–22 | Owner agent investigates "how to enable concierge LLM smalltalk" | Reads ~12 files, runs ~8 `code_run` commands. Normal exploration. |
| 23 | Decides the right fix is "restart `bot:feishu` so it picks up new config" → calls `process(action='kill', label='bot:feishu')` | Kills the **owner bot** — the very process the agent is running inside. |
| (restart attempt) | `BotManager` respawns `fsapp.py` | Import chain hits `tokenjuice.py:73` `SyntaxError: positional argument follows keyword argument`. Process dies. |
| 24 | User sees "turn 24 · LLM Running" in Feishu and then silence | No mechanism surfaces the crash. User waits, then asks. |

The agent's reasoning was reasonable in isolation: it WAS true that the bot
needed a restart to pick up config changes. The boundary failure was that the
agent could execute that restart from inside the very thing being restarted.

### 1.2 What was already isolated

`fsapp.py` and `fsapp_concierge.py` run in **separate OS processes** with
separate kernel singletons, separate Feishu app credentials, and separate
single-instance lock ports (19532 / 19533). At the process boundary,
isolation is real and unchanged.

### 1.3 What was NOT isolated

- **mykeys namespace** — both bots' credentials live in one flat `mykeys`
  dict with prefix discipline (`fs_*` vs `fs_concierge_*`). A typo or wrong
  prefix lookup yields the other bot's data with no type error.
- **`temp/` filesystem** — `process_registry.json`, `concierge_audit.jsonl`,
  `concierge_state/`, `calendar.db` all live side by side. Any agent with
  `file_write` / `file_patch` / `code_run` can edit any of them.
- **agent self-identity** — `WlwlAssHandler` had no way to know "I am
  running inside `bot:feishu`" because the bot spawn didn't pass that
  identity through. Guards like "refuse self-kill" couldn't be written.
- **bot-startup observability** — `BotManager.start()` reported success
  whenever `subprocess.Popen` returned a PID. A process that crashed
  during init wasn't surfaced to the GUI / CLI until much later.

---

## 2. Decision

Five thin layers, each independently shippable. Order matters: 1 → 5 stacks.

### 2.1 Bot self-identity via `WLWL_BOT_KEY`

`BotManager.start(key)` injects `WLWL_BOT_KEY=<key>` into the child process'
environment. Frontends and agents read it to:

- gate self-destructive actions (`do_process kill`)
- gate cross-bot writes (`do_file_write` / `do_file_patch`)
- self-report in logs

This is **not** a security boundary — the env var can be forged. It's a
sanity-check layer that catches the agent-confusion failure mode without
adding ceremony.

### 2.2 Self-kill guard (`do_process`)

`WlwlAssHandler.do_process(action='kill', ...)` refuses two patterns:

- `target == os.getpid()` — explicit self-pid kill
- `target == f"bot:{WLWL_BOT_KEY}"` — label kill targeting this bot

Both return `StepOutcome(error="self_kill_refused")` with a system prompt
that says "you can't kill yourself; ask the user to use the launcher /
GUI." Override is intentional: there is no env-var override, because no
plausible legitimate use case for an agent to kill its own bot exists.

### 2.3 Cross-bot path-write fence (`do_file_write`, `do_file_patch`)

`WlwlAssHandler._check_cross_bot_write(abs_path)` returns a non-empty
refusal reason when:

- `WLWL_BOT_KEY` is set (i.e. we're inside a bot process)
- `WLWL_CROSS_BOT_WRITE` is **not** set (no explicit override)
- `abs_path` falls under `_BOT_OWNED_PATHS[other_bot]`

The registry is module-level in `wlwl_ass.py`:

```python
_BOT_OWNED_PATHS = {
    "feishu_concierge": [
        "temp/concierge_state",
        "temp/concierge_audit.jsonl",
        "temp/concierge_audit.jsonl.id_map.json",
        "temp/concierge_escalations.jsonl",
        "temp/concierge_kb.jsonl",
    ],
    "feishu": [],   # owner bot has no on-disk runtime state
}
```

New bots add their state paths here when they ship. Generic project files
(source code, docs, top-level config) are intentionally NOT in this list —
the fence is for *runtime state*, not for code edits.

Override: `WLWL_CROSS_BOT_WRITE=1` for migration tools that explicitly need
to cross the line. None ship by default.

### 2.4 Bot crash early-detection

`BotManager.start()` now polls the child process for `EARLY_DEATH_S` (2 s
default) after spawn. If it exits within that window, the return becomes
`(False, "<diagnostic>")` instead of `(True, "已启动")`. The diagnostic
includes:

- exit code + actual runtime
- last `LOG_TAIL_BYTES` of the log file (4 KB default)
- a heuristic classification: `SyntaxError → 检查 git diff or preflight`,
  `ModuleNotFoundError → check pip list`, `Address already in use →
  port still held`, etc.

This makes the 2026-05-17 incident class self-diagnosing. A user clicking
"启动" in the GUI Bots tab sees:

```
启动后立刻退出 (pid=58752, rc=1, 运行 <2.0s) — Python SyntaxError
--- 最近日志 (temp/fsapp.log) ---
  File "tokenjuice.py", line 73
    ),
    ^
SyntaxError: positional argument follows keyword argument
```

instead of "已启动 (pid=58752)" followed by silence.

### 2.5 Preflight self-check

`python -m launcher.preflight` runs five check groups, each catching one
class of breakage that previously surfaced only at bot-run time:

| Group | Catches |
|---|---|
| `imports` | SyntaxError / ImportError in any frontend / worker / agent module (the 2026-05-17 incident class) |
| `frontends` | `ast.parse` failures in `frontends/*.py` (without executing them) |
| `tokenjuice` | `Rule()` validation failures (length mismatch / bad regex) |
| `workers` | Each concierge worker factory builds cleanly |
| `pytest` | `pytest --collect-only` succeeds |

GUI integration is a follow-up; for now the CLI is the contract. Exit code
0 = ship-ready, 1 = at least one check failed.

### 2.6 LLM hook circuit breaker (concierge only)

Orthogonal to bot isolation but lands together for self-repair completeness.
Each of the 5 concierge LLM hooks gets a `_CircuitState`:

- 3 consecutive failures → OPEN for 60 s
- During OPEN, the hook short-circuits to `None` immediately (no LLM
  round-trip), and the agent falls back to deterministic rules
- After 60 s, one half-open trial is allowed; success closes the breaker,
  failure re-opens for another 60 s

This means a friend chatting with the bot during an LLM endpoint outage
sees rule-based replies with ~5 ms latency, not 30-60 s timeouts before
each fallback.

---

## 3. Consequences

### 3.1 Positive

- The exact 2026-05-17 incident class can't recur. Self-kill guard +
  preflight + crash detection would have surfaced it in <2 s instead of
  10 minutes of confused user.
- Adding a third / fourth IM-bot frontend now has a documented path:
  add a `_BOT_OWNED_PATHS[<key>]` entry, ship.
- Operational visibility goes up: GUI Bots tab can now report "crashed
  during init" with the actual error.
- LLM endpoint outages no longer block friend-bot replies.

### 3.2 Negative / tradeoffs

- `_BOT_OWNED_PATHS` is a hand-maintained registry. A new bot that ships
  state files but forgets to register them won't get the fence (silent
  failure mode). Documented in the file but not enforceable.
- Crash early-detection adds ~2 s to every bot start in the happy path.
  Acceptable: bots restart at most a few times per day.
- The fence overrides via env var are friction-by-design — anyone who
  needs to cross-edit (migration tools, repairs) has to consciously
  opt in. We accept this; it's a once-per-year operation.

### 3.3 Out of scope

- `temp/bots/<key>/` directory restructuring. Was originally Phase B in
  the discussion that led to this ADR. Deferred because: (a) the path
  fence already prevents the failure mode, (b) the restructure would
  break manual file inspection workflows users have memorised, (c) the
  capability-token mechanism in ADR-0008/0009 is the right long-term
  isolation primitive once we have a second non-Feishu bot needing it.
- Cross-bot capability tokens. The concierge bot already uses the owner
  bot's Feishu app credentials directly (for the escalate worker). The
  token-mediated version is a Phase 3.5 item, out of scope here.

---

## 4. Implementation status (2026-05-18)

| Item | Status | Files |
|---|---|---|
| `WLWL_BOT_KEY` env injection | landed | `launcher/bot_manager.py:start` |
| Self-kill guard | landed + 4 tests | `wlwl_ass.py:do_process`, `tests/test_process_self_kill_guard.py` |
| Cross-bot path-write fence | landed + 5 tests | `wlwl_ass.py:_check_cross_bot_write`, `_BOT_OWNED_PATHS` |
| Bot crash early-detection | landed + 9 tests | `launcher/bot_manager.py:_poll_early_death`, `tests/test_bot_crash_detection.py` |
| Preflight CLI | landed + 7 tests | `launcher/preflight.py`, `tests/test_preflight.py` |
| Tokenjuice Rule validator | landed + 6 tests | `tokenjuice.py:Rule.__post_init__`, `tests/test_tokenjuice_rules.py` |
| Storage resilience | landed + 9 tests | `llmcore/concierge_agent.py:SessionStore`, `tests/test_storage_resilience.py` |
| LLM hook circuit breaker | landed + 5 tests | `llmcore/concierge_agent.py:_CircuitState`, `tests/test_llm_circuit_breaker.py` |
| LLM JSON parser robustness | landed + 17 tests | `frontends/fsapp_concierge.py:_parse_llm_json`, `tests/test_llm_json_parser.py` |
| Concierge LLM live self-test CLI | landed + 8 tests | `launcher/cli_concierge_llm_test.py`, `tests/test_cli_concierge_llm_test.py` |

Total new tests landing with this ADR: 70+. Existing suite (375 pre-ADR) still
green.

---

## 5. How to apply

When adding a new IM-bot frontend:

1. Add a `BotSpec` row in `launcher/bot_manager.py:BOT_SPECS` with a unique
   `lock_port` and `log_filename`.
2. If the bot has on-disk runtime state files, add them to `_BOT_OWNED_PATHS`
   in `wlwl_ass.py` under the bot's key.
3. The frontend script auto-receives `WLWL_BOT_KEY=<key>` in env; the
   bot's agent (if it embeds `WlwlAssHandler`) gets the self-kill guard
   and path fence for free.
4. Run `python -m launcher.preflight` before deploying.

When investigating a "bot started but isn't responding" complaint:

1. `python -m launcher.preflight` — catches import-time breakage.
2. Tail `temp/<botkey>app.log` for the last spawn block.
3. For concierge: `python -m launcher.cli_concierge_llm_test` to verify
   LLM hooks are working independent of Feishu I/O.

When the user reports "the agent did something destructive":

1. Check if the destruction is in `_BOT_OWNED_PATHS` for another bot. If
   so, this should have been blocked.
2. Check if `WLWL_CROSS_BOT_WRITE` was set in the environment.
3. If neither: file an issue. The fence has a gap.
