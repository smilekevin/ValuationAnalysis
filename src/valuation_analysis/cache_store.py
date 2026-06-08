from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


class FileCacheStore:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    def get_json(self, namespace: str, key: str, ttl_seconds: int) -> Any | None:
        cache_path = self._cache_path(namespace, key)
        if not cache_path.exists():
            return None

        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        stored_at = payload.get("stored_at")
        if not isinstance(stored_at, str):
            return None

        try:
            stored_at_dt = datetime.fromisoformat(stored_at)
        except ValueError:
            return None

        age_seconds = (datetime.now(timezone.utc) - stored_at_dt).total_seconds()
        if age_seconds > ttl_seconds:
            return None

        return payload.get("value")

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        cache_path = self._cache_path(namespace, key)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "stored_at": datetime.now(timezone.utc).isoformat(),
                        "value": value,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except Exception:
            return

    def _cache_path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root_dir / namespace / f"{digest}.json"
