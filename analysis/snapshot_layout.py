"""snapshot_layout.py — Resolve asset sub-directories for a snapshot.

SDPCLI can produce two layouts depending on version:

  New layout (current):
    <analysis_root>/<run>/snapshot_N/shaders/
    <analysis_root>/<run>/snapshot_N/meshes/
    <analysis_root>/<run>/snapshot_N/textures/

  Legacy layout (monorepo):
    <analysis_root>/<run>/shaders/
    <analysis_root>/<run>/meshes/
    <analysis_root>/<run>/textures/

All services should call `resolve_asset_dir` instead of hardcoding .parent.
"""
from pathlib import Path


def resolve_asset_dir(snapshot_dir: str | Path, subdir: str) -> Path:
    """Return the Path for an asset sub-directory (shaders/meshes/textures/etc).

    Checks snapshot_dir/subdir first (new layout), then snapshot_dir.parent/subdir
    (legacy layout). Returns the first that exists; falls back to snapshot_dir/subdir
    (new layout default) so callers get a consistent path even if it doesn't exist yet.
    """
    snap = Path(snapshot_dir)
    candidate_new    = snap          / subdir
    candidate_legacy = snap.parent   / subdir
    if candidate_new.exists():
        return candidate_new
    if candidate_legacy.exists():
        return candidate_legacy
    return candidate_new  # default to new layout
