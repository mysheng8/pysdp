from dataclasses import dataclass
from typing import Optional


@dataclass
class JobStatus:
    id: str
    type: str             # Connect / Launch / Capture / Analysis
    status: str           # Pending / Running / Cancelling / Completed / Failed / Cancelled
    phase: Optional[str]  # current phase name when Running
    progress: int         # 0-100
    result: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_dict(cls, d: dict) -> "JobStatus":
        return cls(
            id=d.get("id", ""),
            type=d.get("type", ""),
            status=d.get("status", ""),
            phase=d.get("phase"),
            progress=d.get("progress", 0),
            result=d.get("result"),
            error=d.get("error"),
        )


@dataclass
class DeviceInfo:
    status: str                    # Disconnected / Connecting / Connected / ...
    connected_device: Optional[str]
    session: Optional[dict]

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceInfo":
        return cls(
            status=d.get("status", "Disconnected"),
            connected_device=d.get("connectedDevice"),
            session=d.get("session"),
        )
