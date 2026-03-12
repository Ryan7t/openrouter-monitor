from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


class JsonStateStore:
    def __init__(self, path: str | Path, default_payload: dict[str, Any]) -> None:
        self.path = Path(path)
        self.default_payload = default_payload

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._clone_default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Failed to read state file %s, starting fresh: %s", self.path, exc)
            return self._clone_default()
        if not isinstance(data, dict):
            LOGGER.warning("State file %s is malformed, starting fresh.", self.path)
            return self._clone_default()
        return data

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False)
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(self.path)

    def _clone_default(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.default_payload))


class UserStore(JsonStateStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, {"version": 1, "users": {}})

    def load(self) -> dict[str, Any]:
        data = super().load()
        users = data.get("users")
        if not isinstance(users, dict):
            data = self._clone_default()
        return data


class RuntimeStateStore(JsonStateStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, {"version": 1, "users": {}})

    def load(self) -> dict[str, Any]:
        data = super().load()
        users = data.get("users")
        if not isinstance(users, dict):
            data = self._clone_default()
        return data
