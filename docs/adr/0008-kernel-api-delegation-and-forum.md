# 0008 — Kernel API + Delegated Workers + Forum/Moderator

- **Status:** Proposed (design-only ADR; no implementation lands with this PR)
- **Date:** 2026-05-06
- **Authors:** wlwl-ass core (renamed derivative of upstream GenericAgent — see [ATTRIBUTION.md](../../ATTRIBUTION.md))
- **Supersedes:** *partial* —— this ADR introduces a hierarchy on top of the
  flat all-active-simultaneously model documented in [ADR 0006 Update note](./0006-per-session-api-config.md). ADR 0006 stays valid for per-session selection; this ADR adds privilege + coordination above it.

---

## 0. TL;DR

Today every entry in `temp/launcher_api_configs.json` is a peer — all activate
simultaneously, no privilege ordering, no inter-API channel. We promote one
entry to **`kernel`** kind. Kernel holds the **super-key** (real provider
credential + full capability set). Every other entry becomes a **`worker`**
that declares a use-case (vision / code / web / voice / image-gen / …) and
receives a **capability token** signed by the kernel at boot. Workers see only
the API-base + model + token they need; they don't see the super-key.

Workers and the kernel coordinate through an in-process **forum bus** with
typed topics and pub/sub. The kernel is the default **moderator**; it can
appoint sub-moderators per topic. Moderators arbitrate when workers disagree,
silence misbehaving workers, summarize chatter, and revoke / re-issue
capabilities at runtime.

---

## 1. Context

### 1.1 What the codebase does today

`llmcore/_keys.py::_load_mykeys()` merges three layers:

```
.env / shell env  <  config_store(~/.wlwl-ass/, <project>/.wlwl-ass/)  <  temp/launcher_api_configs.json
```

Every entry in the merged dict activates at startup. `launcher/api_config.py`
recognizes three `kind` values: `native_oai`, `native_claude`, `mixin`. The
mixin entry references its peers by `llm_nos` (or names) and provides
**failover only** — there is no privilege hierarchy, no token, no
inter-config communication, no revocation, no audit channel.

### 1.2 Why this is no longer enough

Several pressures converge:

1. **Capability sprawl.** New tools appear that don't fit a single LLM:
   `vision_tools.py`, `voice_tools.py`, `image_generation.py`,
   `mixture_of_agents.py`, `mcp_client.py`. Today every tool re-implements
   its own picker (`load_api_configs()` → filter by `audio_capable` /
   `image_capable` / model-name regex). The picker logic is duplicated and
   the activation policy ("which API for which tool") lives in code, not
   config.
2. **Privilege inversion risk.** A worker meant only for OCR currently
   receives `mykeys` exposing every other key in the same file. A compromised
   third-party plugin can read them all.
3. **No inter-API coordination.** `tools/mixture_of_agents.py` is the only
   place several APIs talk, and they "talk" by being concatenated into a
   prompt, not by exchanging structured messages with provenance and acks.
4. **No runtime revocation.** When a user spots a misbehaving worker (looping
   image gen, leaking tokens), they have to kill the agent process. There's
   no `disable worker X for the next 10 min` knob.
5. **No audit trail.** Today's only signal that "API B answered tool call
   #347 with cost $0.04" is the JSONL activity log. There's no notion of
   who authorized B to answer, against what scope.

### 1.3 What we want to be true

- One entry in config holds every privileged secret. Everything else inherits
  via signed, scoped tokens.
- Tools declare `required_capabilities`; the kernel routes accordingly.
  Picker logic disappears from `tools/*.py`.
- Workers can ask the kernel — *or each other, when permitted* — for help on
  topics other than their primary one, but only via an auditable channel.
- The kernel can **revoke** any worker's capability without restarting.
- Every `worker → kernel`, `worker → worker`, and `kernel → worker` exchange
  is observable, replay-able, and bounded (retention window, message size).
- Existing single-API users are not regressed: a fresh install with one
  `OPENAI_API_KEY` in `.env` still just works, with the kernel auto-promoted.

---

## 2. Decision

Adopt a **Kernel + Delegated Workers + Forum/Moderator** architecture, layered
*on top of* the existing config-store stack. No existing kind is removed; one
new kind (`kernel`) is added, and worker entries reuse `native_oai` /
`native_claude` plus a `delegation` block.

### 2.1 Architecture diagram

```
       ┌────────────────────────────────────────────────────┐
       │                 Operator (Human)                   │
       │  GUI / Feishu console / CLI — out of scope here    │
       └───────────────────────┬────────────────────────────┘
                               │ register / promote / revoke
                               ▼
       ┌────────────────────────────────────────────────────┐
       │  ┌──────────────────────────────────────────────┐  │
       │  │           KERNEL  (kind: "kernel")            │  │
       │  │  • holds super-key (real provider creds)     │  │
       │  │  • signs capability tokens                   │  │
       │  │  • routes tool calls by required_caps        │  │
       │  │  • moderates forum (default chair)           │  │
       │  │  • can revoke / rotate / hot-replace workers │  │
       │  └──┬───────────────────────────────────────────┘  │
       │     │ token + scoped api_base + model              │
       │     ▼                                               │
       │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌────────┐│
       │  │ vision  │  │ code    │  │ voice   │  │ web    ││
       │  │ worker  │  │ worker  │  │ worker  │  │ worker ││
       │  └────┬────┘  └────┬────┘  └────┬────┘  └────┬───┘│
       │       │            │            │            │     │
       │       └────────────┴─────┬──────┴────────────┘     │
       │                          │ post / read / ack       │
       │                          ▼                         │
       │  ┌──────────────────────────────────────────────┐  │
       │  │              FORUM BUS                        │  │
       │  │  topics: control / route / disagree / audit  │  │
       │  │          + ad-hoc topics by capability       │  │
       │  └──────────────────────────────────────────────┘  │
       └────────────────────────────────────────────────────┘
```

### 2.2 Why this shape vs. alternatives

See [§12 Alternatives](#12-alternatives-considered). Short version:
- *Capability tag dispatch (flat):* loses privilege hierarchy and audit chain.
- *Hierarchical groups (root → group → leaf):* over-engineered for current
  scale (a personal computer running ≤ ~10 LLM endpoints).
- *Status quo (all peers):* loses revocation + privilege containment, which
  are the main motivations.

The chosen shape is the minimum that buys *containment + delegation +
revocation + audit*, without committing to enterprise-IAM complexity.

---

## 3. Data Model

All schemas are Python `TypedDict`-friendly and JSON-serializable. They live
under `launcher/api_config.py` (existing module, additive change).

### 3.1 Kernel config (new `kind`)

```jsonc
{
  "kind": "kernel",
  "name": "primary-kernel",            // 1 active kernel per process
  "apikey": "sk-…",                    // SUPER-KEY — never sent to workers
  "apibase": "https://api.openai.com/v1",
  "model": "gpt-5.4",                  // kernel itself acts as a router model

  "capabilities": ["*"],               // kernel by definition holds everything

  "delegation": {
    "signing_key": "@keyring",         // HMAC key for capability tokens
                                       //   (resolved through config_store
                                       //    keyring; never in plaintext file)
    "default_token_ttl_sec": 3600,
    "max_token_ttl_sec": 86400,
    "audit_topic": "audit",
    "control_topic": "control"
  },

  "moderator": {
    "auto_chair_topics": ["control", "route", "disagree", "audit"],
    "may_appoint_submoderators": true
  }
}
```

Constraints:

- **Exactly one active kernel** per process. If two are loaded, fail loud at
  startup with `"multiple kernels not supported; demote N-1 to worker"`.
- The `apikey` field never leaves `_load_mykeys` → kernel session memory.
- `delegation.signing_key` MUST resolve to a keyring entry (see
  `launcher/config_store.py`), not a literal string in
  `launcher_api_configs.json`. We don't accept plaintext signing keys on
  disk.

### 3.2 Worker config (existing kinds + `delegation`)

```jsonc
{
  "kind": "native_oai",                // unchanged; could be native_claude
  "name": "vision-worker",
  "apikey": "sk-…",                    // OPTIONAL — may be empty if the
                                       //   worker rides the kernel's relay
                                       //   (kernel proxies the request)
  "apibase": "…",
  "model": "gemini-2.5-flash-vision",

  "delegation": {
    "delegated_by": "primary-kernel",  // must match an active kernel name

    "capabilities": [                  // declared scope
      "vision",                        //   (the kernel REJECTS asks for any
      "ocr"                            //    cap not on this list)
    ],

    "may_post_to": ["route", "audit"], // forum write list
    "may_subscribe_to": ["route"],     // forum read list
    "max_concurrency": 2,              // kernel-enforced rate limit
    "max_cost_usd_per_hour": 0.50,     // kernel-enforced budget
    "token_ttl_sec": 3600              // overrides kernel default if smaller
  }
}
```

A worker without a `delegation` block is treated as a **plain peer** under
the legacy flat model — backwards-compat for users who don't opt in to the
hierarchy. The kernel will not sign a token for it; tool calls that require
specific capabilities will not route there.

### 3.3 Capability token

```python
@dataclass(frozen=True)
class CapToken:
    issuer: str            # kernel name
    subject: str           # worker name
    capabilities: tuple[str, ...]
    issued_at: int         # unix seconds
    expires_at: int
    nonce: str             # 16-byte hex; uniqueness across re-issues
    sig: str               # hex(HMAC-SHA256(signing_key, canonical_payload))
```

Wire form: `wlwl.captok.v1.<base64url(json_payload)>.<sig>`. Tokens are
opaque to the worker — it just attaches them on every kernel call. The
kernel re-verifies on each receipt (cheap; HMAC, no DB). We deliberately do
**not** use JWT to avoid algorithm-confusion footguns and dependency churn;
HMAC-SHA256 over a canonical JSON payload covers our threat model
(non-malicious workers, in-process trust).

### 3.4 Forum schemas

```python
@dataclass(frozen=True)
class ForumTopic:
    name: str               # e.g. "route", "vision.ocr", "audit"
    visibility: str         # "kernel-only" | "subscribed" | "global"
    retention_messages: int # ring-buffer cap; older drops on overflow
    retention_seconds: int  # both caps apply; whichever first
    moderator: str          # config name; defaults to kernel
    sub_moderators: tuple[str, ...]

@dataclass(frozen=True)
class ForumMessage:
    topic: str
    author: str             # config name (kernel or worker)
    timestamp: float
    seq: int                # monotonic per topic
    type: str               # "post" | "ack" | "vote" | "complaint"
                            #   | "escalate" | "summary" | "redact"
    payload: dict           # JSON-serializable; content depends on `type`
    in_reply_to: str | None # seq id of parent, for threading
    cap_token_subject: str  # who claimed authorship; verified by bus
```

Five well-known **system topics** ship by default:

| Topic       | Visibility    | Purpose                                                     |
|-------------|---------------|-------------------------------------------------------------|
| `control`   | kernel-only   | kernel ↔ worker meta: registration, token refresh, revoke   |
| `route`     | subscribed    | "I have task X" / "I can take it" / "I'm overloaded"        |
| `disagree`  | subscribed    | when two workers return conflicting answers — moderator arbitrates |
| `audit`     | kernel-only   | append-only event log for replay / forensics                |
| `lounge`    | global        | low-priority chatter (capability descriptions, model gossip) |

Ad-hoc topics are created on first post by a worker that holds
`forum:create` capability. Anyone may *read* a `global` topic; only
authors with the right `forum:post:<topic>` cap may write.

---

## 4. Lifecycle

### 4.1 Startup

```
1. _load_mykeys() reads .env / config_store / launcher_api_configs.json
2. Promote-or-elect: find entries with kind == "kernel"
     a. exactly 1   → use it
     b. zero        → auto-promote: pick highest-priority entry (env > store
                      > launcher), warn user, mint a process-local signing
                      key in memory only (no persistence)
     c. ≥ 2         → fail loud
3. Kernel boots:
     • opens a forum bus (in-memory dict[topic → ring_buffer])
     • registers system topics with itself as moderator
     • for each worker entry with `delegation.delegated_by == self.name`:
         - validates declared `capabilities` is a subset of kernel's caps
         - signs an initial CapToken
         - publishes "registered" event to control topic
         - returns token + scoped (api_base, model) tuple to worker session
4. Workers are constructed (existing ClaudeSession / LLMSession / Native…)
   with the kernel-supplied parameters. They do NOT learn the super-key.
5. ToolClient receives a kernel-aware dispatcher (see §4.2) instead of the
   current direct-pick logic.
```

Failure to verify a worker (bad declaration, expired declaration, capability
escalation attempt) is logged on `audit` and that worker is left registered
*but tokenless* — calls into it return `403 capability_unavailable`.

### 4.2 Runtime: tool call dispatch

```
ToolClient.call(tool_name, args)
   │
   ▼
KernelRouter.route(tool_schema)
   │  read tool_schema.required_capabilities (new field on tools_schema.json)
   ▼
candidates = workers.where(
   token.valid(),
   token.capabilities ⊇ required,
   not silenced(),
   under(max_concurrency),
   under(max_cost_usd_per_hour)
)
   │
   ▼
if len(candidates) == 1: pick it
else:
   post on `route` topic: {"task": tool_name, "required": [...]}
   collect bids for window_ms (configurable, default 200ms)
   pick by policy: lowest-cost, lowest-load, sticky-to-recent, …
   │
   ▼
worker.invoke(tool_name, args, token=cap_token)
   │
   ▼
on success: append to `audit` topic with cost + latency + result hash
on conflict (>=2 workers chimed in with different answers): post on
   `disagree`; moderator picks winner via policy or escalates to human
on error: fall through to next candidate; surface to caller after exhaustion
```

### 4.3 Token rotation

- Kernel rotates a worker's token at `expires_at - 60s` by default, posting
  a `token_refresh` on `control`. Workers attach the new token on the next
  request; the old one is honored for a 30s grace period to absorb in-flight
  calls.
- A worker may proactively `POST /control { type: "refresh" }` if it sees a
  401 with `reason=expired`.
- Operator-initiated revoke is just a token whose `expires_at` is set to
  `now`. The kernel adds the worker to a denylist so a stale local copy of
  the prior token is rejected even within its window.

### 4.4 Worker disable / hot-replace

- **Disable:** kernel marks worker `silenced = true`. Routing skips it; forum
  posts from it are dropped (with a `dropped_silenced` audit entry).
- **Hot-replace:** operator or kernel loads a new worker with the same
  `name`. Old worker's token is revoked atomically with the new worker's
  token issuance. In-flight calls on the old worker complete on the old
  session; new calls land on the new worker.

---

## 5. Forum mechanism — details

### 5.1 Visibility scopes

| Scope          | Read                                  | Write                              |
|----------------|----------------------------------------|------------------------------------|
| `kernel-only`  | Kernel only (workers blocked)          | Kernel only                        |
| `subscribed`   | Workers in `may_subscribe_to`          | Workers in `may_post_to`           |
| `global`       | Anyone (incl. read-only auditors)      | Anyone with `forum:post`           |

Visibility is checked at the bus boundary, not in the moderator. A worker
that smuggles a topic name it doesn't subscribe to gets `404 not_subscribed`
— which itself is an audit event.

### 5.2 Retention

Each topic is a **ring buffer** (deque) capped by `retention_messages` and
`retention_seconds`. Defaults:

| Topic     | retention_messages | retention_seconds |
|-----------|-------------------:|------------------:|
| control   | 1024               | 86400             |
| route     | 512                | 600               |
| disagree  | 512                | 7200              |
| audit     | 8192               | 604800            |
| lounge    | 256                | 600               |

`audit` may *additionally* spool to `temp/forum_audit.jsonl` when its
ring overflows. That file is gitignored; size-capped at 64 MB with a
compaction job pruning older entries on agent restart.

### 5.3 Message types

| Type         | Who sends            | Effect                                                        |
|--------------|----------------------|---------------------------------------------------------------|
| `post`       | anyone with cap      | Plain content; visible to topic readers                       |
| `ack`        | anyone               | Acknowledge a prior `seq`; useful for "I'm taking this task"  |
| `vote`       | anyone with cap      | Lightweight tally; moderator counts                           |
| `complaint`  | any worker           | "Worker X gave me bad output for tool Y"; routed to moderator |
| `escalate`   | anyone               | Force a moderator decision; blocks waiting for resolution     |
| `summary`    | moderator only       | Compact a thread into one canonical entry                     |
| `redact`     | moderator only       | Mark a prior message hidden (retained, not deleted, in audit) |

---

## 6. Moderator mechanism

### 6.1 Default chair

The kernel is the **automatic moderator** of every system topic. For
ad-hoc topics, the topic creator becomes moderator unless the creation
request specifies otherwise. Moderator changes go through the kernel via
`POST /control { type: "appoint_moderator", topic, name }`.

### 6.2 Moderator powers

| Power                  | Who can use                | Scope                                  |
|------------------------|----------------------------|----------------------------------------|
| `silence(worker)`      | moderator                  | within own topic only                  |
| `unsilence(worker)`    | moderator                  | within own topic only                  |
| `redact(seq)`          | moderator                  | within own topic only                  |
| `summarize(seq_range)` | moderator                  | replaces N posts with 1 summary        |
| `pin(seq)`             | moderator                  | sticks a message at the top of the topic|
| `escalate_to_kernel`   | any moderator              | hand the case up                       |
| `revoke_capability(worker, cap)` | KERNEL ONLY      | global, immediate, audited             |
| `appoint_submoderator(worker, topic)` | KERNEL or topic-mod with `may_appoint_submoderators` | per topic |

The asymmetry is deliberate: any moderator can escalate, only the kernel
can take privileged actions. A topic moderator can't accidentally silence a
worker globally.

### 6.3 When forum communication is mandatory

Most calls go direct from `ToolClient` to a single picked worker — no forum
involvement. The forum is **mandatory** only in these situations:

1. **Multi-candidate routing** (§4.2): when ≥ 2 workers match the task,
   bidding goes through `route`.
2. **Disagreement**: when multiple workers chimed in and produced
   conflicting answers (e.g. mixture-of-agents), `disagree` is the only
   place a winner can be picked.
3. **Capability changes**: any grant/revoke writes to `control`.
4. **Audit-relevant events**: tool dispatches, costs, errors → `audit`.
5. **Cross-topic complaints**: a worker complaining about another worker
   MUST go through `complaint` → moderator; direct messaging between
   workers is intentionally **not** a feature (the architecture forbids
   peer back-channels to keep audit complete).

For everything else (ordinary single-worker tool calls), the forum is a
side-channel observer, not on the hot path.

### 6.4 Decision policy

When a moderator must choose (typical: `disagree` topic, two answers
present), the default policy is, in order:

1. **Schema consistency** — if one answer parses against the tool's
   declared output schema and the other doesn't, the parsing one wins.
2. **Cap weight** — if one author holds a more specific capability for
   the task, prefer it (e.g. `vision.ocr.zh` > `vision.ocr`).
3. **Recency of success** — workers are tracked with a rolling success
   counter on `audit`; higher recent success wins.
4. **Cost** — cheaper one wins.
5. **Escalate** — emit an `escalate` event; if a human operator is
   reachable (Feishu / GUI / CLI), they decide. Otherwise return both
   answers up the call stack tagged `unresolved=true` and let the calling
   tool decide.

This policy is itself written in config (`kernel.moderator.policy = […]`)
so it can be re-ordered without code changes.

---

## 7. Capability ↔ forum coupling

```
Capability                  Granted on what?
──────────────────────────  ──────────────────────────────────────────
forum:read:<topic>          all `subscribed` topics in may_subscribe_to
forum:post:<topic>          all topics in may_post_to
forum:create                only if explicitly granted (rare)
forum:mod:<topic>           by appointment
forum:redact:<topic>        forum:mod implies this
forum:revoke                kernel only, never delegated
```

The schema is intentionally hierarchical: holding `forum:mod:vision.*`
implies `forum:mod:vision.ocr` and `forum:mod:vision.caption`.

A worker calling a forum API without the right cap receives
`403 capability_required`. That itself is an audit event — *attempts to
post above your scope* are exactly the signal we want to track.

---

## 8. Relationship with existing systems

### 8.1 Mixin

A `mixin` config under the new model is a **special worker** whose
`capabilities` is the union of its referenced peers'. The kernel routes
to it the same way as any other worker; the mixin's internal failover
behavior is unchanged. The `llm_nos` field still works, but mixin entries
gain an optional `delegation` block to opt into the hierarchy.

### 8.2 launcher_api_configs.json

Add `"kernel"` to `SUPPORTED_KINDS` in `launcher/api_config.py`. Add a
new `KIND_PREFIX["kernel"] = "kernel_config"`. Validation: at most one
kernel; signing_key must reference a keyring entry; capabilities defaults
to `["*"]` and must include `*` (anything else is wrong shape).

### 8.3 ~/.wlwl-ass/config.json

Add a `settings.kernel` section:

```jsonc
{
  "settings": {
    "kernel": {
      "auto_promote_on_first_run": true,   // default true; first install UX
      "auto_promote_warning": true,        // print warning when no explicit kernel
      "default_token_ttl_sec": 3600
    }
  }
}
```

Existing tools (`tools/vision_tools.py` etc.) gain a thin wrapper:

```python
# Before (today):
session = pick_session_with_audio_capable_true()
# After:
session = kernel.dispatch(required=["voice"])
```

The picker functions are kept as deprecated shims that fall back to the
flat behavior when no kernel is active, so single-config users see no
behavior change.

### 8.4 ADR 0006 (per-session API config)

Unchanged — `WLWL_LLM_CONFIG_NAME` still resolves a session to a config by
name. When that config is a worker, the agent talks to it through the
kernel router transparently.

---

## 9. Migration path

| User state                                  | Upgrade behavior                           |
|---------------------------------------------|--------------------------------------------|
| Single API key in `.env`, no GUI configs    | Auto-promoted to kernel on first boot. Warning printed: "promoted to kernel; mint a real signing key with `python -m launcher.config kernel-init` for production use". |
| Multiple `launcher_api_configs.json` rows, no kernel | Same — first one auto-promoted; CLI nag suggests assigning a kernel explicitly. |
| User manually adds a `kind:kernel` entry    | Used as-is; auto-promotion disabled.       |
| User has `mixin` already                    | Mixin coexists; if no kernel, the mixin's first member is auto-promoted. |
| Power user wants several "tenants"          | Out of scope for this ADR. The hierarchy is single-rooted; multi-tenant runs as separate processes (one kernel each). |

A new CLI: `python -m launcher.config kernel-init` walks the user through:

1. Pick which config is the kernel.
2. Generate or paste a signing key, write it to OS keyring.
3. Mark every other config as a worker with declared capabilities and
   `may_post_to` / `may_subscribe_to` defaults.
4. Print a summary + a one-line revert hint (`kernel-revert`).

---

## 10. Failure modes & degradation

| Failure                              | Behavior                                                          |
|--------------------------------------|-------------------------------------------------------------------|
| Kernel session crashes mid-call      | Bus is in-process → all workers see the bus go away. Restart of agent process re-issues all tokens. The crash is logged but not recovered transparently — restart is the correct response (kernel = root of trust). |
| Worker session unresponsive          | Heartbeat (every 60s on `control`); after 3 misses, kernel marks `silenced`. Routing skips it; operator gets a `complaint` from the kernel itself. |
| Token expires mid-streaming response | 30s grace honors in-flight; new tokens picked up on next call.    |
| Forum overflow on `audit`            | Spool to `temp/forum_audit.jsonl`; if disk full, drop with a one-shot loud error to operator. We don't silently lose audit. |
| Two configs claim same `name`        | Fail loud at startup. Same problem as today.                      |
| Worker tries to post on kernel-only topic | `403 capability_required`. Logged on `audit`.               |
| Network blip on relay path           | Existing retry/failover (mixin, NativeClaudeSession retries) applies inside the worker; the kernel sees one logical call. |

---

## 11. Phase plan (design only — no code in this ADR)

| Phase | Scope                                                        | Where it lands                       |
|-------|--------------------------------------------------------------|--------------------------------------|
| **P0** | This ADR + schema sketches in `launcher/api_config.py` comments only. | docs/adr/0008-*.md (this file)       |
| **P1** | Kernel kind + capability token signing + GUI editor for kernel/worker designation. **Behavior unchanged at runtime** — this lands the data model only. | `launcher/api_config.py`, `launcher/config_store.py`, `gui/src/features/api-config/` |
| **P2** | Forum bus (in-memory, ring buffer) + `KernelRouter.route()` + worker `invoke()` wrapper. Tools continue to work via deprecated shim. | new module `llmcore/kernel.py`, new `llmcore/forum.py` |
| **P3** | Tools migrate from picker functions to `kernel.dispatch(required=[...])`. Tool schemas gain `required_capabilities`. | `assets/tools_schema.json`, `tools/*.py` |
| **P4** | Moderation actions (silence / redact / appoint), revocation API, audit spool, Feishu/GUI moderator console. | `launcher/api_server.py` endpoints, GUI page |

Each phase is independently revertable. Anyone can stop after P1 and still
be in a coherent state (kernel exists, but the runtime behaves like today).

---

## 12. Alternatives considered

### 12.1 Capability tag dispatch (flat)

Every config carries `capabilities=[…]`; tools declare `required_caps=[…]`;
dispatcher picks by intersection. **No kernel.**

- ✅ Simplest; no privilege graph to reason about.
- ❌ No place to put the super-key separate from worker keys.
- ❌ No moderator → no resolution policy when ≥ 2 workers match.
- ❌ No revocation channel; you'd have to edit + reload.
- **Rejected.** Solves capability routing but not delegation/audit, which
  are equally motivating.

### 12.2 Hierarchical groups (root → groups → leaves)

Like enterprise IAM: root, groups (vision-team, code-team, …), leaves
(individual configs). Permissions inherit + override.

- ✅ Mature pattern; well-understood.
- ❌ Three layers is overkill at our scale (≤ 10 endpoints typical).
- ❌ Harder UI: users now manage a tree, not a flat list with one promoted.
- ❌ "Group" without inter-group communication is just a label; with
  inter-group, we've reinvented the forum, but per-group.
- **Rejected.** Too much structure; we'd ship it and never use most of it.

### 12.3 Direct peer messaging (no forum, no moderator)

Workers call each other by name. No central bus.

- ✅ Lowest latency.
- ❌ No audit. No revocation. No arbitration on disagreement.
- ❌ Encourages emergent topologies the operator can't observe.
- **Rejected.** "Observable by default" is non-negotiable.

### 12.4 Status quo (do nothing)

- ✅ Zero effort.
- ❌ All five context pressures (§1.2) remain unaddressed.
- **Rejected.**

---

## 13. Consequences

### 13.1 Pros

- ✅ **Containment.** The super-key sits in exactly one place; compromise of
  any worker leaks only its scoped token.
- ✅ **Routing in config, not code.** Tool authors stop maintaining picker
  logic; ops can rebalance by editing config.
- ✅ **Auditability.** Every capability use produces an `audit` event;
  every disagreement produces a `disagree` event; every moderation action
  is itself audited.
- ✅ **Revocability.** A compromised key can be revoked in O(1), with a
  bounded grace window.
- ✅ **Inter-API coordination is a feature, not an emergent behavior.**
  When two workers want to coordinate, there's exactly one channel.

### 13.2 Cons

- ⚠️ **Conceptual tax.** Single-API users now need to grok "kernel" vs
  "worker" even though for them the distinction is virtual. Mitigated by
  auto-promotion + "single config = single kernel, no workers, business
  as usual" mental model.
- ⚠️ **In-process bus.** The forum doesn't survive a kernel crash. We
  accept this — the kernel is the root of trust, and crash means
  re-bootstrap. A persistent bus would tempt us into multi-process
  scenarios this ADR explicitly defers.
- ⚠️ **Latency floor on multi-candidate routing.** The 200ms bidding
  window adds latency when ≥ 2 workers match. Mitigated by `sticky` policy
  for repeated calls of the same shape.
- ⚠️ **Moderator policy can be wrong.** If the default policy picks the
  losing answer often enough, users will distrust it. Mitigated by making
  the policy config-driven and surfacing every disagreement on the
  Activity tab in the GUI.

### 13.3 Out of scope (explicitly)

- Multi-process kernels / kernel HA. (Single process today.)
- Cross-machine forum (federation). (Single host today.)
- Identity/auth for human operators (handled by the existing GUI auth /
  bot allowed_users).
- Cost-aware routing beyond a per-worker `max_cost_usd_per_hour` cap.
- Token usage quota tracking that survives restarts. (Defer to a future
  ADR if metrics show we need it.)

---

## 14. Open questions

1. **Signing-key rotation.** When the operator rotates the kernel's
   signing key, every worker token is invalidated. Do we re-sign all of
   them atomically, or do we surface an explicit "re-bootstrap" prompt?
2. **Mixin under kernel.** When a mixin is a worker, should its peers
   each carry their own capability tokens (richer audit) or share the
   mixin's token (simpler)? Default proposal: shared, until we observe a
   reason otherwise.
3. **Forum on disk by default?** P2 ships in-memory; do `audit` + `disagree`
   spool to disk by default, or only when `settings.audit.persist=true`?
4. **MCP servers as workers.** `tools/mcp_client.py` already calls
   external processes. Do MCP servers register as workers and receive
   capability tokens, or stay as a parallel mechanism? If the former,
   what's the bridging layer?
5. **Deferred until P3.** The exact list of `required_capabilities` per
   tool. Today's tools have implicit ones; we'll enumerate them when
   migrating.

---

## 15. Status notes

This ADR is **Proposed**. It is intentionally design-only:

- No new code lands in this PR.
- No existing config breaks (the data model is purely additive).
- Implementation begins (if approved) in a follow-up PR for **P1** —
  schemas + kernel-init CLI + GUI designation tab. Each subsequent phase
  is its own PR with its own ADR addendum if scope shifts.

Cross-references for reviewers:

- Existing config layering: `llmcore/_keys.py:_load_mykeys`,
  `launcher/api_config.py`, `launcher/config_store.py`.
- Mixin: `llmcore/mixin.py`, `tools/mixture_of_agents.py`.
- Tool picker logic to be replaced: `tools/vision_tools.py`,
  `tools/voice_tools.py`, `tools/image_generation.py`.
- ADR 0006 (per-session API config) — orthogonal; stays valid.
- Attribution context: this design is original to wlwl-ass, not inherited
  from upstream GenericAgent (which has only the flat all-active model).
  No new external borrowings; ATTRIBUTION.md unchanged.
