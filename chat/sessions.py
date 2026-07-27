"""chat/sessions.py — Session CRUD and tree-based message persistence."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _session_root() -> Path:
    from config import get_settings
    cfg = get_settings()
    project = cfg.get("ProjectDir") or str(
        Path(cfg.get("WorkingDirectory", "")) / "project"
    )
    return Path(project) / "chat" / "sessions"


@dataclass
class Message:
    id: str
    parent: str | None
    role: str  # "user" | "assistant" | "tool"
    type: str  # "text" | "tool_call" | "tool_result"
    content: str
    timestamp: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Session:
    id: str
    title: str
    created_at: str
    updated_at: str
    pinned_snapshot_ids: list[int] = field(default_factory=list)
    active_leaf: str | None = None
    messages: dict[str, dict] = field(default_factory=dict)
    _msg_counter: int = field(default=0, repr=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _gen_session_id() -> str:
    ts = int(time.time())
    rand4 = os.urandom(2).hex()
    return f"s_{ts}_{rand4}"


def _next_msg_id(session: Session) -> str:
    session._msg_counter += 1
    return f"msg_{session._msg_counter:03d}"


# ── CRUD ────────────────────────────────────────────────────────────────────────


def create_session() -> Session:
    sid = _gen_session_id()
    now = _now_iso()
    session = Session(
        id=sid,
        title="New Chat",
        created_at=now,
        updated_at=now,
    )
    session_dir = _session_root() / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    save_session(session)
    return session


def load_session(session_id: str) -> Session | None:
    path = _session_root() / session_id / "session.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    session = Session(
        id=data["id"],
        title=data.get("title", "New Chat"),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        pinned_snapshot_ids=data.get("pinned_snapshot_ids", []),
        active_leaf=data.get("active_leaf"),
        messages=data.get("messages", {}),
    )
    if session.messages:
        max_num = 0
        for mid in session.messages:
            m = re.match(r"msg_(\d+)", mid)
            if m:
                max_num = max(max_num, int(m.group(1)))
        session._msg_counter = max_num
    return session


def save_session(session: Session):
    session.updated_at = _now_iso()
    session_dir = _session_root() / session.id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "session.json"
    tmp = path.with_suffix(".tmp")
    data = {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "pinned_snapshot_ids": session.pinned_snapshot_ids,
        "active_leaf": session.active_leaf,
        "messages": session.messages,
    }
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_sessions() -> list[dict]:
    root = _session_root()
    if not root.exists():
        return []
    sessions = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "session.json"
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            sessions.append({
                "id": data["id"],
                "title": data.get("title", "New Chat"),
                "updated_at": data.get("updated_at", ""),
                "pinned_snapshot_ids": data.get("pinned_snapshot_ids", []),
            })
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions


def delete_session(session_id: str):
    session_dir = _session_root() / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


# ── Message operations ──────────────────────────────────────────────────────────


def add_message(
    session: Session,
    role: str,
    content: str,
    parent_id: str | None,
    msg_type: str = "text",
    metadata: dict | None = None,
) -> Message:
    msg_id = _next_msg_id(session)
    now = _now_iso()
    msg = Message(
        id=msg_id,
        parent=parent_id,
        role=role,
        type=msg_type,
        content=content,
        timestamp=now,
        metadata=metadata or {},
    )
    session.messages[msg_id] = {
        "id": msg_id,
        "parent": msg.parent,
        "role": msg.role,
        "type": msg.type,
        "content": msg.content,
        "timestamp": msg.timestamp,
        "metadata": msg.metadata,
    }
    session.active_leaf = msg_id
    return msg


def attachment_dir(session_id: str) -> Path:
    d = _session_root() / session_id / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Tree traversal ──────────────────────────────────────────────────────────────


def get_path(session: Session, leaf_id: str | None = None) -> list[str]:
    """Trace from leaf to root, return ordered [root, ..., leaf]."""
    if leaf_id is None:
        leaf_id = session.active_leaf
    if not leaf_id or leaf_id not in session.messages:
        return []
    path = []
    current = leaf_id
    while current is not None:
        path.append(current)
        msg_data = session.messages.get(current)
        if msg_data is None:
            break
        current = msg_data.get("parent")
    path.reverse()
    return path


def get_fetched_aspects(session: Session) -> list[dict]:
    """Scan tool_result messages on active path for fetch_aspect calls."""
    path_ids = get_path(session)
    aspects = []
    for mid in path_ids:
        msg = session.messages.get(mid)
        if not msg:
            continue
        if msg.get("role") == "tool" and msg.get("type") == "tool_result":
            meta = msg.get("metadata", {})
            if meta.get("tool_name") == "fetch_aspect":
                aspects.append({
                    "snapshot_id": meta.get("snapshot_id"),
                    "aspect": meta.get("aspect"),
                    "msg_id": mid,
                })
    return aspects


# ── Utilities ───────────────────────────────────────────────────────────────────


def auto_title(session: Session):
    """Set title from first user message if still default."""
    if session.title != "New Chat":
        return
    path_ids = get_path(session)
    for mid in path_ids:
        msg = session.messages.get(mid)
        if msg and msg.get("role") == "user" and msg.get("type") == "text":
            raw = msg["content"].strip()
            raw = re.sub(r"[^\w\s一-鿿぀-ゟ゠-ヿ]", "", raw)
            session.title = raw[:40].strip() or "New Chat"
            return


def extract_answer_recap(text: str) -> tuple[str, str]:
    """Extract 【answer】 and 【recap】 lines from assistant text.

    Returns (answer, recap). Falls back to truncated text if markers absent.
    """
    answer = ""
    recap = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("【answer】"):
            answer = stripped[len("【answer】"):].strip()
        elif stripped.startswith("【recap】"):
            recap = stripped[len("【recap】"):].strip()
    if not answer:
        answer = text.strip()[:120]
    if not recap:
        recap = text.strip()[:120]
    return answer, recap
