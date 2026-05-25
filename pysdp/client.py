from typing import Callable, Optional

import requests

from ._jobs import JobPoller
from .exceptions import SdpConnectionError, SdpError, SdpStateError, SdpValidationError


class SdpClient:
    """Synchronous blocking client for the SDPCLI Server HTTP API.

    All job-based operations (connect, launch, capture, analyze) block until
    the underlying job reaches a terminal state or ``timeout`` is exceeded.

    Args:
        base_url: Base URL of the running SDPCLI Server.
        poll_interval: Seconds between job-status polls.
        timeout: Maximum seconds to wait for any single job.
        verbose: If True, print phase/progress to stdout while polling.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:5000",
        poll_interval: float = 2.0,
        timeout: float = 600,
        verbose: bool = False,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._verbose = verbose
        self._poller = JobPoller(base_url, poll_interval)

    # ── Device & Session ─────────────────────────────────────────────────────

    def connect(self, device_id: Optional[str] = None) -> dict:
        """Connect to an Android device.

        Blocks until the connect job completes (~90-120 s).

        Args:
            device_id: ADB device identifier (e.g. ``"192.168.1.100:5555"``).
                       Omit to let the server auto-select.

        Returns:
            ``{"deviceId": ..., "status": "Connected"}``
        """
        body = {"deviceId": device_id} if device_id else {}
        job_id = self._submit_job("/api/connect", body)
        return self._wait_result(job_id)

    def launch(self, package_activity: str) -> dict:
        """Launch the target app and establish a profiling session.

        Blocks until the launch job completes (~30-60 s).

        Args:
            package_activity: ``"com.example.app/com.example.MainActivity"``

        Returns:
            Job result dict (session info).
        """
        job_id = self._submit_job(
            "/api/session/launch", {"packageActivity": package_activity}
        )
        return self._wait_result(job_id)

    def disconnect(self) -> dict:
        """Disconnect from the device and reset to Disconnected state."""
        return self._post("/api/disconnect", {})

    def device_status(self) -> dict:
        """Query the current device and session state.

        Returns:
            ``{"status": ..., "connectedDevice": ..., "session": ...}``
        """
        return self._get("/api/device")

    # ── Capture ──────────────────────────────────────────────────────────────

    def capture(
        self,
        output_dir: Optional[str] = None,
        label: Optional[str] = None,
    ) -> dict:
        """Trigger a GPU frame snapshot and block until complete (~3-5 min).

        Args:
            output_dir: Override output directory for the capture.
            label: Optional label for the capture.

        Returns:
            ``{"sdpPath": ..., "captureId": ..., "sessionId": ...}``
        """
        job_id = self.submit_capture(output_dir=output_dir, label=label)
        return self._wait_result(job_id)

    def submit_capture(
        self,
        output_dir: Optional[str] = None,
        label: Optional[str] = None,
    ) -> str:
        """Submit a capture job without waiting. Returns the job ID."""
        body: dict = {}
        if output_dir:
            body["outputDir"] = output_dir
        if label:
            body["label"] = label
        return self._submit_job("/api/capture", body)

    # ── Analysis ─────────────────────────────────────────────────────────────

    def analyze(
        self,
        sdp_path: str,
        snapshot_id: int,
        output_dir: Optional[str] = None,
        targets: Optional[str] = None,
    ) -> dict:
        """Run offline analysis on an .sdp file and block until complete (~2-10 min).

        Args:
            sdp_path: Absolute path to the ``.sdp`` archive.
            snapshot_id: Capture ID to analyse (>= 2).
            output_dir: Override analysis output root directory.
            targets: Comma-separated target list, e.g. ``"label,metrics,status"``.
                     Defaults to all targets.

        Returns:
            ``{"sdpPath": ..., "captureId": ..., "sessionDir": ...,
               "captureDir": ..., "targets": ...}``
        """
        body: dict = {"sdpPath": sdp_path, "snapshotId": snapshot_id}
        if output_dir:
            body["outputDir"] = output_dir
        if targets:
            body["targets"] = targets
        job_id = self._submit_job("/api/analysis", body)
        return self._wait_result(job_id)

    # ── Job low-level API ─────────────────────────────────────────────────────

    def wait_for_job(
        self,
        job_id: str,
        on_progress: Optional[Callable[[Optional[str], int], None]] = None,
    ) -> dict:
        """Block until a previously submitted job completes.

        Args:
            job_id: Job ID returned by a submit call.
            on_progress: Callback invoked each poll with ``(phase, progress)``.

        Returns:
            Full job dict.
        """
        return self._poller.wait(job_id, self._timeout, on_progress=on_progress)

    def get_job(self, job_id: str) -> dict:
        """Single non-blocking query for job status."""
        return self._get(f"/api/jobs/{job_id}")

    def list_jobs(self) -> list:
        """Return all known jobs (most-recent first)."""
        result = self._get("/api/jobs")
        return result if isinstance(result, list) else []

    def cancel_job(self, job_id: str) -> dict:
        """Request cancellation of a running job."""
        return self._post(f"/api/jobs/{job_id}/cancel", {})

    def delete_job(self, job_id: str) -> dict:
        """Delete a terminal (Completed / Failed / Cancelled) job."""
        return self._delete(f"/api/jobs/{job_id}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _submit_job(self, path: str, body: dict) -> str:
        """POST to a job endpoint and return the job ID."""
        data = self._post(path, body)
        return data["jobId"]

    def _wait_result(self, job_id: str) -> dict:
        """Wait for job completion and return its result dict."""
        on_progress = self._on_progress if self._verbose else None
        job = self._poller.wait(job_id, self._timeout, on_progress=on_progress)
        return job.get("result") or {}

    @staticmethod
    def _on_progress(phase: Optional[str], progress: int) -> None:
        if phase:
            print(f"  [{progress:3d}%] {phase}")

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = requests.post(
                f"{self._base_url}{path}",
                json=body,
                timeout=10,
            )
        except requests.ConnectionError as exc:
            raise SdpConnectionError(
                f"Cannot connect to SDPCLI Server at {self._base_url}: {exc}"
            ) from exc
        self._check_response(resp)
        return resp.json().get("data") or {}

    def _get(self, path: str):
        try:
            resp = requests.get(f"{self._base_url}{path}", timeout=10)
        except requests.ConnectionError as exc:
            raise SdpConnectionError(
                f"Cannot connect to SDPCLI Server at {self._base_url}: {exc}"
            ) from exc
        self._check_response(resp)
        return resp.json().get("data")

    def _delete(self, path: str) -> dict:
        try:
            resp = requests.delete(f"{self._base_url}{path}", timeout=10)
        except requests.ConnectionError as exc:
            raise SdpConnectionError(
                f"Cannot connect to SDPCLI Server at {self._base_url}: {exc}"
            ) from exc
        self._check_response(resp)
        return resp.json().get("data") or {}

    @staticmethod
    def _check_response(resp: requests.Response) -> None:
        if resp.status_code == 409:
            try:
                error = resp.json().get("error", resp.text)
            except Exception:
                error = resp.text
            raise SdpStateError(error)
        if resp.status_code == 400:
            try:
                error = resp.json().get("error", resp.text)
            except Exception:
                error = resp.text
            raise SdpValidationError(error)
        if not resp.ok:
            try:
                error = resp.json().get("error", resp.text)
            except Exception:
                error = resp.text
            raise SdpError(f"HTTP {resp.status_code}: {error}")
