"""dashboards.py — CRUD for the dashboards table."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SELECT_COLS = (
    "SELECT id, title, question_ids,"
    " CAST(created_at AS VARCHAR),"
    " CAST(updated_at AS VARCHAR)"
    " FROM dashboards"
)


def _row_to_dict(row) -> dict:
    return {
        "id":           row[0],
        "title":        row[1],
        "question_ids": json.loads(row[2] or "[]"),
        "created_at":   row[3],
        "updated_at":   row[4],
    }


def create_dashboard(db, *, title: str, question_ids: list[str] = None,
                     dashboard_id: str = None) -> dict:
    """Insert a new dashboard row. Returns the created dashboard dict."""
    did = dashboard_id or str(uuid.uuid4())
    now = _now()
    db.conn().execute(
        "INSERT INTO dashboards (id, title, question_ids, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        [did, title, json.dumps(question_ids or []), now, now],
    )
    return get_dashboard(db, did)


def list_dashboards(db) -> list[dict]:
    """Return all dashboards ordered by created_at ASC."""
    rows = db.conn().execute(_SELECT_COLS + " ORDER BY created_at ASC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_dashboard(db, dashboard_id: str) -> dict | None:
    """Return one dashboard dict or None."""
    row = db.conn().execute(
        _SELECT_COLS + " WHERE id=?", [dashboard_id]
    ).fetchone()
    return _row_to_dict(row) if row else None


def update_dashboard(db, dashboard_id: str, *, title: str = None,
                     question_ids: list[str] = None) -> dict | None:
    """Update mutable fields. Returns updated dict or None if not found."""
    d = get_dashboard(db, dashboard_id)
    if d is None:
        return None
    new_title = title        if title        is not None else d["title"]
    new_qids  = question_ids if question_ids is not None else d["question_ids"]
    db.conn().execute(
        "UPDATE dashboards SET title=?, question_ids=?, updated_at=? WHERE id=?",
        [new_title, json.dumps(new_qids), _now(), dashboard_id],
    )
    return get_dashboard(db, dashboard_id)


def delete_dashboard(db, dashboard_id: str) -> bool:
    """Delete dashboard. Returns True if deleted, False if not found."""
    if get_dashboard(db, dashboard_id) is None:
        return False
    db.conn().execute("DELETE FROM dashboards WHERE id=?", [dashboard_id])
    return True


def seed_builtin_dashboards(db) -> int:
    """Insert a default 'Overview' dashboard wiring the 3 builtin questions.

    Returns the number of dashboards actually inserted (0 if already seeded).
    """
    BUILTINS = [
        {
            "dashboard_id": "builtin-overview",
            "title":        "Overview",
            "question_ids": [
                "builtin-category-breakdown",
                "builtin-top-bottleneck-dcs",
                "builtin-label-quality",
            ],
        },
    ]
    inserted = 0
    for d in BUILTINS:
        if get_dashboard(db, d["dashboard_id"]) is None:
            create_dashboard(db, **d)
            inserted += 1
    return inserted
