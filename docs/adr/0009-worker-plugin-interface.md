# 0009 — Worker Plugin Interface (智能体即插即拔)

- **Status:** Proposed (design-only addendum to ADR-0008; no code lands here)
- **Date:** 2026-05-06
- **Builds on:** [ADR-0008 Kernel API + Delegated Workers + Forum/Moderator](./0008-kernel-api-delegation-and-forum.md)
- **Reviewer hat:** written from a "platform infra" stance — explicit contracts, narrow public surfaces, version negotiation, failure isolation, operability over feature richness.

---

## 0. TL;DR

ADR-0008 introduced `kernel` and `worker` as concepts but left the **worker boundary** implicit: today every "worker" is just a config dict that the kernel pokes via `ClaudeSession.ask` / `LLMSession.ask`. That works for "another LLM endpoint", but it doesn't work for the real long-tail need — adding non-LLM agents (a custom OCR microservice, a GPU image-gen daemon, a local rule-based router, an MCP-bridged tool) without forking the kernel.

This ADR defines six narrow, versioned interfaces that together make worker add/remove/reload a **first-class operation** with no special cases:

1. **`Worker` Protocol** — what every worker must implement.
2. **`WorkerFactory`** — how a config row becomes a Worker instance.
3. **`CapabilityRegistry`** — versioned capability identity, declared before use.
4. **Wire envelope** — a single JSON message shape spanning in-process and subprocess workers.
5. **Plugin manifest** — what a third-party package must ship to be discoverable.
6. **Kernel public API** — `add_worker / remove_worker / reload / list / inspect`, exposed identically through CLI, HTTP, and GUI.

Everything else stays inside the kernel. The kernel team (us) controls the interface; plugin authors target it. Future kernel internals can evolve without breaking plugins as long as these six contracts hold.

---

## 1. Context

### 1.1 What ADR-0008 left implicit

ADR-0008 §3 and §4 talked about workers as "configs with capabilities" and "sessions the kernel routes to". The closest thing to a worker abstraction in the current codebase is `BaseSession` (`llmcore/base.py`) — but that's specifically an LLM HTTP-call shape (`raw_ask` / `make_messages`). Anything that isn't "send a chat-completion request" doesn't fit.

Concrete blockers if we ship 0008 as-is:

- A user wants to add an **on-prem Whisper service** as a `voice` worker. Today: write a fake `LLMSession` subclass, monkey-patch the picker. With 0008 alone: still need a fake LLMSession because the kernel has no other shape to route to.
- A user wants to **disable a worker for 10 minutes** without restarting. ADR-0008 proposes `silenced=true` on the config but doesn't define who flips it, through what API, with what concurrency guarantees on in-flight calls.
- A third party wants to ship `wlwl-ass-worker-deepl` on PyPI. There's no entry-point convention, no manifest, no version handshake.
- A power user wants to **swap a misbehaving worker** at 3 AM mid-task. ADR-0008 mentions hot-replace in one paragraph (§4.4) without specifying drain semantics.

### 1.2 Design goals (in priority order)

1. **Add and remove a worker is a config-only operation 95% of the time.** No `git pull` or restart for the common case (configuring a new LLM endpoint as a worker).
2. **Adding a non-trivial agent is a plugin-only operation.** Drop a Python file in a known directory or `pip install` a wheel. No core code changes.
3. **The kernel never crashes because of a misbehaving plugin.** Subprocess sandbox available; failed registrations rejected with structured errors.
4. **Plugins survive kernel minor upgrades.** SemVer on the API surface; deprecation runway of one minor version.
5. **Every state change is observable.** Add / remove / reload / silence each emit on the `audit` topic with full provenance.
6. **One way to do each thing.** CLI, HTTP, and GUI hit the same kernel methods. No GUI-only or CLI-only operations.

Non-goals (explicit):

- Cross-host distribution (ADR-0008 §13.3 still applies).
- Sandbox stronger than subprocess + capability scope (no namespaces / cgroups / seccomp).
- "Marketplace" for third-party workers. PyPI is the marketplace; the registry is the package index.

---

## 2. Decision overview

```
                        ┌──────────────────────────────────┐
                        │           KERNEL                 │
                        │                                  │
                        │  ┌────────────────────────────┐  │
                        │  │   WorkerRegistry           │  │
                        │  │   (in-mem, mutable)        │  │
                        │  └──────┬─────────────────────┘  │
                        │         │                        │
                        │         │ owns N                 │
                        │         ▼                        │
   ┌──────────────────────┐   ┌─────────────────┐   ┌──────────────────────┐
   │ WorkerFactory        │   │  Worker (Proto) │   │  WireTransport       │
   │ - from_config(c)     │   │  .metadata      │   │  - in-process        │
   │ - from_manifest(m)   │   │  .health()      │   │  - subprocess (rpc)  │
   └──────────────────────┘   │  .invoke(req)   │   └──────────────────────┘
                              │  .shutdown()    │
                              └─────────────────┘
                                       ▲
                                       │ implemented by
                  ┌────────────────────┼─────────────────────┐
                  │                    │                     │
         ┌────────────────┐   ┌────────────────┐   ┌──────────────────┐
         │ LLMWorker      │   │ MCPWorker      │   │ ThirdPartyWorker │
         │ (in-tree;      │   │ (bridges       │   │ (pip-installed   │
         │  wraps         │   │  tools/        │   │  via entry point)│
         │  BaseSession)  │   │  mcp_client)   │   │                  │
         └────────────────┘   └────────────────┘   └──────────────────┘
```

The kernel sees only the **`Worker` Protocol**. Everything else is an
implementation detail. The factory turns config or a manifest into a
running Worker. The transport hides whether the Worker lives in this
process or behind a subprocess pipe.

---

## 3. The `Worker` Protocol

A Python `Protocol` (PEP 544) — duck-typed, importable from
`llmcore/worker.py`. Exact shape:

```python
from typing import Protocol, runtime_checkable
from dataclasses import dataclass

@dataclass(frozen=True)
class WorkerMetadata:
    name: str                         # unique within the kernel; user-visible
    kind: str                         # "llm" | "mcp" | "subprocess" | <plugin-defined>
    capabilities: tuple[str, ...]     # declared scope; verified vs CapabilityRegistry
    api_version: str                  # SemVer; the API version this worker speaks
    config_schema_id: str | None      # optional JSON Schema id for its config block
    owner_plugin: str | None          # which package shipped it (for audit)
    description: str = ""

@dataclass(frozen=True)
class InvokeRequest:
    call_id: str                      # uuid; for tracing across forum/audit
    capability: str                   # which cap we're invoking; ⊆ metadata.capabilities
    payload: dict                     # JSON-serializable; cap-specific schema
    deadline_ms: int                  # absolute or relative; kernel decides
    cap_token: str                    # signed token from kernel
    trace_context: dict               # forum_seq, parent_call_id, …

@dataclass(frozen=True)
class InvokeResponse:
    call_id: str
    ok: bool
    result: dict | None               # on success
    error: dict | None                # {"code": str, "message": str, "retryable": bool}
    cost_usd: float                   # billed; 0 for local
    latency_ms: float
    audit_extras: dict                # provenance the worker wants logged

@dataclass(frozen=True)
class HealthReport:
    state: str                        # "ready" | "degraded" | "down"
    in_flight: int
    queue_depth: int
    last_error: str | None
    version: str                      # build hash / pkg version

@runtime_checkable
class Worker(Protocol):
    @property
    def metadata(self) -> WorkerMetadata: ...

    def health(self) -> HealthReport: ...

    def invoke(self, req: InvokeRequest) -> InvokeResponse: ...

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None: ...

    # ── Optional hooks ──────────────────────────────────────────
    # Not required; kernel uses getattr-with-default.
    def on_token_refresh(self, new_token: str) -> None: ...
    def on_silence(self, reason: str) -> None: ...
    def on_unsilence(self) -> None: ...
    def on_capability_revoked(self, cap: str, reason: str) -> None: ...
```

Design notes:

- **Dataclasses, not classes with behavior.** Workers exchange values, not ORM-like objects. Easier to JSON-encode for subprocess transport.
- **`call_id` mandatory on every request.** It's the join key for `audit`, `route`, `disagree`. Kernel mints it; never the worker.
- **`deadline_ms`, not `timeout_ms`.** Lets the kernel propagate a budget across hops (mixin, sub-routing).
- **`cap_token` on every request.** The worker re-verifies (HMAC is cheap). This guards against impersonation in the subprocess case.
- **`shutdown` MUST honor `drain_timeout_ms`.** Kernel removes the worker from routing immediately; drains by checking `health().in_flight == 0` or the timeout, then forces.
- **Optional hooks via duck-typing.** A worker that doesn't care about silence simply doesn't implement `on_silence`. No "AbstractWorker" forcing empty overrides.

What's intentionally **not** in the protocol:

- No `register()` method. Registration is the kernel's job, not the worker's; the worker is constructed already-registered.
- No streaming / async. The kernel today is sync-call-based (`BaseSession.ask` is a generator but called synchronously). Streaming is a future addendum (see §16).
- No pub/sub on the forum. Forum access is mediated by the kernel; workers receive a `ForumClient` handle on construction. Keeping the protocol surface small means fewer places to break compatibility.

---

## 4. Discovery & registration

Three discovery channels, processed in this order at kernel startup. Later channels can shadow earlier names, with a logged warning.

### 4.1 Channel 1 — In-tree workers

Path: `llmcore/workers/*.py`. Each module exports `WORKER_FACTORY: WorkerFactory`. The kernel imports them eagerly. These ship with wlwl-ass releases.

Bootstrap set:

| Module                 | Provides                                      |
|------------------------|-----------------------------------------------|
| `llm_worker.py`        | `LLMWorker` — wraps `BaseSession` subclasses  |
| `mcp_worker.py`        | `MCPWorker` — wraps `tools/mcp_client.py`     |
| `subprocess_worker.py` | `SubprocessWorker` — JSON-RPC over stdio      |
| `mixin_worker.py`      | `MixinWorker` — wraps existing `MixinSession` |

### 4.2 Channel 2 — Local plugin directory

Path: `<project>/.wlwl-ass/workers/*.py` (project-scoped) and
`~/.wlwl-ass/workers/*.py` (user-scoped). Same export contract as Channel
1. Loaded with `importlib.util.spec_from_file_location` — no `pip install`
required.

This is the "I want to hack on a worker without packaging it" path. Files are loaded once at startup; reload requires kernel reload.

### 4.3 Channel 3 — Installed packages (PyPI / `pip install -e .`)

Python entry points. A third-party package's `pyproject.toml`:

```toml
[project.entry-points."wlwl_ass.workers"]
deepl = "wlwl_ass_worker_deepl:WORKER_FACTORY"
whisper-local = "wlwl_ass_worker_whisper:WORKER_FACTORY"
```

The kernel iterates `importlib.metadata.entry_points(group="wlwl_ass.workers")`. Each yields a name + a dotted import path to a `WorkerFactory`. Failure to import one entry point doesn't kill the others — failures land on `audit` with the exception, the entry is rejected, the rest load.

### 4.4 Registration flow

```
foreach factory found in (channel 1 → 2 → 3):
    metadata = factory.describe()
    if metadata.name in already_registered:
        log "shadowing {name} from {prior_owner} with {new_owner}"
    if metadata.api_version not in kernel.supported_api_versions:
        reject(reason="api_version_unsupported")
        continue
    foreach cap in metadata.capabilities:
        if cap not in CapabilityRegistry:
            reject(reason="undeclared_capability", cap=cap)
            break
    if not validate_config_schema(metadata.config_schema_id, supplied_config):
        reject(reason="config_schema_violation", details=...)
        continue
    worker = factory.build(supplied_config, kernel_handle)
    kernel.workers.add(worker)
    kernel.tokens.issue(worker.metadata.name, metadata.capabilities)
    forum.publish("control", {"type": "registered", ...})
```

A rejected worker is **not** half-registered. The kernel never holds a partially-initialized Worker.

---

## 5. `WorkerFactory`

```python
@dataclass(frozen=True)
class FactoryDescription:
    factory_id: str             # stable; "wlwl_ass.workers.llm" etc.
    api_version: str            # SemVer of the API the factory targets
    config_schema_id: str | None
    capabilities_offered: tuple[str, ...]   # default; can be subset by config
    transport: str              # "in_process" | "subprocess"

class WorkerFactory(Protocol):
    def describe(self) -> FactoryDescription: ...
    def build(self, config: dict, kernel: KernelHandle) -> Worker: ...
```

A factory is **stateless**. `build()` is called once per worker instance. The factory may inspect `config` and reduce `capabilities_offered` to a subset (e.g. an LLM that supports `vision` only when the model name matches a regex).

`KernelHandle` is the narrow surface a worker needs to talk back: the forum client, the cap-token verifier, a logger, and a clock. Workers don't get the full kernel object — they get a **deliberately-limited proxy**. This is the seam that lets us swap in a sandboxed kernel (subprocess case) without changing worker code.

---

## 6. `CapabilityRegistry`

Capabilities are **declared** before they can be granted or required. This prevents typos becoming silent ACL bypasses (a worker declaring `forum.read.*`).

### 6.1 Declaration

```yaml
# llmcore/capabilities.yaml (in-tree built-ins)
- id: vision.ocr.v1
  description: "Optical character recognition on images"
  request_schema_ref: "schemas/vision.ocr.request.json"
  response_schema_ref: "schemas/vision.ocr.response.json"
  cost_estimate_usd: 0.001
  default_deadline_ms: 30000

- id: vision.caption.v1
  description: "Natural-language description of image content"
  …
```

Plugins declare their own with the same shape, registered alongside the factory:

```python
# in a third-party plugin
from llmcore.worker import register_capability

register_capability(
    id="translate.deepl.v1",
    description="DeepL high-quality translation",
    request_schema_ref="…",
    response_schema_ref="…",
    cost_estimate_usd=0.0001,
)
```

### 6.2 Identity

`<vendor>.<scope>.<action>.<version>`

- `vision.ocr.v1`, `voice.tts.v1`, `translate.deepl.v1`
- `<vendor>` is `wlwl` for in-tree, the package short-name for plugins (`deepl`, `whisper`)
- Trailing `.vN` is mandatory and is **not** SemVer — it's an opaque version slug. Breaking changes mint `.v2`; both can coexist; tools choose explicitly.

The kernel's matcher supports glob suffix: a tool requesting `vision.ocr.*` matches any version, with the most-recent winning ties. Tools usually pin a specific version.

### 6.3 Why upfront declaration

Without it, the kernel has no way to:

- Validate that a worker isn't claiming a typo (`forum.write` instead of `forum.post`).
- Render a "what does this worker actually do" UI.
- Cost-estimate before dispatch.
- Validate request/response payloads.

Declaration cost is one YAML entry per capability, paid once per author.

---

## 7. Wire envelope (in-process and subprocess unified)

The `Worker.invoke()` Python signature is the in-process form. The
**wire form** for subprocess workers is a JSON-RPC 2.0 frame over stdio,
using exactly the same field names. This means:

- A subprocess worker is a Python process running an async loop, reading
  newline-delimited JSON, dispatching to the same `Worker` protocol,
  writing newline-delimited JSON back.
- The kernel's `SubprocessWorker` shim is the **only** code that
  serializes/deserializes. Plugin authors write Python; the wire form
  exists at the seam, not in their hands.
- We can later replace stdio with Unix sockets / TCP / gRPC without
  touching plugin code, because `SubprocessWorker` is the implementation
  of the in-process `Worker` protocol.

Frames:

```jsonc
// Kernel → Worker
{
  "jsonrpc": "2.0",
  "id": "<call_id>",
  "method": "invoke",
  "params": { /* InvokeRequest fields */ }
}

// Worker → Kernel (response)
{
  "jsonrpc": "2.0",
  "id": "<call_id>",
  "result": { /* InvokeResponse fields */ }
}

// Worker → Kernel (error)
{
  "jsonrpc": "2.0",
  "id": "<call_id>",
  "error": { "code": -32000, "message": "...", "data": {...} }
}

// Notifications (no id; one-way)
//   - "health"     periodic, every health_interval_ms
//   - "log"        plugin log line; kernel routes to its logger
//   - "metric"     plugin metric tick; kernel forwards to metrics sink
```

Why JSON-RPC 2.0: it's a 2-page spec, has a battle-tested envelope, every language has a parser. We don't need gRPC's IDL/codegen surface for an in-process daemon and aren't going to ship Protocol Buffers as a runtime dependency just for this.

---

## 8. Plugin manifest

Every distributable plugin (Channel 3) ships a `wlwl-ass.toml` at the package root *in addition to* `pyproject.toml`:

```toml
[plugin]
name = "deepl"
version = "0.1.0"
wlwl_ass_api_version = ">=1.0,<2.0"
license = "Apache-2.0"
homepage = "https://github.com/example/wlwl-ass-worker-deepl"

[plugin.workers.deepl]
factory = "wlwl_ass_worker_deepl:WORKER_FACTORY"
default_capabilities = ["translate.deepl.v1"]
config_schema = "wlwl_ass_worker_deepl/config.schema.json"
transport = "in_process"           # or "subprocess"

[plugin.capabilities]
"translate.deepl.v1" = "wlwl_ass_worker_deepl/caps/translate_deepl_v1.yaml"
```

Manifest is read once at install time, validated against the kernel's expected schema, then cached. If the manifest can't be parsed, the plugin doesn't load — but again, the failure is isolated.

---

## 9. Hot add / remove / reload

The kernel exposes a stable public API. Three callers — CLI, HTTP, GUI — share it. There is **one** code path; the others are thin wrappers.

### 9.1 Public methods

```python
class Kernel:
    def add_worker(self, config: dict, *, source: str = "api") -> WorkerMetadata: ...
    def remove_worker(self, name: str, *, drain_timeout_ms: int = 30_000) -> None: ...
    def reload_worker(self, name: str, *, new_config: dict | None = None) -> WorkerMetadata: ...
    def silence(self, name: str, reason: str, *, ttl_sec: int | None = None) -> None: ...
    def unsilence(self, name: str) -> None: ...
    def list_workers(self, *, include_silenced: bool = True) -> list[WorkerView]: ...
    def inspect_worker(self, name: str) -> WorkerInspection: ...
```

### 9.2 Semantics

**`add_worker`**:
1. Resolve factory by `config.kind` (or `config.factory_id`).
2. Run §4.4 registration flow.
3. Return metadata or raise structured `RegistrationError`.

**`remove_worker`**:
1. Mark the worker `removing` — routing immediately skips it.
2. Wait until `health().in_flight == 0` OR `drain_timeout_ms` elapses.
3. Revoke the cap token (atomic: revocation + worker drop are a single step).
4. Call `worker.shutdown()`.
5. Emit `audit { type: "removed", name, reason, drained: bool }`.

A removal is idempotent. Calling `remove_worker("foo")` when `foo` is already gone returns success.

**`reload_worker`**:
- With `new_config`: full replace — same `add_worker` validation, then atomic swap. In-flight calls finish on the old instance, new calls go to the new one. Tokens are reissued (new nonce; old token invalidated after grace).
- Without `new_config`: re-imports the factory's module (for in-tree / local plugins under development). For pip-installed plugins, `reload` is a no-op + warning ("pip plugin reload requires process restart").

**`silence`**:
- Soft variant of remove. Worker stays loaded; routing avoids it; cap token unchanged.
- Optional `ttl_sec` — auto-unsilence after timeout.
- Useful for rate-limit bursts, A/B holdouts, debugging.

### 9.3 Concurrency

All five mutating operations are serialized through a single kernel-owned queue. Routing reads the worker registry under a copy-on-write snapshot — readers never block, writers atomically swap the pointer. This is enough at our scale (tens of workers, dispatches in the hundreds-per-minute, not microseconds).

### 9.4 Driver surfaces

| Surface | Endpoint                           | Notes                                              |
|---------|------------------------------------|----------------------------------------------------|
| CLI     | `python -m launcher.workers add\|remove\|reload\|silence\|list` | Wraps kernel methods; same arguments, same errors. |
| HTTP    | `POST /api/workers`, `DELETE /api/workers/{name}`, `POST /api/workers/{name}/reload`, etc. | Adds OpenAPI rows in `launcher/api_server.py`.     |
| GUI     | `gui/src/features/workers/` page   | `useMutation` calls into HTTP; renders WorkerView. |

All three accept the **same JSON config payload**, return the **same JSON `WorkerMetadata`**, and surface the **same structured error codes**. Help text comes from the same string table. This is the "one way" rule from §1.2.

---

## 10. Trust levels & sandboxing

A worker is one of:

| Trust | Transport     | Failure isolation                       | Suitable for              |
|-------|---------------|------------------------------------------|---------------------------|
| `trusted` | in-process | None — segfault kills kernel             | In-tree workers; user's own local plugins |
| `audited` | in-process | None at runtime; reviewed before install | First-party PyPI packages we maintain |
| `sandboxed` | subprocess | Process boundary; resource limits via `resource` module; no shared address space | Third-party plugins; experimental code |

The trust level lives in the manifest (`transport = "subprocess"` implies `sandboxed`). Operators can override trust in config (downgrade `trusted` to `sandboxed` is always allowed; upgrade requires explicit operator confirmation in CLI / GUI).

Sandboxing details (P3 territory; mentioning here so the boundary is clear):

- Subprocess started with restricted env, working dir, fd table.
- Memory cap via `resource.setrlimit(RLIMIT_AS, …)` on POSIX; job objects on Windows.
- Wall-clock budget enforced per-call by kernel-side `deadline_ms`.
- Subprocess can be killed without taking the kernel down.
- Forum access is mediated — sandboxed workers post via JSON-RPC notifications, not direct bus access.

We don't do containers / namespaces / seccomp. That's a layer too far for a personal-machine agent framework; defer indefinitely.

---

## 11. Versioning

### 11.1 API version (the wlwl-ass ↔ worker contract)

SemVer. Current: `1.0.0` (this ADR is the "1.0" baseline).

- **Major** — breaking: signature change, dataclass field removal, semantic shift. Plugins targeting older majors are rejected at registration with a clear error.
- **Minor** — additive: new optional hooks, new dataclass fields with defaults, new well-known capabilities. Old plugins still load.
- **Patch** — fixes / clarifications. No code-visible change.

The kernel publishes `kernel.supported_api_versions = ["1.x.x"]` and rejects anything outside. We keep one major old + one major new during deprecation periods.

### 11.2 Capability version

Per §6.2 — bake into the cap id (`vision.ocr.v1` vs `vision.ocr.v2`). Both versions can coexist. A request explicitly pins a version (or globs `vision.ocr.*` and accepts the highest installed).

### 11.3 Worker-level version

`WorkerMetadata.api_version` (the API the worker speaks) is independent from `package.version` (the worker's own version). The former gates compatibility; the latter is for humans reading logs.

---

## 12. Observability

Every public-API call is observable through three channels:

### 12.1 `audit` topic (forum)

Every add / remove / reload / silence / unsilence emits an event with: caller (CLI/HTTP/GUI), worker name, before/after metadata diff, reason, latency. ADR-0008 §5.2 already routes `audit` to the in-memory ring + spool file.

### 12.2 Per-worker metrics

Tracked by the kernel without plugin cooperation:

- `invocations_total{worker,capability,outcome}` (counter)
- `latency_ms{worker,capability}` (histogram)
- `cost_usd{worker,capability}` (counter)
- `in_flight{worker}` (gauge)
- `tokens_in{worker}` / `tokens_out{worker}` (counters; LLM workers only)

Stored in `temp/metrics.jsonl` (existing `launcher/metrics.py` format). The GUI Workers page renders rolling-window views; CLI `metrics --worker foo` dumps tables.

### 12.3 Health probe

The kernel polls each worker's `health()` every 30s by default (configurable). Three consecutive `down` reports auto-silence the worker with `reason="health_probe_failed"` and an `audit` event. Operator-initiated `unsilence` clears it; auto-silence does not auto-unsilence (intentional: don't flap).

---

## 13. Plugin distribution model

| Source        | Discovery            | Update path               | Trust default |
|---------------|----------------------|---------------------------|---------------|
| In-tree       | `llmcore/workers/`   | `git pull` + restart       | `trusted`     |
| Local plugin  | `<project>/.wlwl-ass/workers/` or `~/.wlwl-ass/workers/` | edit + `kernel reload <name>` | `trusted` (it's user's own code) |
| `pip install` | entry points         | `pip install -U <pkg>` + kernel restart | `sandboxed` (default; user can override per-worker) |

We don't run a registry. Discoverability is "search PyPI for `wlwl-ass-worker-*`". A future ADR could add a curated index, but we explicitly defer.

### 13.1 Skeleton for third-party authors

A `cookiecutter`-style template (out of scope for this ADR; future tooling) so plugin authors get manifest, factory stub, capability schema, tests, and CI in one shot. The relevant point for the design: we don't *need* a template to make plugins work; the manifest + factory + entry point is the entire requirement.

---

## 14. Phase plan

| Phase | Scope                                                                | Touches                                           |
|-------|----------------------------------------------------------------------|---------------------------------------------------|
| **P0** | This ADR. Schema sketches in code comments only.                    | docs/adr/0009-*.md (this file)                    |
| **P1** | Worker Protocol + WorkerFactory + CapabilityRegistry as Python types. In-tree `LLMWorker` wrapping existing sessions. Kernel.add/remove/reload/list with **trusted in-process only**. CLI driver. | `llmcore/worker.py`, `llmcore/workers/llm_worker.py`, `llmcore/capabilities.yaml`, `launcher/cli_workers.py` |
| **P2** | HTTP endpoints + GUI page. Audit events. Per-worker metrics. Health probe. | `launcher/api_server.py`, `gui/src/features/workers/` |
| **P3** | `SubprocessWorker` + sandboxed transport + JSON-RPC envelope. Local plugin discovery. | `llmcore/workers/subprocess_worker.py`           |
| **P4** | Entry-point discovery + manifest validation. Trust override UI. Plugin author docs / cookiecutter. | `llmcore/discovery.py`, docs                      |
| **P5** | `MCPWorker` migration: existing `tools/mcp_client.py` reframed as a worker; tools call `kernel.dispatch(required=["mcp.<tool>"])`. | `tools/mcp_client.py`                             |

Each phase is independently revertable. P1 alone delivers most of the operator-facing benefit (config-only add/remove with hot reload); P3+ unlock third-party extensibility.

---

## 15. Alternatives considered

### 15.1 Just expose `BaseSession` as the worker contract

Promote `BaseSession` to "the worker interface". Define `Worker` as `BaseSession` + a few extra methods.

- ✅ Zero new abstractions.
- ❌ `BaseSession` is HTTP/LLM-shaped (`raw_ask`, `make_messages`, `system`). Forcing OCR / MCP / tools / rule engines into it is the existing problem.
- ❌ Couples plugin authors to internal LLM-session semantics (history compression, token usage tracking) they shouldn't care about.
- **Rejected.**

### 15.2 Use gRPC as the wire format

Define worker-protocol in `.proto` files; use grpcio.

- ✅ Strong types end-to-end; codegen for many languages.
- ❌ Adds protobuf as a runtime dependency. We're stdlib-first by project rule (CONTRIBUTING.md). For a personal-machine agent, this is overkill.
- ❌ Cross-language workers are not a current need; subprocess + JSON-RPC handles the same isolation.
- **Rejected** for now. Reconsider if cross-language plugins become a real demand.

### 15.3 Function-based plugins (no `Worker` class)

A plugin = a top-level function with a decorator. Less ceremony.

- ✅ Trivial to write a plugin.
- ❌ No place to put state (sessions, caches, connection pools). Workers usually have state.
- ❌ Hooks (`on_silence`, `on_token_refresh`) become magic decorator stacks.
- **Rejected.** Protocols are barely heavier than functions and pay back in clarity.

### 15.4 Don't formalize at all; keep it implicit

Add workers by writing more `BaseSession` subclasses in `llmcore/`. No plugin system.

- ✅ Zero design overhead.
- ❌ Every new agent type requires a core PR. Blocks community contribution. Doesn't satisfy goal #2 from §1.2.
- **Rejected.**

---

## 16. Consequences

### 16.1 Pros

- ✅ **Plugin authors target one Python `Protocol`.** Clear, tested, versioned.
- ✅ **`add / remove / reload` is config-only or single-file.** Most operator scenarios stay out of the kernel codebase.
- ✅ **Subprocess sandbox is opt-in but not bolted-on.** The same `Worker` interface works in both transports — sandboxing is just a different factory.
- ✅ **Deprecation is graceful.** SemVer + `kernel.supported_api_versions` gives plugin authors a runway.
- ✅ **Three drivers, one truth.** CLI / HTTP / GUI are interchangeable.

### 16.2 Cons

- ⚠️ **Six interfaces is more than zero.** Plugin authors must learn the contract. Mitigation: cookiecutter template + a one-page guide; the in-tree `LLMWorker` doubles as a worked example.
- ⚠️ **Two transports = two failure modes.** A plugin author working in-process can develop a bug that only manifests under subprocess (e.g. relies on shared memory). Mitigation: `pytest` fixtures that exercise both transports; recommend authors target subprocess by default.
- ⚠️ **Capability registration is boilerplate.** Every new cap costs one YAML entry. Trade we accept for ACL safety.

### 16.3 Specific decisions revisited from ADR-0008

This ADR sharpens four loose ends in 0008:

| ADR-0008 said                                       | ADR-0009 sharpens to                                                |
|-----------------------------------------------------|----------------------------------------------------------------------|
| "hot-replace … atomically with the new worker's token issuance" (§4.4) | `Kernel.reload_worker` semantics in §9.2 (drain → revoke → swap) |
| "silenced=true" (§4.4)                              | `Kernel.silence(name, reason, ttl_sec=…)` with auto-revert + structured event (§9.2 / §12.1) |
| "MCP servers as workers" (§14 open question)        | `MCPWorker` is one of the in-tree factories; phase P5 migration plan (§14) |
| Worker = config row + LLM session (implicit)        | Worker = anything implementing `Worker` Protocol; LLM session is one of three in-tree implementations (§3) |

ADR-0008 §14 open question #4 is now resolved (MCP folds in via P5). The other 0008 open questions remain.

---

## 17. Open questions

1. **Streaming responses.** `InvokeResponse` is a single value today. For LLM streaming, the current `BaseSession.ask(stream=True)` returns a generator. Should `Worker.invoke()` get a streaming variant (`invoke_stream`), or should streaming live one layer above (kernel buffers a stream, dispatches as one InvokeResponse with chunks in `result["chunks"]`)? Defer to P2 — pick when a real consumer needs it.
2. **Cancellation.** No method to cancel an in-flight call. Today `ToolClient.cancel()` exists at a higher layer. Worker-side cancellation needs a story for subprocess workers (signal? RPC method? timeout-only?). Defer.
3. **Plugin sandboxing on Windows.** `resource.setrlimit` is POSIX-only. Job objects on Windows need a tiny C extension or a `subprocess` wrapper using `psutil`. Out of P3 scope; document as "subprocess sandbox is best-effort on Windows".
4. **Plugin distribution security.** Signing, integrity check, supply-chain. Out of scope; assume operator-installed plugins are trusted-before-install (same as any pip dep).
5. **Multi-tenant kernel.** ADR-0008 §13.3 explicitly defers; this ADR doesn't change that. If we ever want multi-tenant, every `Kernel.*` method gets a `tenant_id` parameter. Plan for it; don't build for it.

---

## 18. Status notes

This ADR is **Proposed**. No code, no breaking change. Implementation begins (if approved) at P1 — `llmcore/worker.py` with the Protocol, `LLMWorker` wrapping `BaseSession`, and `Kernel.add_worker / remove_worker / reload_worker` as in-process-only operations. P1 is roughly 800–1200 lines; subsequent phases are smaller.

Cross-references for reviewers:

- ADR-0008 — the kernel + workers + forum architecture this builds on.
- `BaseSession` (`llmcore/base.py`) — the existing LLM session shape that `LLMWorker` wraps in P1.
- `tools/mcp_client.py` — to be reframed as `MCPWorker` in P5.
- `launcher/api_config.py` — gets the `kind: kernel` addition (from ADR-0008) and per-worker manifest-validation (from this ADR).
- `CONTRIBUTING.md` — the "stdlib-first, self-documenting code, minimal change radius" principles guided several rejections in §15.
- ATTRIBUTION.md — original to wlwl-ass; no external borrowings introduced by this design.
