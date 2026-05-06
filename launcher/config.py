"""``python -m launcher.config`` — manage credentials & settings.

Subcommands:
  list                              show everything (secrets masked)
  list --raw                        show with secrets unmasked (be careful)
  get <path>                        read one value, e.g. providers.openai.api_key
  set <path> <value>                write one value
  set <path> --from-stdin           read value from stdin (paste keys safely)
  delete <path>                     remove one entry
  migrate [--dry-run]               import mykey.py / launcher_api_configs.json
  export [--include-secrets]        dump full config as JSON (default: redacted)
  import <file>                     load config from JSON file
  doctor                            quick diagnostic (which layers are present?)

Layer flags:
  --user  (default)                 ~/.wlwl-ass/config.json
  --project                         <project>/.wlwl-ass/config.json

Examples:
  python -m launcher.config list
  python -m launcher.config set providers.openai.api_key sk-...
  python -m launcher.config set bots.feishu.app_id cli_xxx
  python -m launcher.config set providers.openai.api_key --from-stdin
  python -m launcher.config migrate --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from launcher import config_store, config_migrate


# ─── Pretty printing ─────────────────────────────────────────────────────


def _isatty() -> bool:
    try: return bool(sys.stdout.isatty())
    except Exception: return False


_COLOR = _isatty() and os.environ.get("NO_COLOR", "") == ""


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def _bold(s): return _c(s, "1")
def _dim(s):  return _c(s, "2")
def _green(s): return _c(s, "32")
def _yellow(s): return _c(s, "33")
def _red(s):  return _c(s, "31")
def _cyan(s): return _c(s, "36")


def _force_utf8():
    try:
        enc = (sys.stdout.encoding or "").lower()
        if enc and enc not in ("utf-8", "utf8", "cp65001"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


# ─── Subcommands ─────────────────────────────────────────────────────────


def cmd_list(args) -> int:
    store = config_store.default_store()
    view = store.list_redacted() if not args.raw else store.all()
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    print(_bold(f"wlwl-ass config — {_layer_paths()}"))
    if args.raw:
        print(_yellow("⚠  --raw mode: secrets shown in plaintext."))
    for section in ("providers", "bots", "settings"):
        items = view.get(section) or {}
        title = section.capitalize()
        print()
        print(_bold(title) + _dim(f"  ({len(items)})"))
        if not items:
            print(_dim("  (empty)"))
            continue
        if section == "settings":
            for k, v in sorted(items.items()):
                print(f"  {k:<24}  {v!r}")
            continue
        for entity, fields in sorted(items.items()):
            print(f"  {_cyan(entity)}")
            if not isinstance(fields, dict):
                print(f"    {fields!r}")
                continue
            for k, v in fields.items():
                marker = _dim("(secret)") if config_store.is_secret_key(k) else ""
                print(f"    {k:<18}  {v!r:<40}  {marker}")
    print()
    print(_dim("Layers:"))
    print(_dim(f"  user    {config_store.user_config_path()}"))
    print(_dim(f"  project {config_store.project_config_path()}"))
    if config_store.keyring_available():
        print(_dim("  keyring available (use --keyring on `set` to route secrets)"))
    return 0


def _layer_paths() -> str:
    user = config_store.user_config_path()
    proj = config_store.project_config_path()
    parts = []
    if os.path.isfile(user):
        parts.append("user")
    if os.path.isfile(proj):
        parts.append("project")
    if not parts:
        parts.append("(no files yet)")
    return " + ".join(parts)


def cmd_get(args) -> int:
    store = config_store.default_store()
    value = store.get(args.path)
    if value is None:
        print(_red(f"not set: {args.path}"), file=sys.stderr)
        return 1
    # If it's a secret-named leaf and not --raw, mask.
    last = args.path.split(".")[-1]
    if config_store.is_secret_key(last) and not args.raw:
        s = str(value)
        masked = ("***" + s[-4:]) if len(s) > 6 else "***"
        print(masked)
    else:
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(value)
    return 0


def cmd_set(args) -> int:
    store = config_store.default_store()
    parts = args.path.split(".")
    if len(parts) < 2:
        print(_red("path must include section, e.g. providers.openai.api_key"), file=sys.stderr)
        return 2
    section = parts[0]
    if section not in ("providers", "bots", "settings"):
        print(_red(f"unknown section {section!r} — must be providers / bots / settings"),
              file=sys.stderr)
        return 2

    # Resolve value.
    if args.from_stdin:
        if sys.stdin.isatty():
            print(_dim(f"reading value for {args.path} from stdin (Ctrl+D to end)…"),
                  file=sys.stderr)
        value = sys.stdin.read().rstrip("\n")
    else:
        if args.value is None:
            print(_red("value required (or pass --from-stdin)"), file=sys.stderr)
            return 2
        value = args.value
    # Try JSON-decode for typed values (numbers / lists / bools).
    try:
        decoded = json.loads(value)
        if isinstance(decoded, (int, float, bool, list, dict)):
            value = decoded
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    layer = "project" if args.project else "user"
    if section == "settings":
        if len(parts) != 2:
            print(_red("settings path must be settings.<name>"), file=sys.stderr)
            return 2
        store.set_setting(parts[1], value, layer=layer)
        print(_green(f"✓ set settings.{parts[1]} (layer={layer})"))
        return 0

    if len(parts) < 3:
        print(_red(f"{section} path must be {section}.<name>.<field>"), file=sys.stderr)
        return 2
    entity, field = parts[1], ".".join(parts[2:])
    if section == "providers":
        store.set_provider(entity, {field: value}, layer=layer, merge=True,
                           use_keyring=args.keyring)
    else:
        store.set_bot(entity, {field: value}, layer=layer, merge=True,
                      use_keyring=args.keyring)
    suffix = " (keyring)" if args.keyring and config_store.keyring_available() else ""
    print(_green(f"✓ set {args.path} (layer={layer}){suffix}"))
    return 0


def cmd_delete(args) -> int:
    store = config_store.default_store()
    parts = args.path.split(".")
    layer = "project" if args.project else "user"
    if len(parts) < 2:
        print(_red("path must include section.name"), file=sys.stderr)
        return 2
    section, entity = parts[0], parts[1]
    if section == "providers":
        ok = store.delete_provider(entity, layer=layer)
    elif section == "bots":
        ok = store.delete_bot(entity, layer=layer)
    elif section == "settings":
        ok = store.delete_setting(entity, layer=layer)
    else:
        print(_red(f"unknown section {section!r}"), file=sys.stderr)
        return 2
    if ok:
        print(_green(f"✓ removed {section}.{entity} (layer={layer})"))
        return 0
    print(_yellow(f"not found: {section}.{entity} in layer {layer}"))
    return 1


def cmd_migrate(args) -> int:
    print(_yellow(
        "DEPRECATED: `migrate` is a one-shot upgrade path from mykey.py-based "
        "configs and will be removed in the next release."
    ))
    if config_migrate.already_migrated() and not args.force:
        print(_yellow("already migrated. Use --force to re-run."))
        print(_dim(f"marker: {config_migrate._migrated_marker()}"))
        return 0
    layer = "project" if args.project else "user"
    report = config_migrate.migrate(dry_run=args.dry_run, layer=layer)
    label = "[dry-run] " if args.dry_run else ""
    print(_bold(f"{label}Migration → {layer} layer"))
    if report["legacy_files_seen"]:
        print(_dim("  read from: " + ", ".join(report["legacy_files_seen"])))
    if report["providers_imported"]:
        print(_green(f"  providers ({len(report['providers_imported'])}): ")
              + ", ".join(report["providers_imported"]))
    if report.get("settings_imported"):
        print(_green(f"  settings ({len(report['settings_imported'])}): ")
              + ", ".join(report["settings_imported"]))
    if report["bots_imported"]:
        print(_green(f"  bots ({len(report['bots_imported'])}): ")
              + ", ".join(report["bots_imported"]))
    if report["skipped_placeholder"]:
        print(_yellow(f"  skipped placeholders ({len(report['skipped_placeholder'])}): ")
              + ", ".join(report["skipped_placeholder"][:5])
              + (_dim(" …") if len(report["skipped_placeholder"]) > 5 else ""))
    if not (report["providers_imported"] or report["bots_imported"]):
        print(_yellow("  nothing to import — no real keys found in legacy files."))
    if not args.dry_run:
        print()
        print(_dim("Next: `python -m launcher.config list` to verify."))
        print(_dim("Legacy files were left in place; you can delete them after testing."))
    return 0


def cmd_export(args) -> int:
    store = config_store.default_store()
    view = store.all() if args.include_secrets else store.list_redacted()
    json.dump(view, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_import(args) -> int:
    if not os.path.isfile(args.file):
        print(_red(f"file not found: {args.file}"), file=sys.stderr)
        return 1
    with open(args.file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        print(_red("import file must be a JSON object"), file=sys.stderr)
        return 1
    store = config_store.default_store()
    layer = "project" if args.project else "user"
    n_prov = n_bot = n_set = 0
    for prov_name, fields in (data.get("providers") or {}).items():
        if isinstance(fields, dict):
            store.set_provider(prov_name, fields, layer=layer, merge=False)
            n_prov += 1
    for bot_name, fields in (data.get("bots") or {}).items():
        if isinstance(fields, dict):
            store.set_bot(bot_name, fields, layer=layer, merge=False)
            n_bot += 1
    for k, v in (data.get("settings") or {}).items():
        store.set_setting(k, v, layer=layer)
        n_set += 1
    print(_green(f"✓ imported {n_prov} providers, {n_bot} bots, {n_set} settings (layer={layer})"))
    return 0


def cmd_doctor(args) -> int:
    print(_bold("Config doctor"))
    user = config_store.user_config_path()
    proj = config_store.project_config_path()
    print(f"  user    {user}  {'✓' if os.path.isfile(user) else _dim('(absent)')}")
    print(f"  project {proj}  {'✓' if os.path.isfile(proj) else _dim('(absent)')}")
    print(f"  keyring " + (
        _green("available") if config_store.keyring_available()
        else _dim("(not installed — pip install keyring)")
    ))
    print(f"  migrated {'✓' if config_migrate.already_migrated() else _dim('(no marker)')}")
    print()
    store = config_store.default_store()
    view = store.list_redacted()
    n_p = sum(
        1 for v in view.get("providers", {}).values()
        if isinstance(v, dict) and v.get("api_key")
    )
    n_b = sum(
        1 for v in view.get("bots", {}).values()
        if isinstance(v, dict) and v
    )
    print(f"  providers configured: {n_p}")
    print(f"  bots configured:      {n_b}")
    return 0


# ─── Argparse ────────────────────────────────────────────────────────────


def _add_layer_flag(p):
    p.add_argument("--project", action="store_true",
                   help="target project layer instead of user layer")


def main(argv=None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(prog="ga config", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="show all config (secrets masked)")
    p_list.add_argument("--raw", action="store_true", help="show secrets unmasked")
    p_list.add_argument("--json", action="store_true", help="JSON output")

    p_get = sub.add_parser("get", help="read one value (dotted path)")
    p_get.add_argument("path")
    p_get.add_argument("--raw", action="store_true", help="show secret value unmasked")

    p_set = sub.add_parser("set", help="write one value (dotted path)")
    p_set.add_argument("path")
    p_set.add_argument("value", nargs="?", default=None)
    p_set.add_argument("--from-stdin", action="store_true",
                       help="read value from stdin (safer for paste)")
    p_set.add_argument("--keyring", action="store_true",
                       help="route secret into OS keyring (if available)")
    _add_layer_flag(p_set)

    p_del = sub.add_parser("delete", help="remove one entry")
    p_del.add_argument("path")
    _add_layer_flag(p_del)

    p_mig = sub.add_parser("migrate", help="import legacy mykey/launcher json")
    p_mig.add_argument("--dry-run", action="store_true")
    p_mig.add_argument("--force", action="store_true",
                       help="re-run even if already migrated")
    _add_layer_flag(p_mig)

    p_exp = sub.add_parser("export", help="dump config as JSON (default: redacted)")
    p_exp.add_argument("--include-secrets", action="store_true")

    p_imp = sub.add_parser("import", help="restore config from a JSON file")
    p_imp.add_argument("file")
    _add_layer_flag(p_imp)

    sub.add_parser("doctor", help="quick diagnostic of layers + keyring")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    handlers = {
        "list":    cmd_list,
        "get":     cmd_get,
        "set":     cmd_set,
        "delete":  cmd_delete,
        "migrate": cmd_migrate,
        "export":  cmd_export,
        "import":  cmd_import,
        "doctor":  cmd_doctor,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
