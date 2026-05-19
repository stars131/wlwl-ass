"""``wlwl config`` subcommand — terminal-side API config management.

Mirrors the cc-switch GUI surface but as Unix-style subcommands:

    wlwl config list                       show installed configs
    wlwl config presets [QUERY]            browse the preset library
    wlwl config add --preset ID [--name N] [--apikey KEY]
                                           create a config from a preset
    wlwl config add --name N --kind K --apibase URL --model M --apikey KEY
                                           free-form add
    wlwl config use NAME                   reorder NAME to llm_no=0 (next start)
    wlwl config remove NAME                delete by name
    wlwl config import URL                 import from wlwl-config:// deep link
    wlwl config export                     dump (apikeys masked) as JSON to stdout
    wlwl config probe NAME|--preset ID     measure latency to each apibase
    wlwl config backups                    list rotated snapshots

The shared agent state lives in ``temp/launcher_api_configs.json`` (see
``launcher.api_config``); writes go through ``save_api_configs`` so the
existing validate / backup / atomic-rename path is preserved.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from launcher import api_config, api_endpoint_probe, api_presets

# Force stdout/stderr into ``errors='replace'`` so unicode characters not
# in the current console codepage (e.g. ✓ on Windows GBK) print as ``?``
# instead of crashing the subcommand. Imported lazily — the helper lives
# in agent_loop and is itself stdlib-only.
try:
    from agent_loop import ensure_safe_std_streams
    ensure_safe_std_streams()
except Exception:
    pass


def _project_root() -> str:
    """Project root for config IO. Honours ``WLWL_PROJECT_ROOT`` env var so
    the same `wlwl config ...` works from outside the source tree."""
    return os.environ.get("WLWL_PROJECT_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )


# ── helpers ───────────────────────────────────────────────────────────────


def _find_by_name(configs: list[dict], name: str) -> tuple[int, dict] | None:
    needle = (name or "").strip()
    if not needle:
        return None
    for i, c in enumerate(configs):
        if str(c.get("name") or "") == needle:
            return i, c
    return None


def _print_table(rows: list[list[str]], header: list[str]) -> None:
    """Plain-text aligned table — no extra dep needed."""
    cols = list(zip(header, *rows)) if rows else [[h] for h in header]
    widths = [max(len(str(cell)) for cell in col) for col in cols]
    sep = "  "
    def fmt(line):
        return sep.join(str(c).ljust(w) for c, w in zip(line, widths))
    print(fmt(header))
    print(sep.join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


def _short(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


# ── subcommand implementations ────────────────────────────────────────────


def cmd_list(args) -> int:
    base = _project_root()
    configs = api_config.list_api_configs(base)
    if not configs:
        print("(no API configs yet — try `wlwl config presets` to pick one)")
        return 0
    rows = []
    for i, c in enumerate(configs):
        rows.append([
            str(i),
            _short(c.get("name", ""), 28),
            c.get("kind", ""),
            _short(c.get("apibase", ""), 50),
            _short(c.get("model", ""), 30),
            "yes" if c.get("apikey") else "-",
        ])
    _print_table(rows, ["#", "name", "kind", "apibase", "model", "key"])
    return 0


def cmd_presets(args) -> int:
    rows_data = api_presets.find_preset(args.query or "")
    if args.category:
        rows_data = [r for r in rows_data if r.get("category") == args.category]
    if args.kind:
        rows_data = [r for r in rows_data if r.get("kind") == args.kind]
    if not rows_data:
        print("(no presets match — try `wlwl config presets` without filters)")
        return 0
    if args.detail:
        for r in rows_data:
            print(f"\n[{r['id']}]  {r['provider']}")
            print(f"  kind     : {r['kind']}")
            print(f"  apibase  : {r['apibase']}")
            print(f"  model    : {r['model']}")
            print(f"  category : {r.get('category', '-')}")
            if r.get("website"):
                print(f"  website  : {r['website']}")
            if r.get("apikey_url"):
                print(f"  apikey   : {r['apikey_url']}")
            if r.get("notes"):
                print(f"  notes    : {r['notes']}")
        print()
        return 0
    rows = []
    for r in rows_data:
        rows.append([
            r["id"],
            _short(r["provider"], 28),
            r["kind"],
            r.get("category", "-"),
            _short(r["apibase"], 50),
        ])
    _print_table(rows, ["id", "provider", "kind", "category", "apibase"])
    print(f"\n{len(rows)} preset(s). Use `wlwl config add --preset <id>` to install one.")
    return 0


def _read_apikey_interactive(prompt: str = "API key: ") -> str:
    """Read an apikey from stdin, falling back gracefully when stdin is
    not a TTY (piped input). We avoid getpass on Windows redirected
    stdin which can hang."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    try:
        import getpass
        return getpass.getpass(prompt)
    except Exception:
        return input(prompt).strip()


def cmd_add(args) -> int:
    base = _project_root()
    configs = api_config.load_api_configs(base)

    if args.preset:
        preset = api_presets.get_preset(args.preset)
        if not preset:
            print(f"[wlwl config] unknown preset: {args.preset}", file=sys.stderr)
            print("Try `wlwl config presets` to list available preset ids.",
                  file=sys.stderr)
            return 2
        new = api_presets.preset_to_config(
            preset,
            name=args.name or preset["id"],
            apikey=args.apikey,
        )
        if args.apibase:
            new["apibase"] = args.apibase
        if args.model:
            new["model"] = args.model
    else:
        # Free-form
        missing = []
        if not args.name:
            missing.append("--name")
        if not args.kind:
            missing.append("--kind")
        if not args.apibase:
            missing.append("--apibase")
        if not args.model:
            missing.append("--model")
        if missing:
            print(
                f"[wlwl config] missing required flags (free-form add): "
                f"{', '.join(missing)}",
                file=sys.stderr,
            )
            print("Alternative: `wlwl config add --preset <id>` for a one-liner.",
                  file=sys.stderr)
            return 2
        new = {
            "kind": args.kind,
            "name": args.name,
            "apikey": args.apikey or "",
            "apibase": args.apibase,
            "model": args.model,
        }

    if not new["apikey"]:
        new["apikey"] = _read_apikey_interactive(
            f"API key for {new['name']!r}: "
        )
    if not new["apikey"]:
        print("[wlwl config] no apikey provided — aborting.", file=sys.stderr)
        return 2

    existing = _find_by_name(configs, new["name"])
    if existing is not None:
        idx, _prev = existing
        if not args.force:
            print(
                f"[wlwl config] a config named {new['name']!r} already exists. "
                f"Use --force to overwrite, or pass --name to use a different name.",
                file=sys.stderr,
            )
            return 2
        configs[idx] = new
        action = "replaced"
    else:
        configs.append(new)
        action = "added"

    try:
        api_config.save_api_configs(base, configs)
    except ValueError as exc:
        print(f"[wlwl config] save failed: {exc}", file=sys.stderr)
        return 2
    print(f"[wlwl config] {action} {new['name']!r} → {new['apibase']} ({new['model']})")
    return 0


def cmd_remove(args) -> int:
    base = _project_root()
    configs = api_config.load_api_configs(base)
    existing = _find_by_name(configs, args.name)
    if existing is None:
        print(f"[wlwl config] no such config: {args.name!r}", file=sys.stderr)
        return 1
    idx, _ = existing
    del configs[idx]
    api_config.save_api_configs(base, configs)
    print(f"[wlwl config] removed {args.name!r}")
    return 0


def cmd_use(args) -> int:
    """Reorder so ``name`` is at index 0 — that's the default llm_no when
    the agent next starts (or until the user `/llm`s elsewhere)."""
    base = _project_root()
    configs = api_config.load_api_configs(base)
    existing = _find_by_name(configs, args.name)
    if existing is None:
        print(f"[wlwl config] no such config: {args.name!r}", file=sys.stderr)
        return 1
    idx, row = existing
    if idx == 0:
        print(f"[wlwl config] {args.name!r} is already first.")
        return 0
    configs.insert(0, configs.pop(idx))
    api_config.save_api_configs(base, configs)
    print(f"[wlwl config] {args.name!r} is now llm_no=0 for the next session.")
    return 0


def cmd_import(args) -> int:
    try:
        cfg = api_endpoint_probe.parse_deep_link(args.url)
    except ValueError as exc:
        print(f"[wlwl config] import failed: {exc}", file=sys.stderr)
        return 2
    if not cfg.get("apikey"):
        cfg["apikey"] = _read_apikey_interactive(
            f"API key for {cfg['name']!r}: "
        )
    if not cfg.get("apikey"):
        print("[wlwl config] no apikey provided — aborting.", file=sys.stderr)
        return 2
    base = _project_root()
    configs = api_config.load_api_configs(base)
    existing = _find_by_name(configs, cfg["name"])
    if existing is not None and not args.force:
        print(
            f"[wlwl config] {cfg['name']!r} already exists; use --force to overwrite.",
            file=sys.stderr,
        )
        return 2
    if existing is not None:
        configs[existing[0]] = cfg
    else:
        configs.append(cfg)
    api_config.save_api_configs(base, configs)
    print(f"[wlwl config] imported {cfg['name']!r} → {cfg['apibase']} ({cfg['model']})")
    return 0


def cmd_export(args) -> int:
    base = _project_root()
    configs = api_config.list_api_configs(base)  # masked
    if args.unmask:
        # ``list_api_configs`` masks apikeys; if --unmask, re-read raw.
        # Print a warning so the user notices.
        print(
            "[wlwl config] --unmask: writing real apikeys to stdout. "
            "Pipe to a file ONLY if you trust the destination.",
            file=sys.stderr,
        )
        configs = api_config.load_api_configs(base)
    print(json.dumps({"configs": configs}, ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args) -> int:
    """Run a TCP probe against the candidate URLs and report latency."""
    if args.preset:
        preset = api_presets.get_preset(args.preset)
        if not preset:
            print(f"[wlwl config] unknown preset: {args.preset}", file=sys.stderr)
            return 2
        urls = [preset["apibase"]]
    elif args.name:
        base = _project_root()
        existing = _find_by_name(api_config.load_api_configs(base), args.name)
        if existing is None:
            print(f"[wlwl config] no such config: {args.name!r}", file=sys.stderr)
            return 1
        urls = [existing[1].get("apibase", "")]
    elif args.url:
        urls = [args.url]
    else:
        print(
            "[wlwl config probe] specify NAME, --preset ID, or --url URL.",
            file=sys.stderr,
        )
        return 2
    print(f"[wlwl config] probing {len(urls)} URL(s) (timeout {args.timeout}s)…")
    rows = []
    for u, ms in api_endpoint_probe.probe_all(urls, timeout_s=args.timeout):
        rows.append([u, f"{ms} ms" if ms is not None else "failed"])
    _print_table(rows, ["url", "latency"])
    return 0


def cmd_backups(args) -> int:
    base = _project_root()
    backups = api_config.list_backups(base)
    if not backups:
        print("(no rotated backups yet)")
        return 0
    rows = []
    import time as _t
    for mtime, path in backups:
        ts = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(mtime))
        rows.append([ts, os.path.basename(path)])
    _print_table(rows, ["modified", "file"])
    print(f"\n{len(backups)} backup(s). Copy one over launcher_api_configs.json to restore.")
    return 0


_TUNE_INT_FIELDS = {"max_tokens", "max_retries", "thinking_budget_tokens"}
_TUNE_FLOAT_FIELDS = {"temperature", "connect_timeout", "read_timeout"}
_TUNE_BOOL_FIELDS = {"stream", "fake_cc_system_prompt", "audio_capable", "image_capable"}
_TUNE_STR_FIELDS = {"reasoning_effort", "thinking_type", "api_mode", "apibase", "model"}
_TUNE_ALLOWED = (
    _TUNE_INT_FIELDS
    | _TUNE_FLOAT_FIELDS
    | _TUNE_BOOL_FIELDS
    | _TUNE_STR_FIELDS
)


def _parse_tune_value(field: str, raw: str):
    if field in _TUNE_BOOL_FIELDS:
        v = raw.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"{field} must be true/false, got {raw!r}")
    if field in _TUNE_INT_FIELDS:
        if raw.strip() == "":
            return None
        return int(raw)
    if field in _TUNE_FLOAT_FIELDS:
        if raw.strip() == "":
            return None
        return float(raw)
    return raw


def cmd_tune(args) -> int:
    """Set one or more advanced fields on an existing config in-place.

    Example:
        wlwl config tune my-claude reasoning_effort=high thinking_type=enabled thinking_budget_tokens=4096
    """
    base = _project_root()
    configs = api_config.load_api_configs(base)
    existing = _find_by_name(configs, args.name)
    if existing is None:
        print(f"[wlwl config] no such config: {args.name!r}", file=sys.stderr)
        return 1
    idx, row = existing

    updates: dict[str, object] = {}
    for assignment in args.assignments:
        if "=" not in assignment:
            print(
                f"[wlwl config] bad assignment {assignment!r}; expected field=value",
                file=sys.stderr,
            )
            return 2
        field, raw = assignment.split("=", 1)
        field = field.strip()
        if field not in _TUNE_ALLOWED:
            print(
                f"[wlwl config] unknown field {field!r}. Allowed: "
                f"{', '.join(sorted(_TUNE_ALLOWED))}",
                file=sys.stderr,
            )
            return 2
        try:
            value = _parse_tune_value(field, raw)
        except ValueError as exc:
            print(f"[wlwl config] {exc}", file=sys.stderr)
            return 2
        if (
            field == "reasoning_effort"
            and value
            and value not in api_config.REASONING_EFFORT_VALUES
        ):
            print(
                f"[wlwl config] reasoning_effort must be one of "
                f"{api_config.REASONING_EFFORT_VALUES}, got {value!r}",
                file=sys.stderr,
            )
            return 2
        if (
            field == "thinking_type"
            and value
            and value not in api_config.THINKING_TYPE_VALUES
        ):
            print(
                f"[wlwl config] thinking_type must be one of "
                f"{api_config.THINKING_TYPE_VALUES}, got {value!r}",
                file=sys.stderr,
            )
            return 2
        if (
            field == "api_mode"
            and value
            and value not in api_config.API_MODE_VALUES
        ):
            print(
                f"[wlwl config] api_mode must be one of "
                f"{api_config.API_MODE_VALUES}, got {value!r}",
                file=sys.stderr,
            )
            return 2
        if value is None:
            updates[field] = None
        else:
            updates[field] = value

    for field, value in updates.items():
        if value is None:
            row.pop(field, None)
        else:
            row[field] = value
    configs[idx] = row
    try:
        api_config.save_api_configs(base, configs)
    except ValueError as exc:
        print(f"[wlwl config] save failed: {exc}", file=sys.stderr)
        return 2
    pretty = ", ".join(f"{k}={v!r}" for k, v in updates.items()) or "(no change)"
    print(f"[wlwl config] tuned {args.name!r}: {pretty}")
    return 0


def cmd_show(args) -> int:
    """Pretty-print one config (apikey masked unless --unmask)."""
    base = _project_root()
    configs = (
        api_config.load_api_configs(base) if args.unmask else api_config.list_api_configs(base)
    )
    existing = _find_by_name(configs, args.name)
    if existing is None:
        print(f"[wlwl config] no such config: {args.name!r}", file=sys.stderr)
        return 1
    print(json.dumps(existing[1], ensure_ascii=False, indent=2))
    return 0


# ── argparse + dispatch ───────────────────────────────────────────────────


def build_subparser(subparsers):
    """Add the ``config`` subcommand to a parent argparse subparsers object.

    Returns the ``config`` subparser so callers can introspect it.
    Public so :mod:`launcher.cli_repl` can attach a single subcommand tree
    instead of duplicating argparse definitions."""
    p = subparsers.add_parser(
        "config",
        help="manage API configs (list / presets / add / use / remove / import / export / probe / backups)",
    )
    sub = p.add_subparsers(dest="config_cmd", required=True)

    p_list = sub.add_parser("list", help="list installed configs")
    p_list.set_defaults(func=cmd_list)

    p_presets = sub.add_parser("presets", help="browse the preset library")
    p_presets.add_argument("query", nargs="?", default="",
                           help="optional substring filter (matches id/provider/website)")
    p_presets.add_argument("--category",
                           choices=("official", "cn_official", "aggregator", "third_party"))
    p_presets.add_argument("--kind", choices=("native_oai", "native_claude"))
    p_presets.add_argument("--detail", action="store_true",
                           help="show all preset fields instead of a compact table")
    p_presets.set_defaults(func=cmd_presets)

    p_add = sub.add_parser("add", help="add a new config (preset or free-form)")
    p_add.add_argument("--preset", help="preset id (see `config presets`)")
    p_add.add_argument("--name", help="display name for this config")
    p_add.add_argument("--kind", choices=("native_oai", "native_claude"),
                       help="config kind (required without --preset)")
    p_add.add_argument("--apibase", help="API base URL (overrides preset)")
    p_add.add_argument("--model", help="default model (overrides preset)")
    p_add.add_argument("--apikey", help="API key; prompted on stdin if omitted")
    p_add.add_argument("--force", action="store_true",
                       help="overwrite if a config with the same name exists")
    p_add.set_defaults(func=cmd_add)

    p_use = sub.add_parser("use",
                           help="make NAME the default LLM for the next session")
    p_use.add_argument("name")
    p_use.set_defaults(func=cmd_use)

    p_rm = sub.add_parser("remove", help="delete a config by name")
    p_rm.add_argument("name")
    p_rm.set_defaults(func=cmd_remove)

    p_imp = sub.add_parser("import",
                           help="import from a wlwl-config:// deep link")
    p_imp.add_argument("url")
    p_imp.add_argument("--force", action="store_true",
                       help="overwrite if name already exists")
    p_imp.set_defaults(func=cmd_import)

    p_exp = sub.add_parser("export",
                           help="dump configs as JSON to stdout (apikeys masked)")
    p_exp.add_argument("--unmask", action="store_true",
                       help="DANGEROUS: include real apikeys in the output")
    p_exp.set_defaults(func=cmd_export)

    p_probe = sub.add_parser("probe", help="measure latency to apibase URL(s)")
    p_probe_group = p_probe.add_mutually_exclusive_group()
    p_probe_group.add_argument("name", nargs="?",
                               help="probe an installed config by name")
    p_probe_group.add_argument("--preset", help="probe a preset by id")
    p_probe_group.add_argument("--url", help="probe an arbitrary URL")
    p_probe.add_argument("--timeout", type=float,
                         default=api_endpoint_probe.PROBE_TIMEOUT_S,
                         help=f"TCP connect timeout in seconds (default {api_endpoint_probe.PROBE_TIMEOUT_S})")
    p_probe.set_defaults(func=cmd_probe)

    p_bk = sub.add_parser("backups", help="list rotated config snapshots")
    p_bk.set_defaults(func=cmd_backups)

    p_tune = sub.add_parser(
        "tune",
        help="set advanced fields (temperature, reasoning_effort, thinking_type, ...) on an existing config",
    )
    p_tune.add_argument("name", help="config name")
    p_tune.add_argument(
        "assignments",
        nargs="+",
        metavar="FIELD=VALUE",
        help=(
            "one or more field=value pairs. "
            "Allowed: reasoning_effort, thinking_type, thinking_budget_tokens, "
            "temperature, max_tokens, max_retries, connect_timeout, read_timeout, "
            "stream, fake_cc_system_prompt, audio_capable, image_capable, "
            "api_mode, apibase, model. Pass `field=` (empty value) to clear."
        ),
    )
    p_tune.set_defaults(func=cmd_tune)

    p_show = sub.add_parser("show", help="pretty-print one config as JSON")
    p_show.add_argument("name")
    p_show.add_argument(
        "--unmask",
        action="store_true",
        help="include the real apikey in the output (otherwise '***')",
    )
    p_show.set_defaults(func=cmd_show)

    return p


def main(argv=None) -> int:
    """Standalone entry: ``python -m launcher.cli_config <sub> ...`` or
    invoked from :mod:`launcher.cli_repl` when the user types
    ``wlwl config <sub> ...``."""
    parser = argparse.ArgumentParser(prog="wlwl config")
    sub = parser.add_subparsers(dest="config_cmd", required=True)

    # Inline registration — mirrors build_subparser so we avoid the
    # nested "config config" prog name when invoked standalone.
    sub.add_parser("list", help="list installed configs").set_defaults(func=cmd_list)

    p_presets = sub.add_parser("presets", help="browse the preset library")
    p_presets.add_argument("query", nargs="?", default="")
    p_presets.add_argument("--category",
                           choices=("official", "cn_official", "aggregator", "third_party"))
    p_presets.add_argument("--kind", choices=("native_oai", "native_claude"))
    p_presets.add_argument("--detail", action="store_true")
    p_presets.set_defaults(func=cmd_presets)

    p_add = sub.add_parser("add", help="add a new config (preset or free-form)")
    p_add.add_argument("--preset")
    p_add.add_argument("--name")
    p_add.add_argument("--kind", choices=("native_oai", "native_claude"))
    p_add.add_argument("--apibase")
    p_add.add_argument("--model")
    p_add.add_argument("--apikey")
    p_add.add_argument("--force", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_use = sub.add_parser("use", help="make NAME the default LLM for the next session")
    p_use.add_argument("name")
    p_use.set_defaults(func=cmd_use)

    p_rm = sub.add_parser("remove", help="delete a config by name")
    p_rm.add_argument("name")
    p_rm.set_defaults(func=cmd_remove)

    p_imp = sub.add_parser("import", help="import from a wlwl-config:// deep link")
    p_imp.add_argument("url")
    p_imp.add_argument("--force", action="store_true")
    p_imp.set_defaults(func=cmd_import)

    p_exp = sub.add_parser("export", help="dump configs as JSON to stdout (apikeys masked)")
    p_exp.add_argument("--unmask", action="store_true")
    p_exp.set_defaults(func=cmd_export)

    p_probe = sub.add_parser("probe", help="measure latency to apibase URL(s)")
    g = p_probe.add_mutually_exclusive_group()
    g.add_argument("name", nargs="?")
    g.add_argument("--preset")
    g.add_argument("--url")
    p_probe.add_argument("--timeout", type=float,
                         default=api_endpoint_probe.PROBE_TIMEOUT_S)
    p_probe.set_defaults(func=cmd_probe)

    sub.add_parser("backups", help="list rotated config snapshots").set_defaults(func=cmd_backups)

    p_tune = sub.add_parser("tune", help="set advanced fields on an existing config")
    p_tune.add_argument("name")
    p_tune.add_argument("assignments", nargs="+", metavar="FIELD=VALUE")
    p_tune.set_defaults(func=cmd_tune)

    p_show = sub.add_parser("show", help="pretty-print one config as JSON")
    p_show.add_argument("name")
    p_show.add_argument("--unmask", action="store_true")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
