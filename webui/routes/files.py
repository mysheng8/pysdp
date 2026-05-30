"""
files.py — Local file-system API for browsing and serving files.
"""

import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import logger as _logger_module

_SCREENSHOT_CANDIDATES = [
    "snapshot_screenshot.png", "snapshot_screenshot.jpg",
    "1_screenshot.bmp", "snapshot.png",
]


def _generate_thumbnail(sdp_path: Path, width: int = 250, orientation: str = "landscape") -> str | None:
    """Extract first snapshot screenshot from .sdp and save a width-px JPEG thumbnail.
    orientation: 'landscape' (default) or 'portrait' — rotates to match target.
    Returns the absolute path to the thumbnail file, or None on failure."""
    from PIL import Image

    thumb_dir = sdp_path.parent / ".thumbnails"
    thumb_name = sdp_path.stem + ".jpg"
    thumb_path = thumb_dir / thumb_name

    if thumb_path.exists():
        return str(thumb_path).replace("\\", "/")

    try:
        with zipfile.ZipFile(str(sdp_path), "r") as z:
            names = z.namelist()
            snap_dirs = sorted(set(
                n.split("/")[0] for n in names
                if n.startswith("snapshot_") and "/" in n
            ))
            if not snap_dirs:
                return None

            first_snap = snap_dirs[0]
            for candidate in _SCREENSHOT_CANDIDATES:
                member = f"{first_snap}/{candidate}"
                if member in names:
                    data = z.read(member)
                    img = Image.open(io.BytesIO(data))

                    # Force image to match target orientation by rotating 90° CCW.
                    # Mobile Vulkan screenshots are typically portrait pixels with
                    # landscape content rotated 90° CW, so CCW restores landscape.
                    is_landscape = img.width > img.height
                    if orientation == "landscape" and not is_landscape:
                        img = img.transpose(Image.Transpose.ROTATE_90)
                    elif orientation == "portrait" and is_landscape:
                        img = img.transpose(Image.Transpose.ROTATE_270)

                    # Resize preserving aspect ratio based on width
                    ratio = width / img.width
                    new_h = int(img.height * ratio)
                    img = img.resize((width, new_h), Image.LANCZOS)

                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    img.save(str(thumb_path), format="JPEG", quality=80)
                    return str(thumb_path).replace("\\", "/")
    except Exception:
        pass
    return None


def make_router(db=None) -> APIRouter:
    router = APIRouter()

    # ── SDP metadata extraction (used during ingest) ────────────────────────────

    def _extract_sdp_info(p: Path) -> dict:
        """Extract metadata from an .sdp ZIP file."""
        info = {
            "app": None, "activity": None,
            "device_model": None, "device_manufacturer": None,
            "device_platform": None, "gpu_renderer": None,
            "api": None, "capture_time": None, "snapshot_count": 0,
            "project_id": None, "version_id": None, "label": None,
        }
        try:
            with zipfile.ZipFile(str(p), "r") as z:
                names = z.namelist()

                info["snapshot_count"] = len(set(
                    n.split("/")[0] for n in names
                    if n.startswith("snapshot_") and "/" in n
                ))

                if "capture_info.json" in names:
                    try:
                        ci = json.loads(z.read("capture_info.json"))
                        info["api"] = ci.get("api")
                        info["app"] = ci.get("package")
                        info["activity"] = ci.get("activity")
                        info["project_id"] = ci.get("project_id") or None
                        info["version_id"] = ci.get("version_id") or None
                        info["label"] = ci.get("label") or None
                    except Exception:
                        pass

                if "device_info.json" in names:
                    try:
                        di = json.loads(z.read("device_info.json"))
                        info["gpu_renderer"] = di.get("gpu_renderer")
                    except Exception:
                        pass

                if "sdp.db" in names:
                    with z.open("sdp.db") as dbf:
                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
                        tmp.write(dbf.read())
                        tmp.close()
                    try:
                        conn = sqlite3.connect(tmp.name)
                        conn.row_factory = sqlite3.Row

                        try:
                            row = conn.execute("SELECT * FROM ADBDevice LIMIT 1").fetchone()
                            if row:
                                info["device_model"] = row["productModel"]
                                info["device_manufacturer"] = row["productManufacturer"]
                                info["device_platform"] = row["boardPlatform"]
                        except Exception:
                            pass

                        try:
                            cap = conn.execute(
                                "SELECT processID, rendererString, startTimeTOD "
                                "FROM Capture WHERE captureType=4 ORDER BY captureID LIMIT 1"
                            ).fetchone()
                            if cap:
                                if not info["gpu_renderer"]:
                                    info["gpu_renderer"] = cap["rendererString"] or None
                                if not info["app"]:
                                    proc = conn.execute(
                                        "SELECT uid FROM Process WHERE pid=?", (cap["processID"],)
                                    ).fetchone()
                                    if proc:
                                        info["app"] = proc["uid"]
                            # Use last capture's time as session time
                            last_cap = conn.execute(
                                "SELECT startTimeTOD FROM Capture WHERE captureType=4 ORDER BY captureID DESC LIMIT 1"
                            ).fetchone()
                            if last_cap:
                                ts_us = last_cap["startTimeTOD"]
                                if ts_us and ts_us > 0:
                                    info["capture_time"] = datetime.fromtimestamp(
                                        ts_us / 1_000_000, tz=timezone.utc
                                    ).isoformat()
                        except Exception:
                            pass

                        if not info["api"]:
                            try:
                                row = conn.execute(
                                    "SELECT ApiName FROM DrawCallParameters LIMIT 1"
                                ).fetchone()
                                if row and row[0]:
                                    info["api"] = "GLES" if row[0].startswith("gl") else "Vulkan"
                            except Exception:
                                pass

                        conn.close()
                    finally:
                        os.unlink(tmp.name)
        except Exception:
            pass
        return info

    # ── SDP file list (DB-backed) ─────────────────────────────────────────────

    def _db_list_sdp(scan_dir: str | None = None) -> list[dict]:
        """Query sdp_files from DB, optionally filtered by scan_dir."""
        cur = db.cursor()
        if scan_dir:
            rows = cur.execute(
                "SELECT * FROM sdp_files WHERE scan_dir = ? ORDER BY modified DESC",
                [scan_dir]
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT * FROM sdp_files ORDER BY modified DESC"
            ).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in cur.description]
        files = []
        for row in rows:
            d = dict(zip(cols, row))
            files.append({
                "path": d["path"],
                "name": d["name"],
                "size": d["size"],
                "modified": d["modified"],
                "project_id": d.get("project_id"),
                "version_id": d.get("version_id"),
                "thumbnail": d.get("thumbnail"),
                "info": {
                    "app": d["app"],
                    "activity": d["activity"],
                    "device_model": d["device_model"],
                    "device_manufacturer": d["device_manufacturer"],
                    "device_platform": d["device_platform"],
                    "gpu_renderer": d["gpu_renderer"],
                    "api": d["api"],
                    "capture_time": str(d["capture_time"]) if d["capture_time"] else None,
                    "snapshot_count": d["snapshot_count"],
                    "analysis_dir": d["analysis_dir"],
                    "label": d.get("label"),
                },
            })
        return files

    @router.get("/sdp")
    def list_sdp(dir: str = Query(default="", description="Root directory (optional; omit to list all)")):
        """Return SDP files from DB. If dir given and not yet scanned, returns needs_scan."""
        if not db:
            return JSONResponse({"ok": False, "error": "DB not available"}, status_code=500)

        if not dir:
            # No dir specified — return everything in DB
            files = _db_list_sdp()
            return {"ok": True, "data": files}

        root = Path(dir)
        if not root.exists():
            return JSONResponse({"ok": False, "error": f"Directory not found: {dir}"}, status_code=404)
        if not root.is_dir():
            return JSONResponse({"ok": False, "error": f"Not a directory: {dir}"}, status_code=400)

        norm_dir = str(root).replace("\\", "/")

        cur = db.cursor()
        count = cur.execute(
            "SELECT COUNT(*) FROM sdp_files WHERE scan_dir = ?", [norm_dir]
        ).fetchone()[0]

        if count == 0:
            return {"ok": True, "data": [], "needs_scan": True}

        files = _db_list_sdp(norm_dir)
        return {"ok": True, "data": files}

    @router.post("/sdp/rescan")
    def rescan_sdp(dir: str = Query(..., description="Directory to rescan")):
        """Force rescan of a directory — removes old records and re-ingests."""
        if not db:
            return JSONResponse({"ok": False, "error": "DB not available"}, status_code=500)
        root = Path(dir)
        if not root.exists() or not root.is_dir():
            return JSONResponse({"ok": False, "error": f"Invalid directory: {dir}"}, status_code=400)

        norm_dir = str(root).replace("\\", "/")
        conn = db.conn()
        # Clear old thumbnails so they regenerate with current rotation logic
        thumb_dir = root / ".thumbnails"
        if thumb_dir.is_dir():
            import shutil
            shutil.rmtree(thumb_dir, ignore_errors=True)
        # Preserve existing project/version assignments during rescan
        existing_meta = {}
        for row in conn.execute(
            "SELECT path, project_id, version_id FROM sdp_files WHERE scan_dir = ?", [norm_dir]
        ).fetchall():
            existing_meta[row[0]] = (row[1], row[2])
        conn.execute("DELETE FROM sdp_files WHERE scan_dir = ?", [norm_dir])
        _scan_sdp_dir_with_conn(root, norm_dir, conn)
        # Restore project/version for files that didn't get them from capture_info
        for fpath, (pid, vid) in existing_meta.items():
            if pid:
                conn.execute(
                    "UPDATE sdp_files SET project_id = COALESCE(project_id, ?), version_id = COALESCE(version_id, ?) WHERE path = ?",
                    [pid, vid, fpath]
                )

        count = conn.execute(
            "SELECT COUNT(*) FROM sdp_files WHERE scan_dir = ?", [norm_dir]
        ).fetchone()[0]
        return {"ok": True, "count": count}

    @router.post("/sdp/ingest")
    async def ingest_sdp(request: Request):
        """Ingest a single SDP file into DB (called after capture completes)."""
        if not db:
            return JSONResponse({"ok": False, "error": "DB not available"}, status_code=500)

        body = {}
        try:
            raw = await request.body()
            if raw:
                body = json.loads(raw)
        except Exception:
            pass
        sdp_path = (body.get("path") or "").strip()
        if not sdp_path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)

        p = Path(sdp_path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False, "error": f"File not found: {sdp_path}"}, status_code=404)

        project_id = body.get("project_id")
        version_id = body.get("version_id")
        _ingest_one(p, db, project_id=project_id, version_id=version_id)
        return {"ok": True, "path": str(p).replace("\\", "/")}

    @router.post("/sdp/move")
    async def move_sdp(request: Request):
        """Update project_id and/or version_id for an SDP file."""
        if not db:
            return JSONResponse({"ok": False, "error": "DB not available"}, status_code=500)
        body = {}
        try:
            raw = await request.body()
            if raw:
                body = json.loads(raw)
        except Exception:
            pass
        path = (body.get("path") or "").strip()
        if not path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
        project_id = body.get("project_id")
        version_id = body.get("version_id")
        conn = db.conn()
        conn.execute("UPDATE sdp_files SET project_id = ?, version_id = ? WHERE path = ?", [project_id, version_id, path])
        return {"ok": True}

    @router.post("/sdp/remove")
    async def remove_sdp(request: Request):
        """Remove an SDP file record from the DB (does not delete the file on disk)."""
        if not db:
            return JSONResponse({"ok": False, "error": "DB not available"}, status_code=500)
        body = {}
        try:
            raw = await request.body()
            if raw:
                body = json.loads(raw)
        except Exception:
            pass
        path = (body.get("path") or "").strip()
        if not path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
        db.conn().execute("DELETE FROM sdp_files WHERE path = ?", [path])
        return {"ok": True}

    @router.post("/sdp/gen_thumbnail")
    async def gen_thumbnail(request: Request):
        """Regenerate thumbnail for an SDP file (force recreate)."""
        if not db:
            return JSONResponse({"ok": False, "error": "DB not available"}, status_code=500)
        body = {}
        try:
            raw = await request.body()
            if raw:
                body = json.loads(raw)
        except Exception:
            pass
        path = (body.get("path") or "").strip()
        if not path:
            return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
        p = Path(path.replace("/", os.sep))
        if not p.exists():
            return JSONResponse({"ok": False, "error": f"File not found: {path}"}, status_code=404)
        orientation = body.get("orientation", "landscape")
        # Delete existing thumbnail to force regeneration
        thumb_dir = p.parent / ".thumbnails"
        thumb_file = thumb_dir / (p.stem + ".jpg")
        if thumb_file.exists():
            thumb_file.unlink()
        thumb = _generate_thumbnail(p, orientation=orientation)
        if thumb:
            db.conn().execute("UPDATE sdp_files SET thumbnail = ? WHERE path = ?", [thumb, path])
            return {"ok": True, "thumbnail": thumb}
        return JSONResponse({"ok": False, "error": "Could not extract screenshot"}, status_code=404)

    def _scan_sdp_dir_with_conn(root: Path, norm_dir: str, conn) -> None:
        """Scan directory and ingest all .sdp files using a raw DB connection."""
        from concurrent.futures import ThreadPoolExecutor

        sdp_files = list(root.rglob("*.sdp"))

        def extract_one(f: Path) -> tuple[Path, dict, str | None]:
            info = _extract_sdp_info(f)
            thumb = _generate_thumbnail(f)
            return (f, info, thumb)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(extract_one, sdp_files))

        now = datetime.now(timezone.utc).isoformat()
        for f, info, thumb in results:
            st = f.stat()
            fpath = str(f).replace("\\", "/")
            conn.execute("""
                INSERT OR REPLACE INTO sdp_files
                (path, name, size, modified, app, activity, device_model,
                 device_manufacturer, device_platform, gpu_renderer, api,
                 capture_time, snapshot_count, analysis_dir, scan_dir, ingested_at,
                 project_id, version_id, label, thumbnail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                fpath, f.name, st.st_size, st.st_mtime,
                info.get("app"), info.get("activity"),
                info.get("device_model"), info.get("device_manufacturer"),
                info.get("device_platform"), info.get("gpu_renderer"),
                info.get("api"),
                info.get("capture_time"), info.get("snapshot_count") or 0,
                None, norm_dir, now,
                info.get("project_id"), info.get("version_id"), info.get("label"),
                thumb,
            ])

    def _scan_sdp_dir(root: Path, norm_dir: str, database) -> None:
        """Scan directory and ingest all .sdp files (using WorkspaceDB)."""
        _scan_sdp_dir_with_conn(root, norm_dir, database.conn())

    def _ingest_one(p: Path, database, project_id: str = None, version_id: str = None) -> None:
        """Ingest a single SDP file into DB."""
        st = p.stat()
        fpath = str(p).replace("\\", "/")
        info = _extract_sdp_info(p)
        thumb = _generate_thumbnail(p)
        parent_dir = str(p.parent).replace("\\", "/")
        now = datetime.now(timezone.utc).isoformat()
        pid = project_id or info.get("project_id")
        vid = version_id or info.get("version_id")
        database.conn().execute("""
            INSERT OR REPLACE INTO sdp_files
            (path, name, size, modified, app, activity, device_model,
             device_manufacturer, device_platform, gpu_renderer, api,
             capture_time, snapshot_count, analysis_dir, scan_dir, ingested_at,
             project_id, version_id, label, thumbnail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            fpath, p.name, st.st_size, st.st_mtime,
            info.get("app"), info.get("activity"),
            info.get("device_model"), info.get("device_manufacturer"),
            info.get("device_platform"), info.get("gpu_renderer"),
            info.get("api"),
            info.get("capture_time"), info.get("snapshot_count") or 0,
            None, parent_dir, now, pid, vid, info.get("label"), thumb,
        ])

    @router.get("/results")
    def list_results(dir: str = Query(..., description="Analysis capture directory")):
        """Return the files in an analysis result directory."""
        root = Path(dir)
        if not root.exists():
            return JSONResponse({"ok": False, "error": f"Directory not found: {dir}"}, status_code=404)
        if not root.is_dir():
            return JSONResponse({"ok": False, "error": f"Not a directory: {dir}"}, status_code=400)

        ORDER = [".json", ".md", ".hlsl", ".obj", ".png", ".csv"]

        def sort_key(p: Path):
            try:
                return ORDER.index(p.suffix.lower())
            except ValueError:
                return len(ORDER)

        files = []
        for f in sorted(root.iterdir(), key=sort_key):
            if f.is_file():
                st = f.stat()
                files.append({
                    "path": str(f),
                    "name": f.name,
                    "size": st.st_size,
                    "modified": st.st_mtime,
                    "ext": f.suffix.lstrip(".").lower(),
                })
        return {"ok": True, "data": files}

    @router.get("/analyses")
    def list_analyses(root: str = Query(..., description="Parent directory containing analysis run folders")):
        """Return all analysis runs under root, each with its snapshot subdirectories and files."""
        root_path = Path(root)
        if not root_path.exists():
            return JSONResponse({"ok": False, "error": f"Directory not found: {root}"}, status_code=404)
        if not root_path.is_dir():
            return JSONResponse({"ok": False, "error": f"Not a directory: {root}"}, status_code=400)

        _SCREENSHOT_NAMES = ["snapshot.png", "snapshot_screenshot.png", "snapshot_screenshot.jpg"]

        def _find_in_dir(d: Path) -> str | None:
            for name in _SCREENSHOT_NAMES:
                p = d / name
                if p.exists():
                    return str(p)
            for p in sorted(d.glob("*.bmp")):
                return str(p)
            return None

        def find_screenshot(snap_dir: Path) -> str | None:
            # 1. Look in the snapshot dir itself
            result = _find_in_dir(snap_dir)
            if result:
                return result
            # 2. Derive sdp capture dir by removing the "analysis" segment from the path.
            #    e.g. .../sdp/analysis/<run>/snapshot_N  →  .../sdp/<run>/snapshot_N
            try:
                parts = snap_dir.parts
                analysis_idx = next(i for i in range(len(parts) - 1, -1, -1) if parts[i].lower() == "analysis")
                sdp_parts = parts[:analysis_idx] + parts[analysis_idx + 1:]
                sdp_snap_dir = Path(*sdp_parts)
                result = _find_in_dir(sdp_snap_dir)
                if result:
                    return result
            except (StopIteration, Exception):
                pass
            return None

        def classify_file(f: Path) -> str:
            stem = f.stem.lower()
            ext = f.suffix.lstrip(".").lower()
            if "index" in stem:
                return "skip"
            if ext in ("md",) or any(k in stem for k in ("analysis", "dashboard", "report")):
                return "analysis"
            if any(k in stem for k in ("label", "status", "topdc")):
                return "statistics"
            if any(k in stem for k in ("dc", "buffers", "shaders", "textures", "metrics")):
                return "raw"
            # screenshots are surfaced via the dedicated screenshot field, not file lists
            if ext in ("png", "jpg", "jpeg", "bmp") and any(k in stem for k in ("screenshot", "snapshot")):
                return "skip"
            return "other"

        def file_info(f: Path) -> dict:
            st = f.stat()
            return {
                "path": str(f),
                "name": f.name,
                "size": st.st_size,
                "ext": f.suffix.lstrip(".").lower(),
            }

        runs = []
        for run_dir in sorted(root_path.iterdir(), key=lambda p: p.name, reverse=True):
            if not run_dir.is_dir():
                continue
            snapshots = []
            for snap_dir in sorted(d for d in run_dir.iterdir() if d.is_dir() and d.name.startswith("snapshot_")):
                snap_id = snap_dir.name  # e.g. "snapshot_2"
                groups = {"analysis": [], "statistics": [], "raw": [], "other": []}
                per_dc_files = []
                per_dc_dir = snap_dir / "per_dc_content"
                if per_dc_dir.exists():
                    for f in sorted(per_dc_dir.iterdir()):
                        if f.is_file():
                            per_dc_files.append(file_info(f))
                for f in sorted(snap_dir.iterdir()):
                    if not f.is_file():
                        continue
                    cat = classify_file(f)
                    if cat == "skip":
                        continue
                    groups[cat].append(file_info(f))
                snapshots.append({
                    "id": snap_id,
                    "path": str(snap_dir),
                    "screenshot": find_screenshot(snap_dir),
                    "analysis":   groups["analysis"],
                    "statistics": groups["statistics"],
                    "raw":        groups["raw"],
                    "per_dc":     per_dc_files,
                })
            if snapshots:
                runs.append({"name": run_dir.name, "path": str(run_dir), "snapshots": snapshots})

        return {"ok": True, "data": runs}

    @router.get("/read", operation_id="get_file_read",
                summary="[MCP] Read a text file",
                description="Read a local text file (HLSL shader, JSON, Markdown) and return its content. "
                            "path must be an absolute filesystem path (from shader_stages[].file_path, etc).")
    def read_file(
        path: str = Query(..., description="Absolute path to the file"),
        lines: int = Query(default=0, ge=0, description="Max lines to return (0 = all)"),
    ):
        """Read a text file and return its content."""
        p = Path(path)
        if not p.exists():
            return JSONResponse({"ok": False, "error": f"File not found: {path}"}, status_code=404)
        if not p.is_file():
            return JSONResponse({"ok": False, "error": f"Not a file: {path}"}, status_code=400)

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            if lines > 0:
                content = "\n".join(content.splitlines()[:lines])
            return {"ok": True, "data": {"content": content, "path": str(p), "name": p.name}}
        except Exception as exc:
            _logger_module.get_logger().error(
                "File read failed", exc=exc, context={"path": path}
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.head("/raw", include_in_schema=False)
    def head_raw(path: str = Query(...)):
        p = Path(path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False}, status_code=404)
        return Response(status_code=200)

    @router.get("/raw", operation_id="get_file_raw",
                summary="[MCP] Serve raw file bytes",
                description="Serve any local file as raw bytes. Use for mesh OBJ files (mesh_file path from dc_detail). "
                            "Add download=1 to trigger browser download.")
    def serve_raw(
        path: str = Query(..., description="Absolute path to any file (served as-is)"),
        download: int = Query(default=0, description="Set to 1 to trigger browser download"),
    ):
        """Serve any local file as raw bytes — used by Three.js OBJLoader."""
        p = Path(path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False, "error": f"File not found: {path}"}, status_code=404)
        headers = {}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{p.name}"'
        return FileResponse(str(p), headers=headers)

    @router.get("/image", operation_id="get_file_image",
                summary="[MCP] Serve an image file",
                description="Serve a PNG/JPG/BMP image from the local filesystem. "
                            "Use for screenshots (from /snapshots screenshot field) and textures (textures[].file_path from dc_detail).")
    def serve_image(
        path: str = Query(..., description="Absolute path to an image file"),
        rotate: int = Query(default=0, description="Rotation in degrees (90, -90, 180)"),
    ):
        """Serve an image file (png/jpg/bmp) from the local filesystem."""
        from PIL import Image

        p = Path(path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False, "error": f"File not found: {path}"}, status_code=404)
        ext = p.suffix.lstrip(".").lower()

        needs_convert = ext == "bmp" or rotate != 0

        if needs_convert:
            img = Image.open(str(p))
            if rotate == -90:
                img = img.transpose(Image.ROTATE_90)
            elif rotate == 90:
                img = img.transpose(Image.ROTATE_270)
            elif rotate == 180:
                img = img.transpose(Image.ROTATE_180)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")

        media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
        return FileResponse(str(p), media_type=media)

    @router.get("/sdp_info")
    def sdp_info(path: str = Query(..., description="Absolute path to an .sdp file")):
        """Extract metadata from an .sdp ZIP file (SQLite db + file listing)."""
        log = _logger_module.get_logger()
        p = Path(path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False, "error": f"File not found: {path}"}, status_code=404)

        try:
            with zipfile.ZipFile(str(p), "r") as z:
                names = z.namelist()

                # Snapshot directories
                snap_dirs = sorted(set(
                    n.split("/")[0] for n in names
                    if n.startswith("snapshot_") and "/" in n
                ))

                screenshots = {}
                for sd in snap_dirs:
                    for candidate in [
                        f"{sd}/snapshot_screenshot.png",
                        f"{sd}/snapshot_screenshot.jpg",
                        f"{sd}/1_screenshot.bmp",
                        f"{sd}/snapshot.png",
                    ]:
                        if candidate in names:
                            screenshots[sd] = candidate
                            break

                # Read database
                info = {
                    "app": None,
                    "activity": None,
                    "device_model": None,
                    "device_manufacturer": None,
                    "device_platform": None,
                    "device_serial": None,
                    "android_version": None,
                    "gpu_renderer": None,
                    "api": None,
                    "capture_time": None,
                    "snapshot_count": len(snap_dirs),
                    "snapshots": [],
                }

                if "sdp.db" in names:
                    with z.open("sdp.db") as dbf:
                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
                        tmp.write(dbf.read())
                        tmp.close()

                    try:
                        conn = sqlite3.connect(tmp.name)
                        conn.row_factory = sqlite3.Row

                        # ADBDevice
                        try:
                            row = conn.execute("SELECT * FROM ADBDevice LIMIT 1").fetchone()
                            if row:
                                info["device_model"] = row["productModel"]
                                info["device_manufacturer"] = row["productManufacturer"]
                                info["device_platform"] = row["boardPlatform"]
                                info["device_serial"] = row["deviceName"]
                                info["android_version"] = row["buildVersionRelease"]
                        except Exception:
                            pass

                        # Capture (frame capture = captureType 4)
                        try:
                            caps = conn.execute(
                                "SELECT processID, rendererString, startTimeTOD "
                                "FROM Capture WHERE captureType=4 ORDER BY captureID"
                            ).fetchall()
                            if caps:
                                first_cap = caps[0]
                                info["gpu_renderer"] = first_cap["rendererString"] or None
                                # Process (app package)
                                proc = conn.execute(
                                    "SELECT uid FROM Process WHERE pid=?", (first_cap["processID"],)
                                ).fetchone()
                                if proc:
                                    info["app"] = proc["uid"]
                                # Per-snapshot capture times
                                info["_capture_times"] = []
                                for cap in caps:
                                    ts_us = cap["startTimeTOD"]
                                    if ts_us and ts_us > 0:
                                        info["_capture_times"].append(
                                            datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc).isoformat()
                                        )
                                    else:
                                        info["_capture_times"].append(None)
                                # Session time = last snapshot's time
                                last_cap = caps[-1]
                                ts_us = last_cap["startTimeTOD"]
                                if ts_us and ts_us > 0:
                                    info["capture_time"] = datetime.fromtimestamp(
                                        ts_us / 1_000_000, tz=timezone.utc
                                    ).isoformat()
                        except Exception:
                            pass

                        # Determine API from DrawCallParameters.ApiName
                        try:
                            row = conn.execute(
                                "SELECT ApiName FROM DrawCallParameters LIMIT 1"
                            ).fetchone()
                            if row and row[0]:
                                info["api"] = "GLES" if row[0].startswith("gl") else "Vulkan"
                        except Exception:
                            pass

                        conn.close()
                    finally:
                        os.unlink(tmp.name)

                # capture_info.json — user-specified capture params (written by SDPCLI)
                if "capture_info.json" in names:
                    try:
                        with z.open("capture_info.json") as cif:
                            ci = json.loads(cif.read())
                        if ci.get("api"):
                            info["api"] = ci["api"]
                        if ci.get("package"):
                            info["app"] = ci["package"]
                        if ci.get("activity"):
                            info["activity"] = ci["activity"]
                    except Exception:
                        pass

                # device_info.json fallback for gpu_renderer
                if not info["gpu_renderer"] and "device_info.json" in names:
                    try:
                        with z.open("device_info.json") as dif:
                            di = json.loads(dif.read())
                        info["gpu_renderer"] = di.get("gpu_renderer")
                    except Exception:
                        pass

                # Build snapshot list with screenshot paths and capture times
                capture_times = info.pop("_capture_times", [])
                for i, sd in enumerate(snap_dirs):
                    snap_entry = {"id": sd, "screenshot": None, "capture_time": None}
                    if sd in screenshots:
                        snap_entry["screenshot"] = screenshots[sd]
                    if i < len(capture_times):
                        snap_entry["capture_time"] = capture_times[i]
                    info["snapshots"].append(snap_entry)

            return {"ok": True, "data": info}

        except zipfile.BadZipFile:
            return JSONResponse({"ok": False, "error": "Not a valid ZIP/SDP file"}, status_code=400)
        except Exception as exc:
            log.error("sdp_info failed", exc=exc, context={"path": path})
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.get("/sdp_screenshot")
    def sdp_screenshot(
        path: str = Query(..., description="Absolute path to an .sdp file"),
        snapshot: str = Query(..., description="Snapshot dir name, e.g. snapshot_2"),
        thumb: int = Query(default=1, description="1=thumbnail (max 240px wide), 0=full size"),
    ):
        """Extract and serve a screenshot image from inside an .sdp ZIP."""
        from PIL import Image

        p = Path(path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"ok": False, "error": f"File not found: {path}"}, status_code=404)

        try:
            with zipfile.ZipFile(str(p), "r") as z:
                names = z.namelist()
                for candidate in [
                    f"{snapshot}/snapshot_screenshot.png",
                    f"{snapshot}/snapshot_screenshot.jpg",
                    f"{snapshot}/1_screenshot.bmp",
                    f"{snapshot}/snapshot.png",
                ]:
                    if candidate in names:
                        data = z.read(candidate)
                        ext = candidate.rsplit(".", 1)[-1].lower()

                        if ext == "bmp" or thumb:
                            img = Image.open(io.BytesIO(data))
                            if thumb:
                                img.thumbnail((240, 240))
                            if img.mode in ("RGBA", "P"):
                                img = img.convert("RGB")
                            buf = io.BytesIO()
                            img.save(buf, format="JPEG", quality=75)
                            return Response(content=buf.getvalue(), media_type="image/jpeg")

                        media = {"png": "image/png", "jpg": "image/jpeg"}.get(ext, "application/octet-stream")
                        return Response(content=data, media_type=media)

            return JSONResponse({"ok": False, "error": "Screenshot not found in archive"}, status_code=404)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── Settings from config ─────────────────────────────────────────────────────

    @router.get("/settings")
    def get_settings_route():
        """Return resolved WebUI settings from config."""
        from config import get_settings as _get_cfg
        cfg = _get_cfg()
        working = cfg.get("WorkingDirectory", "")
        project = cfg.get("ProjectDir", "")
        if not project and working:
            project = str(Path(working) / "project")
        from config import resolve_project_subdir as _resolve
        sdp_dir      = str(_resolve("SdpDir",     "sdp")     or cfg.get("SdpDir",     "sdp"))
        analysis_dir = str(_resolve("AnalysisDir","analysis") or cfg.get("AnalysisDir","analysis"))
        report_dir   = str(_resolve("ReportDir",  "reports")  or cfg.get("ReportDir",  "reports"))
        return {
            "ok": True,
            "data": {
                "sdpDir": sdp_dir.replace("\\", "/"),
                "analysisDir": analysis_dir.replace("\\", "/"),
                "reportDir": report_dir.replace("\\", "/"),
                "snapshotId": int(cfg.get("WebSnapshotId", "1")),
                "targets": cfg.get("WebTargets", "screenshot,ingest,dc,shaders,textures,buffers,label,metrics,status,topdc,analysis"),
            },
        }

    @router.post("/settings")
    async def save_settings(request: Request):
        """Save WebUI settings back to config.ini."""
        raw = await request.body()
        if not raw:
            return JSONResponse({"ok": False, "error": "Empty body"}, status_code=400)
        body = json.loads(raw)

        from config import get_config_path, reload as _reload_cfg
        path = get_config_path()
        if not path or not path.exists():
            return JSONResponse({"ok": False, "error": "config.ini not found"}, status_code=500)

        # Map of frontend keys → config.ini keys
        key_map = {
            "sdpDir": "SdpDir",
            "analysisDir": "AnalysisDir",
            "reportDir": "ReportDir",
            "snapshotId": "WebSnapshotId",
            "targets": "WebTargets",
        }

        lines = path.read_text(encoding="utf-8-sig").splitlines()
        updated_keys = set()

        for fe_key, ini_key in key_map.items():
            if fe_key not in body:
                continue
            val = str(body[fe_key])
            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith(f"{ini_key}="):
                    lines[i] = f"{ini_key}={val}"
                    found = True
                    updated_keys.add(ini_key)
                    break
                elif stripped.startswith(f"# {ini_key}="):
                    lines[i] = f"{ini_key}={val}"
                    found = True
                    updated_keys.add(ini_key)
                    break
            if not found:
                lines.append(f"{ini_key}={val}")
                updated_keys.add(ini_key)

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _reload_cfg()

        return {"ok": True, "data": {"updated": list(updated_keys)}}

    return router
