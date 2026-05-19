"""Provider preset library for wlwl-ass.

Inspired by cc-switch (https://github.com/farion1231/cc-switch, MIT) which
ships 50+ preset endpoints for Claude Code / Codex / Gemini CLI etc. This
module mirrors that library shape for wlwl-ass's two native kinds:

    - ``native_claude`` — Anthropic Messages API format
    - ``native_oai``    — OpenAI Chat Completions format

Each preset is a self-contained dict that
:func:`launcher.api_config.normalize_config` will accept after a few field
defaults are filled in (api_kind, apibase, model). The user provides only
``name`` (free-form override) and ``apikey``; everything else is copied
from the preset.

Layout choices:

- ``id`` is a lowercase-kebab slug, stable across releases — CLI flags
  reference presets by this id.
- ``provider`` is the human-readable label (matches cc-switch where
  applicable, so users coming from cc-switch see familiar names).
- ``kind`` maps to the wlwl-ass config kind.
- ``category`` is one of ``official | cn_official | aggregator | third_party``.
- ``website`` and ``apikey_url`` are informational links shown by
  ``wlwl config presets`` and ``--detail``.

This is data, not behavior — no network calls happen on import. The
preset table is small enough to keep inline; adding a new entry takes one
dict literal and zero code changes.
"""
from __future__ import annotations

from typing import Iterable

# A preset row. Field semantics:
#   id           – stable slug, unique within the table; CLI/REPL uses it
#   provider     – display label (matches cc-switch's naming where possible)
#   kind         – "native_oai" or "native_claude"
#   apibase      – fully qualified URL, no trailing slash
#   model        – suggested default model
#   category     – grouping for `--category` filters
#   website      – marketing / docs page
#   apikey_url   – direct link to the key issuance page (optional)
#   notes        – one-liner; shown in `presets --detail` (optional)
PRESETS: list[dict] = [
    # ── Official APIs ──────────────────────────────────────────────────
    {
        "id": "anthropic",
        "provider": "Claude Official",
        "kind": "native_claude",
        "apibase": "https://api.anthropic.com",
        "model": "claude-opus-4-7",
        "category": "official",
        "website": "https://www.anthropic.com/claude",
        "apikey_url": "https://console.anthropic.com/account/keys",
        "notes": "Anthropic official API. Use sk-ant-... keys.",
    },
    {
        "id": "openai",
        "provider": "OpenAI Official",
        "kind": "native_oai",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "category": "official",
        "website": "https://platform.openai.com",
        "apikey_url": "https://platform.openai.com/api-keys",
    },
    {
        "id": "gemini-native",
        "provider": "Google Gemini",
        "kind": "native_claude",
        "apibase": "https://generativelanguage.googleapis.com",
        "model": "gemini-2.5-pro",
        "category": "official",
        "website": "https://ai.google.dev/gemini-api",
        "apikey_url": "https://aistudio.google.com/app/apikey",
        "notes": "Anthropic-format adapter onto Gemini.",
    },
    {
        "id": "azure-openai",
        "provider": "Azure OpenAI",
        "kind": "native_oai",
        "apibase": "https://YOUR_RESOURCE_NAME.openai.azure.com/openai",
        "model": "gpt-4.1",
        "category": "official",
        "website": "https://learn.microsoft.com/azure/ai-services/openai",
        "notes": "Replace YOUR_RESOURCE_NAME and ?api-version=... per deployment.",
    },
    {
        "id": "aws-bedrock",
        "provider": "AWS Bedrock",
        "kind": "native_claude",
        "apibase": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "model": "anthropic.claude-opus-4-1",
        "category": "official",
        "website": "https://aws.amazon.com/bedrock/",
        "notes": "AKSK signing required; not a plain bearer-token endpoint.",
    },

    # ── Chinese official APIs ──────────────────────────────────────────
    {
        "id": "deepseek",
        "provider": "DeepSeek",
        "kind": "native_claude",
        "apibase": "https://api.deepseek.com/anthropic",
        "model": "deepseek-chat",
        "category": "cn_official",
        "website": "https://platform.deepseek.com",
        "apikey_url": "https://platform.deepseek.com/api_keys",
    },
    {
        "id": "deepseek-oai",
        "provider": "DeepSeek (OpenAI-compat)",
        "kind": "native_oai",
        "apibase": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "category": "cn_official",
        "website": "https://platform.deepseek.com",
    },
    {
        "id": "kimi",
        "provider": "Kimi (Moonshot)",
        "kind": "native_claude",
        "apibase": "https://api.moonshot.cn/anthropic",
        "model": "kimi-k2.6",
        "category": "cn_official",
        "website": "https://platform.moonshot.cn/console",
    },
    {
        "id": "kimi-coding",
        "provider": "Kimi For Coding",
        "kind": "native_claude",
        "apibase": "https://api.kimi.com/coding",
        "model": "kimi-k2.6",
        "category": "cn_official",
        "website": "https://www.kimi.com/code/docs/",
    },
    {
        "id": "kimi-oai",
        "provider": "Kimi (OpenAI-compat)",
        "kind": "native_oai",
        "apibase": "https://api.moonshot.cn/v1",
        "model": "kimi-k2-0905-preview",
        "category": "cn_official",
        "website": "https://platform.moonshot.cn",
    },
    {
        "id": "glm",
        "provider": "Zhipu GLM",
        "kind": "native_claude",
        "apibase": "https://open.bigmodel.cn/api/anthropic",
        "model": "glm-4.6",
        "category": "cn_official",
        "website": "https://open.bigmodel.cn",
    },
    {
        "id": "glm-en",
        "provider": "Zhipu GLM (z.ai)",
        "kind": "native_claude",
        "apibase": "https://api.z.ai/api/anthropic",
        "model": "glm-4.6",
        "category": "cn_official",
        "website": "https://z.ai",
    },
    {
        "id": "glm-oai",
        "provider": "Zhipu GLM (OpenAI-compat)",
        "kind": "native_oai",
        "apibase": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.6",
        "category": "cn_official",
        "website": "https://open.bigmodel.cn",
    },
    {
        "id": "bailian",
        "provider": "Aliyun Bailian",
        "kind": "native_claude",
        "apibase": "https://dashscope.aliyuncs.com/apps/anthropic",
        "model": "qwen3-max",
        "category": "cn_official",
        "website": "https://bailian.console.aliyun.com",
    },
    {
        "id": "bailian-coding",
        "provider": "Aliyun Bailian For Coding",
        "kind": "native_claude",
        "apibase": "https://coding.dashscope.aliyuncs.com/apps/anthropic",
        "model": "qwen3-coder-plus",
        "category": "cn_official",
        "website": "https://bailian.console.aliyun.com",
    },
    {
        "id": "qianfan",
        "provider": "Baidu Qianfan Coding",
        "kind": "native_claude",
        "apibase": "https://qianfan.baidubce.com/anthropic/coding",
        "model": "ernie-4.5-turbo-128k",
        "category": "cn_official",
        "website": "https://qianfan.cloud.baidu.com",
    },
    {
        "id": "doubao-seed",
        "provider": "Volcengine DouBao Seed",
        "kind": "native_claude",
        "apibase": "https://ark.cn-beijing.volces.com/api/compatible",
        "model": "doubao-seed-2-0-code-preview-latest",
        "category": "cn_official",
        "website": "https://console.volcengine.com/ark",
    },
    {
        "id": "ark-agentplan",
        "provider": "Volcengine Ark Agentplan",
        "kind": "native_claude",
        "apibase": "https://ark.cn-beijing.volces.com/api/coding",
        "model": "ark-code-latest",
        "category": "cn_official",
        "website": "https://www.volcengine.com/activity/agentplan",
    },
    {
        "id": "byteplus",
        "provider": "BytePlus",
        "kind": "native_claude",
        "apibase": "https://ark.ap-southeast.bytepluses.com/api/coding",
        "model": "ark-code-latest",
        "category": "cn_official",
        "website": "https://www.byteplus.com/en/product/modelark",
    },
    {
        "id": "stepfun",
        "provider": "StepFun",
        "kind": "native_claude",
        "apibase": "https://api.stepfun.com/step_plan",
        "model": "step-3.5-flash-2603",
        "category": "cn_official",
        "website": "https://platform.stepfun.com",
        "apikey_url": "https://platform.stepfun.com/interface-key",
    },
    {
        "id": "stepfun-en",
        "provider": "StepFun (global)",
        "kind": "native_claude",
        "apibase": "https://api.stepfun.ai/step_plan",
        "model": "step-3.5-flash-2603",
        "category": "cn_official",
        "website": "https://platform.stepfun.ai",
    },
    {
        "id": "modelscope",
        "provider": "ModelScope",
        "kind": "native_claude",
        "apibase": "https://api-inference.modelscope.cn",
        "model": "Qwen/Qwen3-Coder-480B-A35B-Instruct",
        "category": "cn_official",
        "website": "https://modelscope.cn",
    },
    {
        "id": "longcat",
        "provider": "Longcat",
        "kind": "native_claude",
        "apibase": "https://api.longcat.chat/anthropic",
        "model": "longcat-flash",
        "category": "cn_official",
        "website": "https://longcat.chat",
    },
    {
        "id": "minimax",
        "provider": "MiniMax",
        "kind": "native_claude",
        "apibase": "https://api.minimaxi.com/anthropic",
        "model": "MiniMax-M2",
        "category": "cn_official",
        "website": "https://platform.minimaxi.com",
    },
    {
        "id": "minimax-en",
        "provider": "MiniMax (global)",
        "kind": "native_claude",
        "apibase": "https://api.minimax.io/anthropic",
        "model": "MiniMax-M2",
        "category": "cn_official",
        "website": "https://www.minimax.io",
    },
    {
        "id": "kat-coder",
        "provider": "KAT-Coder",
        "kind": "native_claude",
        "apibase": "https://api.tbox.cn/api/anthropic",
        "model": "kat-coder-pro-v1",
        "category": "cn_official",
        "website": "https://tbox.cn",
    },
    {
        "id": "xiaomi-mimo",
        "provider": "Xiaomi MiMo",
        "kind": "native_claude",
        "apibase": "https://api.xiaomimimo.com/anthropic",
        "model": "mimo-coder-7b",
        "category": "cn_official",
        "website": "https://xiaomimimo.com",
    },
    {
        "id": "siliconflow",
        "provider": "SiliconFlow",
        "kind": "native_claude",
        "apibase": "https://api.siliconflow.cn",
        "model": "Qwen/Qwen3-Coder-480B-A35B-Instruct",
        "category": "cn_official",
        "website": "https://siliconflow.cn",
    },
    {
        "id": "siliconflow-en",
        "provider": "SiliconFlow (global)",
        "kind": "native_claude",
        "apibase": "https://api.siliconflow.com",
        "model": "Qwen/Qwen3-Coder-480B-A35B-Instruct",
        "category": "cn_official",
        "website": "https://siliconflow.com",
    },
    {
        "id": "nvidia",
        "provider": "NVIDIA NIM",
        "kind": "native_claude",
        "apibase": "https://integrate.api.nvidia.com",
        "model": "deepseek-ai/deepseek-r1",
        "category": "cn_official",
        "website": "https://build.nvidia.com",
    },

    # ── Aggregators & community relays ─────────────────────────────────
    {
        "id": "openrouter",
        "provider": "OpenRouter (Claude-format)",
        "kind": "native_claude",
        "apibase": "https://openrouter.ai/api",
        "model": "anthropic/claude-opus-4-1",
        "category": "aggregator",
        "website": "https://openrouter.ai",
        "apikey_url": "https://openrouter.ai/keys",
    },
    {
        "id": "openrouter-oai",
        "provider": "OpenRouter (OpenAI-format)",
        "kind": "native_oai",
        "apibase": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4.1",
        "category": "aggregator",
        "website": "https://openrouter.ai",
    },
    {
        "id": "therouter",
        "provider": "TheRouter",
        "kind": "native_claude",
        "apibase": "https://api.therouter.ai",
        "model": "claude-opus-4-1",
        "category": "aggregator",
        "website": "https://therouter.ai",
    },
    {
        "id": "novita",
        "provider": "Novita AI",
        "kind": "native_claude",
        "apibase": "https://api.novita.ai/anthropic",
        "model": "anthropic/claude-opus-4-1",
        "category": "aggregator",
        "website": "https://novita.ai",
    },
    {
        "id": "aihubmix",
        "provider": "AiHubMix",
        "kind": "native_claude",
        "apibase": "https://aihubmix.com",
        "model": "claude-opus-4-1",
        "category": "aggregator",
        "website": "https://aihubmix.com",
    },
    {
        "id": "aihubmix-oai",
        "provider": "AiHubMix (OpenAI-format)",
        "kind": "native_oai",
        "apibase": "https://aihubmix.com/v1",
        "model": "gpt-4.1",
        "category": "aggregator",
        "website": "https://aihubmix.com",
    },
    {
        "id": "shengsuanyun",
        "provider": "Shengsuanyun",
        "kind": "native_claude",
        "apibase": "https://router.shengsuanyun.com/api",
        "model": "claude-opus-4-1",
        "category": "aggregator",
        "website": "https://www.shengsuanyun.com",
    },
    {
        "id": "dmxapi",
        "provider": "DMXAPI",
        "kind": "native_claude",
        "apibase": "https://www.dmxapi.cn",
        "model": "claude-opus-4-1",
        "category": "aggregator",
        "website": "https://www.dmxapi.cn",
    },
    {
        "id": "dmxapi-oai",
        "provider": "DMXAPI (OpenAI-format)",
        "kind": "native_oai",
        "apibase": "https://www.dmxapi.cn/v1",
        "model": "gpt-4.1",
        "category": "aggregator",
        "website": "https://www.dmxapi.cn",
    },
    {
        "id": "packycode",
        "provider": "PackyCode",
        "kind": "native_claude",
        "apibase": "https://www.packyapi.com",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://www.packyapi.com",
    },
    {
        "id": "packycode-oai",
        "provider": "PackyCode (OpenAI-format)",
        "kind": "native_oai",
        "apibase": "https://www.packyapi.com/v1",
        "model": "gpt-4.1",
        "category": "third_party",
        "website": "https://www.packyapi.com",
    },
    {
        "id": "claudeapi",
        "provider": "ClaudeAPI",
        "kind": "native_claude",
        "apibase": "https://gw.claudeapi.com",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://claudeapi.com",
    },
    {
        "id": "claudecn",
        "provider": "ClaudeCN",
        "kind": "native_claude",
        "apibase": "https://claudecn.top",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://claudecn.top",
    },
    {
        "id": "runapi",
        "provider": "RunAPI",
        "kind": "native_claude",
        "apibase": "https://runapi.co",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://runapi.co",
    },
    {
        "id": "relaxycode",
        "provider": "RelaxyCode",
        "kind": "native_claude",
        "apibase": "https://www.relaxycode.com",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://www.relaxycode.com",
    },
    {
        "id": "cubence",
        "provider": "Cubence",
        "kind": "native_claude",
        "apibase": "https://api.cubence.com",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://cubence.com",
    },
    {
        "id": "aigocode",
        "provider": "AIGoCode",
        "kind": "native_claude",
        "apibase": "https://api.aigocode.com",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://aigocode.com",
    },
    {
        "id": "rightcode",
        "provider": "RightCode",
        "kind": "native_claude",
        "apibase": "https://www.right.codes/claude",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://right.codes",
    },
    {
        "id": "aicodemirror",
        "provider": "AICodeMirror",
        "kind": "native_claude",
        "apibase": "https://api.aicodemirror.com/api/claudecode",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://aicodemirror.com",
    },
    {
        "id": "aicoding",
        "provider": "AICoding",
        "kind": "native_claude",
        "apibase": "https://api.aicoding.sh",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://aicoding.sh",
    },
    {
        "id": "crazyrouter",
        "provider": "CrazyRouter",
        "kind": "native_claude",
        "apibase": "https://cn.crazyrouter.com",
        "model": "claude-opus-4-1",
        "category": "aggregator",
        "website": "https://crazyrouter.com",
    },
    {
        "id": "sssaicode",
        "provider": "SSSAiCode",
        "kind": "native_claude",
        "apibase": "https://node-hk.sssaicode.com/api",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://sssaicode.com",
    },
    {
        "id": "compshare",
        "provider": "Compshare",
        "kind": "native_claude",
        "apibase": "https://cp.compshare.cn",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://compshare.cn",
    },
    {
        "id": "micu",
        "provider": "Micu API",
        "kind": "native_claude",
        "apibase": "https://www.micuapi.ai",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://www.micuapi.ai",
    },
    {
        "id": "ctok",
        "provider": "CTok.ai",
        "kind": "native_claude",
        "apibase": "https://api.ctok.ai",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://ctok.ai",
    },
    {
        "id": "lemondata",
        "provider": "LemonData",
        "kind": "native_claude",
        "apibase": "https://api.lemondata.cc",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://lemondata.cc",
    },
    {
        "id": "lioncc",
        "provider": "LionCCAPI",
        "kind": "native_claude",
        "apibase": "https://vibecodingapi.ai",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://vibecodingapi.ai",
    },
    {
        "id": "eflowcode",
        "provider": "E-FlowCode",
        "kind": "native_claude",
        "apibase": "https://e-flowcode.cc",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://e-flowcode.cc",
    },
    {
        "id": "pateway",
        "provider": "PatewayAI",
        "kind": "native_claude",
        "apibase": "https://api.pateway.ai",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://pateway.ai",
    },
    {
        "id": "pipellm",
        "provider": "PIPELLM",
        "kind": "native_claude",
        "apibase": "https://cc-api.pipellm.ai",
        "model": "claude-opus-4-1",
        "category": "third_party",
        "website": "https://pipellm.ai",
    },
    {
        "id": "newapi",
        "provider": "NewAPI (self-hosted gateway)",
        "kind": "native_oai",
        "apibase": "https://your-newapi-host/v1",
        "model": "gpt-4.1",
        "category": "third_party",
        "website": "https://github.com/Calcium-Ion/new-api",
        "notes": "Replace your-newapi-host with the gateway you control.",
    },
]

CATEGORIES = ("official", "cn_official", "aggregator", "third_party")


def list_presets(category: str | None = None,
                 kind: str | None = None) -> list[dict]:
    """Return a filtered copy of :data:`PRESETS`.

    The original list is not mutated; callers can safely sort/edit the
    returned copies. Filters are AND-combined."""
    rows = list(PRESETS)
    if category:
        rows = [r for r in rows if r.get("category") == category]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return [dict(r) for r in rows]


def get_preset(preset_id: str) -> dict | None:
    """Look up a preset by its lowercase-kebab id. Returns None if absent."""
    needle = (preset_id or "").strip().lower()
    if not needle:
        return None
    for row in PRESETS:
        if row["id"] == needle:
            return dict(row)
    return None


def find_preset(query: str) -> list[dict]:
    """Loose search over id + provider + website. Used by `wlwl config presets QUERY`."""
    q = (query or "").strip().lower()
    if not q:
        return [dict(r) for r in PRESETS]
    hits = []
    for r in PRESETS:
        hay = (r["id"] + " " + r["provider"] + " " + r.get("website", "")).lower()
        if q in hay:
            hits.append(dict(r))
    return hits


def preset_to_config(preset: dict, name: str | None = None,
                     apikey: str | None = None) -> dict:
    """Convert a preset row into a :mod:`launcher.api_config`-compatible
    config dict. ``name`` defaults to the preset id; ``apikey`` left empty
    when not provided so the validator complains until the user fills it."""
    return {
        "kind": preset["kind"],
        "name": (name or preset["id"]).strip(),
        "apikey": (apikey or "").strip(),
        "apibase": preset["apibase"],
        "model": preset["model"],
    }


def _all_ids() -> Iterable[str]:
    """Iterator used by tests + CLI validation."""
    return (r["id"] for r in PRESETS)
