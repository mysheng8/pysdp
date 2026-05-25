"""jobs.py — Server-side pipeline job manager for Python analysis steps.

Each job runs ingest → label → status → topdc → analysis_md
in a background thread. The browser only submits + polls; refreshing does not
interrupt the pipeline.
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logger as _logger_module
from events import publish as _publish

# ── Job model ─────────────────────────────────────────────────────────────────

VALID_STEPS = ["screenshot", "mesh_stats", "texture_stats", "gles_decompile", "ingest", "label", "status", "topdc", "analysis", "describe"]

# Maps step key → (weight in overall progress, display phase name)
_STEP_META: dict[str, tuple[int, str]] = {
    "screenshot":      (5,  "copy_screenshot"),
    "mesh_stats":      (10, "parse_mesh_stats"),
    "texture_stats":   (10, "parse_texture_stats"),
    "gles_decompile":  (15, "llm_decompile"),
    "ingest":          (20, "ingest_db"),
    "label":           (20, "label_drawcalls"),
    "status":          (15, "generate_stats"),
    "topdc":           (15, "generate_topdc"),
    "analysis":        (25, "report_analysis"),
    "describe":        (10, "vlm_describe"),
}


@dataclass
class PipelineJob:
    id: str
    snapshot_dir: str
    steps: list[str]                  # ordered subset of VALID_STEPS
    status: str = "pending"           # pending | running | completed | failed | cancelled
    phase: str = ""
    progress: int = 0
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled_requested(self) -> bool:
        return self._cancel.is_set()


# ── Job manager ───────────────────────────────────────────────────────────────

_JOB_TTL_SECONDS = 3600  # keep completed jobs for 1 hour


class PipelineJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, PipelineJob] = {}
        self._lock = threading.Lock()

    def submit(self, snapshot_dir: str, steps: list[str], db) -> PipelineJob:
        """Create a job and start it in a background thread immediately."""
        job_id = f"py-{uuid.uuid4().hex[:12]}"
        ordered = [s for s in VALID_STEPS if s in set(steps)]
        job = PipelineJob(id=job_id, snapshot_dir=snapshot_dir, steps=ordered)
        with self._lock:
            self._jobs[job_id] = job
        t = threading.Thread(target=self._run, args=(job, db), daemon=True, name=f"pipeline-{job_id}")
        t.start()
        return job

    def get(self, job_id: str) -> PipelineJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_all(self) -> list[PipelineJob]:
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job and not job.is_terminal():
            job.cancel()
            return True
        return False

    def purge_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired = [
                jid for jid, j in self._jobs.items()
                if j.is_terminal() and j.finished_at and (now - j.finished_at) > _JOB_TTL_SECONDS
            ]
            for jid in expired:
                del self._jobs[jid]
        return len(expired)

    # ── Runner ────────────────────────────────────────────────────────────────

    def _run(self, job: PipelineJob, db) -> None:
        log = _logger_module.get_logger()
        job.status = "running"
        log.info("pipeline job started", context={"job_id": job.id, "steps": job.steps, "dir": job.snapshot_dir})

        # Pre-compute cumulative progress weights for the selected steps
        total_weight = sum(_STEP_META[s][0] for s in job.steps if s in _STEP_META)
        if total_weight == 0:
            total_weight = 1
        done_weight = 0

        try:
            for step in job.steps:
                if job.cancelled_requested():
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    log.info("pipeline job cancelled", context={"job_id": job.id, "at_step": step})
                    return

                weight, phase_name = _STEP_META.get(step, (10, step))
                job.phase = phase_name
                job.progress = int(done_weight / total_weight * 100)

                log.info(f"pipeline step start: {step}", context={"job_id": job.id})
                try:
                    result = _run_step(step, job.snapshot_dir, db)
                    job.result[step] = result
                except Exception as exc:
                    log.error(f"pipeline step failed: {step}", exc=exc, context={"job_id": job.id})
                    # non-fatal steps: continue but record error
                    job.result[step] = {"error": str(exc)}

                done_weight += weight
                job.progress = int(done_weight / total_weight * 100)

            job.status = "completed"
            job.progress = 100
            job.phase = "complete"
            job.finished_at = time.time()
            log.info("pipeline job completed", context={"job_id": job.id})
            _publish("pipeline_done", {"job_id": job.id, "snapshot_dir": job.snapshot_dir, "steps": job.steps})

        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
            log.error("pipeline job fatal error", exc=exc, context={"job_id": job.id})


# ── Per-step dispatch ─────────────────────────────────────────────────────────

_SCREENSHOT_NAMES = ["snapshot.png", "snapshot_screenshot.png", "snapshot_screenshot.jpg"]


def _find_sdp_file(snapshot_dir: Path) -> Path | None:
    """从 dc.json 读取 sdp_name，在 sdp 目录下找对应的 .sdp 文件。
    路径规则：analysis/.../snapshot_N → sdp 目录 = analysis 同级的 sdp/ 或 analysis 父目录
    """
    import json
    dc_json = snapshot_dir / "dc.json"
    if not dc_json.exists():
        return None
    try:
        data = json.loads(dc_json.read_text(encoding="utf-8-sig"))
        sdp_name = data.get("sdp_name", "")
    except Exception:
        return None
    if not sdp_name:
        return None

    # snapshot_dir = .../sdp/analysis/<run>/snapshot_N
    # sdp file candidates:
    #   .../sdp/<sdp_name>.sdp          (sdp 目录与 analysis 同级)
    #   .../sdp/analysis/../<sdp_name>.sdp (analysis 父目录)
    run_dir    = snapshot_dir.parent          # .../sdp/analysis/<run>
    analysis_dir = run_dir.parent            # .../sdp/analysis
    sdp_root   = analysis_dir.parent         # .../sdp

    for candidate_dir in [sdp_root / "sdp", sdp_root, analysis_dir.parent]:
        p = candidate_dir / f"{sdp_name}.sdp"
        if p.exists():
            return p
    return None


def _copy_screenshot(snapshot_dir: str) -> dict:
    import zipfile, json as _json
    snap = Path(snapshot_dir)
    snap_name = snap.name  # e.g. snapshot_6

    # 优先：直接从 .sdp 压缩包中提取
    sdp_file = _find_sdp_file(snap)
    if sdp_file:
        try:
            with zipfile.ZipFile(sdp_file, "r") as zf:
                # 在压缩包中找 snapshot_N/*.bmp 或 snapshot_N/*.png
                members = [m for m in zf.namelist()
                           if m.startswith(snap_name + "/")
                           and Path(m).suffix.lower() in (".bmp", ".png", ".jpg")]
                if members:
                    members.sort()
                    member = members[0]
                    dest_name = Path(member).name
                    dest = snap / dest_name
                    snap.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        with zf.open(member) as src_f, open(dest, "wb") as dst_f:
                            dst_f.write(src_f.read())
                    return {"path": str(dest)}
        except Exception as e:
            pass  # fall through to filesystem fallback

    # 回退：从已解压的 sdp 目录找（老逻辑）
    parts = snap.parts
    try:
        analysis_idx = next(i for i in range(len(parts) - 1, -1, -1) if parts[i].lower() == "analysis")
        sdp_parts = parts[:analysis_idx] + parts[analysis_idx + 1:]
        sdp_snap = Path(*sdp_parts)
    except StopIteration:
        return {"skipped": "no .sdp file found and no 'analysis' segment in path"}

    src = None
    for name in _SCREENSHOT_NAMES:
        p = sdp_snap / name
        if p.exists():
            src = p
            break
    if src is None:
        for p in sorted(sdp_snap.glob("*.bmp")):
            src = p
            break

    if src is None:
        return {"skipped": "no screenshot found in sdp archive or sdp dir"}

    dest = snap / src.name
    snap.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
    return {"path": str(dest)}


def _run_step(step: str, snapshot_dir: str, db) -> Any:
    if step == "screenshot":
        return _copy_screenshot(snapshot_dir)

    if step == "mesh_stats":
        from analysis.mesh_stats_service import generate_buffers_json
        out = generate_buffers_json(snapshot_dir)
        return {"path": str(out)}

    if step == "texture_stats":
        from analysis.texture_stats_service import generate_texture_stats
        run_dir = str(Path(snapshot_dir).parent)
        out = generate_texture_stats(run_dir)
        return {"path": str(out)}

    if step == "gles_decompile":
        from analysis.gles_decompile_service import decompile_shaders
        return decompile_shaders(snapshot_dir)

    if step == "ingest":
        from data.ingest import ingest_snapshot
        return ingest_snapshot(db, snapshot_dir)

    if step == "label":
        from analysis.label_service import generate_label_json
        out = generate_label_json(snapshot_dir, db=db)
        return {"path": str(out)}

    if step == "status":
        from analysis.status_service import generate_status_json
        out = generate_status_json(snapshot_dir, db=db)
        return {"path": str(out)}

    if step == "topdc":
        from analysis.topdc_service import generate_topdc_json
        out = generate_topdc_json(snapshot_dir)
        return {"path": str(out)}

    if step == "analysis":
        from analysis.analysis_md_service import generate_analysis_md
        out = generate_analysis_md(snapshot_dir)
        return {"path": str(out)}

    if step == "describe":
        from analysis.vlm_screenshot_service import generate_scene_description
        out = generate_scene_description(snapshot_dir, db=db)
        return {"path": str(out)}

    raise ValueError(f"Unknown step: {step}")


# ── Module-level singleton (imported by server.py and routes/data.py) ─────────

pipeline_manager = PipelineJobManager()
