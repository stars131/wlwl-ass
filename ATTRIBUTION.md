# ATTRIBUTION / 借鉴声明

> This file declares every external project, paper, dataset, or substantial
> code/idea fragment that **wlwl-ass** has borrowed from. It is a living
> document — see [Update Protocol](#update-protocol--更新规约) at the bottom.
>
> 本文件声明 **wlwl-ass** 借鉴的每一个外部项目、论文、数据集或重要的代
> 码/思路片段。这是一份持续更新的文档 —— 见末尾[更新规约](#update-protocol--更新规约)。

---

## Format / 条目格式

Each entry MUST contain the following fields. 每条记录必须包含以下字段：

```
### N. <Project Name>
- **Source / 源地址**: <URL>
- **License / 协议**: <SPDX identifier or "Unknown">
- **Borrowed Scope / 借鉴范围**: <what was taken — full fork? a single file?
                                  an algorithm? a prompt template? a SOP?>
- **Our Modifications / 我们的修改**: <what we changed on top — renamed?
                                       refactored? extended? bug-fixed?>
- **Borrowed On / 借鉴日期**: YYYY-MM-DD
- **Notes / 备注**: <optional — commit hash referenced, paper citation,
                     compatibility caveats, license obligations we honor>
```

External references that we **link to but do not borrow code/ideas from**
(e.g. third-party news coverage, badges, star-history widgets) do not need
an entry here. They live in `README.md`.

仅作链接而**未借鉴代码或思路**的外部引用（第三方新闻报道、徽章、star-
history 等）不需要在此登记，它们留在 `README.md` 即可。

---

## Borrowings / 借鉴清单

### 1. GenericAgent (upstream / 上游)

- **Source / 源地址**: https://github.com/lsdefine/GenericAgent
- **License / 协议**: MIT
- **Borrowed Scope / 借鉴范围**:
  - **Entire codebase** — wlwl-ass is a renamed derivative of GenericAgent.
    All ~3K lines of core logic, the 9 atomic tools (`code_run`, `file_read`,
    `file_write`, `file_patch`, `web_scan`, `web_execute_js`, `ask_user`,
    plus 2 memory tools), the ~100-line agent loop in `agent_loop.py`, the
    L0–L4 layered memory system, and all packaged SOPs under `memory/` are
    inherited as-is from upstream.
  - **整套代码库** —— wlwl-ass 是 GenericAgent 的更名衍生版本。约 3K 行
    核心逻辑、9 个原子工具（`code_run` / `file_read` / `file_write` /
    `file_patch` / `web_scan` / `web_execute_js` / `ask_user` 加 2 个记忆
    工具）、`agent_loop.py` 中约 100 行的 Agent Loop、L0–L4 分层记忆、
    `memory/` 下所有打包 SOP，全部完整继承自上游。
  - **Bot integrations**: Telegram / QQ / Feishu / WeCom / DingTalk /
    personal WeChat frontends in `frontends/`.
  - **Tauri + React GUI** scaffolding under `gui/`.
  - **Launcher subsystem** (`launcher/`) including config_store, api_server,
    qt_launcher, doctor, cli_init.
- **Our Modifications / 我们的修改**:
  - Renamed all internal identifiers: `GenericAgent` → `WlwlAss` (PascalCase),
    `genericagent` → `wlwl_ass` (Python package), `GA_*` env var prefix →
    `WLWL_*`, `~/.ga/` config dir → `~/.wlwl-ass/`, file `ga.py` →
    `wlwl_ass.py`, Tauri identifier `io.genericagent.gui` → `io.wlwlass.gui`.
  - Preserved external references to upstream untouched: the
    `github.com/lsdefine/GenericAgent` URLs, the arXiv paper citation
    (arxiv.org/abs/2604.17091), the technical-report PDF filename,
    third-party news coverage links, the Trendshift badge, star-history
    widgets, the Datawhale tutorial link.
  - Clean break on `GA_*` → `WLWL_*` / `~/.ga/` → `~/.wlwl-ass/`: no
    backward-compat read of legacy paths. Existing users must re-create
    their `.env` and `~/.wlwl-ass/config.json`.
- **Borrowed On / 借鉴日期**: 2026-05-06
- **Notes / 备注**:
  - Honors MIT license: `LICENSE` file retained verbatim from upstream.
  - Technical report citation (kept verbatim, not retitled):
    *GenericAgent: A Token-Efficient Self-Evolving LLM Agent via Contextual
    Information Density Maximization*, arXiv:2604.17091.
  - The upstream "self-bootstrap proof" claim in `README.md` describes the
    upstream project's history, not wlwl-ass's. We retain it as factual
    upstream provenance rather than reframe it.
  - Tutorial reference: https://datawhalechina.github.io/hello-generic-agent/
    (Datawhale "Hello GenericAgent" tutorial, kept as upstream-relative
    learning resource).

### 2. websockets (Python library)

- **Source / 源地址**: https://github.com/python-websockets/websockets
- **License / 协议**: BSD-3-Clause
- **Borrowed Scope / 借鉴范围**: Runtime dependency. We use the
  `websockets.asyncio.server.serve` API to expose `/api/voice/session` from
  `launcher/voice_ws.py`. No source code copy-pasted; we depend on the
  installed package.
- **Our Modifications / 我们的修改**: None — vanilla import. Pinned `>=12.0`
  in `pyproject.toml [project.optional-dependencies] voice`.
- **Borrowed On / 借鉴日期**: 2026-05-06
- **Notes / 备注**: First non-stdlib server-side dep in the launcher path.
  Documented in CONTRIBUTING.md follow-up. Subsequent kernel-internal
  dispatch and the Worker Protocol stay stdlib.

### 3. pypinyin (Python library)

- **Source / 源地址**: https://github.com/mozillazg/python-pinyin
- **License / 协议**: MIT
- **Borrowed Scope / 借鉴范围**: Optional runtime dependency. Used by
  `voice/wake.py::PhraseMatcher` for Chinese homophone fallback so wake
  phrases match even if STT mishears 三体 ↔ 散体. Module imports lazily; if
  missing, the matcher silently disables pinyin matching and keeps exact
  matching working.
- **Our Modifications / 我们的修改**: None — `lazy_pinyin` API used as-is.
- **Borrowed On / 借鉴日期**: 2026-05-06
- **Notes / 备注**: Listed in `pyproject.toml [project.optional-dependencies]
  voice` so it ships only when the voice extra is installed.

### 4. webrtcvad (Python library)

- **Source / 源地址**: https://github.com/wiseman/py-webrtcvad
- **License / 协议**: MIT (Python wrapper) wrapping Google's WebRTC
  (BSD-3-Clause) C VAD.
- **Borrowed Scope / 借鉴范围**: Reserved as a future runtime dep for
  server-side voice-activity detection over inbound audio frames. Listed in
  the `voice` extra; **not yet imported** in any module — current MVP relies
  on the orchestrator's silence-after-VAD-window logic + the browser's mic
  gating.
- **Our Modifications / 我们的修改**: None yet.
- **Borrowed On / 借鉴日期**: 2026-05-06 (declared dep only; will move to
  imported when MiniMax STT lands and we want server-side cut-points more
  precise than the 800 ms timeout heuristic).

### 5. soundfile (Python library)

- **Source / 源地址**: https://github.com/bastibe/python-soundfile
- **License / 协议**: BSD-3-Clause (Python) over libsndfile (LGPL-2.1).
- **Borrowed Scope / 借鉴范围**: Reserved for server-side audio assembly
  when persisting `audio.opus` files for completed voice sessions. Listed
  in the `voice` extra; **not yet imported** — current MVP appends raw
  inbound bytes verbatim.
- **Our Modifications / 我们的修改**: None yet.
- **Borrowed On / 借鉴日期**: 2026-05-06 (declared dep only).

### 6. React 19 + Vite 6 + Tailwind CSS + Zustand 5 (website stack)

- **Source / 源地址**:
  - https://github.com/facebook/react
  - https://github.com/vitejs/vite
  - https://github.com/tailwindlabs/tailwindcss
  - https://github.com/pmndrs/zustand
- **License / 协议**: MIT (all four)
- **Borrowed Scope / 借鉴范围**: The standalone `voice-website/` codebase
  uses React 19 for component rendering, Vite 6 as the build tool, Tailwind
  CSS for styling, and Zustand for the conversation state store. No code
  copied; standard `npm install` deps. Folder layout
  (`src/components/`, `src/lib/`, `src/state/`, `src/styles/`) follows
  the same convention as upstream `gui/` (which itself follows React +
  Tauri community conventions).
- **Our Modifications / 我们的修改**: None to the libraries; we wrote the
  app on top.
- **Borrowed On / 借鉴日期**: 2026-05-06
- **Notes / 备注**: Bundle size 67.88 KB (gzipped) — the four deps are tiny
  enough to fit under the PRD's 500 KB cap. No analytics, no telemetry, no
  CDN fetches.

### 7. faster-whisper + OpenAI Whisper (local STT fallback)

- **Source / 源地址**:
  - https://github.com/SYSTRAN/faster-whisper (CTranslate2 reimplementation)
  - https://github.com/openai/whisper (the underlying ASR model + weights)
- **License / 协议**:
  - faster-whisper: MIT
  - OpenAI Whisper model weights: MIT (research license)
- **Borrowed Scope / 借鉴范围**: Used as the **local fallback STT engine**
  in `llmcore/workers/local_whisper_worker.py`. When the cloud STT (Xiaomi
  MiMo `mimo-v2-omni` audio understanding) returns an error or is
  unreachable (HTTP 4xx/5xx, timeout, missing key, account balance
  exhausted), the orchestrator's `transcribe` hook in
  `launcher/voice_ws.py` retries the same audio frame against this local
  worker. Default model is `base` (~74 MB, real-time on a 4-core CPU);
  `WLWL_LOCAL_WHISPER_MODEL` env var supports `tiny`/`base`/`small`/
  `medium`/`large-v3`. PyAV (bundled by faster-whisper) decodes the opus
  bytes the browser sends so we don't need a system ffmpeg install.
- **Our Modifications / 我们的修改**: None to the engine. We only thinly
  wrap `WhisperModel.transcribe()` to fit the existing
  `voice.stt.v1` worker contract: input `{audio: <base64>, format: <opus|wav>}`,
  output `{text, confidence, is_final}`. The model is held in a process-level
  singleton (lazy-loaded on first invocation) to avoid repeated 700 ms+
  init costs across utterances.
- **Borrowed On / 借鉴日期**: 2026-05-07
- **Notes / 备注**:
  - Optional dep, **not** in base `pyproject.toml` deps. Install via
    `pip install -e ".[stt-local]"` or just `pip install faster-whisper`.
    Without the package the worker degrades to ok=False with a clear
    "faster-whisper not installed" error, and the chain falls back further
    (or, if there's no further fallback, surfaces the error to the user).
  - First run downloads the model from HuggingFace to `~/.cache/huggingface/`.
    Air-gapped / offline machines can pre-place the snapshot.
  - Whisper's CTC alignment quality on Chinese is acceptable on `base`
    but visibly better on `small`/`medium` — users should bump the env
    var if their machine has the headroom.
  - License obligations: redistribution is allowed; the upstream LICENSE
    files are included in the wheel. We carry no model weights in this
    repository.

### 12. Sophub SOP bulk import (7 SOPs from fudankw.cn/sophub)

- **Source / 源地址**: https://fudankw.cn/sophub/
  - DeepSearch: `https://fudankw.cn/sophub/sop/69f325a2cb3bf06150bb3baf` (author: `ace42@GA`)
  - DeepResearch: `https://fudankw.cn/sophub/sop/69f207e974962f84e0625e0d` (author: `sophub`)
  - Code Review Principles: `https://fudankw.cn/sophub/sop/69f2112374962f84e0625e0f` (author: `ljq`)
  - GitHub Project Learning: `https://fudankw.cn/sophub/sop/69f20d4e74962f84e0625e0e` (author: `GenericAgent`)
  - Pandoc: `https://fudankw.cn/sophub/sop/69f22686ba77d8b04fb0b9be` (author: `wellsoren`)
  - JS Hook Playbook: `https://fudankw.cn/sophub/sop/69f509090399f28c1add9e8e` (author: `ace42@GA`)
  - Cloudflare Turnstile: `https://fudankw.cn/sophub/sop/69f32598cb3bf06150bb3bab` (author: `ace42@GA`)
- **License / 协议**: Sophub-shared (each SOP uploaded by its author to the
  public Sophub registry; no SPDX declared upstream — treated as
  user-contributed content with attribution).
- **Borrowed Scope / 借鉴范围**: 7 markdown SOP files placed verbatim under
  `memory/`:
  - `memory/deepsearch_sop.md` (44.3KB) — Grok+Tavily+FireCrawl 3-path search,
    search-planning rubric, evidence standards (≥2 independent sources).
  - `memory/deepresearch_sop.md` (6.2KB) — DAG decomposition + main/sub-agent
    context isolation rules (5-field `context.json` red-line).
  - `memory/code_review_sop.md` (1.4KB) — 8 universal good-code principles.
  - `memory/github_project_sop.md` (2.3KB) — 5-step methodology for
    unfamiliar GitHub project comprehension.
  - `memory/pandoc_sop.md` (15.9KB) — pandoc 3.x format-conversion reference.
  - `memory/js_hook_sop.md` (43.9KB) — JS runtime hook playbook (33 presets,
    5 injection channels, 5 RE paradigms, anti-debug bypass).
  - `memory/cloudflare_turnstile_sop.md` (13.1KB) — CF Turnstile 3-tier
    handling (physical click / callback hijack / cloud solve).
- **Our Modifications / 我们的修改**: None to content — verbatim. Each file
  appends a 1-line provenance footer (Sophub id + author + fetched date).
  DeepSearch SOP references the upstream author's `.env [LLM_APIS]` config
  convention; readers should map those to our
  `launcher.config set providers.grok` equivalent (see `docs/CONFIG.md`).
  Companion to the in-house `web_search_sop.md` (the Mode-1 quickstart
  written 2026-05-09).
- **Borrowed On / 借鉴日期**: 2026-05-09
- **Notes / 备注**:
  - All 7 passed `tools.skills_guard.scan` defense-in-depth on full content
    (not just preview) — no role-hijack / override-system-prompt findings.
  - Imported via `scripts/import_sophub_sops.py` (re-runnable; will
    overwrite if an upstream SOP is updated).
  - Indexed in `memory/global_mem_insight.txt` L3 list so the agent
    discovers them at navigation time.
  - `cloudflare_turnstile_sop.md` and `js_hook_sop.md` are dual-use —
    useful for legitimate automation / RE engagements, consistent with the
    existing `tmwebdriver_sop` domain. Not a green-light to bypass
    security on third-party services without authorisation.

### 13. wxauto (Python library)

- **Source / 源地址**: https://github.com/cluic/wxauto
- **License / 协议**: MIT
- **Borrowed Scope / 借鉴范围**: Optional runtime dependency. Powers the
  `wechat_send` agent tool (`tools/wechat.py`) — drives the running PC
  WeChat client window via Microsoft UIA to search a contact, switch
  chats, paste text, optionally send file attachments, and press Enter.
  Outbound only — we do not consume any of wxauto's history-reading or
  listener APIs.
- **Our Modifications / 我们的修改**: None — only public `WeChat()`,
  `SendMsg(who=...)`, `SendFiles(filepath=..., who=...)` are called.
  Listed in `pyproject.toml [project.optional-dependencies] wechat`,
  pinned `>=3.9`. Lazy-imported so non-Windows / non-WeChat installs are
  unaffected.
- **Borrowed On / 借鉴日期**: 2026-05-09
- **Notes / 备注**:
  - Windows-only; tested against PC WeChat 3.9.x. WeChat 4.x compatibility
    is not guaranteed by upstream — when wxauto fails on a 4.x client,
    `wechat_send` returns an error string and the agent is expected to
    fall back via `sop_read wechat_ljqctrl_sop` (`memory/wechat_ljqctrl_sop.md`).
  - Side effects are real and irreversible — the tool is registered with
    `risk="high"` and `ASK` permission in `permissions.py`.

### 14. lark-oapi (Lark / Feishu OpenAPI Python SDK)

- **Source / 源地址**: https://github.com/larksuite/oapi-sdk-python
- **License / 协议**: MIT
- **Borrowed Scope / 借鉴范围**: Optional runtime dependency. Two
  independent surfaces:
  - **IM (long-running)**: `frontends/fsapp.py` uses
    `lark.Client.builder()` + `lark.ws.Client` long-connection mode and
    the `client.im.v1.message` resource for inbound/outbound chat,
    image/file upload, and message edit. (Original integration; predates
    this attribution entry.)
  - **Calendar v4 (new)**: `llmcore/workers/feishu_calendar_storage.py`
    uses the `client.calendar.v4.calendar` (`primary()`) and
    `client.calendar.v4.calendar_event` (`create / patch / delete / get
    / list`) resources to implement the `CalendarStorage` Protocol from
    `calendar_worker.py`. Activated when
    `bots.feishu.use_for_calendar=true` in the config store.
- **Our Modifications / 我们的修改**: None — vanilla SDK use via the
  builder pattern. Listed in
  `pyproject.toml [project.optional-dependencies] all-frontends`,
  pinned `>=1.0` (currently exercised on 1.5.5).
- **Borrowed On / 借鉴日期**: 2026-05-09 (back-attribution; the IM use
  predates this commit but was not previously listed)
- **Notes / 备注**:
  - The IM and Calendar surfaces share the same `bots.feishu.app_id` /
    `bots.feishu.app_secret` credentials. The Calendar surface needs the
    `calendar:calendar` permission scope added to the Feishu app and a
    re-publish; the IM surface needs `im:message`, `im:message:send_as_bot`,
    `contact:user.id:readonly`. See `assets/SETUP_FEISHU.md` for the
    full setup.
  - Cached `bots.feishu.calendar_id` is auto-discovered on first use
    (the user's primary calendar). Switching backends (sqlite ↔ feishu)
    does not migrate historical events; old event ids become stale.

### 15. cc-switch (provider preset library inspiration)

- **Source / 源地址**: https://github.com/farion1231/cc-switch
- **License / 协议**: MIT © Jason Young (farion1231)
- **Borrowed Scope / 借鉴范围**:
  - **Provider preset data**: ~45 entries in `launcher/api_presets.py` were
    transcribed from cc-switch's `src/config/claudeProviderPresets.ts` /
    `codexProviderPresets.ts` / `geminiProviderPresets.ts`. Specifically:
    each preset's `name`, `apibase` (from `ANTHROPIC_BASE_URL` /
    OpenAI-compat base), default `model`, and `category` classification
    (`official` / `cn_official` / `aggregator` / `third_party`) were
    derived from the cc-switch tables. The same lowercase-kebab `id` slug
    convention is used so users coming from cc-switch see familiar names.
  - **Design pattern**: The overall "preset library + one-click install"
    UX for `wlwl config presets` / `wlwl config add --preset` is patterned
    on cc-switch's preset picker. The backup-rotation policy
    (`MAX_BACKUPS = 10` snapshots in `temp/backups/`) and the deep-link
    import URI shape (`wlwl-config://provider?preset=...`, with
    `ccswitch://` accepted as alias) also mirror cc-switch's
    `~/.cc-switch/backups/` rotation and `ccswitch://` scheme.
- **Our Modifications / 我们的修改**:
  - **Reimplemented in Python**: cc-switch is Rust+TypeScript; wlwl-ass is
    Python. No source files were copied — `launcher/api_presets.py`,
    `launcher/cli_config.py`, `launcher/api_endpoint_probe.py` are all
    original Python implementations.
  - **Normalised to wlwl-ass's two native kinds** (`native_oai` /
    `native_claude`) rather than cc-switch's per-CLI tables (claude /
    codex / gemini / opencode / openclaw). Providers that offer both
    Anthropic-format and OpenAI-format endpoints appear as separate
    entries (`kimi` / `kimi-oai`, `openrouter` / `openrouter-oai`, etc.).
  - **Curated subset**: ~45 entries instead of cc-switch's 50+, dropping
    affiliate/referral URLs and entries that only exist for cc-switch's
    GUI-specific features (themes, icons, partner promotions).
  - **CLI-first surface**: `wlwl config <sub>` + REPL `/config <sub>`
    rather than cc-switch's Tauri GUI. Endpoint probing uses TCP connect
    timing rather than HTTP requests (most CN relays return 401/403 on
    bare HEADs; TCP latency answers the "is this host reachable + fast"
    question without auth).
- **Borrowed On / 借鉴日期**: 2026-05-16
- **Notes / 备注**:
  - All preset URLs are publicly documented endpoints of the listed
    providers — no proprietary data was inherited.
  - We do NOT carry over cc-switch's `apikey_url` affiliate parameters
    (`?aff=ccswitch`, `?from=CH_4HHXMRYF`, etc.). Where an
    apikey-issuance link is included it points to the bare console page.
  - The `endpointCandidates` multi-URL probing (a more advanced cc-switch
    feature) is implemented but currently each preset only carries one
    `apibase`; surfacing multiple candidate URLs per provider is a
    follow-up if users start asking for it.

### 16. Cal.com (scheduling-assistant design patterns)

- **Source / 源地址**: https://github.com/calcom/cal.com
- **License / 协议**: AGPL-3.0
- **Borrowed Scope / 借鉴范围**:
  - **Design patterns only — no code copy**: ADR-0011 / spec
    `docs/specs/feishu-concierge-bot.md` borrow Cal.com's data-shape
    intuition for the **slot-proposal API** (`duration_minutes`,
    `earliest`, `latest`, `preferred_window` inputs; ranked slot list
    output with `exhausted` flag).
  - **Working-hours / buffer model**: the distinction between
    `working_hours` (publishable availability) and the user's *actual*
    calendar is taken from Cal.com's "event-type schedule" concept,
    where the bookable window is intentionally narrower than the user's
    real free time.
  - **Owner-approval-before-write pattern**: Cal.com's "manual
    approval" event type — bookings sit as `PENDING` until the owner
    approves — is the model behind the concierge's
    `requires_approval=true` capability-token claim on
    `calendar.create_event.v1`.
- **Our Modifications / 我们的修改**:
  - **Reimplemented in Python** in `llmcore/workers/slot_worker.py`
    (Phase 1, not yet landed). No TypeScript / React / Prisma code from
    Cal.com is imported.
  - **Chat-native UX** rather than booking-page UX: the concierge
    proposes slots in natural language inside a Feishu DM; Cal.com's
    UX is web-form.
  - **Owner-approval surface** is a Feishu interactive card, not an
    email + dashboard.
- **Borrowed On / 借鉴日期**: 2026-05-17
- **Notes / 备注**:
  - AGPL-3.0 is preserved by the "design patterns only — no code copy"
    boundary. If any future change pulls actual Cal.com source into
    `wlwl-ass`, this entry must be revised and the AGPL obligations
    (including viral re-licensing of the surrounding module) honored.
  - The closed-source SaaS variants (Calendly, Reclaim.ai, Motion) are
    mentioned in `docs/specs/feishu-concierge-bot.md § 10` as general
    industry-pattern references. No code or specific algorithm is taken
    from them, so they do not get their own ATTRIBUTION entry (per the
    "link only, no borrow" rule above).

### 17. Khoj (personal RAG knowledge base patterns)

- **Source / 源地址**: https://github.com/khoj-ai/khoj
- **License / 协议**: AGPL-3.0
- **Borrowed Scope / 借鉴范围**:
  - **Design patterns only — no code copy**: the concierge's
    `concierge.kb_answer.v1` worker (Phase 1, not yet landed) is
    informed by Khoj's "personal RAG over owner-curated notes" model:
    a small per-user knowledge base, BM25 (or embedding) lookup,
    confidence-thresholded answers, polite "I don't know" fallback.
  - **Per-topic visibility gating**: Khoj's notebook / file-level
    visibility scoping inspired the concierge's `topics_allowed`
    config — the KB can hold more than the bot is allowed to share,
    and the owner curates the allowlist explicitly.
- **Our Modifications / 我们的修改**:
  - **Drastically simplified storage**: a single `temp/concierge_kb.jsonl`
    file rather than Khoj's full-text + embedding + Postgres stack.
  - **Chat-bot-only surface**: no web UI, no desktop client; the
    concierge IS the only consumer.
  - **Topic-allowlist gate** as a hard pre-send filter, not a
    soft-rerank — Khoj allows access via UI controls; we deny outbound
    in worker code so a jailbroken intent can't bypass it.
- **Borrowed On / 借鉴日期**: 2026-05-17
- **Notes / 备注**:
  - AGPL-3.0 boundary preserved by the "design patterns only — no code
    copy" rule. Same caveat as #16.

### 18. NoneBot 2 + Koishi (Chinese chat-bot framework patterns)

- **Source / 源地址**:
  - https://github.com/nonebot/nonebot2 — NoneBot 2, MIT
  - https://github.com/koishijs/koishi — Koishi, MIT
- **License / 协议**: MIT (both)
- **Borrowed Scope / 借鉴范围**:
  - **Design patterns only — no code copy**: the concierge's
    state-machine-first runtime (spec § 4.3) is patterned on
    NoneBot/Koishi's per-session matcher/handler model — intent matched
    by rule first, LLM only on miss; explicit state transitions; plugin
    permission scope is checked at the dispatch boundary, not in
    application code.
  - **Plugin-permission convention**: both projects ship a
    `permission` / `scope` concept where each handler declares what it
    needs. ADR-0011's "capability allowlist stamped at agent boot"
    follows the same shape.
- **Our Modifications / 我们的修改**:
  - **Different runtime substrate**: NoneBot/Koishi are general
    multi-platform bot frameworks; the concierge is a single-purpose
    agent that consumes wlwl-ass's kernel API (ADR-0008) instead of a
    plugin registry. No source from either project is imported.
  - **State machine is bounded to 5 states** specific to
    schedule + Q&A + escalation; NoneBot's matcher tree is open-ended.
- **Borrowed On / 借鉴日期**: 2026-05-17
- **Notes / 备注**:
  - Both projects are widely-used reference implementations in the
    Chinese chat-bot community; their conventions inform what users
    coming from those frameworks will find familiar in wlwl-ass's
    concierge layer.

### 19. anthropic-cookbook (Customer Concierge prompt patterns)

- **Source / 源地址**: https://github.com/anthropics/anthropic-cookbook
- **License / 协议**: MIT
- **Borrowed Scope / 借鉴范围**:
  - **Prompt design patterns**: the concierge's persona template
    (`bots.feishu_concierge.persona`) and out-of-scope refusal phrases
    follow the cookbook's "Customer Service Agent" and "Customer
    Concierge" examples — clear scope statement, named principal,
    explicit deferral phrasing ("let me check with X"), polite refusal
    without revealing internal tool names.
  - **Escalation language**: the "I'll relay this to <owner>" surface
    is patterned on the cookbook's human-handoff examples.
- **Our Modifications / 我们的修改**:
  - **No verbatim prompt copy** lands; the cookbook serves as a style
    reference, and the actual `persona` string in
    `bots.feishu_concierge.persona` is owner-supplied (we provide a
    default that follows the patterns above).
  - **Bilingual** (Chinese-primary, English-secondary) rather than the
    cookbook's English examples.
- **Borrowed On / 借鉴日期**: 2026-05-17
- **Notes / 备注**:
  - MIT-licensed cookbook permits direct reuse, but we keep the
    boundary clean and do not import any prompt strings verbatim. Any
    future code that does paste from the cookbook must update this
    entry's "Borrowed Scope" accordingly.

### 20. Microsoft Sico (Playbook / Experience Learning pattern)

- **Source / 源地址**: https://github.com/microsoft/sico
- **License / 协议**: MIT
- **Borrowed Scope / 借鉴范围**:
  - **Design pattern only**: Sico's reviewed Playbook / Experience Learning
    loop inspired `launcher/playbook.py`, where execution lessons are proposed,
    manually accepted or rejected, and only accepted entries are injected into
    future agent prompts.
  - **Curator target concept**: `curator_propose(..., target="playbook")`
    follows Sico's distinction between raw trajectory learning and curated
    reusable strategies.
- **Our Modifications / 我们的修改**:
  - Implemented a stdlib-only local JSON store instead of Sico's Go backend,
    Python Core service, reverse gRPC, database, Mem0/Qdrant, sandbox, or LLM
    reflector/curator pipeline.
  - Kept wlwl-ass's manual-review boundary: pending Playbook entries are never
    injected into prompts; only accepted entries render in `get_system_prompt()`.
  - Added lightweight API routes under `/api/playbook` and a Settings-page GUI
    review card for accepting or rejecting pending Playbook entries.
- **Borrowed On / 借鉴日期**: 2026-05-24
- **Notes / 备注**:
  - Referenced commit: `5a50b8bf2f52c45cc04a9ae5cd00350b610ad481`.
  - No Sico source files were copied into wlwl-ass; the implementation is a
    small local adaptation of the idea.

---

## Update Protocol / 更新规约

**Whenever code, design ideas, prompt templates, SOPs, dataset entries, or
non-trivial documentation prose are imported from an external project, the
agent or contributor introducing them MUST append a new numbered entry to
this file in the same commit / pull request.**

**只要从外部项目引入代码、设计思路、提示词模板、SOP、数据集条目或非琐碎
的文档文字，引入者（无论是人类贡献者还是 Agent）必须在同一次提交 / PR
里向本文件追加一条新编号记录。**

### Triggers / 触发条件

A new entry is required when any of the following occur:
出现以下任一情形必须新增条目：

1. **Code copy-paste** of more than ~10 lines from another project (even
   with light edits).
   从其他项目**复制粘贴**超过 ~10 行代码（哪怕做了轻度修改）。
2. **Verbatim or near-verbatim** prompt / SOP / system-message reuse.
   原样或近乎原样复用提示词 / SOP / system message。
3. **Algorithm or design pattern** lifted from a paper, blog post, or
   open-source project — even if reimplemented from scratch.
   从论文、博客或开源项目搬运的**算法或设计模式**，即便完全重写。
4. **Bundled assets** (icons, fonts, sample data) shipped under a
   third-party license.
   附带分发的**第三方协议素材**（图标、字体、样本数据等）。
5. **Tooling integration** that reads a third party's protocol / schema
   verbatim (e.g. an MCP server's JSON-RPC contract).
   原样接入第三方协议 / schema 的**工具集成**（如 MCP server JSON-RPC
   契约）。

### Not required for / 无需登记

- Standard library calls / 标准库调用
- Generic API endpoints accessed via documented public interface
  通过公开接口调用的通用 API
- External URLs only linked from README without code reuse
  仅在 README 中外链、未借鉴代码的外部引用

### Enforcement / 执行

- **Pre-commit checklist**: Reviewers should reject any PR introducing a
  new top-level dependency (`pyproject.toml`, `gui/package.json`,
  `gui/src-tauri/Cargo.toml`) or a vendored file under `vendor/` /
  `third_party/` without a corresponding new entry here.
  评审人在收到 PR 时若发现新增顶层依赖或 vendored 文件却未在此处登记，
  应予退回。
- **Agent self-check**: When the agent itself imports an external SOP /
  skill into `memory/`, it MUST run `update_working_checkpoint` with a
  note flagging the new attribution entry needed, and then create the
  entry before merging the skill.
  Agent 在向 `memory/` 引入外部 SOP / Skill 时，必须先用
  `update_working_checkpoint` 标记需要新增记录，落地此条目后再合并。

---

*Last updated / 最后更新：2026-05-16*
