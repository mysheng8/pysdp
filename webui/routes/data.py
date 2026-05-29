"""data.py — /api/data/* endpoints for DuckDB data layer."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import logger as _logger_module
from data.db import WorkspaceDB
from data.ingest import ingest_snapshot
from data.query import get_draw_calls, get_dc_detail, get_setpass
from data import model_registry as _model_registry
from data import questions as _q
from data import dashboards as _dash
from analysis.label_service import generate_label_json
from analysis.status_service import generate_status_json




# ── Pydantic request models ───────────────────────────────────────────────────

class QuestionCreate(BaseModel):
    title: str
    model_name: str
    model_params: Optional[dict] = {}
    viz_type: str = "table"
    viz_config: Optional[dict] = {}


class QuestionUpdate(BaseModel):
    title: Optional[str] = None
    model_params: Optional[dict] = None
    viz_type: Optional[str] = None
    viz_config: Optional[dict] = None


class DashboardCreate(BaseModel):
    title: str
    question_ids: list[str] = []


class DashboardUpdate(BaseModel):
    title: Optional[str] = None
    question_ids: Optional[list[str]] = None


def make_router(db: WorkspaceDB) -> APIRouter:
    """Factory: returns an APIRouter with the db instance injected via closure."""
    router = APIRouter()

    @router.get("/snapshots", summary="[MCP] List ingested snapshots",
                operation_id="get_snapshots",
                description="Return all GPU capture snapshots that have been ingested into the database, "
                            "ordered by ingestion time (newest first). Each entry includes snapshot_id "
                            "(use this as the key for all other queries), sdp_name, run_name, "
                            "snapshot_dir path, and ingested_at timestamp.")
    def list_snapshots():
        """Return all ingested snapshots ordered by ingestion time (newest first)."""
        from pathlib import Path as _Path
        import zipfile as _zipfile, json as _json
        _SCREENSHOT_NAMES = ["snapshot.png", "snapshot_screenshot.png",
                             "snapshot_screenshot.jpg", "snapshot.bmp"]

        def _find_in_dir(d: _Path) -> str | None:
            for name in _SCREENSHOT_NAMES:
                p = d / name
                if p.exists():
                    return str(p)
            for p in sorted(d.glob("*.bmp")):
                return str(p)
            return None

        def _find_sdp_file(snap: _Path, sdp_name: str) -> _Path | None:
            run_dir = snap.parent
            analysis_dir = run_dir.parent
            sdp_root = analysis_dir.parent
            for candidate_dir in [sdp_root, analysis_dir.parent]:
                p = candidate_dir / f"{sdp_name}.sdp"
                if p.exists():
                    return p
            return None

        def _find_screenshot(snapshot_dir: str, sdp_name: str = "") -> str | None:
            d = _Path(snapshot_dir)
            # 1. Look in analysis snapshot dir (already copied there)
            result = _find_in_dir(d)
            if result:
                return result
            # 2. Extract from .sdp archive
            if sdp_name:
                sdp_file = _find_sdp_file(d, sdp_name)
                if sdp_file:
                    try:
                        snap_name = d.name  # snapshot_6
                        with _zipfile.ZipFile(sdp_file, "r") as zf:
                            members = [m for m in zf.namelist()
                                       if m.startswith(snap_name + "/")
                                       and _Path(m).suffix.lower() in (".bmp", ".png", ".jpg")]
                            if members:
                                members.sort()
                                member = members[0]
                                dest = d / _Path(member).name
                                d.mkdir(parents=True, exist_ok=True)
                                if not dest.exists():
                                    with zf.open(member) as sf, open(dest, "wb") as df:
                                        df.write(sf.read())
                                return str(dest)
                    except Exception:
                        pass
            return None

        try:
            rows = db.cursor().execute(
                """
                SELECT s.snapshot_id, s.sdp_name, s.run_name, s.snapshot_dir,
                       CAST(s.ingested_at AS VARCHAR) AS ingested_at,
                       s.snap_index, s.project_id, s.version_id,
                       d.screenshot_path, d.screenshot_width, d.screenshot_height,
                       d.screenshot_size, d.description, d.vlm_model,
                       d.status AS desc_status, d.error_msg AS desc_error,
                       CAST(d.generated_at AS VARCHAR) AS desc_generated_at
                FROM snapshots s
                LEFT JOIN snapshot_descriptions d ON d.snapshot_id = s.snapshot_id
                ORDER BY s.ingested_at DESC
                """
            ).fetchall()
            data = []
            for row in rows:
                (snap_id, sdp_name, run_name, snap_dir, ingested_at,
                 snap_index, project_id, version_id,
                 scr_path, scr_w, scr_h, scr_size,
                 description, vlm_model, desc_status, desc_error, desc_generated_at) = row
                screenshot = scr_path or (_find_screenshot(snap_dir, sdp_name) if snap_dir else None)
                data.append({
                    "snapshot_id":  snap_id,
                    "snap_index":   snap_index,
                    "sdp_name":     sdp_name,
                    "run_name":     run_name,
                    "snapshot_dir": snap_dir,
                    "ingested_at":  str(ingested_at),
                    "project_id":   project_id,
                    "version_id":   version_id,
                    "screenshot":   screenshot,
                    "description": {
                        "text":         description,
                        "vlm_model":    vlm_model,
                        "status":       desc_status,
                        "error":        desc_error,
                        "generated_at": desc_generated_at,
                        "width":        scr_w,
                        "height":       scr_h,
                        "size":         scr_size,
                    } if desc_status is not None else None,
                })
            return {"ok": True, "data": data}
        except Exception as exc:
            _logger_module.get_logger().error("list_snapshots failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/draw_calls", summary="[MCP] List draw calls for a snapshot",
                operation_id="get_draw_calls",
                description="Return all GPU draw calls for a snapshot, with label classification and "
                            "key metrics (clocks, fragments_shaded, memory bytes, shader utilization). "
                            "Use snapshot_id from /snapshots. "
                            "Filter by category (e.g. 'Shadow', 'UI', 'Scene', 'VFX', 'Compute') or "
                            "by reason_tags (comma-separated). Results ordered by api_id. "
                            "Use min_clocks filter in /query_dcs for clock-sorted results.")
    def draw_calls(
        snapshot_id: int = Query(..., description="Snapshot ID to query"),
        category: str = Query(default=None, description="Filter by label category"),
        tags: str = Query(default=None, description="Comma-separated tag list to filter by"),
    ):
        """Return draw calls (with labels) for a snapshot, with optional filters."""
        try:
            tag_list = tags.split(",") if tags else None
            data = get_draw_calls(db, snapshot_id, category=category, tags=tag_list)
            return {"ok": True, "data": data}
        except Exception as exc:
            _logger_module.get_logger().error(
                "draw_calls failed", exc=exc, context={"snapshot_id": snapshot_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/dc/{api_id}", summary="[MCP] Get full draw call detail",
                operation_id="get_dc_detail",
                description="Return complete information for a single draw call: "
                            "base parameters (vertex_count, index_count, pipeline_id, api_name), "
                            "label (category, subcategory, confidence, bottleneck_text, reason_tags), "
                            "metrics (clocks, fragments_shaded, memory bandwidth, shader/texture percentages), "
                            "metric_stats (median/min/max across all DCs for heatmap context), "
                            "shader_stages (stage, entry_point, file_path to HLSL source), "
                            "textures (dimensions, format, file_path), "
                            "mesh_file (path to OBJ), "
                            "render_targets (attachment_type, resource_id, dimensions, format).")
    def dc_detail(
        api_id: int,
        snapshot_id: int = Query(..., description="Snapshot ID to query"),
    ):
        """Return full detail for a single draw call: base + label + metrics + shaders + textures + mesh."""
        try:
            data = get_dc_detail(db, snapshot_id, api_id)
            if data is None:
                return JSONResponse(
                    {"ok": False, "error": f"api_id {api_id} not found in snapshot {snapshot_id}"},
                    status_code=404,
                )
            return {"ok": True, "data": data}
        except Exception as exc:
            _logger_module.get_logger().error(
                "dc_detail failed", exc=exc,
                context={"snapshot_id": snapshot_id, "api_id": api_id},
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/setpass/{api_id}", summary="Get setpass calls for a draw call",
                operation_id="get_setpass")
    def setpass_endpoint(
        api_id: int,
        snapshot_id: int = Query(..., description="Snapshot ID"),
    ):
        try:
            data = get_setpass(db, snapshot_id, api_id)
            return {"ok": True, "data": data}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/refresh_labels")
    def refresh_labels(
        snapshot_id: int = Query(..., description="Snapshot ID to refresh labels for"),
    ):
        """Re-run label_service and status_service for a snapshot, persisting results to DB.

        Looks up snapshot_dir from the snapshots table, then runs:
        1. generate_label_json(snapshot_dir, db=db)
        2. generate_status_json(snapshot_dir, db=db)
        """
        try:
            row = db.cursor().execute(
                "SELECT snapshot_dir FROM snapshots WHERE snapshot_id=?",
                [snapshot_id],
            ).fetchone()
            if row is None:
                return JSONResponse(
                    {"ok": False, "error": f"snapshot_id {snapshot_id} not found"},
                    status_code=404,
                )
            snapshot_dir = row[0]
            generate_label_json(snapshot_dir, db=db)
            generate_status_json(snapshot_dir, db=db)
            return {"ok": True, "snapshot_id": snapshot_id, "snapshot_dir": snapshot_dir}
        except FileNotFoundError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception as exc:
            _logger_module.get_logger().error(
                "refresh_labels failed", exc=exc,
                context={"snapshot_id": snapshot_id},
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── Analysis model endpoints ──────────────────────────────────────────────

    @router.get("/models", operation_id="get_models", summary="[MCP] List analysis models")
    def list_analysis_models():
        """Return metadata for all registered analysis models."""
        return {"ok": True, "data": _model_registry.list_models()}

    @router.post("/models/{name}/run")
    def run_analysis_model(
        name: str,
        snapshot_id: int = Query(..., description="Snapshot ID to run model against"),
        body: dict = Body(default={}),
    ):
        """Run a named analysis model for a snapshot. Optional JSON body passes model params."""
        try:
            result = _model_registry.run_model(name, db, snapshot_id, **body)
            return {"ok": True, "data": result}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception as exc:
            _logger_module.get_logger().error(
                "run_analysis_model failed", exc=exc,
                context={"model": name, "snapshot_id": snapshot_id},
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── Question CRUD endpoints ───────────────────────────────────────────────

    @router.get("/questions", operation_id="get_questions", summary="[MCP] List questions")
    def list_questions():
        """Return all questions ordered by creation time."""
        try:
            return {"ok": True, "data": _q.list_questions(db)}
        except Exception as exc:
            _logger_module.get_logger().error("list_questions failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/questions")
    def create_question(body: QuestionCreate):
        """Create a new question. model_name must be a registered model."""
        try:
            valid_names = {m["name"] for m in _model_registry.list_models()}
            if body.model_name not in valid_names:
                return JSONResponse(
                    {"ok": False, "error": f"Unknown model: {body.model_name}"},
                    status_code=400,
                )
            q = _q.create_question(
                db,
                title=body.title,
                model_name=body.model_name,
                model_params=body.model_params or {},
                viz_type=body.viz_type,
                viz_config=body.viz_config or {},
            )
            return {"ok": True, "data": q}
        except Exception as exc:
            _logger_module.get_logger().error("create_question failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/questions/{question_id}", operation_id="get_question", summary="[MCP] Get a question by ID")
    def get_question(question_id: str):
        """Return a single question by ID."""
        try:
            q = _q.get_question(db, question_id)
            if q is None:
                return JSONResponse(
                    {"ok": False, "error": f"Question '{question_id}' not found"},
                    status_code=404,
                )
            return {"ok": True, "data": q}
        except Exception as exc:
            _logger_module.get_logger().error(
                "get_question failed", exc=exc, context={"question_id": question_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.put("/questions/{question_id}")
    def update_question(question_id: str, body: QuestionUpdate):
        """Update mutable fields of a question (all optional)."""
        try:
            updated = _q.update_question(
                db,
                question_id,
                title=body.title,
                model_params=body.model_params,
                viz_type=body.viz_type,
                viz_config=body.viz_config,
            )
            if updated is None:
                return JSONResponse(
                    {"ok": False, "error": f"Question '{question_id}' not found"},
                    status_code=404,
                )
            return {"ok": True, "data": updated}
        except Exception as exc:
            _logger_module.get_logger().error(
                "update_question failed", exc=exc, context={"question_id": question_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.delete("/questions/{question_id}")
    def delete_question(question_id: str):
        """Delete a question by ID."""
        try:
            deleted = _q.delete_question(db, question_id)
            if not deleted:
                return JSONResponse(
                    {"ok": False, "error": f"Question '{question_id}' not found"},
                    status_code=404,
                )
            return {"ok": True}
        except Exception as exc:
            _logger_module.get_logger().error(
                "delete_question failed", exc=exc, context={"question_id": question_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/questions/{question_id}/run")
    def run_question(
        question_id: str,
        snapshot_id: int = Query(..., description="Snapshot ID to run against"),
    ):
        """Run a question for a specific snapshot.

        Loads the question, dispatches to its model, overlays the question's
        viz settings, and returns the enriched result with question metadata.
        """
        try:
            q = _q.get_question(db, question_id)
            if q is None:
                return JSONResponse(
                    {"ok": False, "error": f"Question '{question_id}' not found"},
                    status_code=404,
                )
            result = _model_registry.run_model(
                q["model_name"], db, snapshot_id, **q["model_params"]
            )
            # Question's viz settings take precedence over model defaults
            result["viz_type"]   = q["viz_type"]
            result["viz_config"] = q["viz_config"]
            result["question"]   = {"id": q["id"], "title": q["title"]}
            return {"ok": True, "data": result}
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception as exc:
            _logger_module.get_logger().error(
                "run_question failed", exc=exc,
                context={"question_id": question_id, "snapshot_id": snapshot_id},
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── Dashboard CRUD endpoints ──────────────────────────────────────────────

    @router.get("/dashboards", operation_id="get_dashboards", summary="[MCP] List dashboards")
    def list_dashboards():
        """Return all dashboards ordered by creation time."""
        try:
            return {"ok": True, "data": _dash.list_dashboards(db)}
        except Exception as exc:
            _logger_module.get_logger().error("list_dashboards failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/dashboards")
    def create_dashboard(body: DashboardCreate):
        """Create a new dashboard with an ordered list of question IDs."""
        try:
            d = _dash.create_dashboard(
                db,
                title=body.title,
                question_ids=body.question_ids,
            )
            return {"ok": True, "data": d}
        except Exception as exc:
            _logger_module.get_logger().error("create_dashboard failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/dashboards/{dashboard_id}", operation_id="get_dashboard", summary="[MCP] Get a dashboard by ID")
    def get_dashboard(dashboard_id: str):
        """Return a single dashboard by ID."""
        try:
            d = _dash.get_dashboard(db, dashboard_id)
            if d is None:
                return JSONResponse(
                    {"ok": False, "error": f"Dashboard '{dashboard_id}' not found"},
                    status_code=404,
                )
            return {"ok": True, "data": d}
        except Exception as exc:
            _logger_module.get_logger().error(
                "get_dashboard failed", exc=exc, context={"dashboard_id": dashboard_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.put("/dashboards/{dashboard_id}")
    def update_dashboard(dashboard_id: str, body: DashboardUpdate):
        """Update title and/or question_ids of a dashboard."""
        try:
            updated = _dash.update_dashboard(
                db,
                dashboard_id,
                title=body.title,
                question_ids=body.question_ids,
            )
            if updated is None:
                return JSONResponse(
                    {"ok": False, "error": f"Dashboard '{dashboard_id}' not found"},
                    status_code=404,
                )
            return {"ok": True, "data": updated}
        except Exception as exc:
            _logger_module.get_logger().error(
                "update_dashboard failed", exc=exc, context={"dashboard_id": dashboard_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.delete("/dashboards/{dashboard_id}")
    def delete_dashboard(dashboard_id: str):
        """Delete a dashboard by ID."""
        try:
            deleted = _dash.delete_dashboard(db, dashboard_id)
            if not deleted:
                return JSONResponse(
                    {"ok": False, "error": f"Dashboard '{dashboard_id}' not found"},
                    status_code=404,
                )
            return {"ok": True}
        except Exception as exc:
            _logger_module.get_logger().error(
                "delete_dashboard failed", exc=exc, context={"dashboard_id": dashboard_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── Label metrics aggregation endpoint ───────────────────────────────────

    @router.get("/available_metrics", summary="[MCP] List available metric columns for a snapshot",
                operation_id="get_available_metrics",
                description="Return the list of GPU metric column names that have at least one non-NULL "
                            "value for the given snapshot. Use this to discover which counters were "
                            "captured (depends on MetricsWhitelist at capture time). "
                            "Pass these names to /label_agg or /clock_correlation.")
    def available_metrics(snapshot_id: int = Query(...)):
        """Return list of metric column names that have at least two distinct non-NULL values for this snapshot."""
        from data.query import _snap_where
        snap_clause, snap_params = _snap_where(snapshot_id)
        where = f"WHERE {snap_clause}" if snap_clause else ""
        try:
            col_result = db.cursor().execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'metrics' ORDER BY ordinal_position"
            )
            all_cols = [row[0] for row in col_result.fetchall()
                        if row[0] not in ("snapshot_id", "api_id")]
            available = []
            for col in all_cols:
                sql = (f"SELECT COUNT(DISTINCT {col}) FROM metrics {where} AND {col} IS NOT NULL"
                       if snap_clause else
                       f"SELECT COUNT(DISTINCT {col}) FROM metrics WHERE {col} IS NOT NULL")
                result = db.cursor().execute(sql, snap_params).fetchone()
                if result and result[0] >= 2:
                    available.append(col)
            return {"ok": True, "data": available}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/label_correlations", summary="[MCP] Per-category correlation between clocks and a metric",
                operation_id="get_label_correlations",
                description="Return Pearson r between GPU clocks and a chosen metric, grouped by "
                            "draw call label category. Identifies which categories have the strongest "
                            "clock-vs-metric relationship. Returns [{category, r, n}]; r=null if <3 pairs. "
                            "Metric must be one of the standard GPU counter names (e.g. fragments_shaded, "
                            "tex_fetch_stall_pct, shaders_busy_pct).")
    def label_correlations(
        snapshot_id: int = Query(...),
        metric: str      = Query("fragments_shaded"),
    ):
        """Return per-category Pearson r between clocks and a given metric.

        Returns [{category, r, n}]. r=null if fewer than 3 paired values.
        """
        from data.query import _snap_where
        import math

        ALLOWED = {
            "clocks","preemptions","read_total_bytes","write_total_bytes",
            "tex_mem_read_bytes","vertex_mem_read_bytes","sp_mem_read_bytes",
            "fragments_shaded","vertices_shaded","reused_vertices","lrz_pixels_killed",
            "avg_bytes_per_fragment","avg_bytes_per_vertex",
            "prims_clipped_pct","prims_trivially_rejected_pct",
            "tex_fetch_stall_pct","tex_l1_miss_pct","tex_l2_miss_pct",
            "tex_pipes_busy_pct","linear_filtered_pct","nearest_filtered_pct",
            "shaders_busy_pct","shaders_stalled_pct",
            "time_alus_working_pct","time_efus_working_pct",
            "time_shading_vertices_pct","time_shading_fragments_pct",
            "time_compute_pct","shader_alu_capacity_pct",
            "wave_context_occupancy_pct",
            "fragment_instructions","vertex_instructions",
            "alu_per_fragment","alu_per_vertex",
            "efu_per_fragment","efu_per_vertex",
        }
        if metric not in ALLOWED:
            return JSONResponse({"ok": False, "error": f"Unknown metric: {metric}"}, status_code=400)

        snap_clause, snap_params = _snap_where(snapshot_id, "dc")
        where = f"WHERE {snap_clause}" if snap_clause else ""
        sql = f"""
            SELECT COALESCE(lb.category, 'Unlabeled') AS category,
                   m.clocks, m.{metric}
            FROM draw_calls dc
            LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
            LEFT JOIN metrics m  ON m.snapshot_id  = dc.snapshot_id AND m.api_id  = dc.api_id
            {where}
        """
        try:
            raw = db.cursor().execute(sql, snap_params).fetchall()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        from collections import defaultdict
        groups: dict[str, list] = defaultdict(list)
        for cat, clk, val in raw:
            if clk is not None and val is not None:
                groups[cat].append((float(clk), float(val)))

        results = []
        for cat, pairs in groups.items():
            n = len(pairs)
            if n < 3:
                results.append({"category": cat, "r": None, "n": n})
                continue
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            xm = sum(xs) / n
            ym = sum(ys) / n
            num   = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
            denom = math.sqrt(sum((x - xm)**2 for x in xs) *
                              sum((y - ym)**2 for y in ys))
            r = round(num / denom, 4) if denom != 0 else None
            results.append({"category": cat, "r": r, "n": n})

        results.sort(key=lambda x: x["r"] or 0, reverse=True)
        return {"ok": True, "data": results}

    @router.get("/clock_correlation", summary="[MCP] Correlation (R²) between clocks and all metrics",
                operation_id="get_clock_correlation",
                description="Return R² (coefficient of determination) between GPU clocks and every "
                            "other available metric. R² ranges 0-1: how much of clock variance is "
                            "explained by each metric. Without category: requires ≥10 paired values, "
                            "uses all DCs. With category filter: requires ≥3 pairs. "
                            "Returns [{metric, r2, r, n}] sorted by r2 desc.")
    def clock_correlation(
        snapshot_id: int = Query(...),
        category: str    = Query(default=None, description="Filter to a single label category"),
    ):
        """Return R² between clocks and every other metric.

        Without category: uses all DCs, requires ≥10 paired values per metric.
        With category: filters to that label only, requires ≥3 paired values.
        Returns [{metric, r2, r, n}] sorted by r2 desc.
        """
        from data.query import _snap_where
        import math

        snap_clause, snap_params = _snap_where(snapshot_id, "dc")

        if category is not None:
            cat_filter  = " AND COALESCE(lb.category, 'Unlabeled') = ?"
            cat_params  = [category]
            min_n       = 3
        else:
            cat_filter  = ""
            cat_params  = []
            min_n       = 10

        where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"

        try:
            col_result = db.cursor().execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'metrics' ORDER BY ordinal_position"
            )
            all_cols = [row[0] for row in col_result.fetchall()
                        if row[0] not in ("snapshot_id", "api_id", "clocks")]

            cols_sql = ", ".join(f"m.{c}" for c in all_cols)
            sql = f"""
                SELECT m.clocks, {cols_sql}
                FROM draw_calls dc
                LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
                LEFT JOIN metrics m  ON m.snapshot_id  = dc.snapshot_id AND m.api_id  = dc.api_id
                {where}{cat_filter}
            """
            result = db.cursor().execute(sql, snap_params + cat_params)
            raw = result.fetchall()

            if not raw:
                return {"ok": True, "data": []}

            clocks_all = [row[0] for row in raw]

            results = []
            for ci, col in enumerate(all_cols):
                vals = [(clocks_all[i], row[ci + 1])
                        for i, row in enumerate(raw)
                        if clocks_all[i] is not None and row[ci + 1] is not None]
                n = len(vals)
                if n < min_n:
                    continue
                xs = [v[0] for v in vals]
                ys = [v[1] for v in vals]
                xm = sum(xs) / n
                ym = sum(ys) / n
                num   = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
                denom = math.sqrt(sum((x - xm)**2 for x in xs) *
                                  sum((y - ym)**2 for y in ys))
                if denom == 0:
                    continue
                r = num / denom
                r2 = r * r
                results.append({"metric": col, "r2": round(r2, 4), "r": round(r, 4), "n": n})

            results.sort(key=lambda x: x["r2"], reverse=True)
            return {"ok": True, "data": results}
        except Exception as exc:
            _logger_module.get_logger().error("clock_correlation failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get(
        "/label_agg_multi",
        summary="[MCP] Multi-metric aggregation by label category",
        description=(
            "Return per-category aggregation for all 5 statistical functions "
            "(sum, median, min, max, variance) × all available metric columns in a single query. "
            "Useful for getting a complete performance breakdown across label categories. "
            "Response: { ok, columns: [str], data: [{category, dc_count, metricName: {sum,median,min,max,variance}}] }"
        ),
        operation_id="get_label_agg_multi",
    )
    def label_agg_multi(snapshot_id: int = Query(..., description="Snapshot ID to query; 1 = all snapshots")):
        """Return per-category aggregation for all 5 aggs × all available metrics in one query.

        Response shape:
          { ok, columns: [str], data: [{category, dc_count, metricName: {sum,median,min,max,variance}}] }
        """
        from data.query import _snap_where

        snap_clause, snap_params = _snap_where(snapshot_id, "dc")

        try:
            col_result = db.cursor().execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'metrics' ORDER BY ordinal_position"
            )
            all_cols = [row[0] for row in col_result.fetchall()
                        if row[0] not in ("snapshot_id", "api_id")]
            available = []
            for col in all_cols:
                chk_sql = (f"SELECT COUNT(DISTINCT {col}) FROM metrics WHERE snapshot_id=? AND {col} IS NOT NULL"
                           if snap_clause else f"SELECT COUNT(DISTINCT {col}) FROM metrics WHERE {col} IS NOT NULL")
                chk_params = [snapshot_id] if snap_clause else []
                result = db.cursor().execute(chk_sql, chk_params).fetchone()
                if result and result[0] >= 2:
                    available.append(col)

            if not available:
                return {"ok": True, "data": [], "columns": []}

            agg_exprs = []
            for col in available:
                agg_exprs += [
                    f'ROUND(CAST(SUM(m.{col}) AS DOUBLE), 2) AS "{col}_sum"',
                    f'ROUND(CAST(MEDIAN(m.{col}) AS DOUBLE), 2) AS "{col}_median"',
                    f'ROUND(CAST(MIN(m.{col}) AS DOUBLE), 2) AS "{col}_min"',
                    f'ROUND(CAST(MAX(m.{col}) AS DOUBLE), 2) AS "{col}_max"',
                    f'ROUND(CAST(VAR_POP(m.{col}) AS DOUBLE), 2) AS "{col}_variance"',
                ]

            snap_clause2, snap_params2 = _snap_where(snapshot_id, "dc")
            where2 = f"WHERE {snap_clause2}" if snap_clause2 else ""
            sql = f"""
                SELECT
                    COALESCE(lb.category, 'Unlabeled') AS category,
                    COUNT(dc.api_id) AS dc_count,
                    {', '.join(agg_exprs)}
                FROM draw_calls dc
                LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
                LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
                {where2}
                GROUP BY COALESCE(lb.category, 'Unlabeled')
                ORDER BY SUM(m.clocks) DESC NULLS LAST
            """
            result = db.cursor().execute(sql, snap_params2)
            flat_cols = [d[0] for d in result.description]
            flat_rows = [dict(zip(flat_cols, row)) for row in result.fetchall()]

            AGGS = ["sum", "median", "min", "max", "variance"]
            nested_rows = []
            for row in flat_rows:
                nested: dict = {"category": row["category"], "dc_count": row["dc_count"]}
                for col in available:
                    nested[col] = {a: row.get(f"{col}_{a}") for a in AGGS}
                nested_rows.append(nested)

            return {"ok": True, "data": nested_rows, "columns": available}
        except Exception as exc:
            _logger_module.get_logger().error("label_agg_multi failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/label_agg_all", operation_id="get_label_agg_all", summary="[MCP] Aggregate all metrics by label category")
    def label_agg_all(
        snapshot_id: int = Query(...),
        agg: str = Query("sum"),
    ):
        """Return per-category aggregation of ALL available metric columns.

        Returns [{category, dc_count, metric1: value, ...}] using the given agg.
        Only columns that have at least one non-NULL value are included.
        """
        from data.query import _snap_where
        snap_clause, snap_params = _snap_where(snapshot_id, "dc")
        where = f"WHERE {snap_clause}" if snap_clause else ""

        AGG_MAP = {
            "sum": "SUM", "avg": "AVG", "median": "MEDIAN",
            "max": "MAX", "min": "MIN", "variance": "VAR_POP",
        }
        if agg not in AGG_MAP:
            return JSONResponse({"ok": False, "error": f"Unknown agg: {agg}"}, status_code=400)
        agg_fn = AGG_MAP[agg]

        try:
            # Discover available metric columns
            col_result = db.cursor().execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'metrics' ORDER BY ordinal_position"
            )
            all_cols = [row[0] for row in col_result.fetchall()
                        if row[0] not in ("snapshot_id", "api_id")]
            available = []
            for col in all_cols:
                chk_sql = f"SELECT 1 FROM metrics {where} AND m.{col} IS NOT NULL LIMIT 1" \
                          if snap_clause else f"SELECT 1 FROM metrics m WHERE m.{col} IS NOT NULL LIMIT 1"
                # simpler: query without alias for check
                chk_sql2 = (f"SELECT 1 FROM metrics WHERE snapshot_id=? AND {col} IS NOT NULL LIMIT 1"
                            if snap_clause else f"SELECT 1 FROM metrics WHERE {col} IS NOT NULL LIMIT 1")
                chk_params = [snapshot_id] if snap_clause else []
                if db.cursor().execute(chk_sql2, chk_params).fetchone():
                    available.append(col)

            if not available:
                return {"ok": True, "data": [], "columns": []}

            agg_exprs = ", ".join(
                f'ROUND(CAST({agg_fn}(m.{c}) AS DOUBLE), 2) AS "{c}"'
                for c in available
            )
            snap_clause2, snap_params2 = _snap_where(snapshot_id, "dc")
            where2 = f"WHERE {snap_clause2}" if snap_clause2 else ""
            sql = f"""
                SELECT
                    COALESCE(lb.category, 'Unlabeled') AS category,
                    COUNT(dc.api_id) AS dc_count,
                    {agg_exprs}
                FROM draw_calls dc
                LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
                LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
                {where2}
                GROUP BY COALESCE(lb.category, 'Unlabeled')
                ORDER BY {agg_fn}(m.clocks) DESC NULLS LAST
            """
            result = db.cursor().execute(sql, snap_params2)
            cols = [d[0] for d in result.description]
            rows = [dict(zip(cols, row)) for row in result.fetchall()]
            return {"ok": True, "data": rows, "columns": available}
        except Exception as exc:
            _logger_module.get_logger().error("label_agg_all failed", exc=exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get(
        "/label_agg",
        summary="[MCP] Aggregate a single metric by label category",
        description=(
            "Return per-category aggregation of one GPU metric column. "
            "agg options: sum | avg | median | max | min | variance. "
            "Returns [{category, value, dc_count}] ordered by value DESC. "
            "Use metric=clocks (default) for GPU time breakdown, or any other metric column name."
        ),
        operation_id="get_label_agg",
    )
    def label_agg(
        snapshot_id: int = Query(..., description="Snapshot ID to query; 1 = all snapshots"),
        metric: str     = Query("clocks", description="Metric column to aggregate (e.g. clocks, fragments_shaded, read_total_bytes)"),
        agg:    str     = Query("sum", description="Aggregation function: sum | avg | median | max | min | variance"),
    ):
        """Return per-category aggregation of a single metric column.

        Returns [{category, value, dc_count}] ordered by value DESC.
        agg=median/variance are computed Python-side (DuckDB MEDIAN/VARIANCE not always available).
        """
        from data.query import _snap_where
        import statistics

        ALLOWED_METRICS = {
            "clocks","preemptions","read_total_bytes","write_total_bytes",
            "tex_mem_read_bytes","vertex_mem_read_bytes","sp_mem_read_bytes",
            "fragments_shaded","vertices_shaded","reused_vertices","lrz_pixels_killed",
            "avg_bytes_per_fragment","avg_bytes_per_vertex",
            "prims_clipped_pct","prims_trivially_rejected_pct",
            "tex_fetch_stall_pct","tex_l1_miss_pct","tex_l2_miss_pct",
            "tex_pipes_busy_pct","linear_filtered_pct","nearest_filtered_pct",
            "shaders_busy_pct","shaders_stalled_pct",
            "time_alus_working_pct","time_efus_working_pct",
            "time_shading_vertices_pct","time_shading_fragments_pct",
            "time_compute_pct","shader_alu_capacity_pct",
            "wave_context_occupancy_pct",
            "fragment_instructions","vertex_instructions",
            "alu_per_fragment","alu_per_vertex",
            "efu_per_fragment","efu_per_vertex",
            "vertices_shaded","fragments_shaded",
        }
        ALLOWED_AGGS = {"sum","avg","median","max","min","variance"}
        if metric not in ALLOWED_METRICS:
            return JSONResponse({"ok": False, "error": f"Unknown metric: {metric}"}, status_code=400)
        if agg not in ALLOWED_AGGS:
            return JSONResponse({"ok": False, "error": f"Unknown agg: {agg}"}, status_code=400)

        snap_clause, snap_params = _snap_where(snapshot_id, "dc")
        where = f"WHERE {snap_clause}" if snap_clause else ""

        # Fetch raw per-DC values grouped by category
        sql = f"""
            SELECT
                COALESCE(lb.category, 'Unlabeled') AS category,
                m.{metric} AS val
            FROM draw_calls dc
            LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
            LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
            {where}
        """
        try:
            result = db.cursor().execute(sql, snap_params)
            raw = result.fetchall()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        # Group by category
        from collections import defaultdict
        groups: dict[str, list[float]] = defaultdict(list)
        for cat, val in raw:
            if val is not None:
                groups[cat].append(float(val))

        rows = []
        for cat, vals in groups.items():
            if not vals:
                continue
            if agg == "sum":
                v = sum(vals)
            elif agg == "avg":
                v = statistics.mean(vals)
            elif agg == "median":
                v = statistics.median(vals)
            elif agg == "max":
                v = max(vals)
            elif agg == "min":
                v = min(vals)
            elif agg == "variance":
                v = statistics.variance(vals) if len(vals) > 1 else 0.0
            rows.append({"category": cat, "value": round(v, 2), "dc_count": len(vals)})

        rows.sort(key=lambda r: r["value"] or 0, reverse=True)
        return {"ok": True, "data": rows}

    @router.get(
        "/label_metrics",
        summary="[MCP] Per-label category metric summary",
        description=(
            "Return key GPU metric aggregates (sum + avg) grouped by label category. "
            "Covers: clocks, shaders_busy_pct, tex_fetch_stall_pct, tex_l1/l2_miss_pct, "
            "fragments_shaded, vertices_shaded, read/write_total_bytes. "
            "Results ordered by clocks_sum DESC. Use snapshot_id=1 for cross-snapshot view."
        ),
        operation_id="get_label_metrics",
    )
    def label_metrics(
        snapshot_id: int = Query(..., description="Snapshot ID to aggregate metrics for; 1 = all snapshots"),
    ):
        """Return per-label aggregated metrics (sum + avg) for a snapshot.

        Joins draw_calls + labels + metrics, groups by labels.category.
        Returns one row per category with count and key metric aggregates,
        ordered by clocks_sum DESC.
        Use snapshot_id=1 to aggregate across all snapshots.
        """
        from data.query import _snap_where
        snap_clause, snap_params = _snap_where(snapshot_id, "dc")
        where = f"WHERE {snap_clause}" if snap_clause else ""
        sql = f"""
            SELECT
                COALESCE(lb.category, 'Unlabeled') AS category,
                COUNT(dc.api_id)                    AS dc_count,
                SUM(m.clocks)                       AS clocks_sum,
                AVG(m.clocks)                       AS clocks_avg,
                AVG(m.shaders_busy_pct)             AS shaders_busy_pct_avg,
                AVG(m.tex_fetch_stall_pct)          AS tex_fetch_stall_pct_avg,
                AVG(m.tex_l1_miss_pct)              AS tex_l1_miss_pct_avg,
                AVG(m.tex_l2_miss_pct)              AS tex_l2_miss_pct_avg,
                SUM(m.fragments_shaded)             AS fragments_shaded_sum,
                AVG(m.fragments_shaded)             AS fragments_shaded_avg,
                SUM(m.vertices_shaded)              AS vertices_shaded_sum,
                AVG(m.vertices_shaded)              AS vertices_shaded_avg,
                SUM(m.read_total_bytes)             AS read_bytes_sum,
                SUM(m.write_total_bytes)            AS write_bytes_sum
            FROM draw_calls dc
            LEFT JOIN labels lb
                ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
            LEFT JOIN metrics m
                ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
            {where}
            GROUP BY COALESCE(lb.category, 'Unlabeled')
            ORDER BY SUM(m.clocks) DESC NULLS LAST
        """
        try:
            result = db.cursor().execute(sql, snap_params)
            cols = [d[0] for d in result.description]
            rows = [dict(zip(cols, row)) for row in result.fetchall()]
            # Round float values to 2 decimal places; leave None as-is
            for row in rows:
                for k, v in row.items():
                    if isinstance(v, float):
                        row[k] = round(v, 2)
            return {"ok": True, "data": rows}
        except Exception as exc:
            _logger_module.get_logger().error(
                "label_metrics failed", exc=exc, context={"snapshot_id": snapshot_id}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/dashboards/{dashboard_id}/run")
    def run_dashboard(
        dashboard_id: str,
        snapshot_id: int = Query(..., description="Snapshot ID to run against"),
    ):
        """Run all questions in a dashboard for a specific snapshot.

        Executes each panel independently — one failing model does not abort the rest.
        Each panel in the response has either {"ok": True, "data": ...} or
        {"ok": False, "error": ...}.
        """
        try:
            d = _dash.get_dashboard(db, dashboard_id)
            if d is None:
                return JSONResponse(
                    {"ok": False, "error": f"Dashboard '{dashboard_id}' not found"},
                    status_code=404,
                )

            panels = []
            for qid in d["question_ids"]:
                q = _q.get_question(db, qid)
                if q is None:
                    panels.append({"question_id": qid, "ok": False,
                                   "error": "Question not found"})
                    continue
                try:
                    result = _model_registry.run_model(
                        q["model_name"], db, snapshot_id, **q["model_params"]
                    )
                    result["viz_type"]   = q["viz_type"]
                    result["viz_config"] = q["viz_config"]
                    result["question"]   = {"id": q["id"], "title": q["title"]}
                    panels.append({"question_id": qid, "ok": True, "data": result})
                except Exception as exc:
                    panels.append({"question_id": qid, "ok": False, "error": str(exc)})

            return {
                "ok": True,
                "data": {
                    "dashboard_id": dashboard_id,
                    "title":        d["title"],
                    "snapshot_id":  snapshot_id,
                    "panels":       panels,
                },
            }
        except JSONResponse:
            raise
        except Exception as exc:
            _logger_module.get_logger().error(
                "run_dashboard failed", exc=exc,
                context={"dashboard_id": dashboard_id, "snapshot_id": snapshot_id},
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── Projects CRUD ────────────────────────────────────────────────────────

    @router.get("/projects")
    def list_projects():
        cols = ["id", "name", "description", "color", "created_at", "updated_at"]
        rows = db.conn().execute(
            "SELECT id, name, description, color, created_at, updated_at FROM projects ORDER BY name"
        ).fetchall()
        return {"ok": True, "data": [dict(zip(cols, r)) for r in rows]}

    @router.get("/projects/{project_id}")
    def get_project(project_id: str):
        cols = ["id", "name", "description", "color", "created_at", "updated_at"]
        row = db.conn().execute(
            "SELECT id, name, description, color, created_at, updated_at FROM projects WHERE id = ?",
            (project_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Not found"}, 404)
        return {"ok": True, "data": dict(zip(cols, row))}

    @router.post("/projects")
    def create_project(data: dict = Body(...)):
        import uuid
        from datetime import datetime, timezone
        name = (data.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name required"}, 400)
        pid = str(uuid.uuid4())
        desc = (data.get("description") or "").strip()
        color = (data.get("color") or "#6366f1").strip()
        now = datetime.now(timezone.utc).isoformat()
        try:
            db.conn().execute(
                "INSERT INTO projects (id, name, description, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, name, desc, color, now, now)
            )
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, 500)
        return {"ok": True, "data": {"id": pid, "name": name, "description": desc, "color": color, "created_at": now, "updated_at": now}}

    @router.put("/projects/{project_id}")
    def update_project(project_id: str, data: dict = Body(...)):
        from datetime import datetime, timezone
        sets, params = [], []
        if "name" in data:
            sets.append("name = ?"); params.append(data["name"].strip())
        if "description" in data:
            sets.append("description = ?"); params.append(data["description"].strip())
        if "color" in data:
            sets.append("color = ?"); params.append(data["color"].strip())
        if not sets:
            return {"ok": True}
        sets.append("updated_at = ?"); params.append(datetime.now(timezone.utc).isoformat())
        params.append(project_id)
        db.conn().execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params)
        return {"ok": True}

    @router.delete("/projects/{project_id}")
    def delete_project(project_id: str):
        db.conn().execute("DELETE FROM versions WHERE project_id = ?", (project_id,))
        db.conn().execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return {"ok": True}

    # ── Versions CRUD ────────────────────────────────────────────────────────

    @router.get("/projects/{project_id}/versions")
    def list_versions(project_id: str):
        cols = ["id", "project_id", "name", "description", "ordinal", "created_at"]
        rows = db.conn().execute(
            "SELECT id, project_id, name, description, ordinal, created_at FROM versions WHERE project_id = ? ORDER BY ordinal, name",
            (project_id,)
        ).fetchall()
        return {"ok": True, "data": [dict(zip(cols, r)) for r in rows]}

    @router.post("/projects/{project_id}/versions")
    def create_version(project_id: str, data: dict = Body(...)):
        import uuid
        from datetime import datetime, timezone
        name = (data.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name required"}, 400)
        vid = str(uuid.uuid4())
        desc = (data.get("description") or "").strip()
        ordinal = data.get("ordinal", 0)
        now = datetime.now(timezone.utc).isoformat()
        try:
            db.conn().execute(
                "INSERT INTO versions (id, project_id, name, description, ordinal, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (vid, project_id, name, desc, ordinal, now)
            )
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, 500)
        return {"ok": True, "data": {"id": vid, "project_id": project_id, "name": name, "description": desc, "ordinal": ordinal, "created_at": now}}

    @router.get("/versions/{version_id}")
    def get_version(version_id: str):
        cols = ["id", "project_id", "name", "description", "ordinal", "created_at"]
        row = db.conn().execute(
            "SELECT id, project_id, name, description, ordinal, created_at FROM versions WHERE id = ?",
            (version_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Not found"}, 404)
        return {"ok": True, "data": dict(zip(cols, row))}

    @router.put("/versions/{version_id}")
    def update_version(version_id: str, data: dict = Body(...)):
        sets, params = [], []
        if "name" in data:
            sets.append("name = ?"); params.append(data["name"].strip())
        if "description" in data:
            sets.append("description = ?"); params.append(data["description"].strip())
        if "ordinal" in data:
            sets.append("ordinal = ?"); params.append(data["ordinal"])
        if not sets:
            return {"ok": True}
        params.append(version_id)
        db.conn().execute(f"UPDATE versions SET {', '.join(sets)} WHERE id = ?", params)
        return {"ok": True}

    @router.delete("/versions/{version_id}")
    def delete_version(version_id: str):
        db.conn().execute("DELETE FROM versions WHERE id = ?", (version_id,))
        return {"ok": True}

    return router
