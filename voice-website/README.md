# wlwl-ass Voice Website

Always-listening voice console for the **wlwl-ass** agent. Built per
[`docs/specs/voice-website-prd.md`](../docs/specs/voice-website-prd.md). Talks
to the backend WebSocket endpoint defined in
[`docs/adr/0010-voice-conversation-loop.md`](../docs/adr/0010-voice-conversation-loop.md).

> This is a **separate codebase** from the Python agent. It can be deployed
> on any static-file host. The MVP target is a Linux server running behind
> nginx (or any reverse proxy that terminates TLS).

## Stack

- **React 19** + **TypeScript (strict)** + **Vite 6**
- **Tailwind CSS** for layout
- **Zustand** for state — one store, narrow API
- **Native Web APIs** — `MediaRecorder`, `AudioContext`, `WebSocket`, `getUserMedia`
- No third-party SDKs, no analytics, no telemetry. ≤ 500 KB gzipped target.

## Run locally (dev)

```bash
cd voice-website
npm install
npm run dev          # http://127.0.0.1:5173
```

In another terminal, run the wlwl-ass voice WebSocket server:

```bash
cd ..
pip install -e ".[voice]"
python -m launcher.voice_ws --host 127.0.0.1 --port 9700
```

Open the dev URL, click 「授权麦克风」, then say **"我对三体世界说话"**.

## Build for production

```bash
npm run build        # → dist/
```

The `dist/` folder is plain static files. Serve with any web server.

## Deploy to a Linux server

Two common shapes:

### Option A — Docker + nginx (recommended)

```bash
docker build -t wlwl-ass-voice-web .
docker run -d --name voice-web --restart=always -p 8080:80 wlwl-ass-voice-web
```

Put a TLS-terminating reverse proxy in front (Caddy / Traefik / nginx-ssl /
Cloudflare). The website itself does **not** terminate TLS — the operator's
reverse proxy does.

### Option B — bare files + nginx

```bash
npm run build
scp -r dist/ user@your-host:/var/www/wlwl-voice/
# on host:
sudo cp /var/www/wlwl-voice/nginx.conf /etc/nginx/sites-available/wlwl-voice
sudo ln -s /etc/nginx/sites-available/wlwl-voice /etc/nginx/sites-enabled/
sudo systemctl reload nginx
```

## Configure server URL

The site connects to the wlwl-ass backend via WebSocket. Default is
`ws://127.0.0.1:9700/api/voice/session`. Change it in the in-site **设置**
panel (⚙ icon) — the choice is persisted in `localStorage`.

For cross-device deployments (mobile phone → home server), you need:

1. wlwl-ass running with a real public hostname behind your reverse proxy
2. Reverse proxy upgrades `wss://` → `ws://` to wlwl-ass
3. Set the **Bearer Token** field in Settings (must match `WLWL_VOICE_AUTH_TOKEN`
   on the server, see [docs/CONFIG.md](../docs/CONFIG.md))
4. Use the `wss://your-domain/api/voice/session` URL in the Settings panel

## Browser support

| Platform                  | Browser           | Status         |
|---------------------------|-------------------|----------------|
| macOS / Windows / Linux   | Chrome ≥ 120      | Full           |
| macOS / Windows / Linux   | Edge ≥ 120        | Full           |
| macOS / Windows / Linux   | Firefox ≥ 121     | Full           |
| macOS                     | Safari ≥ 17       | Full           |
| iOS 16+                   | Safari            | Full (gesture-required mic) |
| Android 12+               | Chrome            | Full           |

The site requires Web Audio + MediaRecorder + WebSocket. If you see an
unexpected failure, check the in-site error toast (bottom-right).

## Privacy

- Mic audio is sent to the WebSocket URL configured in Settings. Default is
  localhost.
- The wlwl-ass backend forwards audio to the configured STT/TTS provider
  (default MiniMax).
- The website itself ships no analytics, stores no audio, and uses
  `localStorage` only for user preferences (server URL, mic device, theme).
- See the in-site **设置** panel for a per-deployment data-flow disclosure.

## File map

```
voice-website/
  package.json            # deps
  tsconfig*.json          # TS strict
  vite.config.ts
  tailwind.config.js
  postcss.config.js
  Dockerfile              # → nginx-served container
  nginx.conf              # SPA fallback + cache headers
  index.html              # HTML shell
  src/
    main.tsx              # React entry
    App.tsx               # top-level layout
    state/
      conversation.ts     # Zustand store (one)
    lib/
      ws.ts               # VoiceSocket — typed WebSocket client
      controller.ts       # Capture+Socket+Playback orchestrator
      audio/
        capture.ts        # MediaRecorder-based mic
        playback.ts       # AudioContext-based TTS playback
    components/
      PermissionGate.tsx  # mic permission flow
      StateIndicator.tsx  # 5-state visual
      Transcript.tsx      # turn list + live partial
      Controls.tsx        # pause / end / forget / diagnostics
      Settings.tsx        # server URL + bearer token panel
    styles/
      index.css           # Tailwind imports + state animations
```

## Roadmap

See [docs/specs/voice-website-prd.md §11](../docs/specs/voice-website-prd.md#11-phase-plan)
for the phased scope. This codebase ships **Phase A (MVP)** functionality.
