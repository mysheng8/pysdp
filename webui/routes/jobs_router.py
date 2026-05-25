"""
jobs_router.py — /api/jobs/* endpoints.

All job-trigger POST endpoints live here:
  C# extraction:
    POST /reply_extract        → trigger SDPCLI server, returns C# jobId

  Python single-step (synchronous, fast):
    POST /ingest               → ingest snapshot into DuckDB
    POST /screenshot           → copy/extract screenshot
    POST /mesh_stats           → parse mesh OBJs, write meshes.json, re-ingest
    POST /texture_stats        → read texture dims, write textures.json, re-ingest
    POST /gles_decompile       → IR3 disasm → GLSL via LLM (cached)
    POST /label                → generate label.json (rule-based)
    POST /scene_describe       → VLM scene description
    POST /report               → status → topdc → analysis_md in sequence

  Python pipeline (async background):
    POST /pipeline             → run any ordered subset of steps, returns job_id
    GET  /pipeline/{job_id}    → poll status
    POST /pipeline/{job_id}/cancel
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import logger as _logger_module
from events import publish as _publish
from jobs import pipeline_manager, VALID_STEPS


class CsExtractRequest(BaseModel):
    sdpPath: str
    snapshotId: int
    outputDir: Optional[str] = None
    targets: str = "dc,shaders,textures,buffers,metrics"


def make_router(db=None) -> APIRouter:
    router = APIRouter()

    # ── C# extraction ──────────────────────────────────────────────────────────

    @router.post(
        "/reply_extract",
        summary="Trigger C# extraction for an SDP file",
        description=(
            "Submit an extraction job to the SDPCLI server for one SDP file. "
            "Returns a jobId (poll via GET /api/sdpcli/jobs/{job_id}). "
            "targets: comma-separated subset of {dc, shaders, textures, buffers, metrics}. "
            "After it completes, run POST /api/jobs/pipeline to run Python analysis steps."
        ),
    )
    def reply_extract(body: CsExtractRequest):
        """Trigger C# extraction for an SDP file. Returns C# jobId."""
        from routes.proxy import _fwd
        payload: dict = {
            "sdpPath":    body.sdpPath,
            "snapshotId": body.snapshotId,
            "targets":    body.targets,
        }
        if body.outputDir:
            payload["outputDir"] = body.outputDir
        return _fwd("POST", "/api/analysis", payload, timeout=60)

    # ── Python single-step ─────────────────────────────────────────────────────

    @router.post("/ingest", summary="Ingest a snapshot directory into DuckDB")
    def ingest(snapshot_dir: str = Query(..., description="Absolute path to snapshot directory")):
        """Ingest a snapshot directory into DuckDB. Idempotent — safe to call multiple times."""
        from data.ingest import ingest_snapshot
        try:
            result = ingest_snapshot(db, snapshot_dir)
            _publish("ingest_done", {"snapshot_id": result["snapshot_id"], "snapshot_dir": snapshot_dir})
            return {"ok": True, "snapshot_id": result["snapshot_id"], "counts": result["counts"]}
        except FileNotFoundError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception as exc:
            _logger_module.get_logger().error("ingest failed", exc=exc, context={"snapshot_dir": snapshot_dir})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/screenshot", summary="Copy/extract screenshot into snapshot directory")
    def run_screenshot(snapshot_dir: str = Query(..., description="Snapshot directory; extracts screenshot from .sdp archive or sibling sdp dir")):
        """Copy/extract screenshot into the snapshot directory."""
        from jobs import _copy_screenshot
        try:
            result = _copy_screenshot(snapshot_dir)
            if "skipped" in result:
                return {"ok": True, "data": result}
            return {"ok": True, "data": {"path": result["path"]}}
        except Exception as exc:
            _logger_module.get_logger().error("screenshot failed", exc=exc, context={"dir": snapshot_dir})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/mesh_stats", summary="Parse mesh OBJs and write meshes.json, then re-ingest")
    def run_mesh_stats(snapshot_dir: str = Query(..., description="Snapshot directory (meshes/ must exist one level up)")):
        """Parse mesh_*.obj files, write meshes.json, then re-ingest to update DuckDB meshes table."""
        from analysis.mesh_stats_service import generate_buffers_json
        from data.ingest import ingest_snapshot
        log = _logger_module.get_logger()
        try:
            out = generate_buffers_json(snapshot_dir)
        except FileNotFoundError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception as exc:
            log.error("mesh_stats failed", exc=exc, context={"dir": snapshot_dir})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        ingest_result = None
        if db is not None:
            try:
                ingest_result = ingest_snapshot(db, snapshot_dir)
            except Exception as exc:
                log.error("mesh_stats/ingest failed", exc=exc, context={"dir": snapshot_dir})

        return {"ok": True, "data": {"path": str(out), "ingest": ingest_result}}

    @router.post("/texture_stats", summary="Read texture dimensions, write textures.json, then re-ingest")
    def run_texture_stats(snapshot_dir: str = Query(..., description="Snapshot directory (textures/ must exist one level up)")):
        """Read texture dimensions (+ optional VLM descriptions), write textures.json, then re-ingest."""
        from analysis.texture_stats_service import generate_texture_stats
        from data.ingest import ingest_snapshot
        log = _logger_module.get_logger()
        try:
            run_dir = str(Path(snapshot_dir).parent)
            out = generate_texture_stats(run_dir)
        except FileNotFoundError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception as exc:
            log.error("texture_stats failed", exc=exc, context={"dir": snapshot_dir})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        ingest_result = None
        if db is not None:
            try:
                ingest_result = ingest_snapshot(db, snapshot_dir)
            except Exception as exc:
                log.error("texture_stats/ingest failed", exc=exc, context={"dir": snapshot_dir})

        return {"ok": True, "data": {"path": str(out), "ingest": ingest_result}}

    @router.post("/gles_decompile", summary="Decompile GLES IR3 disasm to GLSL via LLM")
    def run_gles_decompile(snapshot_dir: str = Query(..., description="Snapshot directory containing dc.json")):
        """Send IR3 disasm from sdp.db to LLM, write pipeline_N_stage.glsl files (cached)."""
        from analysis.gles_decompile_service import decompile_shaders
        log = _logger_module.get_logger()
        try:
            result = decompile_shaders(snapshot_dir)
        except Exception as exc:
            log.error("gles_decompile failed", exc=exc, context={"dir": snapshot_dir})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return {"ok": True, "data": result}

    @router.post("/decompile_single", summary="Decompile a single .disasm file to GLSL via LLM")
    def run_decompile_single(path: str = Query(..., description="Absolute path to a .disasm file")):
        """Decompile one shader disasm file to GLSL, bypassing cache. Returns the GLSL text."""
        from analysis.gles_decompile_service import decompile_single_shader
        result = decompile_single_shader(path)
        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": result.get("error", "unknown")}, status_code=400)
        _publish("shader_updated", {"path": result.get("glsl_path", path)})
        return result

    @router.post("/relabel_single", summary="Re-label a single draw call via LLM")
    def run_relabel_single(
        snapshot_dir: str = Query(..., description="Snapshot directory"),
        api_id: int = Query(..., description="Draw call api_id to relabel"),
    ):
        """Re-run LLM labeling for one draw call. Updates label.json and DuckDB."""
        from analysis.label_service import relabel_single_dc
        result = relabel_single_dc(snapshot_dir, api_id, db=db)
        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": result.get("error", "unknown")}, status_code=400)
        _publish("label_changed", {"snapshot_dir": snapshot_dir, "api_id": api_id, "label": result.get("label")})
        return result

    @router.post("/label", summary="Generate label.json using rule-based classification")
    def run_label(snapshot_dir: str = Query(..., description="Snapshot directory containing dc.json")):
        """Generate label.json from dc.json using rule-based classification."""
        from analysis.label_service import generate_label_json
        try:
            out = generate_label_json(snapshot_dir, db=db)
            _publish("labels_changed", {"snapshot_dir": snapshot_dir})
            return {"ok": True, "data": {"path": str(out)}}
        except FileNotFoundError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception as exc:
            _logger_module.get_logger().error("label failed", exc=exc, context={"dir": snapshot_dir})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/scene_describe", summary="VLM scene description for a snapshot screenshot")
    def run_scene_describe(snapshot_dir: str = Query(..., description="Snapshot directory containing screenshot + optional label.json/metrics.json")):
        """Call VLM (gemini-2.5-flash) to describe the screenshot and write scene_description.md."""
        from analysis.vlm_screenshot_service import generate_scene_description
        try:
            out = generate_scene_description(snapshot_dir, db=db)
            return {"ok": True, "data": {"path": str(out)}}
        except FileNotFoundError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        except Exception as exc:
            _logger_module.get_logger().error("scene_describe failed", exc=exc, context={"dir": snapshot_dir})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/report", summary="Run status → topdc → analysis_md in sequence")
    def run_report(snapshot_dir: str = Query(..., description="Snapshot directory containing label.json + metrics.json")):
        """Run status → topdc → analysis_md in sequence. Returns paths for all three outputs."""
        from analysis.status_service import generate_status_json
        from analysis.topdc_service import generate_topdc_json
        from analysis.analysis_md_service import generate_analysis_md
        results: dict = {}
        errors:  dict = {}
        log = _logger_module.get_logger()

        try:
            out = generate_status_json(snapshot_dir, db=db)
            results["status"] = str(out)
        except Exception as exc:
            log.error("report/status failed", exc=exc, context={"dir": snapshot_dir})
            errors["status"] = str(exc)
            return JSONResponse({"ok": False, "step": "status", "error": str(exc), "results": results}, status_code=500)

        try:
            out = generate_topdc_json(snapshot_dir)
            results["topdc"] = str(out)
        except Exception as exc:
            log.error("report/topdc failed", exc=exc, context={"dir": snapshot_dir})
            errors["topdc"] = str(exc)

        try:
            out = generate_analysis_md(snapshot_dir)
            results["analysis"] = str(out)
        except Exception as exc:
            log.error("report/analysis failed", exc=exc, context={"dir": snapshot_dir})
            errors["analysis"] = str(exc)

        ok = not errors
        if ok:
            _publish("report_done", {"snapshot_dir": snapshot_dir})
        return {"ok": ok, "data": results, **({"errors": errors} if errors else {})}

    # ── Python pipeline (async) ────────────────────────────────────────────────

    @router.post(
        "/pipeline",
        summary="Start a Python analysis pipeline job",
        description=(
            "Run an ordered subset of Python analysis steps in a background thread. "
            "Available steps: " + ", ".join(VALID_STEPS) + ". "
            "Returns job_id for polling."
        ),
    )
    def start_pipeline(
        snapshot_dir: str = Query(..., description="Absolute path to snapshot directory"),
        targets: str = Query(
            default=",".join(VALID_STEPS),
            description="Comma-separated ordered steps to run",
        ),
    ):
        """Start a Python analysis pipeline job. Returns job_id for polling."""
        if not Path(snapshot_dir).is_dir():
            _logger_module.get_logger().error("pipeline submit failed: directory not found",
                                              context={"snapshot_dir": snapshot_dir, "targets": targets})
            return JSONResponse({"ok": False, "error": f"Directory not found: {snapshot_dir}"}, status_code=404)

        steps = [s.strip() for s in targets.split(",") if s.strip() in VALID_STEPS]
        if not steps:
            return JSONResponse({"ok": False, "error": "No valid steps specified"}, status_code=400)

        pipeline_manager.purge_expired()
        job = pipeline_manager.submit(snapshot_dir, steps, db)
        return {"ok": True, "job_id": job.id, "steps": job.steps}

    @router.get("/pipeline/{job_id}", summary="Poll a Python pipeline job")
    def get_pipeline_job(job_id: str):
        """Poll the status of a Python pipeline job."""
        job = pipeline_manager.get(job_id)
        if job is None:
            return JSONResponse({"ok": False, "error": f"Job not found: {job_id}"}, status_code=404)
        return {
            "ok": True,
            "data": {
                "job_id":   job.id,
                "status":   job.status,
                "phase":    job.phase,
                "progress": job.progress,
                "error":    job.error,
                "result":   job.result,
            },
        }

    @router.post("/pipeline/{job_id}/cancel", summary="Cancel a running Python pipeline job")
    def cancel_pipeline_job(job_id: str):
        """Request cancellation of a running Python pipeline job."""
        ok = pipeline_manager.cancel(job_id)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": f"Job not found or already terminal: {job_id}"},
                status_code=404,
            )
        return {"ok": True}

    return router
