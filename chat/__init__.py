"""chat/ — AI chat module. Independent of webui/, depends on data/."""
from __future__ import annotations

from data.db import WorkspaceDB

_db: WorkspaceDB | None = None


def init(db: WorkspaceDB):
    global _db
    _db = db


def get_db() -> WorkspaceDB:
    assert _db is not None, "chat.init(db) not called"
    return _db
