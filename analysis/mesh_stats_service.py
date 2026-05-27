"""mesh_stats_service.py — Parse OBJ mesh files and write buffers.json.

Reads:  <run_dir>/meshes/mesh_{api_id}.obj  (extracted by C# MeshExtractor)
Writes: <snapshot_dir>/buffers.json

Each draw_call entry in buffers.json:
  api_id, mesh_file (relative path), vertex_count, face_count,
  normal_count, uv_count, bbox_min [x,y,z], bbox_max [x,y,z]

Intended to run BEFORE ingest so ingest picks up the stats.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Header comment pattern written by C# MeshExtractor
_RE_COUNTS = re.compile(
    r"#\s*Vertices:\s*(\d+)\s+Faces:\s*(\d+)", re.IGNORECASE
)
_RE_NUV = re.compile(
    r"#\s*Normals:\s*(\d+)\s+TexCoords:\s*(\d+)", re.IGNORECASE
)


def _parse_obj(path: Path) -> dict:
    """Parse a single OBJ file.  Returns stats dict."""
    vertex_count = 0
    face_count   = 0
    normal_count = 0
    uv_count     = 0
    bbox_min = [float("inf"),  float("inf"),  float("inf")]
    bbox_max = [float("-inf"), float("-inf"), float("-inf")]
    counts_from_header = False

    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue

                # Try to read counts from C# header comments (fast path)
                if line.startswith("#") and not counts_from_header:
                    m = _RE_COUNTS.search(line)
                    if m:
                        vertex_count = int(m.group(1))
                        face_count   = int(m.group(2))
                    m2 = _RE_NUV.search(line)
                    if m2:
                        normal_count = int(m2.group(1))
                        uv_count     = int(m2.group(2))
                        counts_from_header = True
                    continue

                # Vertex positions — always parse for bbox
                if line.startswith("v "):
                    parts = line.split()
                    if len(parts) >= 4:
                        try:
                            x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                            if x < bbox_min[0]: bbox_min[0] = x
                            if y < bbox_min[1]: bbox_min[1] = y
                            if z < bbox_min[2]: bbox_min[2] = z
                            if x > bbox_max[0]: bbox_max[0] = x
                            if y > bbox_max[1]: bbox_max[1] = y
                            if z > bbox_max[2]: bbox_max[2] = z
                            if not counts_from_header:
                                vertex_count += 1
                        except ValueError:
                            pass
                elif not counts_from_header:
                    if line.startswith("vn "):
                        normal_count += 1
                    elif line.startswith("vt "):
                        uv_count += 1
                    elif line.startswith("f "):
                        face_count += 1

    except Exception:
        pass

    # Guard: if no vertices were found bbox stays inf/-inf → return None
    if bbox_min[0] == float("inf"):
        bbox_min = bbox_max = None
    else:
        bbox_min = [round(v, 6) for v in bbox_min]
        bbox_max = [round(v, 6) for v in bbox_max]

    return {
        "vertex_count": vertex_count,
        "face_count":   face_count,
        "normal_count": normal_count,
        "uv_count":     uv_count,
        "bbox_min":     bbox_min,
        "bbox_max":     bbox_max,
    }


def generate_buffers_json(snapshot_dir: str | Path) -> Path:
    """
    Scan <run_dir>/meshes/ for OBJ files, parse stats, write
    <run_dir>/meshes/meshes.json (run-level, shared across snapshots).

    Returns path to written meshes.json.
    Raises FileNotFoundError if meshes directory doesn't exist.
    """
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    from analysis.snapshot_layout import resolve_asset_dir
    snap = Path(snapshot_dir)
    meshes_dir = resolve_asset_dir(snap, "meshes")

    if not meshes_dir.exists():
        raise FileNotFoundError(f"Meshes directory not found: {meshes_dir}")

    obj_files = sorted(meshes_dir.glob("mesh_*.obj"))
    if not obj_files:
        raise FileNotFoundError(f"No mesh_*.obj files found in {meshes_dir}")

    _log.info(f"[MeshStats] start: {len(obj_files)} OBJ files")
    _t0 = _time.time()

    entries = []
    for obj in obj_files:
        stem = obj.stem  # mesh_91586
        try:
            api_id = int(stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue

        stats = _parse_obj(obj)
        entries.append({
            "api_id":       api_id,
            "mesh_file":    obj.name,          # relative to meshes_dir
            "vertex_count": stats["vertex_count"],
            "face_count":   stats["face_count"],
            "normal_count": stats["normal_count"],
            "uv_count":     stats["uv_count"],
            "bbox_min":     stats["bbox_min"],
            "bbox_max":     stats["bbox_max"],
        })
        _log.debug(f"[MeshStats] {obj.name}: verts={stats['vertex_count']} faces={stats['face_count']}")

    out = meshes_dir / "meshes.json"
    out.write_text(
        json.dumps({"meshes": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _log.info(f"[MeshStats] done: {_time.time()-_t0:.1f}s, {len(entries)} meshes parsed")
    return out
