# Voice Website — Product Requirements Document (PRD)

> **Audience:** the engineer / vendor / friend who is going to **build the
> website** as a separate codebase from wlwl-ass.
> **Companion design (server side):** [ADR-0010 Voice Conversation Loop](../adr/0010-voice-conversation-loop.md).
> **Status:** Draft v1.0 — 2026-05-06.
> **Owner:** wlwl-ass core team.

---

## 0. TL;DR for the implementer

You're building **a single-page web app** that:

1. Asks for microphone permission.
2. Streams the user's voice to a WebSocket on the wlwl-ass backend.
3. Plays the audio replies the backend streams back.
4. Shows a clear visual state machine — *idle / armed / listening / processing / responding*.
5. Lets the user pause, resume, and delete recordings.

You're **not** building speech-to-text, intent recognition, calendar logic, or any AI. The wlwl-ass backend does all of that. Your job is correct cross-platform audio I/O, a calm UI, and rock-solid handling of mic permission edge cases.

Estimated effort: **~1 sprint for MVP** (one engineer, ~1.5 weeks). Tech stack is your call as long as it meets §4 functional requirements; the team's reference is React 19 + Vite + TypeScript (matches `gui/` in the wlwl-ass repo).

---

## 1. Background

### 1.1 What wlwl-ass is

wlwl-ass is a self-hosted personal AI agent that runs on the user's own machine. It uses an extensible **kernel + worker** architecture (internal docs: ADR-0008, ADR-0009). One of the agents the user can invoke is a **voice-conversation agent** that listens for a wake phrase, holds a multi-turn conversation, and routes commands to internal "skill workers" — currently calendar management and inspiration capture, with more planned.

### 1.2 Why a separate website

The agent's existing UIs are a Tauri desktop app (`gui/`) and chat bots (Feishu / WeChat / Telegram / etc.). None of them is well suited for **always-listening voice** on the user's primary computer or phone — those need a browser-context mic-permission flow and continuous audio streaming.

A web page is the right shape:
- Loads in any browser, no install.
- Standard `getUserMedia` permission flow.
- Works on iOS Safari and Android Chrome out of the box.
- Easy to leave open in a tab as a "voice console".

### 1.3 The example user moment

Anchor the design around this concrete scenario. Test against it.

> User opens the website on her laptop. The site shows a soft-pulsing dot with the label "聆听中…". She glances away and continues writing a doc.
>
> A minute later she says aloud: **"我对三体世界说话。"**
>
> The dot turns green, the site says **"请讲"** in a synthesized voice, and the label changes to "请说指令". She says: **"明天下午三点提醒我去拿快递，再记一句：研究一下 Tauri 2 的更新机制。"**
>
> The dot pulses thoughtfully (PROCESSING state), then says: **"好的，明天下午三点提醒拿快递，已经记下来了。灵感也存进去了。"**
>
> Five minutes later she says **"拜拜。"** The site says **"嗯，再叫我。"** and goes back to soft-pulsing.

### 1.4 Non-goals for the website

- ❌ The site does **not** run any AI / NLP / TTS / ASR. (Backend handles all.)
- ❌ The site does **not** persist anything beyond user preferences (mic device choice, theme).
- ❌ The site does **not** authenticate users in MVP (it's bound to localhost).
- ❌ The site does **not** ship native mobile apps. Mobile = mobile browser.

---

## 2. User personas

### 2.1 Primary: Solo Owner

Power user running wlwl-ass on her own laptop. Mid 20s–50s, technical, tolerates "open localhost in browser" but appreciates polish. Will use this site daily on the same laptop where the agent runs. Primary device: desktop browser.

### 2.2 Secondary: Solo Owner on Mobile

Same person, walking between rooms with her phone. Needs the site to load on iOS Safari / Android Chrome and connect back to her own laptop over the home Wi-Fi. (Out of MVP scope; interface ready for it — see §6.)

### 2.3 Out-of-scope: Multi-tenant SaaS

Strangers using a shared deployment. Will need full auth, session isolation, billing, etc. Not built here.

---

## 3. End-to-end user journey

```
┌───────────────────────────────────────────────────────────────────┐
│ 1. User opens https://localhost:<port>/voice in browser            │
│    Site shows: "需要麦克风权限" + a friendly "授权" button           │
└───────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ 2. User clicks 授权.  Browser shows native mic permission prompt.  │
│    User accepts.                                                   │
└───────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ 3. Site connects WebSocket to wlwl-ass.  Status: IDLE              │
│    Visual: soft pulsing dot, label "聆听中…"                        │
│    Side note: "试试说『我对三体世界说话』"                            │
└───────────────────────────────────────────────────────────────────┘
                                │
                                │ user says wake phrase
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ 4. Server detects wake.  Sends:                                    │
│    {"type":"state","value":"ARMED"}                                │
│    {"type":"transcript.final","role":"user","text":"我对三体世界..."}│
│    binary TTS audio chunks for "请讲"                               │
│    {"type":"state","value":"LISTENING"} (after TTS done)            │
│                                                                    │
│    Site: dot turns green, label "请讲". Plays TTS. Then           │
│           "请说指令".  Mic visualizer becomes active.                │
└───────────────────────────────────────────────────────────────────┘
                                │
                                │ user speaks command
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ 5. Server sends partial transcripts as user speaks:                │
│    {"type":"transcript.partial","text":"明天下午..."}                │
│    Site shows ghosted text live.                                   │
│                                                                    │
│    User stops. VAD silence triggers PROCESSING.                    │
│    {"type":"state","value":"PROCESSING"}                           │
│    {"type":"transcript.final","role":"user","text":"明天下午三点..."}│
│    Site: dot pulses warm-yellow, label "思考中…".                   │
└───────────────────────────────────────────────────────────────────┘
                                │
                                │ server intent-classifies + dispatches
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ 6. Server emits diagnostic events (optional UI):                    │
│    {"type":"intent","intent":"calendar.create_event",...}          │
│    {"type":"dispatch","worker":"calendar","cap":"..."}             │
│    {"type":"dispatch_result","ok":true,"summary":"已添加"}          │
│                                                                    │
│    Then begins TTS reply:                                           │
│    {"type":"state","value":"RESPONDING"}                           │
│    {"type":"tts.start","turn_id":"..."}                            │
│    binary TTS audio chunks                                          │
│    {"type":"transcript.final","role":"agent","text":"好的，..."}    │
│    {"type":"tts.end","turn_id":"..."}                              │
│    {"type":"state","value":"LISTENING"}                            │
│                                                                    │
│    Site: dot turns blue, label "回复中…", plays audio.              │
│           Then back to LISTENING for next turn.                    │
└───────────────────────────────────────────────────────────────────┘
                                │
                                │ user says exit phrase or 60s idle
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ 7. Server sends:                                                    │
│    {"type":"state","value":"IDLE"}                                 │
│    + a brief "嗯，再叫我" TTS                                       │
│                                                                    │
│    Site: returns to soft-pulsing dot, label "聆听中…".              │
│    Above the dot, a collapsed transcript of the just-finished      │
│    conversation, with a small 🗑 button next to it                 │
│    ("删除这段录音").                                                 │
└───────────────────────────────────────────────────────────────────┘
```

---

## 4. Functional requirements

### 4.1 FR-1 — Microphone permission

**Must:**

- F-1.1 First load: show a permission explainer ("我们需要麦克风权限以便聆听你说话；录音保存在你自己的电脑上，30 天后自动删除") **before** triggering the native browser prompt.
- F-1.2 An explicit "授权" button calls `getUserMedia({audio: true, video: false})`.
- F-1.3 If user denies: show a "怎么办" panel with instructions per browser (Chrome / Firefox / Safari / Edge / iOS Safari / Android Chrome — see §5.4 matrix).
- F-1.4 If user grants but later revokes (browser settings): detect via `MediaStreamTrack.onended` / permission API, return to the "怎么办" panel.
- F-1.5 If user has multiple input devices, allow choosing one (Settings panel).

### 4.2 FR-2 — Audio capture

**Must:**

- F-2.1 Sample rate **16 kHz mono**. If the device's mic doesn't natively support that, downsample in the browser (AudioWorklet or `AudioContext({sampleRate:16000})`).
- F-2.2 Encoding: **Opus**, 20 ms frames, target bitrate 24 kbps. Use the browser's `MediaRecorder` with `audio/webm;codecs=opus` *or* `MediaRecorder` with `audio/ogg;codecs=opus`. Decoder agnosticism: the server tells you what's accepted in `{type:"ready", audio_format:{...}}`.
- F-2.3 Stream audio to the WebSocket as **binary frames** as soon as encoded. Don't buffer more than 200 ms client-side.
- F-2.4 Pause when the server's state is `ARMED`, `PROCESSING`, or `RESPONDING`. (Discard locally; do not send.) This avoids the user's voice being captured during the agent's TTS.
- F-2.5 Resume capture on `LISTENING` and on `IDLE`.

**Should:**

- F-2.6 Show a live waveform / VU meter so the user knows the mic is hearing them.

### 4.3 FR-3 — WebSocket connection

**Must:**

- F-3.1 Endpoint URL is configurable (env variable / settings panel; default `ws://localhost:<port>/api/voice/session`, where `<port>` is read from `window.__WLWL_API_PORT__` or a fetched `/api/version` endpoint).
- F-3.2 Optional `Authorization: Bearer <token>` header (for cross-device). Read from settings panel.
- F-3.3 First frame after open: send `{type:"hello", client:"web@<version>", audio_format:{codec:"opus",sample_rate:16000,channels:1}}`.
- F-3.4 Wait for `{type:"ready", session_id, tts_format}` before starting audio. If `{type:"error", fatal:true}` arrives instead, show the error and stop.
- F-3.5 On disconnect (network drop), auto-reconnect with exponential backoff (250 ms → 500 ms → 1 s → 2 s → 5 s, cap 5 s). On reconnect, append `?session=<previous_session_id>` to attempt resume.
- F-3.6 On manual stop / page unload: send `{type:"goodbye"}` then close.

### 4.4 FR-4 — Visual state machine

**Must:** show one of five states, **always visible above the fold**, with both color and a text label:

| Server state    | Color               | Label (zh)     | Label (en)         | Visual idea                              |
|-----------------|---------------------|-----------------|--------------------|-------------------------------------------|
| `IDLE`          | grey-blue, soft pulse | 聆听中…       | Listening for wake | Slow pulse circle (1.5 s period)          |
| `ARMED`         | green, brisk pulse | 已唤醒，请讲   | Activated, speak   | Faster pulse (700 ms)                     |
| `LISTENING`     | green-bright, waveform | 录音中        | Recording          | Live mic waveform                         |
| `PROCESSING`    | warm yellow         | 思考中…        | Thinking           | Spinner / breathing dot                   |
| `RESPONDING`    | blue                | 回复中…        | Responding         | Visualize TTS audio                       |

The transitions are smooth (CSS transitions ≥ 200 ms). Don't flicker.

### 4.5 FR-5 — Live transcript

**Must:**

- F-5.1 Render `transcript.partial` text in a "ghosted" style (low opacity / italic) below the state indicator. Update in place as more partials arrive.
- F-5.2 On `transcript.final` with `role:"user"`, replace the ghost with the final text in normal style.
- F-5.3 On `transcript.final` with `role:"agent"`, render in a different bubble color/alignment than user messages.
- F-5.4 Auto-scroll to keep the latest message in view.

**Should:**

- F-5.5 Allow the user to **copy a turn's text** with a click.
- F-5.6 Show the **detected intent** as a small subtle tag below the user turn (e.g. `calendar.update_event`). Helps debugging and trust.

### 4.6 FR-6 — TTS playback

**Must:**

- F-6.1 On `{type:"tts.start"}`, prepare a playback queue.
- F-6.2 Decode incoming binary frames (Opus chunks per the server's `tts_format`). Play seamlessly via `AudioContext` and a small jitter buffer (~100 ms).
- F-6.3 On `{type:"tts.end"}`, allow the queue to drain.
- F-6.4 Volume slider in settings.

**Should:**

- F-6.5 If decoding Opus is hard (Safari edge cases), allow the server to be configured to send WAV; query and accept whatever `tts_format` the server announces.
- F-6.6 Don't drop the user's mic capture during TTS — the server already discards it (FR-2.4 mirror), but a "barge-in" UX where user speaking interrupts TTS is nice-to-have v2.

### 4.7 FR-7 — User controls

**Must:**

- F-7.1 **Pause** button — temporarily mute the mic from sending. Server is told via `{type:"pause"}`. Visual: replaces state indicator with a "已暂停" badge.
- F-7.2 **Resume** button — sends `{type:"resume"}`.
- F-7.3 **End conversation** button — sends `{type:"goodbye"}` and shows a "对话已结束" toast. WebSocket stays open for the next conversation; state goes IDLE.
- F-7.4 **Delete this conversation** button (next to the just-finished transcript in IDLE state) — sends `{type:"forget_session"}` if the conversation is current, or HTTP `DELETE /api/voice/sessions/<uuid>` if it's a past session in the history.

**Should:**

- F-7.5 **History** view: list past conversations (`GET /api/voice/sessions`) with timestamp and one-line summary. Each row has a 🗑 button.
- F-7.6 Settings panel: bearer token, server URL, mic device choice, TTS volume, theme.

### 4.8 FR-8 — Error states

For each error type from the server `{type:"error", code:"..."}`, show a clear toast / banner:

| Error code                  | UI message                                                  | Recovery                          |
|-----------------------------|--------------------------------------------------------------|-----------------------------------|
| `auth_required`             | "需要授权令牌，请在设置中填入 Bearer token"                   | Open Settings panel                |
| `audio_format_unsupported`  | "音频格式不支持，正在尝试备用格式…"                           | Fall back & reconnect              |
| `rate_limited`              | "请求过于频繁，稍候片刻"                                     | Wait + auto-retry                  |
| `no_stt_worker`             | "语音识别服务暂时不可用，请检查后端配置"                       | Show "刷新" + link to docs         |
| `no_tts_worker`             | "语音合成不可用，回复将以文字形式显示"                         | Hide TTS-only UI bits              |
| `internal_error`            | "服务出错。"" + detail toggle                                  | Auto-reconnect                     |

Network-level errors (WS close code 1006 etc.) → "连接断开，重连中…" with progress dots.

### 4.9 FR-9 — Cross-device readiness (interface only, not full impl)

**Must (interface only):**

- F-9.1 The server URL is configurable, not hard-coded.
- F-9.2 The bearer token field exists in Settings.
- F-9.3 The connection uses `wss://` when the URL has https origin; `ws://` for http.

**Need not in MVP:** TLS certs, a settings UI for "share with phone" — the user runs a reverse proxy themselves; the website just needs to *work* once they point it at the right URL.

---

## 5. Non-functional requirements

### 5.1 Performance budgets

| Metric                                                | Target          |
|-------------------------------------------------------|-----------------|
| Time to interactive (cold load)                       | ≤ 1.5 s on localhost |
| Time from page load → mic capture starts (after consent) | ≤ 600 ms        |
| Audio chunk RTT (browser → server → echo back, on localhost) | ≤ 80 ms     |
| Visible state transition latency                      | ≤ 200 ms        |
| TTS gap (last audio chunk played → mic re-armed)      | ≤ 150 ms        |

### 5.2 Reliability

- Auto-reconnect on transient network drop (FR-3.5).
- No data loss on a 5 s disconnect: queue local audio for up to 5 s if the WS is in retry; on resume, send. If reconnect succeeds within 30 s, resume the same session.
- Browser refresh ends the current conversation gracefully (sends `{goodbye}` in `beforeunload`).

### 5.3 Accessibility

- All state info must be conveyed by **text label**, not color alone (color-blind users).
- Keyboard shortcuts: `Space` toggles pause/resume, `Esc` ends conversation.
- Screen reader: announce state transitions via `aria-live="polite"`.
- TTS playback obeys system volume; provide a per-site volume slider.
- Visual indicator when the agent is speaking, since hearing-impaired users won't catch the audio.

### 5.4 Browser & device support matrix

| Platform                  | Browser           | Status                          |
|---------------------------|-------------------|---------------------------------|
| macOS / Windows / Linux   | Chrome ≥ 120      | **Full support**                |
| macOS / Windows / Linux   | Edge ≥ 120        | Full support                    |
| macOS / Windows / Linux   | Firefox ≥ 121     | Full support                    |
| macOS                     | Safari ≥ 17       | Full support                    |
| iOS 16+                   | Safari            | Full support; user-gesture required to start mic (handled by FR-1.2 explicit button) |
| Android 12+               | Chrome            | Full support                    |
| Older browsers            | —                 | Show "请升级浏览器" banner       |

Test specifically:
- iOS Safari mic permission revocation behavior (different from desktop)
- Android Chrome backgrounded tab keeps WS alive (per spec it does; verify)

### 5.5 Code-level expectations

- **Stack:** team's reference is **React 19 + TypeScript + Vite + Tailwind**, mirroring `gui/` in the wlwl-ass repo. Other stacks are accepted **only if they meet all FRs and NFRs**; document the tradeoffs in the PR.
- **Test coverage:** every FR has at least one Playwright e2e test against a mock WebSocket. ≥ 70% line coverage on UI logic.
- **Lint / format:** ESLint strict + Prettier. No warnings in CI.
- **No external CDN:** everything self-hostable. The site must work offline once loaded (modulo the WebSocket).
- **Bundle size:** ≤ 500 KB gzipped for the JS+CSS bundle. (Don't ship `webrtcvad`, don't ship a full ML model, don't ship moment.js.)

---

## 6. API contract — full spec

This is the **frozen interface** between the website and wlwl-ass. The server side commits to honoring it; the website builds against it.

### 6.1 WebSocket endpoint

```
ws://<host>:<port>/api/voice/session
       ?session=<uuid>            (optional; resume)
   Headers (optional):
       Authorization: Bearer <token>
```

`<host>:<port>` is whatever the wlwl-ass launcher prints on boot. The website should fetch `GET <api-base>/api/version` first to discover the WebSocket URL, OR read it from `window.__WLWL_API_BASE__` injected by Tauri/launcher.

### 6.2 Frame types

WebSocket carries two interleaved kinds of frames in the same connection:

- **Binary frames = audio.**
- **Text frames = JSON envelopes.**

### 6.3 Browser → Server (text JSON)

| Frame type            | Schema                                                                  | When                              |
|-----------------------|-------------------------------------------------------------------------|-----------------------------------|
| `hello`               | `{type, client, audio_format:{codec, sample_rate, channels}}`           | First frame after open             |
| `pause`               | `{type}`                                                                | User clicks Pause                  |
| `resume`              | `{type}`                                                                | User clicks Resume                 |
| `forget_session`      | `{type}`                                                                | User clicks "delete this conversation" mid-session |
| `goodbye`             | `{type}`                                                                | User clicks End / page unload      |

### 6.4 Browser → Server (binary)

Audio frames. Format declared in `hello`. Server expects:

- Opus 16 kHz mono, 20 ms frames preferred.
- Webm-OPUS or OGG-OPUS container both accepted; server unwraps.
- Send when state is `IDLE` (for wake-word listening) or `LISTENING` (for command capture).
- Discard locally during `ARMED`, `PROCESSING`, `RESPONDING`.

### 6.5 Server → Browser (text JSON)

| Type                  | Schema                                                                  | Meaning                          |
|-----------------------|-------------------------------------------------------------------------|----------------------------------|
| `ready`               | `{type, session_id, tts_format:{codec,sample_rate,channels}}`           | Handshake complete; start audio  |
| `state`               | `{type, value: "IDLE"\|"ARMED"\|"LISTENING"\|"PROCESSING"\|"RESPONDING"}` | UI state change                  |
| `transcript.partial`  | `{type, text, conf}`                                                    | Live STT partial; replace last partial in UI |
| `transcript.final`    | `{type, text, conf, role:"user"\|"agent", turn_id}`                     | A finalized turn                 |
| `intent`              | `{type, intent, confidence, args}`                                      | Optional diagnostic; show as tag  |
| `dispatch`            | `{type, worker, cap, call_id}`                                          | Optional diagnostic              |
| `dispatch_result`     | `{type, call_id, ok, summary?, error?}`                                 | Optional diagnostic              |
| `tts.start`           | `{type, turn_id}`                                                       | Binary audio frames follow       |
| `tts.end`             | `{type, turn_id}`                                                       | Audio for this turn done         |
| `error`               | `{type, code, message, fatal:bool}`                                     | See §4.8 error code table        |

### 6.6 Server → Browser (binary)

TTS audio chunks. Format announced in `ready.tts_format`. 20 ms Opus frames, prefixed with **4-byte little-endian seq number** so out-of-order frames can be reordered (rare but cheap insurance).

```
+--------+-------------------+
|  seq   |    opus payload   |
| (u32le)|     (≤ 480 bytes) |
+--------+-------------------+
```

### 6.7 Worked-out example: one full conversation turn

```
Browser → ws.open()
Browser → text:    {"type":"hello","client":"web@1.0",
                    "audio_format":{"codec":"opus","sample_rate":16000,"channels":1}}
Server  → text:    {"type":"ready","session_id":"a83f-…",
                    "tts_format":{"codec":"opus","sample_rate":24000,"channels":1}}
Server  → text:    {"type":"state","value":"IDLE"}
Browser → binary:  <audio frames flowing continuously…>
Server  → text:    {"type":"transcript.partial","text":"我对三体","conf":0.55}
Server  → text:    {"type":"transcript.partial","text":"我对三体世界","conf":0.71}
Server  → text:    {"type":"transcript.final","text":"我对三体世界说话","conf":0.94,"role":"user","turn_id":"t1"}
Server  → text:    {"type":"state","value":"ARMED"}
Server  → text:    {"type":"tts.start","turn_id":"t2"}
Server  → binary:  <Opus chunks for "请讲">
Server  → text:    {"type":"tts.end","turn_id":"t2"}
Server  → text:    {"type":"state","value":"LISTENING"}
Browser → binary:  <user speaking command…>
Server  → text:    {"type":"transcript.partial","text":"明天下午…","conf":0.66}
Server  → text:    {"type":"transcript.final","text":"明天下午三点提醒我去拿快递","conf":0.91,"role":"user","turn_id":"t3"}
Server  → text:    {"type":"state","value":"PROCESSING"}
Server  → text:    {"type":"intent","intent":"calendar.create_event","confidence":0.88,"args":{"title":"拿快递","start_at":"2026-05-07T15:00:00+08:00"}}
Server  → text:    {"type":"dispatch","worker":"calendar","cap":"calendar.create_event.v1","call_id":"c1"}
Server  → text:    {"type":"dispatch_result","call_id":"c1","ok":true,"summary":"已添加"}
Server  → text:    {"type":"tts.start","turn_id":"t4"}
Server  → binary:  <Opus chunks for "好的，明天下午三点提醒拿快递。">
Server  → text:    {"type":"tts.end","turn_id":"t4"}
Server  → text:    {"type":"state","value":"LISTENING"}
…(more turns)…
Server  → text:    {"type":"transcript.final","text":"拜拜","conf":0.95,"role":"user","turn_id":"t9"}
Server  → text:    {"type":"tts.start","turn_id":"t10"}
Server  → binary:  <"嗯，再叫我">
Server  → text:    {"type":"tts.end","turn_id":"t10"}
Server  → text:    {"type":"state","value":"IDLE"}
```

### 6.8 HTTP endpoints (auxiliary)

The website also calls these REST endpoints:

| Method | Path                                | Purpose                           |
|--------|-------------------------------------|-----------------------------------|
| GET    | `/api/version`                      | Discover server version + WS URL   |
| GET    | `/api/voice/sessions`               | List past sessions (history view)  |
| GET    | `/api/voice/sessions/<uuid>/manifest` | Session metadata + transcript    |
| DELETE | `/api/voice/sessions/<uuid>`        | Delete a past session              |

Auth identical to WebSocket: optional bearer for cross-device.

---

## 7. UX requirements

### 7.1 First-load experience

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│           欢迎，wlwl-ass 语音控制台                   │
│                                                     │
│   说话前需要授权麦克风。录音和文字保存在你自己的电脑上， │
│   30 天后自动删除。你也可以随时删除任何一段对话。      │
│                                                     │
│              ┌───────────────────┐                  │
│              │  授权麦克风          │                  │
│              └───────────────────┘                  │
│                                                     │
│              【设置】 【关于】                          │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 7.2 Active state (IDLE)

```
┌─────────────────────────────────────────────────────┐
│  ●     聆听中…                              [⏸  设置]  │
│  (soft pulse, grey-blue)                             │
│                                                     │
│  试试说： 我对三体世界说话                              │
│                                                     │
│ ─────────────────────────────────────────────────── │
│ [history button] 上一段对话： 12:34 · 添加 1 条日程   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 7.3 Active state (LISTENING / PROCESSING)

```
┌─────────────────────────────────────────────────────┐
│  ▮▮▮▮ ▮▮ ▮▮▮ ▮         录音中                          │
│  (live waveform, green)                              │
│                                                     │
│  > 明天下午三点提醒我去……                             │
│  (ghosted text, updating live)                       │
│                                                     │
│ ─────────────────────────────────────────────────── │
│   You │ 我对三体世界说话                                │
│  Agent│ 请讲                                          │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 7.4 RESPONDING state

State indicator changes color, agent's reply text appears as it's TTS'd, mic capture is paused (no waveform).

### 7.5 Error state — mic denied

```
┌─────────────────────────────────────────────────────┐
│   ⚠ 麦克风权限未授予                                    │
│                                                     │
│   你拒绝了麦克风访问。要恢复：                            │
│                                                     │
│   • Chrome / Edge：地址栏左侧锁图标 → 网站设置 →        │
│                    麦克风 → 允许                       │
│   • Safari：偏好设置 → 网站 → 麦克风                    │
│   • iOS：设置 → Safari → 麦克风                         │
│                                                     │
│              ┌─────────┐ ┌────────────┐             │
│              │  重试    │ │  使用文字模式  │             │
│              └─────────┘ └────────────┘             │
│                                                     │
└─────────────────────────────────────────────────────┘
```

(Text-mode is a stretch goal: type into a chat box, server still does intent + dispatch, no audio.)

### 7.6 Don't

- Don't make the user click "Start" every conversation. The site is always-on once granted.
- Don't show 30 diagnostic events by default. Hide intent / dispatch tags behind a "diagnostic mode" toggle.
- Don't rely on hover-only affordances on mobile.
- Don't auto-reload the page on transient errors — that breaks long contexts.

---

## 8. Privacy & security

### 8.1 Data flow disclosure

The site **must** include an `/about` or expandable info panel stating exactly:

- Mic audio is sent to `ws://localhost:<port>` running on this same computer.
- The server (wlwl-ass) sends each utterance to **MiniMax** for STT/TTS.
- Audio + transcripts are saved on this computer at `temp/voice_sessions/`.
- Default retention: 30 days. Configurable in wlwl-ass settings.
- The site itself stores no data beyond preferences in `localStorage`.

### 8.2 Local-only by default

In MVP, the server URL must default to `localhost`. The site refuses to connect to non-localhost URLs unless the user explicitly enables it in Settings (with a warning about TLS / bearer token).

### 8.3 Don't leak

- Never log audio frames to the browser console.
- Never send audio to a third-party analytics service.
- The site itself ships no analytics in MVP.

---

## 9. Out of scope (explicit non-goals)

- ❌ Speech recognition / synthesis in the browser.
- ❌ Authentication beyond bearer token.
- ❌ User accounts / cloud sync.
- ❌ Native mobile apps (mobile = mobile browser).
- ❌ Group / multi-user voice rooms.
- ❌ Recording playback UI in MVP. (Past audio files exist on disk; the user opens them with a system player if needed. v2 adds in-site playback.)
- ❌ Automatic translation of foreign-language utterances. (MiniMax does Chinese + English natively; users speaking other languages: not MVP.)
- ❌ Wake-word training UI. (Wake phrase configured in `~/.wlwl-ass/config.json`; technical user only.)

---

## 10. Acceptance criteria

The website ships when **all** of the following pass.

### 10.1 Smoke

- [ ] AC-1. Cold-load on Chrome/Edge/Firefox/Safari/iOS-Safari/Android-Chrome → site renders the welcome screen within 1.5 s on localhost.
- [ ] AC-2. Click "授权麦克风" → native prompt appears → grant → state IDLE within 600 ms.
- [ ] AC-3. Say wake phrase → state ARMED → "请讲" plays → state LISTENING within 3 s.

### 10.2 End-to-end (mocked server)

- [ ] AC-4. Full happy-path conversation per §3 journey, scripted server replies → all five states observed in correct order.
- [ ] AC-5. Two consecutive conversation turns in the same session → both round-trip cleanly.
- [ ] AC-6. Exit phrase recognized → state IDLE within 1 s after server sends it.
- [ ] AC-7. 60 s idle → state goes IDLE with farewell TTS.

### 10.3 Reliability

- [ ] AC-8. Network drop mid-conversation (server kills WS) → site shows "重连中…" → server comes back → conversation resumes via `?session=` resume.
- [ ] AC-9. User hits Pause → mic stops sending → server confirms with state hold → user hits Resume → flow continues.
- [ ] AC-10. Page unload (close tab) → server receives `goodbye`. (Verify via test backend.)
- [ ] AC-11. Five concurrent tabs (same browser) → only one is "active"; others show "已在另一标签页运行" guard. (Avoids dual-mic capture.)

### 10.4 Permissions

- [ ] AC-12. User denies mic → friendly "怎么办" panel with browser-specific instructions.
- [ ] AC-13. User revokes mic via browser settings while site is open → site detects within 5 s and shows the panel.
- [ ] AC-14. Multiple input devices → settings panel lists them; switching is hot.

### 10.5 UI quality

- [ ] AC-15. State color + text label both update on transition (color-blind compliance).
- [ ] AC-16. Live transcript ghosting → final → no jank.
- [ ] AC-17. TTS playback has zero stutters in a 5-second sustained reply.
- [ ] AC-18. Keyboard `Space` pauses; `Esc` ends. ARIA live region announces state changes.

### 10.6 Errors

- [ ] AC-19. Each error code from §4.8 displays the right message and recovery action.
- [ ] AC-20. WebSocket close codes 1000 (normal), 1006 (abnormal), 4401 (auth-required, custom) handled.

### 10.7 Privacy

- [ ] AC-21. About panel correctly describes data flow (§8.1).
- [ ] AC-22. "Delete this conversation" sends `forget_session` and the row disappears.
- [ ] AC-23. History view loads from `/api/voice/sessions`, deletion removes the row.

### 10.8 Performance

- [ ] AC-24. Bundle size ≤ 500 KB gzipped.
- [ ] AC-25. Time-to-mic-capture (after consent) ≤ 600 ms on a mid-tier laptop.
- [ ] AC-26. Visible state transition ≤ 200 ms.

### 10.9 Cross-device readiness

- [ ] AC-27. Settings panel exposes server URL + bearer token fields.
- [ ] AC-28. URL with `https://` scheme uses `wss://`; with `http://` uses `ws://`.

### 10.10 Code & testability

- [ ] AC-29. ≥ 70 % unit test coverage on UI logic.
- [ ] AC-30. Playwright e2e suite covers AC-3, AC-4, AC-8, AC-12, AC-22.

Total: 30 ACs.

---

## 11. Phase plan

### Phase A — MVP (target: 1.5 weeks, single engineer)

Scope: AC-1 through AC-23 (excluding AC-11 multi-tab guard and AC-13 deep permission detection).
Stack: React 19 + Vite + TS + Tailwind. State: Zustand. Mock-server for tests via `msw` + a small Python stub.

### Phase B — Polish (1 week)

Scope: remaining ACs, A11y audit, performance tuning to hit AC-24/25/26, UI animation polish.

### Phase C — Cross-device (deferred until backend P6 lands)

Scope: end-to-end test against `wss://` + bearer; mobile field testing on iOS & Android; reverse-proxy walkthrough doc.

### Phase D — Stretch (post-launch, prioritize on user feedback)

- Text-mode fallback (AC error path → typed input).
- Barge-in (interrupt TTS when user starts speaking).
- In-site audio playback of past sessions.
- Multi-user share (would require auth redesign — likely bumped to v2 PRD).

---

## 12. Open questions for the website implementer

1. **Mobile background tab behavior.** iOS Safari aggressively suspends background tabs. If the user backgrounds the tab, do we keep the WS alive (via WebRTC data channel? service worker?) or accept that the agent only listens when the tab is foreground? **Recommendation:** foreground-only in MVP; document the limitation.
2. **Multiple-tab guard.** Two tabs both capturing mic = chaos. Use `BroadcastChannel` to detect a sibling and show a "已在另一标签页运行" lock screen. Phase B.
3. **TTS playback gap on Safari.** WebKit's `AudioContext` has a longer warm-up than Chrome's. May need to keep an empty `OscillatorNode` running silently to prevent suspension. Test early.
4. **Encoding choice fallback.** If the browser can't produce Opus (rare; old Safari), fall back to `audio/wav` with PCM 16. Server must accept this in the `hello` handshake. Document the negotiation.
5. **i18n.** MVP is bilingual hardcoded (zh / en); copy lives in a single `i18n.ts` file. Other languages = drop a JSON; defer.

---

## 13. Glossary

- **VAD** — Voice Activity Detection. Decides when the user has stopped talking.
- **STT / ASR** — Speech-to-Text / Automatic Speech Recognition.
- **TTS** — Text-to-Speech.
- **Wake phrase** — short utterance that activates the agent. Default `我对三体世界说话`.
- **Exit phrase** — utterance that ends the conversation and returns to IDLE.
- **Worker** — a wlwl-ass plugin that handles a class of tasks (calendar, inspiration, …). The website doesn't see workers directly; it sees only the conversation surface.
- **Capability** — the granular ability a worker offers (e.g. `calendar.create_event.v1`). Same — invisible to the website.

---

## 14. Sign-off

When the engineer believes acceptance criteria are met, they:

1. Open a PR (in the website repo, not wlwl-ass).
2. Run the Playwright suite + post the results.
3. Demo the §1.3 anchor scenario in person or via screen recording.
4. Cross-link this PRD's commit hash in the PR description.

The wlwl-ass core team verifies by running the full §3 journey on their machine and ticks the AC checklist.

---

*Last updated: 2026-05-06 — draft v1.0 by wlwl-ass core (PM hat).*
