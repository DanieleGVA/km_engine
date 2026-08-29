"""Persistent content-hash cache for incremental ingestion (FR1.4)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class HashCache:
    """A small JSON-backed mapping ``key -> content hash``.

    Keys are caller-defined (the pipeline uses ``<namespace>:<relpath>``).
    Writes are atomic (temp file + replace) so a crash cannot corrupt the
    cache.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {str(k): str(v) for k, v in raw.items()}
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str) -> str | None:
        """Return the cached hash for ``key``, or None."""
        return self._data.get(key)

    def set(self, key: str, digest: str) -> None:
        """Store ``digest`` for ``key``."""
        self._data[key] = digest

    def remove(self, key: str) -> None:
        """Forget ``key``."""
        self._data.pop(key, None)

    def flush(self) -> None:
        """Persist the cache atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, sort_keys=True, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def as_dict(self) -> dict[str, str]:
        """Return a copy of the in-memory cache (test helper)."""
        return dict(self._data)
