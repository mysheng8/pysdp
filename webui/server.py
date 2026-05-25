"""
server.py — pySdp WebUI entry point.

Starts a FastAPI + uvicorn server on localhost:8000.
Serves static files at /static/ and proxies SDPCLI API calls.

Usage:
    python webui/server.py [--port 8000] [--host 127.0.0.1] [--sdpcli http://localhost:5000]
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# ── Parse CLI args early so env vars are set before routes are imported ───────
if __name__ == "__main__":
    _p = argparse.ArgumentParser(description="pySdp WebUI", add_help=False)
    _p.add_argument("--port",   type=int, default=8000)
    _p.add_argument("--host",   default="127.0.0.1")
    _p.add_argument("--sdpcli", default=None, metavar="URL",
                    help="SDPCLI Server URL (default: http://localhost:5000)")
    _p.add_argument("-h", "--help", action="store_true")
    _args, _ = _p.parse_known_args()
    if _args.sdpcli:
        os.environ["SDPCLI_URL"] = _args.sdpcli

# Make webui/ importable when run as `python webui/server.py`
sys.path.insert(0, str(Path(__file__).parent))
# Make pySdp/ root importable so `import analysis` resolves to pySdp/analysis/
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request                  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles           # noqa: E402

from routes.proxy           import router as proxy_router                    # noqa: E402
from routes.files           import make_router as _make_files_router         # noqa: E402
from routes.logs            import router as logs_router                     # noqa: E402
from routes.data            import make_router as _make_data_router          # noqa: E402
from routes.jobs_router     import make_router as _make_jobs_router          # noqa: E402
from routes.snapshot_router import router as snapshot_router                 # noqa: E402
from events                 import router as events_router, _set_loop as _events_set_loop  # noqa: E402
import logger as _logger_module                       # noqa: E402
from data.db import WorkspaceDB                       # noqa: E402

# ── App ───────────────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="pySdp WebUI",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_tags=[
        {
            "name": "frontend",
            "description": "Transparent proxy to SDPCLI server — used by the browser SPA. "
                           "Not intended for direct external use.",
        },
        {
            "name": "snapshot",
            "description": "Typed snapshot workflow commands: connect, disconnect, launch, capture, "
                           "device/package/activity discovery.",
        },
        {
            "name": "jobs",
            "description": "Job trigger endpoints: C# extraction (reply_extract), Python pipeline steps "
                           "(ingest, mesh_stats, texture_stats, label, screenshot, scene_describe, report), "
                           "and async pipeline with polling.",
        },
        {
            "name": "files",
            "description": "Read-only local filesystem access: browse SDP files, list analysis results, "
                           "serve text/image/raw files.",
        },
        {
            "name": "data",
            "description": "DuckDB query layer: snapshots, draw calls, metrics, labels, questions, dashboards. "
                           "**MCP-exposed endpoints are in this group.**",
        },
        {
            "name": "logs",
            "description": "WebUI server log streaming and management.",
        },
    ],
)

# ── DuckDB workspace DB (global singleton) ─────────────────────────────────────
_db = WorkspaceDB()

# ── Model registration — import triggers all @register decorators ─────────────
import analysis.models  # noqa: E402  # registers category_breakdown, top_bottleneck_dcs, label_quality

# ── Seed built-in questions (idempotent) ──────────────────────────────────────
from data.questions import seed_builtin_questions as _seed_builtin_questions  # noqa: E402
_seeded = _seed_builtin_questions(_db)
_logger_module.get_logger().info(f"Seeded {_seeded} built-in questions")

# ── Seed built-in dashboards (idempotent) ─────────────────────────────────────
from data.dashboards import seed_builtin_dashboards as _seed_builtin_dashboards  # noqa: E402
_seeded_dash = _seed_builtin_dashboards(_db)
_logger_module.get_logger().info(f"Seeded {_seeded_dash} built-in dashboards")

# ── Seed default project (idempotent) ────────────────────────────────────────
def _seed_default_project(db: WorkspaceDB):
    import uuid
    from datetime import datetime, timezone
    exists = db.conn().execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    if exists > 0:
        return
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.conn().execute(
        "INSERT INTO projects (id, name, description, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "Default", "Uncategorized snapshots", "#6b7280", now, now)
    )
    vid = str(uuid.uuid4())
    db.conn().execute(
        "INSERT INTO versions (id, project_id, name, description, ordinal, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (vid, pid, "unversioned", "No version specified", 0, now)
    )
    _logger_module.get_logger().info(f"Created default project '{pid}' with version '{vid}'")

_seed_default_project(_db)

# ── Exception middleware — catches any unhandled error in route handlers ──────

@app.middleware("http")
async def _exception_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        _logger_module.get_logger().error(
            f"Unhandled exception: {exc}",
            exc=exc,
            context={"method": request.method, "path": str(request.url.path)},
        )
        return JSONResponse(
            {"ok": False, "error": f"Internal server error: {exc}"},
            status_code=500,
        )

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(proxy_router,                    prefix="/api/sdpcli")
app.include_router(snapshot_router,                 prefix="/api/snapshot", tags=["snapshot"])
app.include_router(_make_files_router(db=_db),      prefix="/api/files",    tags=["files"])
app.include_router(logs_router,                     prefix="/api/logs")
app.include_router(_make_data_router(_db),          prefix="/api/data",     tags=["data"])
app.include_router(_make_jobs_router(db=_db),       prefix="/api/jobs",     tags=["jobs"])
app.include_router(events_router,                   prefix="/api")

# ── Chat AI module ───────────────────────────────────────────────────────────
from chat import init as _chat_init  # noqa: E402
_chat_init(_db)
from routes.chat import router as _chat_router  # noqa: E402
app.include_router(_chat_router, prefix="/api/chat", tags=["chat"])

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── MCP layer (fastapi-mcp) ───────────────────────────────────────────────────
try:
    from fastapi_mcp import FastApiMCP
    _mcp = FastApiMCP(
        app,
        name="pySdp",
        description=(
            "Snapdragon GPU profiling data — query draw calls, GPU metrics, labels, "
            "and performance correlations from DuckDB."
        ),
        include_operations=[
            # snapshots + draw calls
            "get_snapshots",
            "get_draw_calls",
            "get_dc_detail",
            # metrics
            "get_available_metrics",
            "get_label_correlations",
            "get_clock_correlation",
            "get_label_agg_multi",
            "get_label_agg",
            "get_label_agg_all",
            "get_label_metrics",
            # questions + dashboards
            "get_models",
            "get_questions",
            "get_question",
            "get_dashboards",
            "get_dashboard",
            # file access
            "get_file_read",
            "get_file_raw",
            "get_file_image",
        ],
    )
    _mcp.mount_http()
except ImportError:
    pass  # fastapi-mcp not installed; MCP layer disabled


@app.on_event("startup")
async def _on_startup():
    _events_set_loop(asyncio.get_running_loop())


@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """Entry point for `pysdp` CLI command and `python -m pySdp`."""
    import uvicorn

    p = argparse.ArgumentParser(description="pySdp WebUI")
    p.add_argument("--port",   type=int, default=8000)
    p.add_argument("--host",   default="127.0.0.1")
    p.add_argument("--sdpcli", default=None, metavar="URL",
                   help="SDPCLI Server URL (default: http://localhost:5000)")
    p.add_argument("--skip-sdpcli-check", action="store_true",
                   help="Skip SDPCLI version check on startup")
    args = p.parse_args()
    if args.sdpcli:
        os.environ["SDPCLI_URL"] = args.sdpcli

    if not args.skip_sdpcli_check:
        try:
            from scripts.fetch_sdpcli import check_sdpcli_version
            check_sdpcli_version(auto_download=True)
        except Exception:
            pass

    from routes.proxy import SDPCLI_BASE
    log = _logger_module.get_logger()
    log.info("WebUI starting", context={
        "host": args.host, "port": args.port, "sdpcli": SDPCLI_BASE
    })

    print(f"pySdp WebUI   -> http://{args.host}:{args.port}")
    print(f"SDPCLI Server -> {SDPCLI_BASE}")
    print(f"Log level     -> {log._min_level.upper()}" + (" (per-asset debug enabled)" if log.is_debug else ""))
    print(f"Log file      -> {Path(__file__).parent / 'logs' / 'webui.log'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    if getattr(_args, "help", False):
        _p.print_help()
        sys.exit(0)
    if _args.sdpcli:
        os.environ["SDPCLI_URL"] = _args.sdpcli

    from routes.proxy import SDPCLI_BASE
    import uvicorn

    log = _logger_module.get_logger()
    log.info("WebUI starting", context={
        "host": _args.host, "port": _args.port, "sdpcli": SDPCLI_BASE
    })

    print(f"pySdp WebUI   -> http://{_args.host}:{_args.port}")
    print(f"SDPCLI Server -> {SDPCLI_BASE}")
    print(f"Log level     -> {log._min_level.upper()}" + (" (per-asset debug enabled)" if log.is_debug else ""))
    print(f"Log file      -> {Path(__file__).parent / 'logs' / 'webui.log'}")
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="warning")
