class SdpError(Exception):
    """Base exception for all pysdp errors."""


class SdpStateError(SdpError):
    """Device state does not satisfy the operation's precondition (HTTP 409)."""


class SdpValidationError(SdpError):
    """Invalid parameters (HTTP 400)."""


class SdpJobError(SdpError):
    """Job ended in Failed or Cancelled state."""

    def __init__(self, message: str, error: str | None = None):
        super().__init__(message)
        self.error = error


class SdpTimeoutError(SdpError):
    """Job did not complete within the configured timeout."""


class SdpConnectionError(SdpError):
    """Cannot connect to the SDPCLI Server."""
