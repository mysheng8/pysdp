"""questions.py — CRUD for the questions table."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row, cols) -> dict:
    d = dict(zip(cols, row))
    # Deserialize JSON fields
    d["model_params"] = json.loads(d.get("model_params") or "{}")
    d["viz_config"]   = json.loads(d.get("viz_config")   or "{}")
    # Normalize created_at to string (DuckDB may return datetime objects)
    if "created_at" in d and d["created_at"] is not None:
        d["created_at"] = str(d["created_at"])
    return d


def create_question(db, *, title: str, model_name: str, model_params: dict = None,
                    viz_type: str = "table", viz_config: dict = None,
                    is_builtin: bool = False, question_id: str = None) -> dict:
    """Insert a new question row. Returns the created question dict."""
    qid         = question_id or str(uuid.uuid4())
    params_json = json.dumps(model_params or {})
    config_json = json.dumps(viz_config   or {})
    created_at  = _now()
    db.conn().execute(
        """INSERT INTO questions (id, title, model_name, model_params, viz_type, viz_config, is_builtin, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [qid, title, model_name, params_json, viz_type, config_json, is_builtin, created_at],
    )
    return get_question(db, qid)


_SELECT_COLS = (
    "id, title, model_name, model_params, viz_type, viz_config, is_builtin,"
    " CAST(created_at AS VARCHAR) AS created_at"
)


def list_questions(db) -> list:
    """Return all questions ordered by created_at ASC."""
    result = db.conn().execute(
        f"SELECT {_SELECT_COLS} FROM questions ORDER BY created_at ASC"
    )
    cols = [d[0] for d in result.description]
    return [_row_to_dict(r, cols) for r in result.fetchall()]


def get_question(db, question_id: str):
    """Return one question dict or None."""
    result = db.conn().execute(
        f"SELECT {_SELECT_COLS} FROM questions WHERE id=?",
        [question_id],
    )
    cols = [d[0] for d in result.description]
    row  = result.fetchone()
    return _row_to_dict(row, cols) if row else None


def update_question(db, question_id: str, *, title=None, model_params=None,
                    viz_type=None, viz_config=None):
    """Update mutable fields. Returns updated dict or None if not found."""
    q = get_question(db, question_id)
    if q is None:
        return None
    new_title      = title        if title        is not None else q["title"]
    new_params     = model_params if model_params is not None else q["model_params"]
    new_viz_type   = viz_type     if viz_type     is not None else q["viz_type"]
    new_viz_config = viz_config   if viz_config   is not None else q["viz_config"]
    db.conn().execute(
        "UPDATE questions SET title=?, model_params=?, viz_type=?, viz_config=? WHERE id=?",
        [new_title, json.dumps(new_params), new_viz_type, json.dumps(new_viz_config), question_id],
    )
    return get_question(db, question_id)


def delete_question(db, question_id: str) -> bool:
    """Delete question. Returns True if deleted, False if not found."""
    q = get_question(db, question_id)
    if q is None:
        return False
    db.conn().execute("DELETE FROM questions WHERE id=?", [question_id])
    return True


def seed_builtin_questions(db) -> int:
    """Insert built-in questions if they don't already exist. Returns count inserted."""
    BUILTINS = [
        {
            "question_id": "builtin-category-breakdown",
            "title":        "Category Breakdown",
            "model_name":   "category_breakdown",
            "model_params": {},
            "viz_type":     "bar_chart",
            "viz_config":   {"x": "category", "y": "clocks_pct", "label": "dc_count"},
            "is_builtin":   True,
        },
        {
            "question_id": "builtin-top-bottleneck-dcs",
            "title":        "Top Bottleneck Draw Calls",
            "model_name":   "top_bottleneck_dcs",
            "model_params": {"top_n": 20},
            "viz_type":     "table",
            "viz_config":   {},
            "is_builtin":   True,
        },
        {
            "question_id": "builtin-label-quality",
            "title":        "Label Quality",
            "model_name":   "label_quality",
            "model_params": {},
            "viz_type":     "bar_chart",
            "viz_config":   {"x": "tag", "y": "count"},
            "is_builtin":   True,
        },
    ]
    inserted = 0
    for q in BUILTINS:
        existing = get_question(db, q["question_id"])
        if existing is None:
            create_question(db, **q)
            inserted += 1
    return inserted
