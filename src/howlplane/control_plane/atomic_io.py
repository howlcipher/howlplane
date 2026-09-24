#!/usr/bin/env python3
"""
atomic_io.py

Atomic filesystem persistence and fail-closed artifact loading for HowlPlane.
Guarantees that process interruption during a write never leaves a truncated
or corrupt file in place, and malformed state artifacts fail closed.
"""

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, Optional, Union
import yaml


class CorruptArtifactError(ValueError):
    """Raised when a critical state artifact is empty, truncated, or unparseable."""
    pass


def atomic_write_text(
    file_path: Union[str, Path],
    content: str,
    encoding: str = "utf-8",
    sync: bool = True,
) -> Path:
    """
    Atomically writes string content to target path using a tempfile and os.replace.
    Ensures parent directories exist and syncs to disk before replacement.
    """
    target = Path(file_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # Use same directory to ensure same filesystem for atomic rename (os.replace)
    prefix = f".{target.name}."
    suffix = f".{os.getpid()}.{time.time_ns()}.tmp"
    temp_file = target.parent / f"{prefix}{suffix}"

    try:
        with open(temp_file, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            if sync:
                os.fsync(f.fileno())
        os.replace(temp_file, target)
        return target
    except Exception:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass
        raise


def atomic_write_json(
    file_path: Union[str, Path],
    data: Any,
    indent: int = 2,
    sync: bool = True,
) -> Path:
    """Serializes data to formatted JSON and writes atomically."""
    content = json.dumps(data, indent=indent)
    return atomic_write_text(file_path, content, sync=sync)


def atomic_write_yaml(
    file_path: Union[str, Path],
    data: Any,
    sort_keys: bool = False,
    sync: bool = True,
) -> Path:
    """Serializes data to YAML and writes atomically."""
    content = yaml.dump(data, sort_keys=sort_keys)
    return atomic_write_text(file_path, content, sync=sync)


def safe_load_text(file_path: Union[str, Path], encoding: str = "utf-8") -> str:
    """Loads file content, raising FileNotFoundError or CorruptArtifactError if empty."""
    p = Path(file_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"State file not found: {file_path}")
    content = p.read_text(encoding=encoding)
    if not content or not content.strip():
        raise CorruptArtifactError(f"Critical artifact at '{file_path}' is empty or truncated (0 bytes).")
    return content


def _safe_parse(file_path: Union[str, Path], parser_fn, format_name: str) -> Any:
    text = safe_load_text(file_path)
    try:
        parsed = parser_fn(text)
        if parsed is None or not isinstance(parsed, (dict, list)):
            raise CorruptArtifactError(f"{format_name} artifact at '{file_path}' is empty or not an object/list.")
        return parsed
    except CorruptArtifactError:
        raise
    except Exception as exc:
        raise CorruptArtifactError(f"{format_name} artifact at '{file_path}' is corrupted/truncated: {exc}") from exc


def safe_load_json(file_path: Union[str, Path]) -> Dict[str, Any]:
    """Safely loads and validates JSON artifact, failing closed on corruption."""
    return _safe_parse(file_path, json.loads, "JSON")


def safe_load_yaml(file_path: Union[str, Path]) -> Dict[str, Any]:
    """Safely loads and validates YAML artifact, failing closed on corruption."""
    return _safe_parse(file_path, yaml.safe_load, "YAML")
