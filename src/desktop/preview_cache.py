"""Persistent, lossless intermediate files for Neural Rendering previews.

Keys describe the source and every processing card before the cached output.
Each entry is a directory containing a completed artifact and a small JSON
manifest. Rendering happens in the normal job directory; a completed directory
is moved into the cache, so cancellation never exposes a partial result.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..core.paths import PREVIEW_CACHE


CACHE_SCHEMA = 1
_LOCK = threading.RLock()


def file_identity(path: str | Path) -> dict[str, Any]:
    """Cheap identity for large media and external settings files."""
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return {
        "path": os.path.normcase(str(resolved)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def optional_file_identity(path: str | Path) -> dict[str, Any] | None:
    try:
        return file_identity(path)
    except OSError:
        return None


def cache_key(payload: dict[str, Any]) -> str:
    serialized = json.dumps({"schema": CACHE_SCHEMA, **payload}, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class PreviewStageCache:
    def __init__(self, root: Path = PREVIEW_CACHE) -> None:
        self.root = Path(root)
        self.used_keys: set[str] = set()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _entry(self, key: str) -> Path:
        return self.root / key

    def lookup(self, key: str, previous: Path | None = None) -> Path | None:
        entry = self._entry(key)
        with _LOCK:
            try:
                manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
                if manifest.get("schema") != CACHE_SCHEMA or manifest.get("key") != key:
                    return None
                if manifest.get("alias") is True:
                    if previous is None or not previous.is_file():
                        return None
                    result = previous
                else:
                    name = manifest.get("artifact")
                    if not isinstance(name, str) or not name or Path(name).name != name:
                        return None
                    result = entry / name
                    if not result.is_file() or result.stat().st_size != manifest.get("size"):
                        return None
                os.utime(entry, None)
                self.used_keys.add(key)
                return result
            except (OSError, ValueError, TypeError, KeyError):
                return None

    def publish(self, key: str, directory: Path, result: Path,
                previous: Path | None = None) -> Path:
        """Publish one finished stage without copying potentially huge video."""
        try:
            alias = previous is not None and result.resolve() == previous.resolve()
            if alias:
                artifact = None
                size = 0
            else:
                if not result.is_file() or result.stat().st_size <= 0:
                    return result
                if result.parent.resolve() != directory.resolve():
                    return result
                artifact = result.name
                size = result.stat().st_size
            manifest = {"schema": CACHE_SCHEMA, "key": key, "alias": alias,
                        "artifact": artifact, "size": size}
            (directory / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            with _LOCK:
                self.root.mkdir(parents=True, exist_ok=True)
                existing = self.lookup(key, previous)
                if existing is not None:
                    return existing
                entry = self._entry(key)
                if entry.exists():
                    shutil.rmtree(entry)
                try:
                    directory.rename(entry)
                except OSError:
                    # Test/custom cache locations can be on another volume.
                    # Copy into the destination volume, then rename there.
                    incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=self.root))
                    try:
                        shutil.copytree(directory, incoming, dirs_exist_ok=True)
                        incoming.rename(entry)
                    finally:
                        if incoming.exists():
                            shutil.rmtree(incoming, ignore_errors=True)
                os.utime(entry, None)
                self.used_keys.add(key)
                return previous if alias else entry / artifact
        except OSError:
            # Cache storage is optional; the completed job file still feeds the
            # remainder of this preview when the disk/cache is unavailable.
            return result
