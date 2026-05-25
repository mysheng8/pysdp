"""
snapshot_router.py — /api/snapshot/* endpoints.

Wraps the SDPCLI snapshot workflow commands as typed FastAPI endpoints:
  POST /connect       → connect to Android device via ADB
  POST /disconnect    → disconnect current device session
  POST /launch        → launch app on connected device
  POST /capture       → trigger GPU frame capture
  GET  /device        → current device/session status
  GET  /jobs/{job_id} → poll a snapshot job (connect/launch/capture)
  POST /jobs/{job_id}/cancel

All POST commands return 202 + { jobId } on success (except disconnect which is synchronous).
Poll job status via GET /api/snapshot/jobs/{job_id}.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from routes.proxy import _fwd


# ── Pydantic request models ───────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    deviceId: Optional[str] = None


class LaunchRequest(BaseModel):
    packageActivity: Optional[str] = None


class CaptureRequest(BaseModel):
    outputDir: Optional[str] = None
    label: Optional[str] = None


# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()


@router.post(
    "/connect",
    summary="Connect to an Android device via ADB",
    description=(
        "Start an async connect job on the SDPCLI server. "
        "deviceId: optional ADB serial (e.g. '192.168.1.100:5555'); "
        "if omitted, SDPCLI auto-selects the first available device. "
        "Returns jobId — poll via GET /api/snapshot/jobs/{job_id}. "
        "Returns 409 if already connecting/connected."
    ),
)
def connect(body: ConnectRequest):
    payload = {}
    if body.deviceId:
        payload["deviceId"] = body.deviceId
    return _fwd("POST", "/api/connect", payload, timeout=120)


@router.post(
    "/disconnect",
    summary="Disconnect the current device session",
    description="Synchronously disconnects the active device session. No jobId returned.",
)
def disconnect():
    return _fwd("POST", "/api/disconnect", {}, timeout=60)


@router.post(
    "/launch",
    summary="Launch an app on the connected device",
    description=(
        "Start an async launch job. Device must be in Connected state. "
        "packageActivity: 'com.example.app/com.example.MainActivity' format; "
        "if omitted, uses the package/activity from config.ini. "
        "Returns jobId — poll via GET /api/snapshot/jobs/{job_id}. "
        "Returns 409 if not in Connected state."
    ),
)
def launch(body: LaunchRequest):
    payload = {}
    if body.packageActivity:
        payload["packageActivity"] = body.packageActivity
    return _fwd("POST", "/api/session/launch", payload, timeout=120)


@router.post(
    "/capture",
    summary="Trigger a GPU frame capture",
    description=(
        "Start an async GPU snapshot capture job. Device must be in SessionActive state. "
        "outputDir: optional absolute path for capture output (defaults to config.ini OutputDir). "
        "label: optional frame label string. "
        "Returns jobId — poll via GET /api/snapshot/jobs/{job_id}. "
        "Returns 409 if not in SessionActive state."
    ),
)
def capture(body: CaptureRequest):
    payload = {}
    if body.outputDir:
        payload["outputDir"] = body.outputDir
    if body.label:
        payload["label"] = body.label
    return _fwd("POST", "/api/capture", payload, timeout=300)


@router.get(
    "/device",
    summary="Get current device/session status",
    description="Return the current device connection and session state from SDPCLI.",
)
def device():
    return _fwd("GET", "/api/device", silent=True)


@router.get(
    "/devices",
    summary="List connected ADB devices",
    description="Run 'adb devices' and return connected devices as [{serial, state}]. "
                "Use serial from this list as deviceId in POST /api/snapshot/connect.",
)
def list_devices():
    return _fwd("GET", "/api/devices")


@router.get(
    "/app/packages",
    summary="List installed packages on device",
    description="Run 'adb shell pm list packages -3' and return third-party package names. "
                "Optionally pass serial to target a specific device.",
)
def list_packages(serial: str = ""):
    path = "/api/app/packages" + (f"?serial={serial}" if serial else "")
    return _fwd("GET", path)


@router.get(
    "/app/activities",
    summary="List activities for a package",
    description="Run dumpsys to enumerate activities for the given package. "
                "Returns [{packageName/activityName}]. "
                "Pass the chosen entry as packageActivity in POST /api/snapshot/launch.",
)
def list_activities(package: str, serial: str = ""):
    path = f"/api/app/activities?package={package}" + (f"&serial={serial}" if serial else "")
    return _fwd("GET", path)


@router.get(
    "/jobs/{job_id}",
    summary="Poll a snapshot job",
    description="Poll the status of a connect/launch/capture job by jobId.",
)
def get_job(job_id: str):
    return _fwd("GET", f"/api/jobs/{job_id}")


@router.post(
    "/jobs/{job_id}/cancel",
    summary="Cancel a snapshot job",
)
def cancel_job(job_id: str):
    return _fwd("POST", f"/api/jobs/{job_id}/cancel", {})
