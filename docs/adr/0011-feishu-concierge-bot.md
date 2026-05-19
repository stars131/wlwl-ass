# 0011 — Feishu Concierge Bot (朋友代聊 / 日程协商机器人)

- **Status:** Proposed (design-only ADR; no implementation lands with this PR)
- **Date:** 2026-05-17
- **Builds on:** [ADR-0008 Kernel + Workers + Forum](./0008-kernel-api-delegation-and-forum.md), [ADR-0009 Worker Plugin Interface](./0009-worker-plugin-interface.md), [ADR-0010 Voice Conversation Loop](./0010-voice-conversation-loop.md)
- **Companion spec:** [`docs/specs/feishu-concierge-bot.md`](../specs/feishu-concierge-bot.md)

---

## 0. TL;DR

We add a **second Feishu application** with a separate `app_id` / `app_secret`,
hosted by a new long-running process `frontends/fsapp_concierge.py`. It runs a
**privilege-restricted agent** (the "Concierge") that can: (a) negotiate meeting
slots against the owner's calendar, (b) answer a curated allowlist of questions
about the owner from a small RAG knowledge base, and (c) escalate anything
ambiguous to the owner via an interactive Feishu card sent through the **existing
owner** bot. The concierge agent has **no access to `code_run`, `file_write`,
`web_execute_js`, `start_long_term_update`, or any owner-private memory**.

The decision is to **split the trust boundary at the Feishu app level**, not at
the persona level within one app, because (i) Feishu's identity model binds one
bot to one app, (ii) per-`open_id` system-prompt switching is not a security
boundary against a jailbreaking user, and (iii) splitting at the app level lets
the kernel issue distinct capability tokens to the two processes (ADR-0008's
super-key / delegated-token model only works if there are actually two callers).

---

## 1. Context

### 1.1 What the codebase does today

`frontends/fsapp.py` is the canonical Feishu loop:

- Spawns one `GeneraticAgent` per inbound `open_id` and pools them.
- The agent has the full 9-atomic-tool set, including `code_run` and `file_write`.
- `bots.feishu.allowed_users` is the only access gate. Empty list = deny all,
  `["*"]` = allow public (intentional explicit opt-in for the RCE risk).
- `bots.feishu.user_prompts[open_id]` switches the *system prompt* per user, but
  the underlying tool set is identical for everyone.

`launcher/bot_manager.py` holds six declarative `BotSpec` rows
(`tg / qq / feishu / wecom / dingtalk / wechat`), each tied to a `lock_port`,
a `script` under `frontends/`, and a tuple of required `mykey_fields`. The GUI
Bots tab iterates this dict.

`llmcore/workers/calendar_worker.py` provides the four calendar capabilities
(`create / update / delete / query`) over either a local SQLite file or — when
`bots.feishu.use_for_calendar=true` — the owner's Feishu primary calendar
(`llmcore/workers/feishu_calendar_storage.py`). Both backends honor the same
`CalendarStorage` Protocol.

ADR-0008/0009 propose a kernel + worker + forum architecture but are both
"Proposed" — no code has landed against them. The voice loop (ADR-0010) is the
first design that actively dispatches through the kernel.

### 1.2 The pressure

The owner wants to direct messages from friends — meeting requests, "where is
she", "when is she free" — to flow through the same Feishu account they already
live in, without making themselves answer every one personally. Today the only
options are:

1. **Hand-route**: friends DM the owner, owner replies. No automation.
2. **Calendly-style external service**: friend gets a booking link via SMS/email.
   Loses the Feishu-native chat experience, and Calendly doesn't speak Chinese
   IM idiom.
3. **Give friends access to the existing owner bot**: setting
   `bots.feishu.allowed_users=["*"]` immediately. But the owner bot has
   `code_run`. Any friend can `do_code_run("os.system('rm -rf /')")` and the
   agent will cheerfully oblige. This has been a hard "don't" since the
   `allowed_users` change in 2026-03.

The product question is *not* "should we add a concierge" — it's "how do we add
one without re-creating risk #3."

### 1.3 What changed in the last 30 days

- The voice stack (ADR-0010) landed in code 2026-05-06 (per memory file
  `project_voice_stack_implementation.md`), including kernel-style dispatch
  for `calendar.*` capabilities. The pattern is no longer purely
  hypothetical — there's a working precedent for "agent delegates a small
  fixed set of capabilities via the kernel."
- `feishu_calendar_storage.py` shipped, meaning we can write events into the
  owner's actual Feishu calendar without the agent ever holding a calendar
  API client directly.
- Per `feedback_iteration_cadence.md` (2026-05-13), cadence has tightened
  but free_pool / journey-test / sensitivity-gate constraints stay hard.
  This ADR's privilege model (capability tokens, audit log, owner approval)
  is consistent with those constraints.

---

## 2. Decision

### 2.1 Two Feishu apps, two processes, one kernel

| Layer            | Owner stack (today)                  | Concierge stack (new)                  |
|------------------|--------------------------------------|----------------------------------------|
| Feishu app       | App A (owner self-built)             | App B (new self-built)                 |
| Config slot      | `bots.feishu.*`                      | `bots.feishu_concierge.*`              |
| Frontend script  | `frontends/fsapp.py`                 | `frontends/fsapp_concierge.py`         |
| Lock port        | 19532                                | 19533                                  |
| Agent class      | `GeneraticAgent` (full tool set)     | `ConciergeAgent` (restricted tool set) |
| Allowed callers  | `bots.feishu.allowed_users`          | `bots.feishu_concierge.allowed_friends`|
| Kernel token     | All capabilities                     | 6-capability allowlist                 |
| Audit log        | Per-session under `temp/`            | `temp/concierge_audit.jsonl`            |
| Identity in IM   | "Alice 的助理"                       | "Alice 的小秘书"                       |

Both processes register with `launcher/bot_manager.py`. The Bots tab gains a
seventh row automatically once the `BotSpec` for `feishu_concierge` is added.

### 2.2 Restricted capability set, enforced by tool registry

`ConciergeAgent` is constructed with an explicit `capabilities=` argument naming
exactly six kernel capabilities (see spec § 4.2). Any code path that tries to
call a capability outside this set fails at the kernel dispatch boundary, not at
prompt-time. This makes the boundary unjailbreakable by the friend, no matter
what they paste.

### 2.3 State-machine-first runtime (no ReAct loop)

The concierge runs a 5-state machine (`NEW → INTENT_CLASSIFY → … → CLOSED`) per
friend session. The LLM is invoked **only** for: (a) intent classification on
rule-miss, (b) reply rendering once the state machine has resolved which
capability to call. There is no agent-style "think → tool → think → tool" loop.
This bounds latency, cost, and the jailbreak surface.

### 2.4 Owner-approval gate on calendar writes

`calendar.create_event.v1` calls dispatched **by the concierge** carry a
`requires_approval=true` token claim. The worker returns `pending_approval` and
holds the call open. The concierge sends a Feishu interactive card to the owner
via the **owner** app credentials (held by the kernel-trust `escalate_worker`,
not the concierge). The owner taps Approve / Decline; the resolution flows back
to the worker; the calendar write either commits or aborts.

The concierge process **never sees the owner app's credentials** and **never
holds the calendar API client**. Both are kernel-trust resources.

### 2.5 Per-friend rate limit + global rate limit + size cap

Hard limits enforced at the frontend boundary (before any agent logic):

- 8 inbound / minute per friend
- 60 inbound / minute globally
- 4 KB max inbound text
- 800 char max outbound text per turn

These are blast-radius limits independent of the agent's own behavior — a
runaway agent still can't spam outbound, because the frontend caps the rate.

---

## 3. Consequences

### 3.1 Positive

- **Sharp privilege boundary.** The "what can the concierge do?" question has a
  one-page answer (the capability table in the spec). Auditing it doesn't require
  reading the agent prompt — the wrong tool literally isn't there.
- **Reuses existing infrastructure.** Calendar storage, bot_manager lifecycle,
  config_store mapping, Feishu WS adapter, interactive-card rendering — all
  battle-tested in `fsapp.py`. The concierge mostly composes things, doesn't
  invent them.
- **Validates ADR-0008/0009.** First production-leaning consumer of the kernel
  token model. If the design holds up here, ADR-0008 can graduate from
  Proposed → Accepted.
- **Lets the owner open up access incrementally.** A trusted friend whitelist
  → broader friend group → public (`allowed_friends=["*"]`) is a continuous
  knob, with the audit log providing visibility at every step.

### 3.2 Negative

- **Two Feishu apps to register / publish / maintain.** Operationally one app
  is simpler. The user has to create a second app on the Feishu Open Platform,
  open `im:message:send_as_bot` and `im:message` scopes a second time, and
  publish a second version. SETUP_FEISHU_CONCIERGE.md must walk through this.
- **Doubles the WS connection count.** Two long-running Feishu WebSocket
  clients, two lock ports, two `subprocess.Popen` rows in
  `launcher/process_registry.json`. Small in absolute terms; visible in the
  Bots tab UI.
- **KB curation overhead.** v1 ships a CLI for `temp/concierge_kb.jsonl`. A
  GUI page is deferred to Phase 2. Some users will find JSONL editing tedious.
- **Two-process state coordination.** The escalate-approve flow crosses
  process boundaries (concierge process holds the friend's session state;
  owner process receives the card click). This is acceptable because the
  kernel mediates, but it adds debuggability cost when something goes wrong
  in escalation.

### 3.3 Reversibility

This decision is reversible at three levels:

- **Disable**: `python -m launcher.config set bots.feishu_concierge.app_id ""`
  — `BotSpec.configured` flips false, the GUI hides the row, no behavior
  change.
- **Roll back**: delete `frontends/fsapp_concierge.py` and the four new worker
  modules. The owner stack is untouched.
- **Merge personas**: a v2 ADR could supersede this one by reverting to a
  single-app model with a within-app trust boundary (e.g. Feishu's "guest"
  user property, if/when that becomes available). The spec's data shapes
  (KB topic schema, escalation card schema, audit JSONL) survive that
  migration.

---

## 4. Alternatives considered

### 4.1 Persona-only split (one app, two prompts)

**Rejected.** This is what `bots.feishu.user_prompts[open_id]` enables today —
the same `GeneraticAgent` answers everyone, with a different system prompt
based on who's asking. Two problems:

1. The system prompt is not a security boundary; LLMs can be jailbroken into
   ignoring it.
2. Even if the prompt held, the tool set is the same — `code_run` is callable
   from any session.

The persona switch is a UX feature, not a privilege boundary, and we should
stop pretending otherwise.

### 4.2 Single-app with backend role check on each tool call

**Rejected.** We could wrap every tool's `invoke()` with a check like
`if caller_open_id not in OWNER_OPEN_IDS: deny`. But:

1. Every tool author has to remember to add the check. Forget once → leak.
2. The check has to know the caller open_id, which means plumbing identity
   through every call site.
3. Feishu app B + a restricted agent class achieves the same result
   declaratively, with less surface area.

### 4.3 External SaaS (Calendly + Zapier + Feishu webhook)

**Rejected.** Loses the conversational Chinese-IM feel, requires
internet-out from the owner's machine, doesn't compose with the wlwl-ass
kernel, costs subscription money. The whole point of wlwl-ass is to run
this stuff on your own laptop with your own keys.

### 4.4 LLM-ReAct agent with prompted refusal

**Rejected.** Same problem as 4.1 — prompts aren't a boundary. Also, ReAct
loops on the friend-facing path mean unbounded latency (each turn could
multi-call tools) and unpredictable cost. The state machine in §2.3 makes
both bounded.

### 4.5 Group-chat-style "ambient" concierge

**Rejected for v1.** Putting the concierge in a multi-friend group chat
(e.g. "Alice's social circle") sounded clever but introduces
addressability issues (who is the concierge replying to?) and turns
every escalation into a multi-recipient leak. v2 can revisit if there's
demand.

---

## 5. Implementation footprint

See spec § 8 (Phase 0 → Phase 4). Phase-1 footprint is roughly:

- 4 new worker modules under `llmcore/workers/` (~800 lines total)
- 1 new agent class `llmcore/concierge_agent.py` (~300 lines)
- 1 new frontend `frontends/fsapp_concierge.py` (~400 lines, mostly mirrored
  from `fsapp.py`)
- 1 new CLI `launcher/cli_kb.py` (~150 lines)
- ~50 lines of changes to `launcher/config_store.py` and
  `launcher/bot_manager.py`
- ~6 test files

The bulk of `fsapp.py`'s 880 lines deals with multimodal inbound (image, audio,
file) and the per-step task card. The concierge doesn't need either in v1, so
`fsapp_concierge.py` is markedly shorter.

---

## 6. Open questions

Forwarded from spec § 11:

1. **Card-button auth** — owner app or concierge app renders the
   escalation card? Decision: owner app, see spec §5.3.
2. **KB GUI tab** — Phase 2 or later? Defer; collect 1 month of CLI usage
   first.
3. **Multi-owner mode** — kernel layout supports it but v1 ships single-owner.
   Note for v2.

---

## 7. Acceptance for graduation to "Accepted"

This ADR moves from Proposed → Accepted when:

- Phase 1 ships with all tests green.
- One real-world friend-to-owner journey runs end-to-end (Phase 2 exit
  criteria in spec § 8).
- The audit log has captured at least 7 days of usage and the owner can
  point at a row and say "yes, that reply was appropriate."
- A security-review pass (per `.claude/skills/security-review`) finds no
  capability leak in the worker dispatch path.
