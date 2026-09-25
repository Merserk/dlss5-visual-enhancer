from __future__ import annotations

"""Automatic cleanup of stale app-local temp caches and job leftovers.

Crash-orphaned files are covered here by an age-gated startup sweep over only
this install's portable locations:

1. ``<app>/temp/app`` (native Qt preview/preset temp artifacts).
2. Orphaned ``<app>/jobs/*`` per-render dirs left behind by killed runs.

No system/user temp directory is inspected or migrated. This build is isolated
from previous installations. Final deliverables in ``OUTPUTS/`` and anything
in ``LOGS/`` are never touched. Every failure is swallowed per-entry so
cleanup can never block startup.
"""

import os
import shutil
import time
from pathlib import Path

from .paths import APP_TEMP, JOBS, PREVIEW_CACHE

# Agreed retention: treat anything older than 24h as stale
# (startup sweep + periodic sweep).
CACHE_SWEEP_INTERVAL_SECONDS = 3600
CACHE_MAX_AGE_SECONDS = 24 * 3600
PREVIEW_CACHE_MAX_BYTES = 8 * 1024 ** 3


def resolve_app_temp_dir() -> Path | None:
    """Return this installation's fixed app-local temp directory."""
    try:
        resolved = APP_TEMP.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _entry_age_seconds(stat_result: os.stat_result, now: float) -> float:
    # Stale only when BOTH mtime and ctime are old: protects files that were
    # created long ago but are still being written/read, and vice versa.
    newest = max(stat_result.st_mtime, getattr(stat_result, "st_ctime", stat_result.st_mtime))
    return now - newest


def _remove_entry(path: Path) -> int:
    """Remove one top-level cache entry; return bytes freed (best effort)."""
    try:
        if path.is_symlink() or path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            path.unlink(missing_ok=True)
            return size
        if path.is_dir():
            size = 0
            try:
                for root, _dirs, files in os.walk(path):
                    for name in files:
                        try:
                            size += (Path(root) / name).stat().st_size
                        except OSError:
                            continue
            except OSError:
                pass
            shutil.rmtree(path, ignore_errors=True)
            return size
    except OSError:
        pass
    return 0


def sweep_dir_by_age(root: Path, max_age_seconds: int, *,
                     excluded_names: frozenset[str] = frozenset()) -> tuple[int, int]:
    """Remove direct children of ``root`` older than ``max_age_seconds``.

    Returns ``(removed_count, freed_bytes)``. Never raises, never removes
    ``root`` itself, never follows symlinks.
    """
    removed = 0
    freed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return (0, 0)
    now = time.time()
    for entry in entries:
        if entry.name in excluded_names:
            continue
        try:
            stat_result = os.stat(entry.path, follow_symlinks=False)
        except OSError:
            continue
        if _entry_age_seconds(stat_result, now) <= max_age_seconds:
            continue
        freed += _remove_entry(Path(entry.path))
        removed += 1
    return (removed, freed)


def prune_preview_cache(max_bytes: int = PREVIEW_CACHE_MAX_BYTES, *,
                        root: Path = PREVIEW_CACHE,
                        protected_keys: set[str] | None = None,
                        min_idle_seconds: int = 600) -> tuple[int, int]:
    """Evict least recently used stage entries when the preview cache is full."""
    protected_keys = protected_keys or set()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return (0, 0)
    sized: list[tuple[float, Path, int]] = []
    total = 0
    now = time.time()
    for entry in entries:
        path = Path(entry.path)
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            size = 0
            for folder, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        size += (Path(folder) / name).stat().st_size
                    except OSError:
                        pass
            total += size
            if entry.name not in protected_keys and _entry_age_seconds(stat, now) > min_idle_seconds:
                sized.append((stat.st_mtime, path, size))
        except OSError:
            continue
    removed = freed = 0
    for _mtime, path, size in sorted(sized):
        if total <= max_bytes:
            break
        actual = _remove_entry(path)
        if not path.exists():
            removed += 1
            freed += actual
            total -= size
    return removed, freed


def cleanup_old_caches(
    max_age_seconds: int = CACHE_MAX_AGE_SECONDS,
    logs_dir: Path | None = None,
) -> dict[str, int]:
    """Sweep all stale cache locations. Never raises; logs one summary line.

    Returns ``{"removed": N, "freed_bytes": B}`` for optional status display.
    """
    started = time.time()
    removed_total = 0
    freed_total = 0

    app_dir = resolve_app_temp_dir()
    if app_dir is not None:
        try:
            removed, freed = sweep_dir_by_age(
                app_dir, max_age_seconds, excluded_names=frozenset({PREVIEW_CACHE.name}))
            removed_total += removed
            freed_total += freed
        except Exception:
            pass

    try:
        if PREVIEW_CACHE.is_dir():
            removed, freed = sweep_dir_by_age(PREVIEW_CACHE, max_age_seconds)
            removed_total += removed
            freed_total += freed
            removed, freed = prune_preview_cache(root=PREVIEW_CACHE)
            removed_total += removed
            freed_total += freed
    except Exception:
        pass

    try:
        if JOBS.is_dir():
            removed, freed = sweep_dir_by_age(JOBS, max_age_seconds)
            removed_total += removed
            freed_total += freed
    except Exception:
        pass

    elapsed = time.time() - started
    try:
        from . import app_log

        app_log.info(
            "cache",
            f"removed={removed_total} freed_mb={freed_total / (1024 * 1024):.1f} "
            f"elapsed_s={elapsed:.1f}",
        )
    except Exception:
        pass
    return {"removed": removed_total, "freed_bytes": freed_total}
