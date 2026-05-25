import time
from typing import Callable, Optional

import requests

from .exceptions import SdpConnectionError, SdpJobError, SdpTimeoutError


class JobPoller:
    """Polls GET /api/jobs/{id} until the job reaches a terminal state."""

    def __init__(self, base_url: str, poll_interval: float = 2.0):
        self._base_url = base_url.rstrip("/")
        self._poll_interval = poll_interval

    def wait(
        self,
        job_id: str,
        timeout: float,
        on_progress: Optional[Callable[[Optional[str], int], None]] = None,
    ) -> dict:
        """Block until the job completes.

        Args:
            job_id: ID of the job to poll.
            timeout: Maximum seconds to wait before raising SdpTimeoutError.
            on_progress: Optional callback invoked each poll with (phase, progress).

        Returns:
            Full job dict (includes ``result`` key when Completed).

        Raises:
            SdpJobError: Job ended with status Failed or Cancelled.
            SdpTimeoutError: Timeout elapsed before the job reached a terminal state.
            SdpConnectionError: Cannot reach the SDPCLI Server.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self._fetch(job_id)
            status = job.get("status", "")

            if on_progress is not None:
                on_progress(job.get("phase"), job.get("progress", 0))

            if status == "Completed":
                return job
            if status in ("Failed", "Cancelled"):
                raise SdpJobError(
                    f"Job {job_id} ended with status '{status}': {job.get('error', '')}",
                    error=job.get("error"),
                )

            time.sleep(self._poll_interval)

        raise SdpTimeoutError(f"Job {job_id} did not complete within {timeout:.0f}s")

    def _fetch(self, job_id: str) -> dict:
        try:
            resp = requests.get(
                f"{self._base_url}/api/jobs/{job_id}",
                timeout=10,
            )
        except requests.ConnectionError as exc:
            raise SdpConnectionError(
                f"Cannot connect to SDPCLI Server: {exc}"
            ) from exc
        resp.raise_for_status()
        envelope = resp.json()
        return envelope.get("data", envelope)
