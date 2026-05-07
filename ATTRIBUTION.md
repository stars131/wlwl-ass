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

*Last updated / 最后更新：2026-05-06*
