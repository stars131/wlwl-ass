# Voice Stack Runbook

End-to-end run instructions for the voice agent: the wlwl-ass Python backend
+ the standalone React website (`voice-website/`).

> **Status:** MVP. STT/TTS use mock workers by default — real MiniMax workers
> drop in by registering a different factory (see [§5](#5-swap-in-real-stttts)).

---

## 1. Topology recap

```
┌─────────────────┐  WebSocket (ws://… /api/voice/session)
│  voice-website  │ ◄──────────────────────────────────►  ┌─────────────────┐
│  (React + Vite) │                                       │   wlwl-ass      │
│  port 5173 dev  │                                       │  voice_ws.py    │
│  port 80 nginx  │                                       │  port 9700      │
└─────────────────┘                                       └────────┬────────┘
                                                                   │ kernel.dispatch
                                                                   ▼
                                                          ┌─────────────────┐
                                                          │  Kernel +       │
                                                          │  workers        │
                                                          │  (calendar,     │
                                                          │   inspiration,  │
                                                          │   mock_stt,     │
                                                          │   mock_tts)     │
                                                          └─────────────────┘
```

MVP target: **both run on the same machine (localhost)**. Cross-device works
via reverse proxy + bearer token (see [§6](#6-cross-device-deployment)).

---

## 2. Backend setup (Python)

### Prerequisites

- Python 3.10–3.13 (pyproject says `>=3.10,<3.14`; works on 3.14 too as of writing)
- pip
- ~50 MB disk for SQLite + temp/voice_sessions

### Install

```bash
cd /path/to/wlwl-ass
pip install -e ".[voice]"           # adds websockets + webrtcvad + soundfile + pypinyin
pip install -e ".[test]"            # pytest + pytest-asyncio (only if you want to run tests)
```

### Verify

```bash
python -m pytest tests/test_voice_stack.py -v
# expect: 20 passed

python -m pytest tests/test_voice_ws_integration.py -v
# expect: 1 passed (spins up the WS server in-process)

python -m launcher.cli_workers factories
# expect: 4 in-tree factories listed
```

### Run the voice WebSocket server

```bash
python -m launcher.voice_ws --host 127.0.0.1 --port 9700
```

The server auto-registers default workers (calendar, inspiration, mock_stt,
mock_tts). On start it prints:

```
INFO voice WS listening on ws://127.0.0.1:9700/api/voice/session
```

The server is **always-on**. Workers can be added / removed at runtime via
the CLI — see [§4](#4-managing-workers-while-server-runs).

---

## 3. Frontend setup (Website)

```bash
cd voice-website
npm install
```

### Dev mode

```bash
npm run dev
```

Open `http://127.0.0.1:5173` in any modern browser. Click 「授权麦克风」.
Say `我对三体世界说话` — the agent will reply `请讲`. Then say one of:

- `明天下午三点提醒我去拿快递`  → calendar event created
- `记一下：研究 Tauri 2 的更新机制`  → inspiration note saved
- `明天有什么安排` → reads back the events

End with `拜拜` (or wait 60 s).

### Production build

```bash
npm run build              # outputs dist/
npm run preview            # serves dist/ at http://0.0.0.0:4173 for sanity check
```

Bundle size: ~71 KB gzipped (well under the 500 KB cap from the PRD).

---

## 4. Managing workers while server runs

The `launcher.cli_workers` CLI hits the in-process kernel. (The MVP runs
the CLI in the **same** Python process as the WebSocket server only when
called from the same shell — for true cross-process control, the HTTP
endpoints from ADR-0009 P2 are needed; not in this milestone.)

```bash
# list
python -m launcher.cli_workers list

# add a worker
python -m launcher.cli_workers add calendar work-cal db_path=/tmp/cal.db

# remove
python -m launcher.cli_workers remove work-cal --drain-ms 5000

# silence (soft remove with TTL)
python -m launcher.cli_workers silence work-cal --reason "investigating" --ttl 600

# inspect
python -m launcher.cli_workers inspect work-cal

# tail audit topic
python -m launcher.cli_workers forum-tail --topic audit -n 30
```

---

## 5. Swap in real STT / TTS

The mock workers are stand-ins. To wire MiniMax (or any other provider):

1. Implement a factory under `llmcore/workers/minimax_stt_worker.py` exposing
   capability `voice.stt.v1`, hitting MiniMax's Speech-to-Text endpoint with
   the audio payload from `InvokeRequest.payload['audio']` (base64 Opus).
2. Same for `voice.tts.v1` with streaming via `Worker.invoke_stream`.
3. Register the factory in `llmcore/workers/__init__.py::iter_builtin_factories`.
4. Restart the server and:

   ```bash
   python -m launcher.cli_workers add minimax_stt minimax-stt apikey=sk-...
   python -m launcher.cli_workers remove stt          # drop the mock
   ```

The orchestrator picks the new worker automatically — no code change.

---

## 6. Cross-device deployment

Default MVP runs on localhost. To use the website from a phone over your home
network:

### 6.1 wlwl-ass behind a reverse proxy

```nginx
# /etc/nginx/sites-available/wlwl-ass
server {
    listen 443 ssl http2;
    server_name voice.your-home.example;
    ssl_certificate     /etc/letsencrypt/live/voice.your-home.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/voice.your-home.example/privkey.pem;

    location /api/voice/session {
        proxy_pass http://127.0.0.1:9700;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Caddy is even shorter:

```caddy
voice.your-home.example {
    reverse_proxy 127.0.0.1:9700
}
```

### 6.2 Enable bearer token on wlwl-ass

```bash
WLWL_VOICE_AUTH_TOKEN="$(openssl rand -hex 32)" \
python -m launcher.voice_ws --host 127.0.0.1 --port 9700 --auth-token "$WLWL_VOICE_AUTH_TOKEN"
```

(Or pass `--auth-token` directly. The token must be ≥ 16 chars; nothing
enforces that today, but reviewers should.)

### 6.3 Configure the website

In the website's Settings panel (⚙ icon top-right):

- **Server URL**: `wss://voice.your-home.example/api/voice/session`
- **Bearer Token**: paste the token

> ⚠️ Browsers don't allow custom WS headers via the standard API. The
> current implementation reserves the bearer field but the server doesn't
> use it through the browser path yet; until the subprotocol-tunnel approach
> lands, the only practical cross-device hardening is firewall + TLS + a
> hard-to-guess URL path.
>
> This is **explicitly tracked** as a follow-up. See ADR-0010 §6.4.

### 6.4 Deploy the website

Either:

- `voice-website/` Dockerfile → any container host (8080:80)
- `npm run build` → upload `dist/` to any web server

Both behind whatever TLS termination you already trust.

---

## 7. Linux server quick-start (the eventual deploy target)

Single-machine, both services on one box (Ubuntu 22.04+):

```bash
# system deps
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip nodejs npm caddy

# clone + bootstrap
git clone https://your-mirror.example/wlwl-ass.git
cd wlwl-ass
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[voice]"

# build the website
cd voice-website
npm install
npm run build
cp -r dist /var/www/wlwl-voice
cd ..

# systemd unit for the voice WS server
sudo tee /etc/systemd/system/wlwl-voice-ws.service <<'EOF'
[Unit]
Description=wlwl-ass voice WebSocket server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/wlwl-ass
ExecStart=/opt/wlwl-ass/.venv/bin/python -m launcher.voice_ws --host 127.0.0.1 --port 9700
Restart=on-failure
RestartSec=2
User=wlwl
Environment=WLWL_LOG_LEVEL=INFO

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wlwl-voice-ws

# Caddyfile — TLS for both the static site and the WS endpoint
sudo tee /etc/caddy/Caddyfile <<'EOF'
voice.your-home.example {
    encode zstd gzip
    @ws path /api/voice/session*
    handle @ws {
        reverse_proxy 127.0.0.1:9700
    }
    handle {
        root * /var/www/wlwl-voice
        try_files {path} /index.html
        file_server
    }
}
EOF

sudo systemctl reload caddy
```

DNS-A-record the hostname to the box's IP, hit it from any phone, grant mic,
say the wake phrase.

---

## 8. Health checks

```bash
# Are the workers up?
python -m launcher.cli_workers list

# Is anything in the audit topic?
python -m launcher.cli_workers forum-tail -n 50

# Voice sessions on disk:
ls -lh temp/voice_sessions/

# DB sizes
ls -lh temp/calendar.db temp/inspiration.db temp/forum_audit.jsonl
```

---

## 9. Common issues

| Symptom                                       | Cause                                     | Fix                                            |
|-----------------------------------------------|-------------------------------------------|-------------------------------------------------|
| Browser shows mic granted but no transcript   | wlwl-ass server not running               | Start `python -m launcher.voice_ws`             |
| No reply audio (transcript only)              | Browser can't decode `mock` codec         | Expected with mock TTS; swap in real TTS worker |
| Wake phrase never matches                     | Mock STT cycle exhausted; or audio too quiet | Provide more `canned_responses` to `mock_stt` worker, or restart server |
| `capability_unavailable` on dispatch          | Worker not loaded                         | `python -m launcher.cli_workers list`           |
| WebSocket 4401                                | Bearer token mismatch                     | Re-check Settings panel + server `--auth-token` |
| Test failures on Windows console              | Encoding mismatch on stdout               | Run with `PYTHONIOENCODING=utf-8`               |

---

## 10. Where the code lives

```
wlwl-ass/
├── llmcore/
│   ├── captoken.py           # HMAC-SHA256 capability tokens
│   ├── capabilities.py       # CapabilityRegistry + YAML loader
│   ├── capabilities.yaml     # 12 built-in capability declarations
│   ├── worker.py             # Worker / Factory / KernelHandle Protocols
│   ├── forum.py              # ForumBus (5 system topics)
│   ├── kernel.py             # Kernel singleton + dispatch + dispatch_stream
│   └── workers/
│       ├── __init__.py             # iter_builtin_factories
│       ├── llm_worker.py            # wraps BaseSession
│       ├── calendar_worker.py       # SQLite events
│       ├── inspiration_worker.py    # SQLite + FTS5 notes
│       └── mock_voice_worker.py     # mock STT + mock TTS (streaming)
├── voice/
│   ├── wake.py               # PhraseMatcher (+ optional pinyin)
│   ├── intent.py             # RuleIntentClassifier + LLMIntentClassifier
│   ├── storage.py            # VoiceSession + retention gc
│   └── orchestrator.py       # 5-state ConversationOrchestrator
├── launcher/
│   ├── voice_ws.py           # WebSocket server
│   └── cli_workers.py        # python -m launcher.cli_workers
├── tests/
│   ├── test_voice_stack.py         # 20 tests: contracts → workers → orchestrator
│   └── test_voice_ws_integration.py # 1 e2e test: real WS server + client
├── docs/
│   ├── adr/
│   │   ├── 0008-kernel-api-delegation-and-forum.md
│   │   ├── 0009-worker-plugin-interface.md
│   │   └── 0010-voice-conversation-loop.md
│   └── specs/
│       └── voice-website-prd.md     # the website's contract
└── voice-website/                    # standalone codebase
    ├── package.json
    ├── tsconfig.app.json
    ├── vite.config.ts
    ├── tailwind.config.js
    ├── Dockerfile
    ├── nginx.conf
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── state/conversation.ts
        ├── lib/
        │   ├── ws.ts
        │   ├── controller.ts
        │   └── audio/{capture,playback}.ts
        ├── components/
        │   ├── PermissionGate.tsx
        │   ├── StateIndicator.tsx
        │   ├── Transcript.tsx
        │   ├── Controls.tsx
        │   └── Settings.tsx
        └── styles/index.css
```
