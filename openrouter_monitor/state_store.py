from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import BalanceSnapshot


LOGGER = logging.getLogger(__name__)
SNAPSHOT_RETENTION_DAYS = 7


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


class SnapshotStore(JsonStateStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, {"version": 1, "snapshots": {}})

    def load(self) -> dict[str, Any]:
        data = super().load()
        snapshots = data.get("snapshots")
        if not isinstance(snapshots, dict):
            data = self._clone_default()
        return data

    def record_snapshot(self, key_id: str, balance: float, now: datetime) -> None:
        state = self.load()
        snapshots = state.setdefault("snapshots", {})
        key_snapshots = snapshots.setdefault(key_id, [])

        if not isinstance(key_snapshots, list):
            key_snapshots = []
            snapshots[key_id] = key_snapshots

        snapshot_entry = {
            "balance": balance,
            "timestamp": now.isoformat(),
        }
        key_snapshots.append(snapshot_entry)

        cutoff = now - timedelta(days=SNAPSHOT_RETENTION_DAYS)
        cleaned = [
            s for s in key_snapshots
            if isinstance(s, dict) and self._is_after_cutoff(s.get("timestamp"), cutoff)
        ]
        snapshots[key_id] = cleaned

        self.save(state)

    def get_snapshots(self, key_id: str, now: datetime) -> list[BalanceSnapshot]:
        state = self.load()
        snapshots = state.get("snapshots", {})
        key_snapshots = snapshots.get(key_id, [])

        if not isinstance(key_snapshots, list):
            return []

        cutoff = now - timedelta(days=SNAPSHOT_RETENTION_DAYS)
        result: list[BalanceSnapshot] = []

        for entry in key_snapshots:
            if not isinstance(entry, dict):
                continue
            timestamp_str = entry.get("timestamp")
            if not timestamp_str:
                continue
            try:
                timestamp = datetime.fromisoformat(str(timestamp_str))
                if timestamp >= cutoff:
                    result.append(BalanceSnapshot(
                        key_id=key_id,
                        balance=float(entry.get("balance", 0)),
                        timestamp=timestamp,
                    ))
            except (ValueError, TypeError):
                continue

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def _is_after_cutoff(self, timestamp_str: Any, cutoff: datetime) -> bool:
        if not timestamp_str:
            return False
        try:
            timestamp = datetime.fromisoformat(str(timestamp_str))
            return timestamp >= cutoff
        except (ValueError, TypeError):
            return False
