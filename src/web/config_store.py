from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    """Return the directory containing the Phronosis SQLite database."""
    return Path(os.getenv("SQLITE_PATH", "/data/call_graph.db")).parent


def read_file_config() -> dict[str, Any]:
    """Read config.json from the data directory; returns empty dict on absence or parse error."""
    path = _data_dir() / "config.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def write_file_config(updates: dict[str, Any]) -> None:
    """Merge updates into config.json; None values remove the corresponding key."""
    path = _data_dir() / "config.json"
    current = read_file_config()
    for k, v in updates.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2))


def get_key_info() -> dict[str, dict]:
    """Return masked previews of the configured API key environment variables."""
    def _preview(name: str) -> dict:
        """Build a set/not-set info dict with a masked preview of an API key value."""
        val = os.getenv(name, "")
        if not val:
            return {"set": False, "preview": "not set"}
        if len(val) > 14:
            preview = val[:10] + "•••" + val[-4:]
        else:
            preview = val[:4] + "••••"
        return {"set": True, "preview": preview}

    return {
        "ANTHROPIC_API_KEY": _preview("ANTHROPIC_API_KEY"),
        "OPENAI_API_KEY": _preview("OPENAI_API_KEY"),
    }
