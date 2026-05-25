"""
proxy.py — Transparent proxy to the SDPCLI Server (default: http://localhost:5000).

All /api/sdpcli/* routes forward to the corresponding /api/* endpoint on the
SDPCLI Server and relay the JSON response back to the browser unchanged.
"""

import asyncio
import json
import os

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import logger as _logger_module

SDPCLI_BASE = os.environ.get("SDPCLI_URL", "http://localhost:5000")

router = APIRouter(tags=["frontend"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _fwd(method: str, path: str, body=None, *, silent: bool = False, timeout: int = 30) -> JSONResponse:
    """Forward a request to the SDPCLI Server.

    Args:
        silent:  When True, suppresses warning logs for connection errors
                 (used for high-frequency polling routes like /api/device).
        timeout: Request timeout in seconds (default 30; use higher values for
                 long-running operations like connect/capture/analysis).
    """
    log = _logger_module.get_logger()
    url = f"{SDPCLI_BASE}{path}"
    ctx = {"method": method, "url": url}

    try:
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        elif method == "POST":
            r = requests.post(url, json=body, timeout=timeout)
        elif method == "DELETE":
            r = requests.delete(url, timeout=timeout)
        else:
            return JSONResponse({"ok": False, "error": "Method not allowed"}, status_code=405)

        # Log server-side errors (5xx) as errors; 4xx as warnings
        if r.status_code >= 500:
            log.error(f"SDPCLI returned {r.status_code}", context={**ctx, "status": r.status_code})
        elif r.status_code >= 400:
            log.warning(f"SDPCLI returned {r.status_code}", context={**ctx, "status": r.status_code})

        try:
            return JSONResponse(content=r.json(), status_code=r.status_code)
        except Exception:
            return JSONResponse(
                {"ok": False, "error": r.text or f"HTTP {r.status_code}"},
                status_code=r.status_code,
            )

    except requests.ConnectionError as exc:
        if not silent:
            log.warning("SDPCLI Server unreachable", context={**ctx, "error": str(exc)})
        return JSONResponse(
            {"ok": False, "error": "Cannot connect to SDPCLI Server — is it running?"},
            status_code=503,
        )
    except Exception as exc:
        log.error("Proxy unexpected error", exc=exc, context=ctx)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/connect")
async def connect(request: Request):
    body = await _body(request)
    return await asyncio.to_thread(_fwd, "POST", "/api/connect", body, timeout=120)


@router.post("/disconnect")
async def disconnect():
    return await asyncio.to_thread(_fwd, "POST", "/api/disconnect", {}, timeout=60)


@router.post("/session/launch")
async def launch(request: Request):
    body = await _body(request)
    return await asyncio.to_thread(_fwd, "POST", "/api/session/launch", body, timeout=120)


@router.post("/capture")
async def capture(request: Request):
    body = await _body(request)
    return await asyncio.to_thread(_fwd, "POST", "/api/capture", body, timeout=300)


@router.post("/analysis")
async def analysis(request: Request):
    body = await _body(request)
    return await asyncio.to_thread(_fwd, "POST", "/api/analysis", body, timeout=60)


@router.get("/devices")
async def list_devices():
    return await asyncio.to_thread(_fwd, "GET", "/api/devices")


@router.get("/app/packages")
async def list_packages(serial: str = ""):
    path = "/api/app/packages" + (f"?serial={serial}" if serial else "")
    return await asyncio.to_thread(_fwd, "GET", path)


@router.get("/app/activities")
async def list_activities(package: str, serial: str = ""):
    path = f"/api/app/activities?package={package}" + (f"&serial={serial}" if serial else "")
    return await asyncio.to_thread(_fwd, "GET", path)


@router.get("/status")
async def sdpcli_status():
    return await asyncio.to_thread(_fwd, "GET", "/api/status", silent=True)


@router.get("/device")
async def device():
    return await asyncio.to_thread(_fwd, "GET", "/api/device", silent=True)


@router.get("/jobs")
async def list_jobs():
    return await asyncio.to_thread(_fwd, "GET", "/api/jobs")


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    return await asyncio.to_thread(_fwd, "GET", f"/api/jobs/{job_id}")


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    return await asyncio.to_thread(_fwd, "POST", f"/api/jobs/{job_id}/cancel", {})


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    return await asyncio.to_thread(_fwd, "DELETE", f"/api/jobs/{job_id}")
