"""query.py — typed read API for the DuckDB workspace DB."""
from __future__ import annotations

import json

from data.db import WorkspaceDB

# ── Helpers ─────────────────────────────────────────────────────────────────────

_ALL_SNAPSHOTS = 1  # sentinel: skip snapshot_id filter → query all snapshots


def _snap_where(snapshot_id: int, alias: str = "") -> tuple[str, list]:
    """Return (WHERE/AND clause fragment, params) for a snapshot_id filter.

    When snapshot_id == 1 returns ("", []) — no filter applied (all snapshots).
    alias="" → bare column name; alias="dc" → "dc.snapshot_id".
    """
    if snapshot_id == _ALL_SNAPSHOTS:
        return "", []
    col = f"{alias}.snapshot_id" if alias else "snapshot_id"
    return f"{col} = ?", [snapshot_id]

def _rows_to_dicts(result) -> list[dict]:
    """Convert a DuckDB result to a list of dicts using result.description."""
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


def _parse_reason_tags(row: dict) -> dict:
    """Parse the reason_tags JSON string in-place and return the mutated dict."""
    raw = row.get("reason_tags")
    if isinstance(raw, str):
        try:
            row["reason_tags"] = json.loads(raw)
        except (ValueError, TypeError):
            row["reason_tags"] = []
    elif raw is None:
        row["reason_tags"] = []
    return row


def _filter_none_values(d: dict) -> dict:
    """Return a new dict with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


# ── Public API ───────────────────────────────────────────────────────────────────

def get_draw_calls(
    db: WorkspaceDB,
    snapshot_id: int,
    *,
    category: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    """Return draw_calls LEFT JOINed with labels for a snapshot.

    Each row: {api_id, dc_id, api_name, pipeline_id, vertex_count, index_count,
               category, subcategory, detail, confidence, label_source,
               reason_tags (parsed list)}

    Filters (optional):
    - category: exact match on labels.category
    - tags: post-filter — any tag in reason_tags must match at least one item in tags
    """
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    sql = f"""
        SELECT
            dc.api_id,
            dc.dc_id,
            dc.api_name,
            dc.pipeline_id,
            dc.vertex_count,
            dc.index_count,
            dc.instance_count,
            dc.first_vertex,
            dc.first_index,
            dc.vertex_offset,
            dc.first_instance,
            dc.draw_count,
            dc.group_count_x,
            dc.group_count_y,
            dc.group_count_z,
            lb.category,
            lb.subcategory,
            lb.detail,
            lb.confidence,
            lb.label_source,
            lb.reason_tags,
            m.clocks,
            m.fragments_shaded,
            m.vertices_shaded,
            m.read_total_bytes,
            m.write_total_bytes,
            m.shaders_busy_pct,
            m.shaders_stalled_pct,
            m.time_alus_working_pct,
            m.tex_fetch_stall_pct,
            m.tex_l1_miss_pct,
            m.tex_pipes_busy_pct,
            m.lrz_pixels_killed
        FROM draw_calls dc
        LEFT JOIN labels lb
            ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m
            ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where}
    """
    params: list = snap_params[:]

    if category is not None:
        sql += " AND lb.category = ?"
        params.append(category)

    sql += " ORDER BY dc.api_id"

    result = db.conn().execute(sql, params)
    rows = _rows_to_dicts(result)

    # Parse reason_tags JSON
    for row in rows:
        _parse_reason_tags(row)

    # Post-filter by tags (Python-side, simpler than DuckDB JSON gymnastics)
    if tags:
        tag_set = set(tags)
        rows = [r for r in rows if tag_set.intersection(r.get("reason_tags") or [])]

    return rows


def get_labels(db: WorkspaceDB, snapshot_id: int) -> dict[int, dict]:
    """Return all labels for a snapshot keyed by api_id.

    Each value: {api_id, category, subcategory, detail, reason_tags (list),
                 confidence, label_source, bottleneck_text, labeled_at}
    """
    snap_clause, snap_params = _snap_where(snapshot_id)
    where = f"WHERE {snap_clause}" if snap_clause else ""
    sql = f"""
        SELECT api_id, category, subcategory, detail, reason_tags,
               confidence, label_source, bottleneck_text, labeled_at
        FROM labels
        {where}
        ORDER BY api_id
    """
    result = db.conn().execute(sql, snap_params)
    rows = _rows_to_dicts(result)

    out: dict[int, dict] = {}
    for row in rows:
        _parse_reason_tags(row)
        # Convert labeled_at to string for JSON serialisability
        if row.get("labeled_at") is not None:
            row["labeled_at"] = str(row["labeled_at"])
        out[row["api_id"]] = row

    return out


def get_metrics(db: WorkspaceDB, snapshot_id: int) -> dict[int, dict]:
    """Return all metrics for a snapshot keyed by api_id.

    Returns all columns present in the schema; only non-None values included.
    Actual columns populated depend on MetricsWhitelist in config.ini at capture time.
    """
    snap_clause, snap_params = _snap_where(snapshot_id)
    where = f"WHERE {snap_clause}" if snap_clause else ""
    sql = f"SELECT * FROM metrics {where} ORDER BY api_id"
    result = db.conn().execute(sql, snap_params)
    rows = _rows_to_dicts(result)

    out: dict[int, dict] = {}
    for row in rows:
        api_id = row["api_id"]
        out[api_id] = _filter_none_values(row)

    return out


def get_dc_detail(db: WorkspaceDB, snapshot_id: int, api_id: int) -> dict | None:
    """Return full detail for one DC: base fields + label + metrics + shader_stages
    + textures + mesh_file.

    Returns None if api_id is not found in draw_calls for this snapshot.
    Each query uses an independent cursor to avoid shared-connection result set conflicts.
    """

    # ── Base DC row ──────────────────────────────────────────────────────────────
    dc_rows = _rows_to_dicts(db.cursor().execute(
        """
        SELECT api_id, dc_id, api_name, pipeline_id,
               vertex_count, index_count, instance_count,
               first_vertex, first_index, vertex_offset, first_instance,
               draw_count, group_count_x, group_count_y, group_count_z
        FROM draw_calls
        WHERE snapshot_id = ? AND api_id = ?
        """,
        [snapshot_id, api_id],
    ))
    if not dc_rows:
        return None
    dc = dc_rows[0]

    # ── Label ────────────────────────────────────────────────────────────────────
    label_rows = _rows_to_dicts(db.cursor().execute(
        """
        SELECT category, subcategory, detail, confidence, label_source,
               reason_tags, bottleneck_text
        FROM labels
        WHERE snapshot_id = ? AND api_id = ?
        """,
        [snapshot_id, api_id],
    ))
    if label_rows:
        lb = label_rows[0]
        _parse_reason_tags(lb)
        dc["label"] = lb
    else:
        dc["label"] = None

    # ── Metrics ──────────────────────────────────────────────────────────────────
    metrics_rows = _rows_to_dicts(db.cursor().execute(
        "SELECT * FROM metrics WHERE snapshot_id = ? AND api_id = ?",
        [snapshot_id, api_id],
    ))
    if metrics_rows:
        dc["metrics"] = _filter_none_values(metrics_rows[0])
    else:
        dc["metrics"] = None

    # ── Metric stats (median / min / max across all DCs in this snapshot) ────────
    # Used by frontend for heatmap coloring. Only compute if this DC has metrics.
    if dc["metrics"]:
        metric_cols = [
            r[0] for r in db.cursor().execute("DESCRIBE metrics").fetchall()
            if r[0] not in ("snapshot_id", "api_id")
        ]
        if metric_cols:
            aggs = ", ".join(
                f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {c}) AS {c}_median,"
                f"min({c}) AS {c}_min,"
                f"max({c}) AS {c}_max"
                for c in metric_cols
            )
            stats_cur = db.cursor()
            stats_cur.execute(
                f"SELECT {aggs} FROM metrics WHERE snapshot_id = ?",
                [snapshot_id],
            )
            stats_row = stats_cur.fetchone()
            stats_desc = [d[0] for d in stats_cur.description]
            metric_stats: dict[str, dict] = {}
            if stats_row:
                for col_name, val in zip(stats_desc, stats_row):
                    if val is None:
                        continue
                    for suffix in ("_median", "_min", "_max"):
                        if col_name.endswith(suffix):
                            metric = col_name[: -len(suffix)]
                            if metric not in metric_stats:
                                metric_stats[metric] = {}
                            metric_stats[metric][suffix[1:]] = val
                            break
            dc["metric_stats"] = metric_stats
        else:
            dc["metric_stats"] = {}
    else:
        dc["metric_stats"] = {}

    # ── Shader stages ────────────────────────────────────────────────────────────
    dc["shader_stages"] = _rows_to_dicts(db.cursor().execute(
        """
        SELECT ss.pipeline_id, ss.stage, ss.module_id, ss.entry_point, ss.file_path
        FROM dc_shader_stages dcs
        JOIN shader_stages ss
            ON ss.snapshot_id = dcs.snapshot_id
           AND ss.pipeline_id = dcs.pipeline_id
           AND ss.stage = dcs.stage
        WHERE dcs.snapshot_id = ? AND dcs.api_id = ?
        ORDER BY ss.pipeline_id, ss.stage
        """,
        [snapshot_id, api_id],
    ))

    # ── Textures ─────────────────────────────────────────────────────────────────
    dc["textures"] = _rows_to_dicts(db.cursor().execute(
        """
        SELECT t.texture_id, t.width, t.height, t.depth,
               t.format, t.layers, t.levels, t.file_path
        FROM dc_textures dct
        JOIN textures t
            ON t.snapshot_id = dct.snapshot_id
           AND t.texture_id = dct.texture_id
        WHERE dct.snapshot_id = ? AND dct.api_id = ?
        ORDER BY t.texture_id
        """,
        [snapshot_id, api_id],
    ))

    # ── Mesh file ────────────────────────────────────────────────────────────────
    mesh_rows = db.cursor().execute(
        "SELECT mesh_file FROM meshes WHERE snapshot_id = ? AND api_id = ?",
        [snapshot_id, api_id],
    ).fetchall()
    dc["mesh_file"] = mesh_rows[0][0] if mesh_rows else None

    # ── Render targets (from dc_render_targets table) ────────────────────────────
    rt_cur = db.cursor()
    rt_cur.execute(
        "SELECT attachment_index, attachment_type, resource_id, renderpass_id, "
        "framebuffer_id, width, height, format "
        "FROM dc_render_targets WHERE snapshot_id = ? AND api_id = ? "
        "ORDER BY attachment_index",
        [snapshot_id, api_id],
    )
    rt_cols = [d[0] for d in rt_cur.description]
    dc["render_targets"] = [dict(zip(rt_cols, row)) for row in rt_cur.fetchall()]

    return dc


def query_dcs(
    db: WorkspaceDB,
    snapshot_id: int,
    *,
    category: str | None = None,
    min_clocks: int | None = None,
    label_source: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    """Filtered query combining draw_calls + labels + metrics.

    Each row: {api_id, dc_id, api_name, category, subcategory, confidence,
               label_source, clocks, read_total_bytes, write_total_bytes}

    All filters are optional and ANDed. Results ordered by clocks DESC (nulls last).
    """
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    sql = f"""
        SELECT
            dc.api_id,
            dc.dc_id,
            dc.api_name,
            lb.category,
            lb.subcategory,
            lb.confidence,
            lb.label_source,
            lb.reason_tags,
            m.clocks,
            m.read_total_bytes,
            m.write_total_bytes
        FROM draw_calls dc
        LEFT JOIN labels lb
            ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m
            ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where}
    """
    params: list = snap_params[:]

    if category is not None:
        sql += " AND lb.category = ?"
        params.append(category)

    if min_clocks is not None:
        sql += " AND m.clocks >= ?"
        params.append(min_clocks)

    if label_source is not None:
        sql += " AND lb.label_source = ?"
        params.append(label_source)

    # Order: clocks DESC, NULLs last
    sql += " ORDER BY m.clocks DESC NULLS LAST, dc.api_id"

    result = db.conn().execute(sql, params)
    rows = _rows_to_dicts(result)

    # Parse reason_tags for tag post-filter
    for row in rows:
        _parse_reason_tags(row)

    # Post-filter by tags
    if tags:
        tag_set = set(tags)
        rows = [r for r in rows if tag_set.intersection(r.get("reason_tags") or [])]

    # Remove reason_tags from final output (it was only needed for tag filtering)
    for row in rows:
        row.pop("reason_tags", None)

    return rows
