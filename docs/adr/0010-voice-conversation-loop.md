# 0010 — Voice Conversation Loop (语音对话回路)

- **Status:** Proposed (design-only; no code lands here)
- **Date:** 2026-05-06
- **Builds on:** [ADR-0008 Kernel API + Workers + Forum/Moderator](./0008-kernel-api-delegation-and-forum.md), [ADR-0009 Worker Plugin Interface](./0009-worker-plugin-interface.md)
- **Companion document:** [Voice Website PRD](../specs/voice-website-prd.md) — what the separately-implemented website must deliver to plug in here.

---

## 0. TL;DR

A web page captures the user's microphone continuously. Audio streams to wlwl-ass over a WebSocket. A **server-side STT + transcript-pattern wake detector** waits for the configured wake phrase (default `我对三体世界说话`). On wake, the agent greets via TTS and enters a multi-turn conversation. Each user utterance is **STT'd → intent-classified → dispatched** to the right worker via the kernel router (ADR-0008). The agent replies via TTS; the loop continues until an exit phrase or 60s idle. Every session's audio + transcript is persisted with a configurable retention window; the user can request deletion of any session.

Two new workers ship with this design — **calendar_worker** (SQLite-backed event store) and **inspiration_worker** (SQLite-backed note store with full-text search). Both are first-class plugins under ADR-0009, so the website doesn't know they exist; it only knows it sent audio in and got audio + transcripts back. Adding a third worker (e.g. weather, todo, smart-home) is a config change plus the worker package — no website redeploy.

MiniMax is the bootstrap provider for both ASR and TTS, behind versioned capability slugs (`voice.stt.v1`, `voice.tts.v1`) so swapping providers is a config edit.

---

## 1. Context

### 1.1 What we have already

- `tools/voice_tools.py` — existing voice helpers, currently file-in / file-out (no streaming).
- `assets/vision_api.template.py` — multimodal API template for vision; speech is partially analogous.
- `frontends/fsapp.py` — full Feishu integration with audio inbound (file-attach) but not real-time streaming.
- `launcher/api_server.py` — stdlib `http.server` for the GUI. Does **not** speak WebSocket today.
- `agent_loop.py` — synchronous turn-based loop. The voice loop is asynchronous and concurrent with it.

### 1.2 What is new

- **Real-time bidirectional audio** between a browser and wlwl-ass.
- A **state machine for conversation**, distinct from the agent's tool-call loop.
- **Wake-word and exit-word detection** at the *transcript* level (not raw audio).
- **Intent classification** as a routing layer above the kernel — the kernel routes by capability, but first we have to know which capability the user's utterance maps to.
- Two **stateful workers** (calendar, inspiration) with persistent storage. So far every worker the kernel has talked to is request/response stateless against an LLM.

### 1.3 Constraints decided upstream

| Decision         | Value                                                | Implication                                             |
|------------------|------------------------------------------------------|---------------------------------------------------------|
| TTS / ASR        | MiniMax T2A + MiniMax Speech-to-Text                 | Provider behind a swappable capability slug             |
| Hosting          | localhost MVP, interface ready for cross-device      | WebSocket protocol carries optional auth header         |
| Calendar storage | local SQLite                                         | calendar_worker is single-machine MVP                   |
| Conversation     | multi-turn until exit phrase / 60s idle              | ConversationOrchestrator state machine has 5 states    |

---

## 2. End-to-end user journey

```
1. User opens https://localhost:<port>/voice
2. Browser asks for mic permission. User grants.
3. Page goes to IDLE state. Status: "聆听中…"
4. WebSocket opens to ws://localhost:<port>/api/voice/session
5. Mic capture starts (16 kHz mono Opus, ~100 ms frames)
6. wlwl-ass continuously STTs incoming audio.
7. User says "我对三体世界说话".
8. Wake detector fires. wlwl-ass:
     a. starts a new ConversationSession (uuid)
     b. emits state.update → ARMED
     c. synthesizes "请讲" via TTS
     d. streams TTS chunks back; browser plays them
     e. transitions to LISTENING when TTS finishes
9. User says "把明天下午三点的咖啡会议改成四点".
10. wlwl-ass:
     a. STT: full transcript with confidence
     b. IntentClassifier (LLM) → intent: "calendar.update_event",
        args: {match: "咖啡会议", date: "<tomorrow>", new_start: "16:00"}
     c. kernel.dispatch(required=["calendar.update_event.v1"], payload=args)
     d. calendar_worker resolves the event by fuzzy match, updates SQLite
     e. result: "已经把明天的咖啡会议从 15:00 改到 16:00。"
     f. TTS streams reply back; state RESPONDING → ARMED
11. User says "记一下：要研究 Tauri 2 的更新机制".
12. → intent: "inspiration.record" → inspiration_worker writes note → reply.
13. User says "拜拜".
14. Exit phrase matched. State → IDLE. WebSocket stays open. Wake detector
    re-armed for the next session.
```

Persisted artifacts after this session:

```
temp/voice_sessions/<session_uuid>/
   audio.opus              # full recording, 16 kHz mono Opus
   transcript.jsonl        # one line per turn
   manifest.json           # start, end, duration, exit_reason, intent_log
```

`transcript.jsonl` example:

```jsonl
{"t":1714972801.12,"role":"system","event":"wake_matched","phrase":"我对三体世界说话"}
{"t":1714972801.95,"role":"agent","kind":"tts","text":"请讲"}
{"t":1714972806.41,"role":"user","kind":"stt","text":"把明天下午三点的咖啡会议改成四点","conf":0.93}
{"t":1714972806.42,"role":"system","event":"intent","value":"calendar.update_event","args":{...}}
{"t":1714972806.93,"role":"system","event":"dispatch","worker":"calendar","cap":"calendar.update_event.v1","call_id":"..."}
{"t":1714972807.18,"role":"system","event":"dispatch_result","ok":true}
{"t":1714972807.31,"role":"agent","kind":"tts","text":"已经把..."}
...
{"t":1714972847.02,"role":"system","event":"exit_matched","phrase":"拜拜"}
```

---

## 3. Architecture

```
   ┌────────────────────────────────────────────────────────────────────┐
   │                         BROWSER                                    │
   │  ┌──────────────────────┐      ┌──────────────────────────────┐   │
   │  │  Mic capture         │      │  TTS playback queue           │   │
   │  │  AudioWorklet        │      │  Web Audio API + jitter buf  │   │
   │  │  → Opus 16 kHz mono  │      │  ← Opus chunks               │   │
   │  └──────┬───────────────┘      └──────────────┬───────────────┘   │
   │         │                                      │                   │
   │         │  ws.send(binary, frame)              │  ws.onmessage     │
   │         └──────────────┬───────────────────────┘                   │
   │                        │                                            │
   └────────────────────────┼────────────────────────────────────────────┘
                            │
                            │ ws://localhost:<port>/api/voice/session
                            │ (optional Authorization: Bearer <token>)
                            ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                  WLWL-ASS  (Python process)                        │
   │                                                                    │
   │   ┌────────────────────────────────────────────────────────────┐  │
   │   │   VoiceIngressEndpoint                                     │  │
   │   │   (new module: launcher/voice_ws.py — stdlib + asyncio +   │  │
   │   │    `websockets` library; one of the few new deps)          │  │
   │   └──────────────┬─────────────────────────────────────────────┘  │
   │                  │ AudioFrame / ControlFrame                      │
   │                  ▼                                                │
   │   ┌────────────────────────────────────────────────────────────┐  │
   │   │   ConversationOrchestrator                                 │  │
   │   │   (new module: voice/orchestrator.py — async state machine)│  │
   │   │                                                            │  │
   │   │     IDLE ─── wake ────► ARMED ─── greet done ───► LISTENING│  │
   │   │      ▲                    ▲                          │     │  │
   │   │      │                    │                  VAD silence  │  │
   │   │      │                    │                          ▼     │  │
   │   │      │                    └──── tts done ── PROCESSING ──► │  │
   │   │      │                                          │          │  │
   │   │      └────── exit / 60s idle ───── RESPONDING ◄─┘          │  │
   │   │                                                            │  │
   │   └──┬─────────┬──────────┬──────────┬────────────┬───────────┘  │
   │      │         │          │          │            │              │
   │      ▼         ▼          ▼          ▼            ▼              │
   │   ┌─────┐  ┌──────┐  ┌────────┐  ┌────────┐  ┌──────┐           │
   │   │ STT │  │ Wake │  │ Intent │  │ Kernel │  │ TTS  │           │
   │   │ Wkr │  │ Match│  │ Class  │  │ Router │  │ Wkr  │           │
   │   └─────┘  └──────┘  └────┬───┘  └────┬───┘  └──────┘           │
   │                           │           │                          │
   │                           │           │ kernel.dispatch(req)     │
   │                           │           ▼                          │
   │                           │     ┌──────────────────────┐         │
   │                           │     │ Workers (ADR-0009)    │         │
   │                           │     │  - calendar_worker    │         │
   │                           │     │  - inspiration_worker │         │
   │                           │     │  - <future>           │         │
   │                           │     └──────────────────────┘         │
   │                           │                                       │
   │   ┌───────────────────────┴─────────────────────────────────────┐ │
   │   │   SessionStorage                                             │ │
   │   │   temp/voice_sessions/<uuid>/{audio.opus,transcript.jsonl,   │ │
   │   │                                manifest.json}                │ │
   │   └──────────────────────────────────────────────────────────────┘ │
   └────────────────────────────────────────────────────────────────────┘
```

---

## 4. Components

### 4.1 VoiceIngressEndpoint (`launcher/voice_ws.py`)

Responsibilities:

- Accept WebSocket connections at `/api/voice/session`.
- Validate optional bearer token (config: `settings.voice.auth_token`; if unset, accept anything from localhost).
- Demux incoming frames: binary = audio, text JSON = control.
- Per-connection back-pressure (drop oldest audio frame if orchestrator's input queue exceeds 10s of audio).
- Forward control + audio to `ConversationOrchestrator.feed()`.
- Forward orchestrator's outbound events (state changes, transcript chunks, TTS chunks) to the WebSocket.

Stack: Python 3.10+ asyncio, the `websockets` PyPI library (stdlib `http.server` doesn't do WS). One new dep, opt-in:

```toml
# pyproject.toml — new optional extra
[project.optional-dependencies]
voice = [
    "websockets>=12.0",
    "webrtcvad>=2.0.10",     # voice activity detection
    "soundfile>=0.12",        # opus assembly for storage
]
```

### 4.2 ConversationOrchestrator (`voice/orchestrator.py`)

A small async state machine, one instance per WebSocket connection. States:

| State        | Mic flowing? | TTS flowing? | What happens                                                  |
|--------------|:------------:|:------------:|---------------------------------------------------------------|
| `IDLE`       | yes          | no           | STT continuously; wake-detector watches transcript             |
| `ARMED`      | paused\*     | yes          | Greeting is speaking. Don't capture user audio yet            |
| `LISTENING`  | yes          | no           | Capture user utterance. VAD watches for silence               |
| `PROCESSING` | paused\*     | no           | STT final → intent → dispatch in flight                        |
| `RESPONDING` | paused\*     | yes          | TTS streaming reply back to browser                            |

\* "Mic paused" doesn't actually stop the browser sending — the orchestrator just discards incoming audio frames during these states. The browser shows "Speaking…" so the user knows not to talk over the agent.

Transitions (events):

```
IDLE       --[wake matched]------------> ARMED
ARMED      --[greet TTS finished]-------> LISTENING
LISTENING  --[VAD silence ≥800ms]-------> PROCESSING
PROCESSING --[reply text ready]---------> RESPONDING
RESPONDING --[TTS finished]-------------> LISTENING (multi-turn)
RESPONDING --[exit phrase in last user utterance]--> IDLE
LISTENING  --[60s timeout]--------------> IDLE (with farewell TTS)
ARMED      --[60s timeout no user]------> IDLE
*          --[error]--------------------> IDLE (with apology TTS)
```

The state machine is itself unaware of the kernel — it just calls `intent.classify(text)` and `kernel.dispatch(req)` as opaque async functions. This keeps the orchestrator testable in isolation.

### 4.3 Wake & exit phrase matching

Server-side. Operates on the *partial* STT transcript so it triggers fast (typical wake latency ≤ 250 ms after user finishes speaking).

```python
class PhraseMatcher:
    def __init__(self, phrases: list[str], min_confidence: float = 0.6): ...
    def feed(self, partial: str, conf: float) -> str | None:
        """Return the matched phrase, or None."""
```

Implementation: substring match after normalization (lowercase, strip punctuation, NFC-normalize). For Chinese, also try pinyin fallback so 三体 vs 散体 (homophone variant from STT errors) still hits.

Defaults (from `settings.voice` in `~/.wlwl-ass/config.json`):

```jsonc
{
  "settings": {
    "voice": {
      "wake_phrases": ["我对三体世界说话"],
      "exit_phrases": ["拜拜", "退出", "谢谢就这样", "再见"],
      "wake_min_confidence": 0.6,
      "vad_silence_ms": 800,
      "session_idle_timeout_s": 60,
      "max_recording_age_days": 30,
      "max_recording_size_mb": 200
    }
  }
}
```

User can override `wake_phrases` to anything. We keep the Three-Body reference as the default because it's distinctive enough to not false-trigger.

### 4.4 IntentClassifier (`voice/intent.py`)

LLM-based. Sends:

- The user transcript
- The current registered set of capabilities (from `CapabilityRegistry`, ADR-0009 §6) with their descriptions
- A few-shot prompt with examples

Receives JSON-schema-constrained output:

```jsonc
{
  "intent": "calendar.update_event",      // matches a registered cap, or "_chat" for free chat
  "confidence": 0.91,
  "args": { ... },                         // schema validated against the cap's request_schema_ref
  "fallback_reply": "如果路由失败，让 agent 直接说这句"
}
```

Why the kernel doesn't do this routing itself (ADR-0008 §4.2 routes by **declared** required_capabilities on a tool schema): voice utterances don't carry tool schemas — they're free text. So the IntentClassifier is the layer that *produces* `required_capabilities` for the kernel.

If `intent == "_chat"` or `confidence < 0.5`, fall through to the default chat agent (existing `agent_runner_loop`). The chat agent then decides whether to use tools or just chat.

### 4.5 STT and TTS as workers

Per ADR-0009, anything pluggable is a `Worker`. STT and TTS each become a worker with capabilities:

| Worker (default)    | Capability slug          | Request schema (sketch)                          |
|---------------------|--------------------------|--------------------------------------------------|
| `minimax_stt_worker`| `voice.stt.v1`           | `{audio: bytes, format: "opus", sample_rate: 16000}` → `{text, confidence, words?[]}` |
| `minimax_tts_worker`| `voice.tts.v1`           | `{text, voice_id?, speed?, format: "opus"}` → stream of `{seq, audio_chunk}`         |

For streaming TTS, this ADR uses a small extension of ADR-0009's protocol: a worker may return a generator yielding `InvokeResponse` chunks instead of a single response. The kernel router supports both; the orchestrator consumes the stream and forwards each chunk to the WebSocket.

(ADR-0009 §17 Open Q1 — streaming responses — is now answered: yes, opt-in via `Worker.invoke_stream(req) -> AsyncIterator[InvokeResponse]`. The base `invoke()` stays for non-streaming. Workers declare which they implement via their `WorkerMetadata`. The kernel router probes the right one.)

Provider swap path: drop in a `whisper_local_stt_worker` (using `faster-whisper`) declaring the same `voice.stt.v1` cap. Set `routing_priority` higher in config; old MiniMax worker stays as failover.

### 4.6 calendar_worker

An in-tree worker (`llmcore/workers/calendar_worker.py` per ADR-0009 §4.1 channel 1).

**Storage:** `temp/calendar.db` (SQLite). Initialized on first call. Schema:

```sql
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,            -- uuid
    title         TEXT NOT NULL,
    start_at      TEXT NOT NULL,                -- ISO 8601 with TZ
    end_at        TEXT,                          -- nullable (single-point events)
    location      TEXT,
    notes         TEXT,
    tags          TEXT,                          -- JSON array string
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    source        TEXT,                          -- 'voice' | 'manual' | 'import' | …
    source_session TEXT                          -- voice session uuid if applicable
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_at);
CREATE INDEX IF NOT EXISTS events_title ON events(title);
```

**Capabilities offered:**

| Cap slug                       | Request                                                  | Response                                          |
|--------------------------------|----------------------------------------------------------|---------------------------------------------------|
| `calendar.create_event.v1`     | `{title, start_at, end_at?, location?, notes?, tags?[]}` | `{id, normalized_event}`                          |
| `calendar.update_event.v1`     | `{match: {by_id?, by_title_substring?, near_date?}, patch: {...}}` | `{id, before, after}`                  |
| `calendar.delete_event.v1`     | `{match: {...}, soft: bool}`                             | `{id, deleted: true}`                             |
| `calendar.query_events.v1`     | `{from, to, q?, tags?}`                                  | `{events: [...]}`                                 |
| `calendar.summarize_window.v1` | `{from, to}` (e.g. "今天剩下的安排")                      | `{summary: text}`  (uses the kernel's chat LLM)   |

**Fuzzy match:** `update_event` and `delete_event` accept human-style references. Implementation: SQL LIKE on title + date proximity, then if multiple match, ask user to disambiguate via the orchestrator (`request_clarification` API).

**Storage layer evolution path:** `CalendarStorage` is a Protocol so `SQLiteStorage` is one impl. A future `GoogleCalendarStorage` plugin (its own pip package per ADR-0009 channel 3) drops in by registering an alternative `calendar_worker` with the same cap slugs and a higher routing priority.

### 4.7 inspiration_worker

Same shape; `temp/inspiration.db`:

```sql
CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    tags        TEXT,                  -- JSON array
    links       TEXT,                  -- JSON array of URLs
    created_at  TEXT NOT NULL,
    source      TEXT,
    source_session TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    text, content=notes, content_rowid=rowid, tokenize='unicode61'
);
-- triggers to keep FTS in sync (omitted for brevity)
```

Capabilities:

| Cap slug                        | Notes                                                 |
|---------------------------------|-------------------------------------------------------|
| `inspiration.record.v1`         | core insert; auto-tags via the kernel's chat LLM      |
| `inspiration.query.v1`          | full-text search; FTS5 MATCH                           |
| `inspiration.list_recent.v1`    | last N notes                                           |
| `inspiration.summarize_recent.v1`| LLM-summarized digest                                 |

Auto-tagging: on record, the worker calls `kernel.dispatch(required=["chat.classify_tags.v1"])` with the note text and a prompt to extract 1–5 tags. This is opportunistic — if no chat worker is up, the note is stored untagged.

### 4.8 SessionStorage

A small filesystem-backed log writer (`voice/storage.py`), one instance per voice session. Writes:

```
temp/voice_sessions/<session_uuid>/
  audio.opus                # full session, written by Opus encoder
  transcript.jsonl          # appended turn-by-turn (see §2 example)
  manifest.json             # written on close: {start, end, duration_s,
                            #   exit_reason, intents_detected[],
                            #   workers_invoked[], total_cost_usd}
```

Background cleanup task: on agent startup, sweep `voice_sessions/`, delete sessions older than `settings.voice.max_recording_age_days`. Cap total directory size at `max_recording_size_mb` MB; oldest first when over.

User commands (via voice or kernel CLI):
- "删除我刚才的对话" → orchestrator marks current session for redaction; all artifacts deleted on session close.
- "删除所有录音" → bulk wipe (requires approval card via the future Operator console).

---

## 5. Wire protocol (browser ↔ wlwl-ass)

This is the **frozen public interface** the website team builds against. Full
spec lives in the [Voice Website PRD](../specs/voice-website-prd.md) §6; reproduced
here as a concise contract.

### 5.1 Endpoint

`ws://localhost:<api_port>/api/voice/session`
optional `Authorization: Bearer <token>` (cross-device deployments)
optional query `?session=<resume_uuid>` (resume a paused session)

`<api_port>` is whatever `launcher/api_server.py` is using; the launcher prints it on boot.

### 5.2 Frame types

Two channels, same WebSocket:

- **Binary frames** = audio. From browser: 20 ms Opus packets at 16 kHz mono. From server: 20 ms Opus packets at 24 kHz mono (TTS), prefixed with a 4-byte little-endian `seq` so the browser can handle out-of-order arrivals (rare on a single-WS connection but cheap insurance).
- **Text frames** = JSON control envelopes:

```jsonc
// browser → server (control)
{"type":"hello", "client":"web@1.0", "audio_format":{"codec":"opus","sample_rate":16000,"channels":1}}
{"type":"pause"}                       // mute mic for a moment
{"type":"resume"}
{"type":"forget_session"}              // ask server to delete this session's artifacts on close
{"type":"goodbye"}                     // graceful disconnect

// server → browser (events)
{"type":"ready",            "session_id":"<uuid>", "tts_format":{"codec":"opus","sample_rate":24000,"channels":1}}
{"type":"state",            "value":"IDLE"|"ARMED"|"LISTENING"|"PROCESSING"|"RESPONDING"}
{"type":"transcript.partial","text":"我对三体...","conf":0.6}
{"type":"transcript.final", "text":"我对三体世界说话","conf":0.94,"role":"user"|"agent"}
{"type":"intent",           "intent":"calendar.create_event","confidence":0.91,"args":{...}}
{"type":"dispatch",         "worker":"calendar","cap":"calendar.create_event.v1","call_id":"..."}
{"type":"dispatch_result",  "call_id":"...","ok":true,"summary":"已添加"}
{"type":"tts.start",        "turn_id":"..."}             // followed by binary chunks
{"type":"tts.end",          "turn_id":"..."}
{"type":"error",            "code":"...","message":"...","fatal":bool}
```

Error codes (extensible):

| Code                     | Meaning                                                  | Browser action                |
|--------------------------|----------------------------------------------------------|-------------------------------|
| `auth_required`          | Missing/invalid bearer token                             | Show login UI                 |
| `audio_format_unsupported`| Hello frame's audio_format not accepted                  | Re-encode and reconnect       |
| `rate_limited`           | Too many sessions / too much audio                       | Back off + retry              |
| `no_stt_worker`          | No worker offering `voice.stt.v1` is online              | Show "voice unavailable"      |
| `no_tts_worker`          | Same for `voice.tts.v1`                                  | Show "TTS unavailable"; can still display transcripts |
| `internal_error`         | Server bug; details in `message`                         | Reconnect with backoff        |

### 5.3 Lifecycle

```
1. Browser opens WS, sends {hello}.
2. Server replies {ready, session_id, tts_format} or {error, fatal:true}.
3. Browser starts streaming audio binary frames.
4. State events flow as per §4.2 transitions.
5. On {goodbye} or transport close, server flushes manifest.json.
```

---

## 6. Privacy & data handling

### 6.1 Defaults

- All audio + transcripts persist to `temp/voice_sessions/`.
- Retention: `max_recording_age_days = 30` (configurable; 0 = no persistence at all).
- Disk-cap: `max_recording_size_mb = 200` MB (oldest first eviction).
- No upload to any third party other than the configured STT/TTS provider (MiniMax). The provider is told **only** the audio for the duration of one call; nothing persists on their side per their TOS.

### 6.2 User controls

- `{type:"forget_session"}` control frame → server marks session for delete-on-close.
- Voice command "删除上一段对话" → orchestrator handles in-session.
- CLI: `python -m launcher.voice forget --session <uuid>`, `--all`, `--older-than 7d`.
- HTTP: `DELETE /api/voice/sessions/<uuid>`.

### 6.3 Redaction

Not in MVP — if a user blurts a credit-card number, the transcript and audio just contain it. v2 idea: regex-redact transcripts before persistence; out of scope here. Documented as Open Q.

### 6.4 Cross-device security note

The MVP runs on localhost. WebSocket has no auth. When a user wants cross-device:

1. They run wlwl-ass behind a reverse proxy (caddy / nginx) terminating TLS.
2. They set `settings.voice.auth_token` to a strong random string.
3. The website is configured with that token in its bearer header.
4. The server rejects any connection where the bearer is wrong or missing on non-localhost source.

We do not ship a TLS server inside wlwl-ass. The reverse-proxy approach is documented; making it foolproof is out of scope.

---

## 7. Latency budget (target)

For a "把会议改到四点" → "已修改" round trip, target:

| Stage                                                | Budget    | Notes                                               |
|------------------------------------------------------|-----------|-----------------------------------------------------|
| User stops speaking → VAD detects silence            | 800 ms    | Configurable; lowering increases false cuts         |
| Final STT request → result                           | 600 ms    | MiniMax Speech-to-Text typical                       |
| IntentClassifier round-trip                          | 800 ms    | One LLM call with constrained output                 |
| Worker dispatch + SQL update                         | 50 ms     | Local SQLite                                         |
| TTS first byte                                       | 500 ms    | MiniMax T2A streaming                                |
| TTS first byte → browser audio first frame          | 100 ms    | WebSocket + jitter buffer                            |
| **Total: VAD silence start → user hears first reply byte** | **~2.85 s** | Acceptable for voice agent; below the 4 s "feels broken" threshold |

If we hit ≥ 4 s the user perceives stalls. The orchestrator emits `{type:"state","value":"PROCESSING"}` immediately after VAD trip so the UI can show a "thinking" indicator within 50 ms.

---

## 8. Failure & degradation

| Failure                          | Behavior                                                          |
|----------------------------------|-------------------------------------------------------------------|
| STT worker offline               | Orchestrator emits `error{code:no_stt_worker, fatal:true}`. Browser shows "voice unavailable, please check workers". |
| TTS worker offline               | Replies in text only via `transcript.final{role:"agent"}`; browser optionally TTS-via-browser fallback (out of scope). |
| MiniMax API down                 | Worker raises retryable error; kernel router moves on to next `voice.stt.v1` candidate (if any), else returns `no_stt_worker`. |
| Intent classifier returns garbage| Falls back to `_chat` (raw chat agent). User feels a slight delay but conversation continues. |
| WebSocket drops mid-conversation | Server keeps session alive for 30 s; browser may reconnect with `?session=<uuid>` and resume. |
| Disk full when writing recording | Audio capture is dropped silently; transcript continues. Audit event emitted. |
| Calendar / inspiration DB locked | Worker returns `retryable=true`; kernel retries with backoff (max 3); else surfaces error to TTS. |
| User says exit phrase mid-TTS    | TTS finishes its current sentence, then state → IDLE.            |

---

## 9. Phase plan

| Phase | Scope                                                                | Touches                                                  |
|-------|----------------------------------------------------------------------|----------------------------------------------------------|
| **P0** | This ADR + companion PRD.                                          | docs/adr/0010-*.md, docs/specs/voice-website-prd.md     |
| **P1** | `VoiceIngressEndpoint` + `ConversationOrchestrator` skeleton; mock STT/TTS workers (echo); two-utterance smoke test from a Python client. | `launcher/voice_ws.py`, `voice/orchestrator.py`, tests   |
| **P2** | MiniMax STT + TTS workers under ADR-0009 plugin model. Streaming responses (`Worker.invoke_stream`). | `llmcore/workers/minimax_stt_worker.py`, `..._tts_worker.py`, ADR-0009 §17 Open Q1 closed |
| **P3** | IntentClassifier + integration with `kernel.dispatch`. Fall-through to chat agent. | `voice/intent.py`                                         |
| **P4** | calendar_worker + inspiration_worker (in-tree, SQLite). Fuzzy-match logic + clarification flow. | `llmcore/workers/calendar_worker.py`, `..._inspiration_worker.py` |
| **P5** | SessionStorage + retention + user-facing forget commands.            | `voice/storage.py`, CLI `python -m launcher.voice`       |
| **P6** | Cross-device readiness: bearer token validation, config docs for reverse proxy. | `launcher/voice_ws.py`, `docs/CONFIG.md`                 |
| **P7+** | Future workers via the same plugin path: weather, todo, smart-home, … | follow ADR-0009 §13                                      |

P1–P5 deliver a working MVP on the operator's laptop. P6 unlocks "agent on home server, browser on phone".

---

## 10. Alternatives considered

### 10.1 Browser-side wake word (Picovoice Porcupine / TFLite WASM)

- ✅ Saves continuous bandwidth.
- ❌ Custom wake phrase ("我对三体世界说话") needs a custom-trained model — Porcupine free tier doesn't support arbitrary phrases. Paying or self-training defeats the simplicity goal.
- ❌ Updating the wake phrase requires a model rebuild + browser update.
- **Rejected for MVP.** Server-side STT pattern-match is good enough on localhost; can revisit when we go cross-device and bandwidth matters.

### 10.2 WebRTC instead of WebSocket

- ✅ Designed for real-time audio; better jitter handling.
- ❌ Requires a STUN/TURN setup; complex on localhost.
- ❌ Most browsers support WS audio fine for our latency budget.
- **Rejected.** Keep the simpler stack.

### 10.3 STT/TTS in-process (faster-whisper + Coqui TTS)

- ✅ No external API; better privacy.
- ❌ Bigger memory footprint; some platforms (especially Windows ARM, low-RAM laptops) struggle.
- ❌ Voice quality below MiniMax for Chinese.
- **Deferred.** Plug-in architecture (§4.5) means a `whisper_local_stt_worker` is a future drop-in without changing this ADR.

### 10.4 Hard-coded calendar / inspiration logic in orchestrator

- ✅ Faster MVP; no kernel.dispatch overhead.
- ❌ Adds tight coupling; a third worker means modifying orchestrator.
- ❌ Wastes the entire ADR-0008/0009 investment.
- **Rejected.** The whole point is extensibility.

---

## 11. Consequences

### 11.1 Pros

- ✅ **One way to add a new voice intent.** Drop a worker exposing a capability + describe it in the registry → IntentClassifier picks it up.
- ✅ **Zero website rebuilds for new agents.** Browser sees only TTS/STT pass-through; intent routing is server-side.
- ✅ **Provider swap is config.** MiniMax → Whisper local: change `routing_priority` in two configs.
- ✅ **Sessions are auditable.** Every voice session leaves a complete trace.
- ✅ **MVP pathway is cheap.** P1–P3 are roughly 1.5–2 weeks of one-engineer work.

### 11.2 Cons

- ⚠️ **One new runtime dep (`websockets`).** First non-stdlib server-side dep in the launcher path. Documented in `pyproject.toml [project.optional-dependencies] voice`.
- ⚠️ **Stateful workers.** calendar/inspiration workers own SQLite handles; they need careful concurrency (per-DB-path lock). Documented in their factory.
- ⚠️ **Streaming workers extend ADR-0009.** Adds `Worker.invoke_stream` as the answer to ADR-0009 Open Q1 — small surface growth in the contract.
- ⚠️ **Audio retention is on by default (30 days).** Users who don't want this need to set `max_recording_age_days=0`. Documented prominently in onboarding.
- ⚠️ **Wake-word relies on STT being up.** If MiniMax is down, no wake word works either. `error{code:no_stt_worker}` mitigates UX, but the system is voice-deaf in that state.

---

## 12. Open questions

1. **Streaming workers in ADR-0009.** This ADR commits to `Worker.invoke_stream` as the streaming variant. Is that signature final, or do we want server-sent-event-style framing? Decided: AsyncIterator yielding `InvokeResponse` chunks with `result.is_final: bool`. Codify in ADR-0009 P2 implementation.
2. **Multi-utterance dispatch.** A user saying "把会议改到四点，再记一句要研究 Tauri" packs two intents. MVP picks **one** (classifier returns the higher-confidence one); the unhandled half is voiced back with "我先做了 X，你说的 Y 要我也做吗?". v2 adds parallel dispatch.
3. **Voice authentication.** Anyone holding the mic on a localhost machine can talk to the agent. If we go cross-device, do we need speaker verification? Out of scope for now; documented.
4. **Calendar conflicts.** If "明天 3 点开会" but there's already a 2:30–4:00 event, do we warn? Yes, calendar_worker emits a warning in the response; orchestrator passes it to TTS. Simple confirmation flow added to P4.
5. **Inspiration "记录什么" disambiguation.** "记一下要研究 Tauri" — does the worker store "要研究 Tauri" or "记一下要研究 Tauri"? Strip a small list of leading verbs (记 / 记一下 / 记录 / 帮我记 / …) before save. Acceptable for MVP; not perfect.
6. **Concurrent voice + Feishu.** A user mid-voice-conversation receives a Feishu chat asking the agent to do something. Today the kernel is single-event-loop; concurrent calls to calendar_worker need its per-DB lock. Documented; not a problem for MVP since the lock is brief.

---

## 13. Status notes

This ADR is **Proposed**. No code, no schema migrations, no breaking changes to existing modules. Implementation begins (if approved) at P1 — `voice_ws.py` skeleton plus mock STT/TTS for smoke-testing, ~600 lines + tests. Each phase is independently revertable.

Cross-references:

- ADR-0008 §4.2 — kernel routing; voice intent classifier produces the `required_capabilities` the kernel routes by.
- ADR-0009 §3 — `Worker` Protocol; STT/TTS/calendar/inspiration are all workers.
- ADR-0009 §4 — discovery; calendar / inspiration are in-tree (channel 1); future calendar-via-Google is a pip plugin (channel 3).
- ADR-0009 §17 Open Q1 — streaming workers; this ADR pins down the answer.
- `docs/specs/voice-website-prd.md` — the contract the website team builds against.
- ATTRIBUTION.md — voice-recognition prior art (Porcupine, Whisper.cpp) referenced for design choices but no code borrowed; no new entry needed yet.
