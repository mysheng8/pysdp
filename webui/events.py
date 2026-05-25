"""
events.py — Server-Sent Events (SSE) bus for real-time UI updates.

Usage:
  Server-side:  from events import publish
                publish("label_changed", {"snapshot_id": 2, "api_id": 98})

  Client-side:  const es = new EventSource('/api/events');
                es.addEventListener('label_changed', e => { ... });
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()

# ── Subscriber management ────────────────────────────────────────────────────

_subscribers: list[asyncio.Queue] = []
_sub_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def _set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def publish(event_type: str, data: dict[str, Any] | None = None) -> None:
    """Publish an event to all connected SSE clients.

    Thread-safe — can be called from sync route handlers or background threads.
    """
    payload = json.dumps(data or {}, ensure_ascii=False)
    message = f"event: {event_type}\ndata: {payload}\n\n"

    loop = _loop
    if loop is None:
        return

    loop.call_soon_threadsafe(_broadcast, message)


def _broadcast(message: str) -> None:
    """Put message into all subscriber queues. Must run on the event loop thread."""
    with _sub_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass


# ── SSE endpoint ─────────────────────────────────────────────────────────────

@router.get("/events", summary="SSE stream for real-time UI updates")
async def sse_stream():
    """Server-Sent Events stream. Emits typed events when data changes."""
    # Capture event loop on first connection
    if _loop is None:
        _set_loop(asyncio.get_running_loop())

    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    with _sub_lock:
        _subscribers.append(q)

    async def _generate():
        try:
            yield f"event: connected\ndata: {{\"ts\":{int(time.time())}}}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _sub_lock:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
