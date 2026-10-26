"""Atomic persistence for follower mappings and signal offsets."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def empty_state() -> dict[str, Any]:
    return {"positions": {}, "signal_date": None, "offset": 0, "processed": []}


def load_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"State {path} tidak dapat dibaca: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"State {path} harus berupa object JSON")
    result = empty_state()
    result.update(data)
    if not isinstance(result["positions"], dict) or not isinstance(result["processed"], list):
        raise ValueError(f"Format state {path} tidak valid")
    return result


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
