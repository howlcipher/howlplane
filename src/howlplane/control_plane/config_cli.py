"""`howlplane config`: effective configuration with provenance.

Read-only. It asks the existing `ConfigLoader` what each layer supplied and
applies the same precedence the loader's settings model uses
(environment > dotenv > local config > settings.yaml > defaults). It is an
inspection surface, not a second configuration mechanism.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ValidationError

CONFIG_SCHEMA = "howlplane.config/v1"
_SENSITIVE = ("key", "secret", "token", "password")
_MASK = "********"


def _leaves(value: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten nested mappings to dotted keys; lists and opaque dicts stay leaves."""
    if isinstance(value, dict) and prefix.count(".") < 1 and prefix not in (
        "providers", "roles", "mcp_servers", "agents"
    ):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: value}


def _lookup(layer: Dict[str, Any], dotted: str) -> Tuple[bool, Any]:
    node: Any = layer
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _env_name(dotted: str) -> str:
    return dotted.replace(".", "__").upper()


def _dotenv() -> Dict[str, str]:
    path = Path(".env")
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
        return {k.upper(): v for k, v in dotenv_values(path).items() if v is not None}
    except Exception:
        return {}


def _sensitive(dotted: str) -> bool:
    return any(word in dotted.lower() for word in _SENSITIVE)


def _layers(loader: Any, dotted: str, dotenv: Dict[str, str]) -> List[Dict[str, Any]]:
    """Every layer defining `dotted`, highest precedence first."""
    found = []
    env = _env_name(dotted)
    if env in {k.upper() for k in os.environ}:
        value = next(v for k, v in os.environ.items() if k.upper() == env)
        found.append({"source": "environment", "detail": env, "value": value})
    if env in dotenv:
        found.append({"source": "dotenv", "detail": f".env ({env})", "value": dotenv[env]})
    ok, value = _lookup(loader.local_layer, dotted)
    if ok:
        found.append({"source": "local_config", "detail": str(loader.local_config_path), "value": value})
    ok, value = _lookup(loader.file_layer, dotted)
    if ok:
        found.append({"source": "config_file", "detail": str(loader.config_path), "value": value})
    return found


def _field_info(dotted: str) -> Optional[Any]:
    from howlplane.control_plane.config_loader import AppSettings
    model: Any = AppSettings
    info = None
    for part in dotted.split("."):
        fields = getattr(model, "model_fields", None)
        if not fields or part not in fields:
            return info if info is not None and not fields else None
        info = fields[part]
        ann = info.annotation
        model = ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None
        if model is None:
            break
    return info


def _redact(dotted: str, value: Any) -> Any:
    return _MASK if _sensitive(dotted) and value not in ("", None) else value


def build_report(loader: Any = None) -> Dict[str, Any]:
    """Effective settings with source and validity. Raises ValidationError if invalid."""
    if loader is None:
        from howlplane.control_plane.config_loader import ConfigLoader
        loader = ConfigLoader()
    dotenv = _dotenv()
    entries = []
    for dotted, effective in sorted(_leaves(loader.config).items()):
        layers = _layers(loader, dotted, dotenv)
        entries.append({
            "key": dotted,
            "value": _redact(dotted, effective),
            "source": layers[0]["source"] if layers else "default",
            "source_detail": layers[0]["detail"] if layers else "built-in default",
            "valid": True,
        })
    return {"schema": CONFIG_SCHEMA, "config_file": str(loader.config_path),
            "local_config": str(loader.local_config_path) if loader.local_config_path else None,
            "settings": entries}


def _validation_errors(exc: Exception) -> List[str]:
    if isinstance(exc, ValidationError):
        return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return [str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__]


def _show(args: argparse.Namespace) -> int:
    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print("HowlPlane configuration\n")
    width = max((len(e["key"]) for e in report["settings"]), default=0)
    for e in report["settings"]:
        print(f"{e['key']:<{width}}  {e['value']!r}  ({e['source']})")
    print(f"\nConfig file: {report['config_file']}")
    print("Explain one setting: howlplane config explain <key>")
    return 0


def _validate(args: argparse.Namespace) -> int:
    try:
        build_report()
    except Exception as exc:
        errors = _validation_errors(exc)
        if args.json:
            print(json.dumps({"schema": CONFIG_SCHEMA, "valid": False, "errors": errors}, indent=2))
        else:
            print("Configuration is INVALID:")
            for line in errors:
                print(f"  - {line}")
            print("Fix the setting above in settings.yaml, the local config, or the environment.")
        return 1
    if args.json:
        print(json.dumps({"schema": CONFIG_SCHEMA, "valid": True, "errors": []}, indent=2))
    else:
        print("Configuration is valid.")
    return 0


def _explain(args: argparse.Namespace) -> int:
    from howlplane.control_plane.config_loader import ConfigLoader
    try:
        loader = ConfigLoader()
    except Exception as exc:
        print(f"Configuration is invalid: {'; '.join(_validation_errors(exc))}")
        return 1
    leaves = _leaves(loader.config)
    if args.key not in leaves:
        near = sorted(k for k in leaves if args.key in k)[:5]
        print(f"Unknown setting: {args.key}. " + (f"Did you mean: {', '.join(near)}?" if near else
              "Run `howlplane config show` to list settings."))
        return 1
    layers = _layers(loader, args.key, _dotenv())
    info = _field_info(args.key)
    doc = {
        "schema": CONFIG_SCHEMA, "key": args.key, "value": _redact(args.key, leaves[args.key]),
        "source": layers[0]["source"] if layers else "default",
        "layers": [{**layer, "value": _redact(args.key, layer["value"])} for layer in layers],
        "default": _redact(args.key, None if info is None or info.is_required() else info.get_default(call_default_factory=True)),
        "type": str(getattr(info, "annotation", "unknown")),
        "valid": True,
    }
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
        return 0
    print(f"{doc['key']} = {doc['value']!r}")
    print(f"Source: {doc['source']}" + (f" ({layers[0]['detail']})" if layers else " (built-in default)"))
    print(f"Default: {doc['default']!r}")
    print(f"Type: {doc['type']}")
    if len(layers) > 1:
        print("Also set (overridden):")
        for layer in layers[1:]:
            print(f"  - {layer['source']}: {layer['value']!r} ({layer['detail']})")
    print("Valid: yes")
    return 0


def command(args: argparse.Namespace) -> int:
    action = getattr(args, "config_action", None)
    return {"show": _show, "validate": _validate, "explain": _explain}[action](args)
