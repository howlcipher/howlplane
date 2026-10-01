"""Derive every evidence action string production code can emit, from the AST."""

import ast
from pathlib import Path
from typing import Dict, Set

_CONSTANTS = {"TIA_ACTION", "EVIDENCE_ACTION", "GATE_BLOCKED_ACTION"}
_POSITIONAL_EMITTERS = {"audit", "_record_evidence", "record_decision"}
_NOT_EMITTERS = {"add_argument", "add_parser"}  # argparse `action=` is not evidence


def _is_action_token(value: str) -> bool:
    return value.replace("_", "").isalpha() and value.islower()


def emitted_actions(src_root: Path) -> Dict[str, Set[str]]:
    """Map each emitted evidence action to the ``file:line`` sites that emit it."""
    found: Dict[str, Set[str]] = {}
    for path in sorted(Path(src_root).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in _NOT_EMITTERS:
                    continue
                for kw in node.keywords:
                    if kw.arg == "action" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        found.setdefault(kw.value.value, set()).add(f"{path}:{node.lineno}")
                if name in _POSITIONAL_EMITTERS:
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _is_action_token(arg.value):
                            found.setdefault(arg.value, set()).add(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id in _CONSTANTS
                        and isinstance(node.value, ast.Constant)
                    ):
                        found.setdefault(node.value.value, set()).add(f"{path}:{node.lineno}")
    return found
