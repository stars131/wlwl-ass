"""CapabilityRegistry — declared capability ids + their request/response schemas.

Capability id format: ``<vendor>.<scope>.<action>.<vN>``
Built-in declarations live in capabilities.yaml (this module reads it lazily).
Third-party plugins register additional capabilities at import time via
``register_capability(...)``.

Validation:
  - tools' ``required_capabilities`` is checked at dispatch time against this.
  - workers' declared ``capabilities`` is checked at registration.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class CapabilityDecl:
    id: str
    description: str
    request_schema: dict | None = None
    response_schema: dict | None = None
    cost_estimate_usd: float = 0.0
    default_deadline_ms: int = 30_000
    owner_plugin: str | None = None
    extras: dict = field(default_factory=dict)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, CapabilityDecl] = {}
        self._lock = threading.RLock()

    def register(self, decl: CapabilityDecl) -> None:
        with self._lock:
            if decl.id in self._caps and self._caps[decl.id] != decl:
                # idempotent re-registration is OK; conflicting redefinition is not
                existing = self._caps[decl.id]
                if existing.owner_plugin != decl.owner_plugin:
                    raise ValueError(
                        f"capability {decl.id!r} already registered by "
                        f"{existing.owner_plugin}, cannot be redefined by {decl.owner_plugin}"
                    )
            self._caps[decl.id] = decl

    def get(self, cap_id: str) -> CapabilityDecl | None:
        with self._lock:
            return self._caps.get(cap_id)

    def has(self, cap_id: str) -> bool:
        return self.get(cap_id) is not None

    def all(self) -> list[CapabilityDecl]:
        with self._lock:
            return list(self._caps.values())

    def match_glob(self, pattern: str) -> list[CapabilityDecl]:
        """Match ``vision.ocr.*`` against registered ids."""
        if not pattern.endswith(".*"):
            d = self.get(pattern)
            return [d] if d else []
        prefix = pattern[:-1]
        with self._lock:
            return [d for d in self._caps.values() if d.id.startswith(prefix)]

    def validate_request(self, cap_id: str, payload: dict) -> tuple[bool, str]:
        decl = self.get(cap_id)
        if decl is None:
            return False, f"capability {cap_id!r} not declared"
        if decl.request_schema is None:
            return True, ""
        return _check_schema(payload, decl.request_schema)


def _check_schema(payload: Any, schema: dict) -> tuple[bool, str]:
    """Tiny JSON-Schema-ish checker — type + required fields only.

    Sufficient for the registered capabilities; we deliberately avoid a
    jsonschema dependency. If a schema needs fancier validation later we
    revisit (TODO: optional jsonschema if installed).
    """
    t = schema.get("type")
    if t == "object":
        if not isinstance(payload, dict):
            return False, f"expected object, got {type(payload).__name__}"
        required = schema.get("required", [])
        for key in required:
            if key not in payload:
                return False, f"missing required field {key!r}"
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in payload:
                ok, msg = _check_schema(payload[key], sub)
                if not ok:
                    return False, f"{key}: {msg}"
        return True, ""
    if t == "array":
        if not isinstance(payload, list):
            return False, f"expected array, got {type(payload).__name__}"
        items = schema.get("items")
        if items:
            for i, item in enumerate(payload):
                ok, msg = _check_schema(item, items)
                if not ok:
                    return False, f"[{i}]: {msg}"
        return True, ""
    if t == "string":
        if not isinstance(payload, str):
            return False, f"expected string, got {type(payload).__name__}"
        return True, ""
    if t == "integer":
        if not isinstance(payload, int) or isinstance(payload, bool):
            return False, f"expected integer, got {type(payload).__name__}"
        return True, ""
    if t == "number":
        if not isinstance(payload, (int, float)) or isinstance(payload, bool):
            return False, f"expected number, got {type(payload).__name__}"
        return True, ""
    if t == "boolean":
        if not isinstance(payload, bool):
            return False, f"expected boolean, got {type(payload).__name__}"
        return True, ""
    return True, ""  # unknown type: skip


# ── Built-in capability bootstrap ────────────────────────────────────────

_BUILTIN_PATH = os.path.join(_HERE, "capabilities.yaml")
_DEFAULT_REGISTRY: CapabilityRegistry | None = None


def default_registry() -> CapabilityRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        reg = CapabilityRegistry()
        _load_builtin(reg)
        _DEFAULT_REGISTRY = reg
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Test helper — wipes the singleton so tests can import a clean state."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


def register_capability(
    *, id: str, description: str, request_schema: dict | None = None,
    response_schema: dict | None = None, cost_estimate_usd: float = 0.0,
    default_deadline_ms: int = 30_000, owner_plugin: str | None = None,
    **extras: Any,
) -> CapabilityDecl:
    decl = CapabilityDecl(
        id=id, description=description,
        request_schema=request_schema, response_schema=response_schema,
        cost_estimate_usd=cost_estimate_usd,
        default_deadline_ms=default_deadline_ms,
        owner_plugin=owner_plugin, extras=dict(extras),
    )
    default_registry().register(decl)
    return decl


def _load_builtin(reg: CapabilityRegistry) -> None:
    if not os.path.isfile(_BUILTIN_PATH):
        return
    text = open(_BUILTIN_PATH, encoding="utf-8").read()
    items = _parse_yaml_list(text)
    for item in items:
        reg.register(CapabilityDecl(
            id=item["id"],
            description=item.get("description", ""),
            request_schema=item.get("request_schema"),
            response_schema=item.get("response_schema"),
            cost_estimate_usd=float(item.get("cost_estimate_usd", 0.0)),
            default_deadline_ms=int(item.get("default_deadline_ms", 30_000)),
            owner_plugin="wlwl-ass-builtin",
            extras=item.get("extras", {}),
        ))


def _parse_yaml_list(text: str) -> list[dict]:
    """Tiny YAML subset parser — only handles the shape we ship.

    Specifically: top-level list of mappings. Mapping values may be strings,
    numbers, booleans, or nested objects expressed as JSON (``{...}``) or
    inline arrays (``[...]``). We don't pull in PyYAML for one config file.
    """
    out: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- "):
            if cur is not None:
                out.append(cur)
            cur = {}
            tail = line[2:].strip()
            if tail and ":" in tail:
                k, v = tail.split(":", 1)
                cur[k.strip()] = _coerce(v.strip())
        elif cur is not None and line.startswith("  ") and ":" in line:
            stripped = line.strip()
            k, v = stripped.split(":", 1)
            cur[k.strip()] = _coerce(v.strip())
    if cur is not None:
        out.append(cur)
    return out


def _coerce(v: str) -> Any:
    if not v:
        return ""
    if v.startswith(("{", "[")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    if v in ("true", "false"):
        return v == "true"
    if v == "null":
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            return v[1:-1]
        return v
