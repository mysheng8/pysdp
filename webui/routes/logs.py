"""
logs.py — /api/logs  endpoints for querying the in-memory exception log.
"""

from typing import Optional

from fastapi import APIRouter, Query

import logger as _logger_module

router = APIRouter()


@router.get("")
def get_logs(
    level:  Optional[str] = Query(default=None, description="Filter: info | warning | error"),
    limit:  int            = Query(default=100, ge=1, le=500),
    offset: int            = Query(default=0,   ge=0),
):
    """Return recent log records, newest first."""
    log   = _logger_module.get_logger()
    recs  = log.get_records(level=level, limit=limit, offset=offset)
    return {"ok": True, "data": recs, "total": log.total}


@router.delete("")
def clear_logs():
    """Clear the in-memory log buffer (does not delete the log file)."""
    _logger_module.get_logger().clear()
    return {"ok": True, "data": {"cleared": True}}
