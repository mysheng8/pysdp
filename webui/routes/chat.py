"""routes/chat.py — Chat AI SSE streaming endpoint + session management."""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel


router = APIRouter()


# ── Request models ─────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    messages: list[dict] = []
    message: str | None = None
    session_id: str | None = None
    snapshot_ids: list[int] = []
    skill_id: str | None = None
    skill_params: dict | None = None


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    pinned_snapshot_ids: list[int] | None = None


# ── Session CRUD ───────────────────────────────────────────────────────────────


@router.get("/sessions")
def list_sessions_route():
    from chat.sessions import list_sessions
    return {"sessions": list_sessions()}


@router.post("/sessions")
def create_session_route():
    from chat.sessions import create_session
    session = create_session()
    return {"id": session.id, "title": session.title, "created_at": session.created_at}


@router.get("/sessions/{session_id}")
def get_session_route(session_id: str):
    from chat.sessions import load_session, get_path
    session = load_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    path_ids = get_path(session)
    messages = [session.messages[mid] for mid in path_ids if mid in session.messages]
    return {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "pinned_snapshot_ids": session.pinned_snapshot_ids,
        "active_leaf": session.active_leaf,
        "messages": messages,
    }


@router.delete("/sessions/{session_id}")
def delete_session_route(session_id: str):
    from chat.sessions import delete_session
    delete_session(session_id)
    return {"ok": True}


@router.patch("/sessions/{session_id}")
def update_session_route(session_id: str, body: SessionUpdateRequest):
    from chat.sessions import load_session, save_session
    session = load_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if body.title is not None:
        session.title = body.title
    if body.pinned_snapshot_ids is not None:
        session.pinned_snapshot_ids = body.pinned_snapshot_ids
    save_session(session)
    return {"ok": True, "title": session.title, "pinned_snapshot_ids": session.pinned_snapshot_ids}


# ── Status & skills ────────────────────────────────────────────────────────────


@router.get("/status")
def chat_status():
    from chat.llm_client import get_client
    client = get_client()
    return {"ok": True, "enabled": client.is_enabled, "model": client.model_name}


@router.get("/skills")
def chat_skills():
    from chat.skills import get_skills
    skills = get_skills()
    return {"skills": [s.to_dict() for s in skills]}


# ── Chat streaming ─────────────────────────────────────────────────────────────


@router.post("")
async def chat_stream(body: ChatRequest):
    from chat.llm_client import get_client
    from chat.skills import get_skill_by_id, execute_skill
    client = get_client()
    if not client.is_enabled:
        return JSONResponse(
            {"ok": False, "error": "Chat not configured — set ChatApiEndpoint/Key/Model in secrets.ini"},
            status_code=503,
        )

    skill_context = None
    if body.skill_id:
        skill = get_skill_by_id(body.skill_id)
        if skill:
            skill_result = await execute_skill(skill, body.snapshot_ids, body.skill_params)
            filled_prompt = skill.fill_template(body.snapshot_ids, body.skill_params)
            user_prompt = (body.skill_params or {}).get("user_prompt", "")
            if user_prompt:
                filled_prompt += f"\n\nUser's additional request: {user_prompt}"
            if skill_result:
                filled_prompt += f"\n\nHere is pre-computed data:\n```json\n{json.dumps(skill_result, default=str, ensure_ascii=False)}\n```\nInterpret and explain this data to the user."
            skill_context = filled_prompt

    # New session-based flow
    if body.message is not None:
        from chat.sessions import load_session, create_session

        if body.session_id:
            session = load_session(body.session_id)
            if not session:
                return JSONResponse({"error": "Session not found"}, status_code=404)
        else:
            session = create_session()

        async def generate_session():
            try:
                event_count = 0
                async for event in client.stream_chat_session(
                    session, body.message, body.snapshot_ids, skill_context=skill_context
                ):
                    event_count += 1
                    payload = json.dumps(event.data, default=str, ensure_ascii=False)
                    if event.type == 'tool_result':
                        img_count = len(event.data.get('images', []))
                        print(f"[CHAT SSE] #{event_count} type={event.type} tool={event.data.get('name')} images={img_count}")
                    elif event.type != 'token':
                        print(f"[CHAT SSE] #{event_count} type={event.type}")
                    yield f"event: {event.type}\ndata: {payload}\n\n"
                print(f"[CHAT SSE] Session stream complete, {event_count} events")
            except Exception as e:
                print(f"[CHAT SSE] ERROR: {e}")
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

        return StreamingResponse(
            generate_session(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Legacy flow (messages array)
    messages = body.messages

    async def generate():
        try:
            event_count = 0
            async for event in client.stream_chat(messages, body.snapshot_ids, skill_context=skill_context):
                event_count += 1
                payload = json.dumps(event.data, default=str, ensure_ascii=False)
                if event.type == 'tool_result':
                    img_count = len(event.data.get('images', []))
                    print(f"[CHAT SSE] #{event_count} type={event.type} tool={event.data.get('name')} images={img_count} payload_len={len(payload)}")
                elif event.type != 'token':
                    print(f"[CHAT SSE] #{event_count} type={event.type} payload_len={len(payload)}")
                yield f"event: {event.type}\ndata: {payload}\n\n"
            print(f"[CHAT SSE] Stream complete, {event_count} events total")
        except Exception as e:
            print(f"[CHAT SSE] ERROR: {e}")
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
