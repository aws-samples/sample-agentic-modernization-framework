"""Path containment for discovery-export adapters."""

from __future__ import annotations

from pathlib import Path


def safe_path(base_dir: str | Path, file_path: str | Path) -> Path:
    """Resolve file_path and require it to stay inside base_dir (prevents path traversal).

    Symlinks and `..` segments are resolved before the check. Raises ValueError
    if the resolved path escapes the base directory.
    """
    base_resolved = Path(base_dir).resolve()
    resolved = Path(file_path).resolve()
    if not resolved.is_relative_to(base_resolved):
        raise ValueError(f"Path traversal detected: {file_path} resolves outside {base_dir}")
    return resolved
