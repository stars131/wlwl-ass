"""Adapters for the unified IM gateway.

Each module in this package exports a class implementing
``frontends.gateway.PlatformAdapter``. The gateway loads adapters lazily
so missing optional deps (e.g. ``lark_oapi``) don't break the rest of
the runtime.
"""
