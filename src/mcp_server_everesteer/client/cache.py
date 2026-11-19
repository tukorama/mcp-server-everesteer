"""Memory + disk JSON cache for slow-changing platform catalogs and schemas."""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, cast

from mcp_server_everesteer.config import write_private_json

logger = logging.getLogger(__name__)

MINUTE = 60
HOUR = 3600
DAY = 24 * 3600

_now = time.time


class JsonCache:
    """Scoped per-account memory + disk JSON cache."""

    def __init__(self, directory: Path, default_ttl: float = HOUR, *, persist: bool = True) -> None:
        self.directory = directory
        self.default_ttl = default_ttl
        self.persist = persist
        self._memory: dict[str, tuple[float, Any]] = {}

    def _path(self, key: str) -> Path:
        return self.directory / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    def get(self, key: str, ttl: float | None = None) -> Any | None:
        effective_ttl = self.default_ttl if ttl is None else ttl
        if effective_ttl <= 0:
            return None
        hit = self._memory.get(key)
        if hit and _now() - hit[0] <= effective_ttl:
            return hit[1]
        if not self.persist:
            return None
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict):
            return None
        entry_dict: dict[str, Any] = {str(k): v for k, v in cast(dict[Any, Any], entry).items()}
        saved_at = entry_dict.get("saved_at", 0)
        if not isinstance(saved_at, int | float) or _now() - saved_at > effective_ttl:
            return None
        data = entry_dict.get("data")
        self._memory[key] = (float(saved_at), data)
        return data

    def set(self, key: str, data: Any, *, disk: bool = True) -> None:
        if self.default_ttl <= 0:
            return
        saved = _now()
        self._memory[key] = (saved, data)
        if not (self.persist and disk):
            return
        try:
            write_private_json(self._path(key), {"saved_at": saved, "data": data})
        except OSError as exc:
            logger.warning("Could not write cache entry: %s", exc)
