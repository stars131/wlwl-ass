# Feishu Concierge Bot — 朋友代聊与日程协商机器人

> **Status / 状态**: Proposed (design-only spec; companion to [ADR-0011](../adr/0011-feishu-concierge-bot.md))
> **Date / 日期**: 2026-05-17
> **Builds on / 依赖**:
>   - [ADR-0008 Kernel + Workers + Forum](../adr/0008-kernel-api-delegation-and-forum.md)
>   - [ADR-0009 Worker Plugin Interface](../adr/0009-worker-plugin-interface.md)
>   - [`frontends/fsapp.py`](../../frontends/fsapp.py) — existing owner-facing Feishu loop
>   - [`llmcore/workers/calendar_worker.py`](../../llmcore/workers/calendar_worker.py) — calendar event store (SQLite or Feishu cloud)
>   - [`launcher/bot_manager.py`](../../launcher/bot_manager.py) — 6-slot bot lifecycle manager

---

## 0. TL;DR

Today wlwl-ass exposes **one** Feishu app that gives the *owner* a full agent (with
`code_run`, file IO, browser control, etc.). The owner can chat with it from their
phone, but the owner cannot safely let friends DM that same bot — every friend would
inherit code-execution power, every friend's question would leak into the owner's
private agent history, and the bot would either over-share private context or be too
locked-down to be useful.

This spec adds a **second** Feishu app — the **Concierge Bot** (内置代号 `feishu_concierge`,
对外通常起名"X的小秘书"/"X的助理") — that:

1. Talks to the owner's friends in their own Feishu sessions.
2. Negotiates meeting times against the owner's calendar (SQLite or Feishu cloud, both already supported).
3. Answers a curated set of Q&A topics about the owner (a small RAG knowledge base the owner explicitly opts content into).
4. **Escalates** anything ambiguous to the owner via the original (owner) bot, with one-tap Approve / Decline buttons on a Feishu interactive card.
5. **Cannot run code, write arbitrary files, control a browser, or read owner-private memory.** It speaks only through a fixed allowlist of kernel capabilities.

The user experience is what 飞书/钉钉 power-users already pay for via Calendly,
Reclaim.ai, or 阿里钉钉 AI 排期 — but it lives inside the wlwl-ass kernel
architecture so it can co-host other workers (calendar, inspiration, KB, audit) and
escalate through the owner's existing IM channel rather than email.

---

## 1. Goals & non-goals

### 1.1 Goals

| # | Goal | How we measure |
|---|------|----------------|
| G1 | A friend can DM the concierge "周五下午能见个面吗" and get a sensible reply within 5s without the owner being online. | E2E test in `tests/test_concierge_journey.py` with mock LLM + mock Feishu adapter. |
| G2 | The concierge proposes 1–3 calendar slots based on the owner's actual availability. | `concierge.propose_slot.v1` reads from `calendar.query_events.v1` and applies owner-configured working hours + buffer. |
| G3 | When the friend accepts a slot, an event is booked in the owner's calendar with attendee + topic in `notes`. | `calendar.create_event.v1` is dispatched; event row in `temp/calendar.db` or Feishu cloud. |
| G4 | Owner gets a Feishu interactive card on the **owner** bot for every booked or escalated item with Approve / Decline / Reschedule buttons. | New `concierge.escalate.v1` capability + owner-bot card render path. |
| G5 | Concierge cannot run `code_run`, `file_write`, `file_patch`, `web_execute_js`, `start_long_term_update`. | Tool registry filter at agent boot; tested in `tests/test_concierge_restricted_tools.py`. |
| G6 | Owner can review every concierge reply for the past 7 days via `/audit` command on the owner bot, or via the GUI Bots tab. | Audit log written to `temp/concierge_audit.jsonl`; rendered as a card. |
| G7 | Concierge supports multi-friend isolation (no cross-pollination). | Per-`open_id` `ConciergeSession` with its own state machine, history, and rate-limit bucket. |
| G8 | Concierge is safe under public access (`allowed_friends = ["*"]`). | Per-IP and per-`open_id` rate limit; size limits; PII redaction in logs; no agent-style tool execution. |

### 1.2 Non-goals (explicit)

- **Not a general-purpose agent.** It does not pick up new skills, write code, or self-update its KB.
- **Not a group-chat bot.** v1 is 1:1 friend DMs only. Group chats are deferred.
- **No cross-platform federation.** v1 ships Feishu only. The same design generalises to Telegram/QQ/WeCom in v2, but is out of scope here.
- **No outbound contact discovery.** The concierge only replies; it does not proactively DM friends (cron-driven nudges are a v3 idea).
- **No voice loop integration in v1.** The orchestrator from ADR-0010 stays the owner's interface.

---

## 2. End-to-end user journey

### 2.1 Friend → Concierge: schedule a meeting

```
Friend (张三 open_id=ou_zs):
    "下周想找你吃个饭，方便吗"

Concierge:
    "好呀！我帮 Alice 看一下日历。她下周一般 18:30 之后有空，
     周二、周三、周四晚上都可以，你想约哪天？"

Friend:
    "周三吧 19:00 行不行"

Concierge:
    "周三 19:00–21:00 我帮她占住了，等她确认后通知你。
     你想约在哪里？"

Friend:
    "国贸附近就行"

Concierge:
    "OK，我把'周三 19:00 / 国贸附近 / 跟张三吃饭'发给 Alice 了。
     她确认后我会回复你。"

    ↑ (escalation card sent to owner)

[Owner sees on their Feishu owner-bot:]
    📅 张三 提议: 周三 19:00–21:00 · 国贸 · 吃饭
    [✅ Confirm]  [❌ Decline]  [🕐 Suggest other time]

Owner taps [✅ Confirm]:
    → calendar.create_event.v1 fires
    → concierge replies to 张三: "确认啦，周三 19:00 国贸见。"
    → owner-bot replies: "已加到日历。"
```

### 2.2 Friend → Concierge: Q&A about the owner

```
Friend (李四):
    "Alice 现在在哪个公司啊"

Concierge:
    [topic="employer", in KB allowlist]
    "她现在在 ABC 公司做 ML，2025 年开始的。"

Friend:
    "她平时几点能联系到"

Concierge:
    [topic="contact_window", in KB allowlist]
    "工作日 19:30 之后比较方便，急事可以直接微信她。"

Friend:
    "她身份证号是多少"

Concierge:
    [topic NOT in allowlist]
    "这个我不能告诉你，需要她本人回复你哈。我帮你转告？"
    [if friend says yes → escalation card to owner]
```

### 2.3 Friend → Concierge: out-of-scope question

```
Friend:
    "帮我用 Python 写个爬虫"

Concierge:
    "抱歉，我只能帮 Alice 处理日程和回答她允许的几个问题。
     代码相关的事情你可能想直接找 Claude / Codex～"
    [logged but no escalation; KB miss + tool-allowlist miss]
```

---

## 3. Architecture

### 3.1 Two-app layout

```
┌────────────────────────┐         ┌────────────────────────┐
│  Feishu App A (owner)  │         │  Feishu App B (concier.)│
│  app_id=cli_owner_xx   │         │  app_id=cli_concie_yy   │
│  scope: full agent     │         │  scope: restricted     │
│  bind: owner only      │         │  bind: friends or "*"  │
└──────────┬─────────────┘         └──────────┬──────────────┘
           │                                  │
           │ WS event (im.message)            │ WS event (im.message)
           ▼                                  ▼
┌──────────────────────┐         ┌────────────────────────────┐
│ frontends/fsapp.py   │         │ frontends/fsapp_concierge.py│
│  per-open_id          │         │  per-open_id                │
│  GeneraticAgent pool  │         │  ConciergeAgent pool        │
│  lock_port 19532      │         │  lock_port 19533            │
└──────────┬───────────┘         └──────────┬─────────────────┘
           │                                │
           │ agent.put_task / hooks         │ session.handle_msg
           ▼                                ▼
┌────────────────────────────────────────────────────────────┐
│                    llmcore.kernel.Kernel                    │
│                                                            │
│   ┌───────────────────────────────────────────────────┐    │
│   │  Worker registry (ADR-0009 capability tokens)     │    │
│   │                                                   │    │
│   │  • calendar.create_event.v1 ─┐                    │    │
│   │  • calendar.query_events.v1 ─┤── calendar_worker  │    │
│   │  • calendar.update_event.v1 ─┘                    │    │
│   │                                                   │    │
│   │  • concierge.kb_answer.v1    ── kb_worker (NEW)   │    │
│   │  • concierge.propose_slot.v1 ── slot_worker (NEW) │    │
│   │  • concierge.escalate.v1     ── escalate_worker   │    │
│   │  • concierge.audit.v1        ── audit_worker(NEW) │    │
│   └───────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

Why two apps instead of two personas under one app: Feishu binds **one bot identity
per app**. The friend needs to see a different bot in their contact list with a
different name/icon and chat history, otherwise the owner's chat with their own
agent and the friend's chat with the concierge would share `open_id` namespaces and
the user-prompt persona switch from today's `bots.feishu.user_prompts` is not
enforced at the transport layer. Two apps cleanly separates identity, audit, and
permission scope.

### 3.2 Configuration shape

A new top-level slot under `bots`:

```json
{
  "bots": {
    "feishu": {
      "app_id": "cli_owner_xxx",
      "app_secret": "...",
      "allowed_users": ["ou_owner"],
      "use_for_calendar": true,
      "calendar_id": "primary@feishu",
      "system_prompt": "...",
      "user_prompts": {}
    },
    "feishu_concierge": {
      "app_id": "cli_concierge_yyy",
      "app_secret": "...",
      "owner_open_id_on_owner_app": "ou_owner",
      "allowed_friends": ["ou_zhangsan", "ou_lisi"],
      "rate_limit_per_friend": {"window_s": 60, "max_msgs": 8},
      "rate_limit_global":     {"window_s": 60, "max_msgs": 60},
      "working_hours":         {"weekday": ["09:00-12:00", "14:00-18:00"],
                                "weekend": []},
      "meeting_buffer_min":    15,
      "default_meeting_minutes": 60,
      "auto_confirm_below_min": 0,
      "kb_path":               "temp/concierge_kb.jsonl",
      "audit_path":            "temp/concierge_audit.jsonl",
      "retention_days":        7,
      "persona": "你是 Alice 的助理小 W。语气友好、简洁、靠谱。当不确定时永远问 Alice。",
      "topics_allowed":        ["employer", "contact_window", "weekend_plan",
                                "tech_stack", "city"],
      "exit_phrase":           "拜拜"
    }
  }
}
```

Why a separate slot (not `bots.feishu[1]` indexed list): keeps backward
compatibility (existing fsapp.py keeps reading `bots.feishu.*` unmodified), and the
field set is genuinely different (concierge has working_hours, KB, audit; owner
doesn't).

Both slots flatten into `mykeys` via `launcher/config_store.py::synthesize_mykeys_from_store()` — extend the `bot_env_map` block (lines 620–634) with:

```python
"feishu_concierge": [
    ("app_id", "fs_concierge_app_id"),
    ("app_secret", "fs_concierge_app_secret"),
    ("allowed_friends", "fs_concierge_allowed_friends"),
    ("owner_open_id_on_owner_app", "fs_concierge_owner_open_id"),
    ("persona", "fs_concierge_persona"),
    ("working_hours", "fs_concierge_working_hours"),
    ("topics_allowed", "fs_concierge_topics_allowed"),
    # …
],
```

### 3.3 Lock port + bot_manager slot

Add a 7th entry to `BOT_SPECS` in `launcher/bot_manager.py`:

```python
"feishu_concierge": BotSpec(
    key="feishu_concierge",
    display_name="飞书·小秘书",
    script="fsapp_concierge.py",
    mykey_fields=("fs_concierge_app_id", "fs_concierge_app_secret",
                  "fs_concierge_owner_open_id"),
    sdk_modules=("lark_oapi",),
    lock_port=19533,  # next port after feishu's 19532
    log_filename="fsapp_concierge.log",
),
```

The GUI Bots tab picks this up automatically — no UI changes needed if the table
iterates `BOT_SPECS`.

### 3.4 Process model

`frontends/fsapp_concierge.py` is a **separate process** (not a thread under
`fsapp.py`) for three reasons:

1. **Crash isolation.** A panic in the friend-facing loop must not take down the
   owner's agent.
2. **Independent Feishu WS connections.** Lark's SDK ties the WS subscription to
   the app credential pair; two apps need two WS clients, and the SDK is happiest
   with one per process.
3. **Single-instance lock per app.** Reuses the existing `socket.bind(127.0.0.1, port)`
   pattern from `fsapp.py:41-58` so a stale concierge process is detected the same
   way as a stale owner process.

The process boots, calls `Kernel.connect()` (in-process kernel client), reads its
config, opens the Feishu WS, and runs an event loop nearly identical to `fsapp.py`
but with `ConciergeAgent` instead of `GeneraticAgent`.

---

## 4. ConciergeAgent — the runtime

### 4.1 Why not reuse GeneraticAgent

`GeneraticAgent` carries all 9 atomic tools including `code_run` and `file_write`.
Even with prompt-only restrictions ("don't run code"), an LLM can be jailbroken by
the user. The friend is the user here. The only defensible boundary is **tool
availability** — if the tool isn't registered, the agent literally cannot call it.

### 4.2 Allowlisted capability set

```python
CONCIERGE_CAPABILITIES = (
    "calendar.query_events.v1",        # read only
    "concierge.propose_slot.v1",       # find free slots
    "calendar.create_event.v1",        # write — only after owner approval
    "concierge.kb_answer.v1",          # Q&A over KB
    "concierge.escalate.v1",           # send card to owner
    "concierge.audit.v1",              # log every reply
)
```

Notably absent: `code_run`, `file_write`, `file_patch`, `web_scan`,
`web_execute_js`, `start_long_term_update`, anything from `tools/`.

### 4.3 Two execution modes

The agent can run in **structured-state-machine mode** *or* **LLM-ReAct mode**.
v1 ships state-machine mode because:

- It's testable end-to-end with no LLM (mock intent classifier suffices).
- It bounds the latency and cost per friend message.
- ReAct loops are where jailbreaks usually escape; constraining the state
  transitions removes the surface.

State machine (per `(friend_open_id)` session):

```
NEW → INTENT_CLASSIFY → {
    qa:          ANSWER_FROM_KB → (miss?) ESCALATE → CLOSED
    schedule:    PROPOSE_SLOTS  → AWAIT_FRIEND_PICK → CONFIRM → ESCALATE_OR_BOOK → CLOSED
    smalltalk:   SHORT_REPLY    → CLOSED
    out_of_scope: REJECT_POLITELY → CLOSED
    escalate_now: ESCALATE → AWAIT_OWNER → RELAY_TO_FRIEND → CLOSED
}
```

`CLOSED` doesn't end the conversation forever; it ends the current intent. The
next inbound friend message starts a fresh `NEW` for that intent.

### 4.4 Intent classifier

Tiny LLM call (cheapest model in mixin, or a local rule first-pass) that maps the
incoming text to one of:
`qa | schedule | smalltalk | out_of_scope | escalate_now`.

Rules first (regex / keyword), LLM second (only on rule miss). This is the same
pattern as `voice/intent.py:RuleIntentClassifier`. The concierge intents are a
strict subset, so the rule layer often resolves it without the LLM.

### 4.5 Working memory per friend

`temp/concierge_state/<friend_open_id>.json`:

```json
{
  "open_id": "ou_zhangsan",
  "friend_alias": "张三",
  "in_flight_intent": "schedule",
  "proposed_slots": [
    {"start": "2026-05-21T19:00:00+08:00", "end": "2026-05-21T21:00:00+08:00"}
  ],
  "topic_summary": "吃饭，国贸附近",
  "escalation_id": "esc_2f3a...",
  "last_msg_ts": 1747500000,
  "msg_count_window": [...]   // rolling timestamps for rate limit
}
```

Small JSON files (one per friend) rather than SQLite because:

- Most friends will send <5 messages a week. The hot set is tiny.
- File-per-friend = trivial debugging.
- No schema migrations to worry about; the persisted shape is just `dataclasses.asdict()`.

Mirror trim: anything older than `retention_days` is moved to
`temp/concierge_state/.archive/`.

---

## 5. New kernel workers

All four are net-new modules under `llmcore/workers/`. Each implements the
`Worker` Protocol from `llmcore/worker.py` exactly the way `calendar_worker.py`
does.

### 5.1 `kb_worker.py` — `concierge.kb_answer.v1`

**Storage**: `temp/concierge_kb.jsonl` — one JSON object per line:

```json
{
  "topic":   "employer",
  "summary": "她现在在 ABC 公司做 ML，2025 年开始的。",
  "long":    "Alice 自 2025-08 起在 ABC 公司机器学习平台组担任 SDE-II …",
  "visibility": "friends",   // friends | close_friends | family
  "updated_at": "2026-05-12T10:00:00+08:00"
}
```

**Lookup**: BM25 over `topic + summary + long`. v1 uses
`tools/forum_harvester.py`-style token overlap; v2 may switch to embeddings via
`session_search.py`'s existing infra.

**Output schema**:

```json
{
  "matched": true,
  "topic":   "employer",
  "text":    "她现在在 ABC 公司做 ML，2025 年开始的。",
  "score":   0.82,
  "in_allowlist": true
}
```

`in_allowlist=false` even on a positive match means the topic exists in KB but is
gated by `topics_allowed`. The agent then declines or escalates.

The KB is **owner-curated**: the owner edits `temp/concierge_kb.jsonl` directly, or
through a GUI page (v2). v1 ships a `python -m launcher.cli_kb` CLI for
`add/edit/list/delete` so the owner can manage it without touching JSONL by hand.

### 5.2 `slot_worker.py` — `concierge.propose_slot.v1`

**Input**:

```json
{
  "duration_minutes": 60,
  "earliest": "2026-05-20T00:00:00+08:00",
  "latest":   "2026-05-27T23:59:59+08:00",
  "topic_summary": "吃饭",
  "preferred_window": "evening"       // morning | afternoon | evening | any
}
```

**Logic**:

1. Read `calendar.query_events.v1` for the window.
2. Mask out anything outside `working_hours` (concierge's window, distinct from
   the owner's actual work day — this is "when am I OK with you booking me").
3. Mask out buffer (`meeting_buffer_min`) around existing events.
4. Filter by `preferred_window` if given.
5. Return up to 3 slots, earliest first.

**Output**:

```json
{
  "slots": [
    {"start": "2026-05-21T19:00:00+08:00", "end": "2026-05-21T20:00:00+08:00", "rank": 1},
    {"start": "2026-05-22T19:30:00+08:00", "end": "2026-05-22T20:30:00+08:00", "rank": 2}
  ],
  "exhausted": false
}
```

### 5.3 `escalate_worker.py` — `concierge.escalate.v1`

**Input**:

```json
{
  "kind":   "schedule_request" | "qa_unsure" | "manual_relay" | "off_topic_flag",
  "friend_open_id": "ou_zhangsan",
  "friend_alias":   "张三",
  "summary":        "周三 19:00 国贸 吃饭",
  "proposed_slot":  {...},                     // present if kind=schedule_request
  "payload":        {...}                       // intent-specific
}
```

**Effect**: Builds a Feishu interactive card and sends it to the **owner**'s open_id
on the **owner app** (note: this requires the escalate worker to hold the owner
app's credentials, *not* the concierge app's — see §6.3 for the privilege boundary).

Card schema (Feishu 2.0):

```json
{
  "schema": "2.0",
  "config": {"streaming_mode": false},
  "header": {"title": {"tag": "plain_text",
                       "content": "📅 张三 提议：周三 19:00 / 国贸 / 吃饭"}},
  "body": {
    "elements": [
      {"tag": "markdown", "content": "**张三** 想约你周三 19:00–20:00 国贸吃饭。"},
      {"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": "✅ 确认"},
         "value": {"esc_id": "esc_2f3a", "decision": "confirm"}, "type": "primary"},
        {"tag": "button", "text": {"tag": "plain_text", "content": "❌ 拒绝"},
         "value": {"esc_id": "esc_2f3a", "decision": "decline"}, "type": "danger"},
        {"tag": "button", "text": {"tag": "plain_text", "content": "🕐 改时间"},
         "value": {"esc_id": "esc_2f3a", "decision": "reschedule"}}
      ]}
    ]
  }
}
```

The owner-bot already receives `im.message.action.v1` events from card button
clicks (Feishu's standard); a small handler in `frontends/fsapp.py` routes those
clicks back to `concierge.escalate.resolve.v1` (v1.1; v1 can ship with simple
text-reply approval like "确认 esc_2f3a").

**Output**:

```json
{"escalation_id": "esc_2f3a...", "card_message_id": "om_xxx", "sent": true}
```

### 5.4 `audit_worker.py` — `concierge.audit.v1`

Append-only JSONL writer. Every concierge action (in + out + tool call) becomes
one row.

```json
{
  "t": 1747500001.23,
  "friend_open_id": "ou_zhangsan",
  "kind": "outbound_reply",
  "intent": "schedule",
  "state_before": "PROPOSE_SLOTS",
  "state_after":  "AWAIT_FRIEND_PICK",
  "in_text":      "下周想找你吃个饭",
  "out_text":     "好呀！周二、周三、周四晚上...",
  "capabilities_used": ["concierge.propose_slot.v1", "calendar.query_events.v1"],
  "latency_ms": 412
}
```

PII redaction: friend's exact open_id is hashed (SHA-256 truncated to 12 chars) so
the audit log can be shared in bug reports without leaking identifiers. The actual
mapping `ou_xxx → hash` lives in `temp/concierge_state/.id_map.json` (gitignored).

---

## 6. Safety & privilege boundaries

### 6.1 Privilege table

| Capability                 | Owner agent (existing) | Concierge agent (new)              |
|----------------------------|------------------------|------------------------------------|
| `code_run`                 | ✅                     | ❌ (not registered)                |
| `file_read`                | ✅ (sandboxed)         | ❌                                 |
| `file_write` / `file_patch`| ✅                     | ❌                                 |
| `web_scan`, `web_execute_js`| ✅                    | ❌                                 |
| `ask_user`                 | ✅ (owner)             | ❌ (would loop on friend)          |
| `start_long_term_update`   | ✅                     | ❌                                 |
| `update_working_checkpoint`| ✅                     | ❌                                 |
| `calendar.query_events.v1` | ✅                     | ✅                                 |
| `calendar.create_event.v1` | ✅                     | ✅ but **only after owner approve**|
| `calendar.update_event.v1` | ✅                     | ❌ (v1; allowed in v2 w/ approval) |
| `calendar.delete_event.v1` | ✅                     | ❌                                 |
| `concierge.kb_answer.v1`   | ✅ (debugging)         | ✅                                 |
| `concierge.propose_slot.v1`| ✅                     | ✅                                 |
| `concierge.escalate.v1`    | ❌ (no escalation target)| ✅                                |
| `concierge.audit.v1`       | ✅ (read)              | ✅ (write)                         |

The "only after owner approve" qualifier for `calendar.create_event.v1` is
**enforced in the worker**, not the agent. The worker receives a capability token
carrying a `requires_approval=true` claim; when the concierge calls it, the worker
returns `pending_approval` and blocks until `concierge.escalate.resolve.v1` arrives
with the matching `escalation_id`. This is the ADR-0008 kernel-token mechanism at
work — the concierge can't bypass approval by crafting a malicious payload.

### 6.2 Rate limiting

Two-tier:

- **Per friend**: 8 messages / minute by default. After threshold, concierge sends
  "稍等一下，让我跟 Alice 确认" and silently drops further messages for 60s.
- **Global**: 60 messages / minute across all friends. Hitting this triggers a
  warning card to the owner ("⚠️ 小秘书最近 1 分钟收到 60+ 条消息，是否要暂停？").

Counters live in `concierge_state/<friend>.json:msg_count_window` (rolling list of
timestamps; trimmed at every send).

### 6.3 Credential boundary

The concierge process holds **only**:
- `fs_concierge_app_id`, `fs_concierge_app_secret` (its own bot)
- A kernel capability token

It does **not** see:
- The owner app's `fs_app_id` / `fs_app_secret`
- Any LLM API key directly — all LLM calls go through the kernel which holds the
  super-key and stamps a delegated token per call (ADR-0008 § 4)

When the concierge dispatches `concierge.escalate.v1`, the escalate worker (which
the kernel registered as a kernel-trust worker, not a concierge-trust worker)
loads the **owner** app's credentials from the kernel's secret store and sends
the card. The concierge never sees those credentials — it only sees the
`escalation_id` returned. This is intentional: a compromised concierge process
must not be able to impersonate the owner bot.

### 6.4 Content safety

- **Inbound**: friend messages are size-capped at 4 KB. Images, files, audio are
  acknowledged but not OCR'd in v1 — concierge replies "我先跟 Alice 说有附件
  哈" and escalates.
- **Outbound**: concierge replies are size-capped at 800 chars and one message
  per turn (no multi-card explosions).
- **Topic gate**: `topics_allowed` in config; KB answers outside this set are
  rejected before send.
- **PII redaction in audit**: friend open_ids hashed; phone numbers / emails in
  reply text replaced with `[redacted]` before append to JSONL.

### 6.5 What's still scary

Be honest about residual risks:

- **Social-engineering bypass.** A friend who knows Alice well could craft a
  message that pattern-matches an in-allowlist topic but extracts info Alice
  didn't realise she'd disclosed. Mitigation: `topics_allowed` is conservative
  by default; the audit log lets Alice spot it next day.
- **Calendar exposure.** Even free/busy + working hours leaks pattern data
  (when Alice is usually free → when she's at home alone). Mitigation:
  `working_hours` is the *publishable* window, not Alice's real one.
- **Impersonation.** A friend's Feishu account can be compromised. Concierge
  can't tell. Mitigation: high-stakes escalations (e.g. cancelling existing
  events, changing location to somewhere new) always escalate; owner approval
  is the human-in-the-loop.

---

## 7. Sample turn — wire-level

### 7.1 Inbound from Feishu (WS event)

```json
{
  "schema": "2.0",
  "event": {
    "message": {
      "chat_id": "oc_chat_xx",
      "message_id": "om_msg_xx",
      "message_type": "text",
      "content": "{\"text\":\"下周想找你吃个饭，方便吗\"}"
    },
    "sender": {"sender_id": {"open_id": "ou_zhangsan"}}
  }
}
```

### 7.2 ConciergeAgent.handle (pseudocode)

```python
def handle(open_id: str, text: str) -> str:
    self.audit(kind="inbound", in_text=text, friend=open_id)
    if self.rate_limit_hit(open_id):
        return self.rate_limit_reply()

    sess = self.session(open_id)
    intent = self.intent_classify(text, sess)
    self.audit(kind="intent", intent=intent.kind, payload=intent.payload)

    if intent.kind == "schedule":
        slots = self.kernel.dispatch(
            required=["concierge.propose_slot.v1"],
            payload={
                "duration_minutes": intent.duration or self.cfg.default_meeting_minutes,
                "earliest": now(),
                "latest":   now() + week(1),
                "preferred_window": intent.window,
            },
        ).result["slots"]
        sess.state = "PROPOSE_SLOTS"
        sess.proposed_slots = slots
        return self.render_slots(slots, sess)

    if intent.kind == "schedule_pick":
        slot = sess.proposed_slots[intent.pick_index]
        esc = self.kernel.dispatch(
            required=["concierge.escalate.v1"],
            payload={
                "kind": "schedule_request",
                "friend_open_id": open_id,
                "summary": intent.topic_summary,
                "proposed_slot": slot,
            },
        ).result
        sess.escalation_id = esc["escalation_id"]
        sess.state = "AWAIT_OWNER"
        return f"OK，我把'{intent.topic_summary}'发给 Alice 了，她确认后回复你。"

    if intent.kind == "qa":
        ans = self.kernel.dispatch(
            required=["concierge.kb_answer.v1"],
            payload={"q": text},
        ).result
        if ans["matched"] and ans["in_allowlist"]:
            return ans["text"]
        return self.polite_unknown_reply(text)

    if intent.kind == "out_of_scope":
        return self.cfg.out_of_scope_reply

    return self.fallback_reply
```

### 7.3 Outbound via Feishu

Same `_send_raw(...)` path as `fsapp.py` — text or interactive card depending on
`render_slots` output. The concierge **does not** use the `_TaskCard` streaming
pattern (it has no multi-turn agent loop to stream), so messages are sent as a
single text or card per reply, lower latency than the owner agent.

---

## 8. Implementation phases

### Phase 0 — Spec & ADR (this PR)
- `docs/specs/feishu-concierge-bot.md` (this file)
- `docs/adr/0011-feishu-concierge-bot.md`
- ATTRIBUTION entries for referenced projects

### Phase 1 — Plumbing (no Feishu I/O yet)
**Files added:**
- `llmcore/workers/kb_worker.py`
- `llmcore/workers/slot_worker.py`
- `llmcore/workers/escalate_worker.py` (stubbed: writes card JSON to log)
- `llmcore/workers/audit_worker.py`
- `llmcore/concierge_agent.py` — restricted-tool agent
- `launcher/cli_kb.py` — KB manager CLI
- `tests/test_kb_worker.py`
- `tests/test_slot_worker.py`
- `tests/test_concierge_state.py`

**Files changed:**
- `launcher/config_store.py` — extend `bot_env_map` and `BOT_FIELD_DEFS`
- `launcher/bot_manager.py` — register `feishu_concierge` BotSpec
- `docs/CONFIG.md` — document the new keys

**Exit criteria:** Unit tests pass; `python -m launcher.cli_kb list` works; the
concierge can be instantiated in-process and answer a canned message against mock
kernel workers.

### Phase 2 — Feishu wiring
**Files added:**
- `frontends/fsapp_concierge.py` — the second-app loop, modeled on `fsapp.py`
- `frontends/gateway_adapters/feishu_concierge.py` — optional, if migrating to gateway
- `tests/test_concierge_journey.py` — E2E happy path with mock Feishu adapter
- `assets/SETUP_FEISHU_CONCIERGE.md` — companion to existing SETUP_FEISHU.md

**Exit criteria:** Two Feishu apps configured; concierge replies to a real friend
message in <5s; calendar event lands after owner approves the card.

### Phase 3 — Hardening
- Rate-limit telemetry
- Audit dashboard in GUI Bots tab (read-only viewer for the JSONL)
- Topic-allowlist enforcement test suite
- Soak test: 1000 simulated friend messages over 1 hour, asserting no crashes,
  no privilege leaks, audit log complete.

### Phase 4 — v2 (deferred, listed for completeness)
- Group chats
- Interactive-card button handler in `fsapp.py` so owner doesn't have to text "确认"
- Multi-language friend support (en/zh switch by detected language)
- Telegram / WeCom / QQ concierge counterparts (reuse `ConciergeAgent` across adapters)
- Voice loop integration: friends can leave a voice note, concierge transcribes
  and replies in text. Owner can approve via voice from the existing voice site.

---

## 9. Acceptance criteria

A reviewer should be able to verify the design + Phase-1 code by:

1. `pytest tests/test_kb_worker.py tests/test_slot_worker.py tests/test_concierge_state.py` — all green.
2. `python -m launcher.cli_kb add --topic employer --summary "Alice 在 ABC 公司"` — JSONL row appended.
3. Inspecting `ConciergeAgent.CAPABILITIES` (or its tool registry filter) — `code_run` and `file_write` MUST NOT appear.
4. `python -m launcher.config set bots.feishu_concierge.app_id …` — config persists; `python -m launcher.bot_manager status` shows the new slot.
5. Running `tests/test_concierge_journey.py` — a friend "下周吃饭" message produces a `propose_slot` dispatch, owner gets an escalation card, concierge replies "等她确认".
6. Audit JSONL has one row per inbound + intent + outbound; PII fields hashed.

For Phase 2 specifically: a tester with two Feishu accounts (one as owner, one as
friend) can run the full journey end-to-end against real Feishu cloud in <10
minutes following `SETUP_FEISHU_CONCIERGE.md`.

---

## 10. References — projects that informed this design

(Borrowing details — license, scope of borrow, modifications — go in
[ATTRIBUTION.md](../../ATTRIBUTION.md). Cited here for design context only.)

- **Cal.com** (AGPL-3.0) and **Calendly** (closed) — slot-proposal UX,
  buffer/window primitives, owner-approval-before-write pattern.
- **Reclaim.ai** / **Motion** (closed, SaaS) — AI-assisted negotiation tone,
  "let me check with X" deferral pattern, working-hours vs. publishable-hours
  distinction.
- **Khoj** (`khoj-ai/khoj`, AGPL-3.0) — personal RAG over owner-curated notes;
  per-topic visibility gating; chat-bot delivery surface.
- **Lobechat** (`lobehub/lobe-chat`, MIT) — multi-persona bot plugin layout
  pattern (one app, many personas) — informs why we go the *other* way
  (multi-app, single-persona) for the trust boundary.
- **NoneBot 2** (`nonebot/nonebot2`, MIT) and **Koishi** (`koishijs/koishi`,
  MIT) — Chinese-community chat-bot framework patterns: per-session state,
  intent dispatch, plugin permission scope. Concierge's state-machine-first
  intent layer is closer to these than to an LLM-ReAct agent.
- **Wechaty** (`wechaty/wechaty`, Apache 2.0) and **wxauto** (existing wlwl-ass
  dependency, see ATTRIBUTION #13) — IM-bot framework patterns; 1:1 vs group
  message normalization.
- **Anthropic Customer Concierge cookbook**
  (`anthropics/anthropic-cookbook` examples directory, MIT) — prompt patterns
  for polite refusal, scope-bounded persona, escalation phrases.
- **Feishu Open Platform docs** (lark-oapi-python, MIT) — IM, Calendar v4,
  Interactive Cards 2.0 schemas; the concierge wire format follows these
  one-for-one for round-trip compatibility.

---

## 11. Open questions (call out before implementation)

- **Card button auth.** When the owner taps "✅ 确认" on the escalation card,
  Feishu sends `im.message.action.v1` to whichever app rendered the card.
  Should the *owner* app handle that (and signal the concierge via kernel),
  or should the *concierge* app render the card (so the friend's identity
  is visible in the same audit chain)? Current proposal: owner app renders
  + handles, because the owner DM is the natural channel. Locked in §5.3.
- **KB curation UX.** v1 ships CLI only. Do we need a `Concierge` GUI tab
  in Phase 2, or is the audit-log JSON viewer enough? Deferred — collect
  feedback after first month of real use.
- **Multi-owner mode.** Two owners sharing one concierge (e.g. couples,
  small teams) — out of scope for v1 but the kernel/worker layout already
  supports it: distinct `bots.feishu_concierge[]` entries, each pointing
  at one owner's calendar + KB. Note this as a non-blocker.
