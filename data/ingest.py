"""ingest.py — ingest a snapshot directory into DuckDB."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.snapshot_layout import resolve_asset_dir as _rad
from data.db import WorkspaceDB

# All known metric snake_case keys (mirrors DrawCallModels.cs CounterToKey).
# Used to validate keys from metrics.json before writing to DB.
_ALL_METRIC_KEYS = frozenset({
    "clocks", "preemptions", "avg_preemption_delay",
    "read_total_bytes", "write_total_bytes", "tex_mem_read_bytes",
    "vertex_mem_read_bytes", "sp_mem_read_bytes",
    "avg_bytes_per_fragment", "avg_bytes_per_vertex",
    "fragments_shaded", "vertices_shaded", "reused_vertices",
    "pre_clipped_polygons", "lrz_pixels_killed",
    "avg_polygon_area", "avg_vertices_per_polygon",
    "prims_clipped_pct", "prims_trivially_rejected_pct",
    "tex_fetch_stall_pct", "tex_l1_miss_pct", "tex_l2_miss_pct",
    "tex_pipes_busy_pct", "linear_filtered_pct", "nearest_filtered_pct",
    "anisotropic_filtered_pct", "non_base_level_tex_pct",
    "l1_tex_cache_miss_per_pixel", "textures_per_fragment", "textures_per_vertex",
    "shaders_busy_pct", "shaders_stalled_pct",
    "time_alus_working_pct", "time_efus_working_pct",
    "time_shading_vertices_pct", "time_shading_fragments_pct",
    "time_compute_pct", "shader_alu_capacity_pct",
    "wave_context_occupancy_pct", "instruction_cache_miss_pct",
    "fragment_instructions", "fragment_alu_instr_full",
    "fragment_alu_instr_half", "fragment_efu_instructions",
    "vertex_instructions", "alu_per_fragment", "alu_per_vertex",
    "efu_per_fragment", "efu_per_vertex",
    "vertex_fetch_stall_pct", "stalled_on_system_mem_pct",
})


def _read_json(path: Path) -> dict | None:
    """Read a JSON file, returning None if the file does not exist."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _resolve_asset_path(snap: Path, rel: str) -> str:
    """Resolve an asset path from C# JSON to an absolute path string.

    C# writes relative paths like '../../shaders/pipeline_X.hlsl'.  The relative
    anchor is ambiguous, so we use a two-step strategy:
      1. Try to find the file by its basename via resolve_asset_dir (handles new/legacy layouts).
      2. Fall back to joining snap/rel and resolving — works if path is already absolute.
    Returns empty string if rel is empty/None or the file cannot be found.
    """
    if not rel:
        return ""
    from analysis.snapshot_layout import resolve_asset_dir
    fname = Path(rel).name  # basename: 'pipeline_X.hlsl', 'mesh_N.obj', etc.
    # Determine sub-directory from extension / prefix
    suffix = Path(fname).suffix.lower()
    if suffix in (".hlsl", ".spv", ".glsl", ".disasm"):
        candidate = resolve_asset_dir(snap, "shaders") / fname
    elif suffix == ".obj":
        candidate = resolve_asset_dir(snap, "meshes") / fname
    elif suffix in (".png", ".jpg", ".jpeg", ".bmp"):
        candidate = resolve_asset_dir(snap, "textures") / fname
    else:
        candidate = None

    if candidate and candidate.exists():
        return str(candidate)

    # Fallback: try joining with snap (handles already-absolute paths too)
    try:
        p = Path(rel)
        if p.is_absolute():
            return str(p) if p.exists() else ""
        resolved = (snap / rel).resolve()
        return str(resolved) if resolved.exists() else ""
    except Exception:
        return ""


def ingest_snapshot(db: WorkspaceDB, snapshot_dir: str | Path, project_id: str | None = None, version_id: str | None = None) -> dict:
    """
    Ingest all C# JSON outputs from snapshot_dir into DuckDB.

    Returns:
        {
            "snapshot_id": int,
            "counts": {
                "draw_calls": int,
                "shader_stages": int,
                "dc_shader_stages": int,
                "textures": int,
                "dc_textures": int,
                "meshes": int,
                "metrics": int,
                "labels": int,
            }
        }
    """
    snap = Path(snapshot_dir)
    conn = db.conn()

    # ── 1. Load JSON files ──────────────────────────────────────────────────────
    dc_data = _read_json(snap / "dc.json")
    if dc_data is None:
        raise FileNotFoundError(f"dc.json not found in {snap}")

    shaders_raw   = _read_json(snap / "shaders.json")
    textures_raw  = _read_json(snap / "textures.json")
    buffers_raw   = _read_json(_rad(snap, "meshes") / "meshes.json")
    metrics_raw   = _read_json(snap / "metrics.json")
    label_raw     = _read_json(snap / "label.json")

    # ── 1b. Load run-level texture stats (Python-generated) ────────────────────
    # <run>/textures/textures.json has width/height/size for every extracted PNG.
    # Used to fill texture rows that C# textures.json leaves as None.
    _tex_stats: dict[int, dict] = {}
    _tex_stats_path = _rad(snap, "textures") / "textures.json"
    if _tex_stats_path.exists():
        try:
            _ts = json.loads(_tex_stats_path.read_text(encoding="utf-8-sig"))
            for t in (_ts.get("textures") or []):
                tid = t.get("texture_id")
                if tid is not None:
                    _tex_stats[tid] = t
        except Exception:
            pass

    # ── 2. Derive metadata ──────────────────────────────────────────────────────
    snap_index: int  = dc_data.get("snapshot_id", 0)   # C# index within the session
    sdp_name: str    = dc_data.get("sdp_name", "")
    run_name: str    = snap.parent.name          # {analysisRoot}/{run_name}/snapshot_{N}/
    snapshot_dir_str = str(snap.resolve())
    ingested_at      = datetime.now(timezone.utc).isoformat()

    # ── 2b. Resolve snapshot_id conflicts ──────────────────────────────────────
    # C# assigns snapshot_id within a session — different SDPs produce the same IDs.
    # Rule 1: same snapshot_dir already ingested → reuse its DB snapshot_id (idempotent).
    # Rule 2: snap_index taken by a different snapshot_dir → allocate max+1 as DB PK.
    existing_by_dir = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE snapshot_dir = ?", [snapshot_dir_str]
    ).fetchone()
    if existing_by_dir:
        snapshot_id = existing_by_dir[0]
    else:
        existing_by_id = conn.execute(
            "SELECT snapshot_dir FROM snapshots WHERE snapshot_id = ?", [snap_index]
        ).fetchone()
        if existing_by_id:
            row = conn.execute("SELECT MAX(snapshot_id) FROM snapshots").fetchone()
            max_id = (row[0] if row else None) or 0
            snapshot_id = max_id + 1
        else:
            snapshot_id = snap_index

    draw_calls: list[dict[str, Any]] = dc_data.get("draw_calls", [])

    # ── 3. Begin transaction ────────────────────────────────────────────────────
    try:
        conn.rollback()
    except Exception:
        pass
    conn.begin()
    try:
        counts = _ingest_all(
            conn,
            snap=snap,
            snapshot_id=snapshot_id,
            snap_index=snap_index,
            sdp_name=sdp_name,
            run_name=run_name,
            snapshot_dir_str=snapshot_dir_str,
            ingested_at=ingested_at,
            draw_calls=draw_calls,
            shaders_raw=shaders_raw,
            textures_raw=textures_raw,
            buffers_raw=buffers_raw,
            metrics_raw=metrics_raw,
            label_raw=label_raw,
            tex_stats=_tex_stats,
            project_id=project_id,
            version_id=version_id,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"snapshot_id": snapshot_id, "counts": counts}


def _ingest_all(
    conn,
    *,
    snap: Path,
    snapshot_id: int,
    snap_index: int = 0,
    sdp_name: str,
    run_name: str,
    snapshot_dir_str: str,
    ingested_at: str,
    draw_calls: list[dict],
    shaders_raw: dict | None,
    textures_raw: dict | None,
    buffers_raw: dict | None,
    metrics_raw: dict | None,
    label_raw: dict | None,
    tex_stats: dict | None = None,
    project_id: str | None = None,
    version_id: str | None = None,
) -> dict:
    _tex_stats: dict = tex_stats or {}
    counts = {
        "draw_calls": 0,
        "shader_stages": 0,
        "dc_shader_stages": 0,
        "textures": 0,
        "dc_textures": 0,
        "meshes": 0,
        "render_targets": 0,
        "metrics": 0,
        "labels": 0,
    }

    # ── snapshots ───────────────────────────────────────────────────────────────
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (snapshot_id, sdp_name, run_name, snapshot_dir, ingested_at, snap_index, project_id, version_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [snapshot_id, sdp_name, run_name, snapshot_dir_str, ingested_at, snap_index, project_id, version_id],
    )

    # ── draw_calls ──────────────────────────────────────────────────────────────
    dc_rows = [
        (
            snapshot_id,
            _parse_api_id(dc.get("api_id")),
            _parse_api_id(dc.get("dc_id")),
            dc.get("api_name", ""),
            dc.get("pipeline_id"),
            dc.get("parameters", ""),
            dc.get("vertex_count", 0),
            dc.get("index_count", 0),
            dc.get("instance_count", 0),
            dc.get("first_vertex", 0),
            dc.get("first_index", 0),
            dc.get("vertex_offset", 0),
            dc.get("first_instance", 0),
            dc.get("draw_count", 0),
            dc.get("group_count_x", 0),
            dc.get("group_count_y", 0),
            dc.get("group_count_z", 0),
        )
        for dc in draw_calls
    ]
    if dc_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO draw_calls "
            "(snapshot_id, api_id, dc_id, api_name, pipeline_id, parameters, "
            " vertex_count, index_count, instance_count, first_vertex, first_index, "
            " vertex_offset, first_instance, draw_count, "
            " group_count_x, group_count_y, group_count_z) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            dc_rows,
        )
    counts["draw_calls"] = len(dc_rows)

    # ── setpass (state-setting calls before each DC) ─────────────────────────────
    setpass_rows: list[tuple] = []
    for dc in draw_calls:
        api_id = _parse_api_id(dc.get("api_id"))
        if not api_id:
            continue
        for sp in (dc.get("setpass") or []):
            call_id = sp.get("id")
            if call_id is None:
                continue
            setpass_rows.append((
                snapshot_id,
                api_id,
                call_id,
                sp.get("name", ""),
                sp.get("parameters", ""),
            ))
    if setpass_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO setpass VALUES (?, ?, ?, ?, ?)",
            setpass_rows,
        )
    counts["setpass"] = len(setpass_rows)

    # ── shader_stages (from dc.json inline stages, deduped by (snapshot_id, pipeline_id, stage)) ──
    shader_stage_seen: set[tuple] = set()
    shader_stage_rows: list[tuple] = []
    dc_shader_rows: list[tuple] = []

    for dc in draw_calls:
        api_id      = _parse_api_id(dc.get("api_id"))
        pipeline_id = dc.get("pipeline_id")
        if pipeline_id is None:
            continue
        stages: list[dict] = dc.get("shader_stages") or []
        for s in stages:
            stage = s.get("stage", "").capitalize()
            module_id   = s.get("module_id")
            entry_point = s.get("entry_point", "")
            file_path   = _resolve_asset_path(snap, s.get("file", "") or s.get("file_path", ""))
            key = (snapshot_id, pipeline_id, stage)
            if key not in shader_stage_seen:
                shader_stage_seen.add(key)
                shader_stage_rows.append((snapshot_id, pipeline_id, stage, module_id, entry_point, file_path))
            dc_shader_rows.append((snapshot_id, api_id, pipeline_id, stage))

    # Also pull from shaders.json if available (may have richer file_path info)
    if shaders_raw:
        shader_dcs: list[dict] = shaders_raw.get("draw_calls") or shaders_raw.get("shaders") or []
        # shaders.json structure: list of {api_id, pipeline_id, shader_stages: [...], shader_files: [...]}
        if isinstance(shader_dcs, list):
            for sdc in shader_dcs:
                pipeline_id = sdc.get("pipeline_id")
                api_id      = _parse_api_id(sdc.get("api_id"))
                if pipeline_id is None:
                    continue
                stages = sdc.get("shader_stages") or []
                # Fallback: derive stages from shader_files when shader_stages is empty
                if not stages:
                    for sf in (sdc.get("shader_files") or []):
                        # e.g. "../../shaders/pipeline_9_frag.disasm"
                        fname = sf.replace("\\", "/").split("/")[-1]
                        parts = fname.replace(".", "_").split("_")
                        # pipeline_N_stage.ext → parts = [pipeline, N, stage, ext]
                        if len(parts) >= 4:
                            stage_name = parts[2].lower()
                            stage_map = {"frag": "Fragment", "vert": "Vertex", "comp": "Compute"}
                            if stage_name in stage_map:
                                stages.append({
                                    "stage": stage_map[stage_name],
                                    "module_id": None,
                                    "entry_point": "main",
                                    "file": sf,
                                })
                for s in stages:
                    stage       = s.get("stage", "").capitalize()
                    module_id   = s.get("module_id")
                    entry_point = s.get("entry_point", "")
                    file_path   = _resolve_asset_path(snap, s.get("file", "") or s.get("file_path", ""))
                    key = (snapshot_id, pipeline_id, stage)
                    if key not in shader_stage_seen:
                        shader_stage_seen.add(key)
                        shader_stage_rows.append((snapshot_id, pipeline_id, stage, module_id, entry_point, file_path))
                    elif file_path:
                        # Override earlier row (from dc.json) that had empty file_path
                        for i, row in enumerate(shader_stage_rows):
                            if row[:3] == (snapshot_id, pipeline_id, stage) and not row[5]:
                                shader_stage_rows[i] = (snapshot_id, pipeline_id, stage, module_id or row[3], entry_point or row[4], file_path)
                                break
                    if api_id is not None:
                        row = (snapshot_id, api_id, pipeline_id, stage)
                        if row not in set(dc_shader_rows):
                            dc_shader_rows.append(row)

    if shader_stage_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO shader_stages VALUES (?, ?, ?, ?, ?, ?)",
            shader_stage_rows,
        )
    counts["shader_stages"] = len(shader_stage_rows)

    if dc_shader_rows:
        # Deduplicate before insert
        unique_dc_shader = list({r: None for r in dc_shader_rows}.keys())
        conn.executemany(
            "INSERT OR REPLACE INTO dc_shader_stages VALUES (?, ?, ?, ?)",
            unique_dc_shader,
        )
        counts["dc_shader_stages"] = len(unique_dc_shader)

    # ── textures ────────────────────────────────────────────────────────────────
    texture_seen: set[int] = set()
    texture_rows: list[tuple] = []
    dc_texture_rows: list[tuple] = []

    tex_dir = _rad(snap, "textures")

    if textures_raw:
        texture_dcs: list[dict] = textures_raw.get("draw_calls") or textures_raw.get("textures") or []
        if isinstance(texture_dcs, list):
            for tdc in texture_dcs:
                api_id = _parse_api_id(tdc.get("api_id"))
                # Per-DC texture list
                textures_list: list[dict] = tdc.get("textures") or []
                for t in textures_list:
                    tex_id = t.get("texture_id")
                    if tex_id is None:
                        continue
                    if tex_id not in texture_seen:
                        texture_seen.add(tex_id)
                        # Resolve texture file from run-level textures/ dir
                        tex_file = _resolve_asset_path(snap, t.get("file", "") or t.get("file_path", ""))
                        if not tex_file:
                            candidate = tex_dir / f"texture_{tex_id}.png"
                            if candidate.exists():
                                tex_file = str(candidate)
                        # Prefer Python-generated stats (width/height populated from PNG)
                        ts = _tex_stats.get(tex_id, {})
                        texture_rows.append((
                            snapshot_id,
                            tex_id,
                            ts.get("width")  or t.get("width"),
                            ts.get("height") or t.get("height"),
                            t.get("depth"),
                            t.get("format"),
                            t.get("layers"),
                            t.get("levels"),
                            tex_file or (str(tex_dir / ts["file"]) if ts.get("file") else ""),
                        ))
                    if api_id is not None:
                        dc_texture_rows.append((snapshot_id, api_id, tex_id))

                # Also cover flat texture_ids list
                for tex_id in (tdc.get("texture_ids") or []):
                    if tex_id is None:
                        continue
                    if tex_id not in texture_seen:
                        texture_seen.add(tex_id)
                        ts = _tex_stats.get(tex_id, {})
                        candidate = tex_dir / f"texture_{tex_id}.png"
                        tex_file = str(candidate) if candidate.exists() else (
                            str(tex_dir / ts["file"]) if ts.get("file") else ""
                        )
                        texture_rows.append((
                            snapshot_id, tex_id,
                            ts.get("width"), ts.get("height"),
                            None, None, None, None, tex_file,
                        ))
                    if api_id is not None:
                        dc_texture_rows.append((snapshot_id, api_id, tex_id))

    if texture_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO textures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            texture_rows,
        )
    counts["textures"] = len(texture_rows)

    if dc_texture_rows:
        unique_dc_tex = list({r: None for r in dc_texture_rows}.keys())
        conn.executemany(
            "INSERT OR REPLACE INTO dc_textures VALUES (?, ?, ?)",
            unique_dc_tex,
        )
        counts["dc_textures"] = len(unique_dc_tex)

    # ── meshes ──────────────────────────────────────────────────────────────────
    valid_dc_api_ids = {row[1] for row in dc_rows}  # set of api_ids that exist in draw_calls
    mesh_rows: list[tuple] = []
    if buffers_raw:
        buffer_dcs: list[dict] = buffers_raw.get("meshes") or buffers_raw.get("draw_calls") or buffers_raw.get("buffers") or []
        if isinstance(buffer_dcs, list):
            for bdc in buffer_dcs:
                api_id    = _parse_api_id(bdc.get("api_id"))
                mesh_file = _resolve_asset_path(snap, bdc.get("mesh_file", ""))
                if api_id and mesh_file and api_id in valid_dc_api_ids:
                    mesh_rows.append((
                        snapshot_id,
                        api_id,
                        mesh_file,
                        _int_or_none(bdc.get("vertex_count")),
                        _int_or_none(bdc.get("face_count")),
                        _int_or_none(bdc.get("normal_count")),
                        _int_or_none(bdc.get("uv_count")),
                        bdc.get("bbox_min"),
                        bdc.get("bbox_max"),
                    ))

    if mesh_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO meshes "
            "(snapshot_id, api_id, mesh_file, vertex_count, face_count, "
            " normal_count, uv_count, bbox_min, bbox_max) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            mesh_rows,
        )
    counts["meshes"] = len(mesh_rows)

    # ── render_targets (from dc.json per-DC render_targets array) ────────────────
    rt_rows: list[tuple] = []
    valid_api_ids = valid_dc_api_ids
    for dc in draw_calls:
        api_id = _parse_api_id(dc.get("api_id"))
        if not api_id:
            continue
        for rt in (dc.get("render_targets") or []):
            rt_rows.append((
                snapshot_id,
                api_id,
                rt.get("attachment_index", 0),
                rt.get("attachment_type"),
                rt.get("resource_id"),
                rt.get("renderpass_id"),
                rt.get("framebuffer_id"),
                rt.get("width"),
                rt.get("height"),
                rt.get("format"),
            ))

    if rt_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO dc_render_targets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rt_rows,
        )
    counts["render_targets"] = len(rt_rows)

    # ── metrics ─────────────────────────────────────────────────────────────────
    # Discover which keys are present in this metrics.json (varies by MetricsWhitelist)
    # and build a dynamic INSERT with only those columns + the two PK columns.
    metrics_rows: list[tuple] = []
    metrics_cols: list[str] = []  # ordered non-PK columns actually found

    if metrics_raw:
        all_dcs = metrics_raw.get("draw_calls") or []
        # Collect the union of keys present across all DCs in this snapshot
        present_keys: set[str] = set()
        for dc in all_dcs:
            present_keys.update((dc.get("metrics") or {}).keys())
        # Keep only known schema columns, preserve a stable order
        _ORDERED = [k for k in [
            "clocks", "preemptions", "avg_preemption_delay",
            "read_total_bytes", "write_total_bytes", "tex_mem_read_bytes",
            "vertex_mem_read_bytes", "sp_mem_read_bytes",
            "avg_bytes_per_fragment", "avg_bytes_per_vertex",
            "fragments_shaded", "vertices_shaded", "reused_vertices",
            "pre_clipped_polygons", "lrz_pixels_killed",
            "avg_polygon_area", "avg_vertices_per_polygon",
            "prims_clipped_pct", "prims_trivially_rejected_pct",
            "tex_fetch_stall_pct", "tex_l1_miss_pct", "tex_l2_miss_pct",
            "tex_pipes_busy_pct", "linear_filtered_pct", "nearest_filtered_pct",
            "anisotropic_filtered_pct", "non_base_level_tex_pct",
            "l1_tex_cache_miss_per_pixel", "textures_per_fragment", "textures_per_vertex",
            "shaders_busy_pct", "shaders_stalled_pct",
            "time_alus_working_pct", "time_efus_working_pct",
            "time_shading_vertices_pct", "time_shading_fragments_pct",
            "time_compute_pct", "shader_alu_capacity_pct",
            "wave_context_occupancy_pct", "instruction_cache_miss_pct",
            "fragment_instructions", "fragment_alu_instr_full",
            "fragment_alu_instr_half", "fragment_efu_instructions",
            "vertex_instructions", "alu_per_fragment", "alu_per_vertex",
            "efu_per_fragment", "efu_per_vertex",
            "vertex_fetch_stall_pct", "stalled_on_system_mem_pct",
        ] if k in present_keys and k in _ALL_METRIC_KEYS]
        metrics_cols = _ORDERED

        # Integer columns
        _INT_COLS = frozenset({
            "clocks", "preemptions", "read_total_bytes", "write_total_bytes",
            "tex_mem_read_bytes", "vertex_mem_read_bytes", "sp_mem_read_bytes",
            "fragments_shaded", "vertices_shaded", "reused_vertices",
            "pre_clipped_polygons", "lrz_pixels_killed",
            "fragment_instructions", "fragment_alu_instr_full",
            "fragment_alu_instr_half", "fragment_efu_instructions",
            "vertex_instructions",
        })

        for dc in all_dcs:
            api_id = _parse_api_id(dc.get("api_id"))
            if not api_id:
                continue
            m = dc.get("metrics") or {}
            if not m:
                continue
            vals: list = [snapshot_id, api_id]
            for col in metrics_cols:
                raw = m.get(col)
                vals.append(_int_or_none(raw) if col in _INT_COLS else _float_or_none(raw))
            metrics_rows.append(tuple(vals))

    if metrics_rows and metrics_cols:
        col_names = ", ".join(metrics_cols)
        placeholders = ", ".join(["?"] * (2 + len(metrics_cols)))
        conn.executemany(
            f"INSERT OR REPLACE INTO metrics (snapshot_id, api_id, {col_names}) "
            f"VALUES ({placeholders})",
            metrics_rows,
        )
    counts["metrics"] = len(metrics_rows)

    # ── labels ──────────────────────────────────────────────────────────────────
    label_rows: list[tuple] = []
    if label_raw:
        labeled_at = datetime.now(timezone.utc).isoformat()
        for dc in (label_raw.get("draw_calls") or []):
            api_id = _parse_api_id(dc.get("api_id"))
            if not api_id:
                continue
            lb = dc.get("label") or {}
            label_rows.append((
                snapshot_id,
                api_id,
                lb.get("category", ""),
                lb.get("subcategory", ""),
                lb.get("detail", ""),
                json.dumps(lb.get("reason_tags") or []),
                float(lb.get("confidence", 0.0)),
                lb.get("label_source", "rule"),
                lb.get("bottleneck_text"),  # may be None
                None,                       # embedding — Phase 5
                labeled_at,
            ))

    if label_rows:
        # Only insert labels whose api_id exists in draw_calls (guards against stale label.json)
        valid_api_ids = {row[1] for row in dc_rows}
        label_rows = [r for r in label_rows if r[1] in valid_api_ids]
        conn.executemany(
            "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            label_rows,
        )
    counts["labels"] = len(label_rows)

    return counts


# ── Type-coercion helpers ───────────────────────────────────────────────────────

def _parse_api_id(v) -> int:
    """Parse api_id from either int or encoded string like '1.1.31' (submit.cmdBuf.drawcall)."""
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if "." in s:
        parts = s.split(".")
        try:
            return int(parts[-1])
        except ValueError:
            return 0
    try:
        return int(s)
    except ValueError:
        return 0


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
