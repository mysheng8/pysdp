"""routes/chat.py — Chat AI SSE streaming endpoint."""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel


router = APIRouter()


class ChatRequest(BaseModel):
    messages: list[dict]
    snapshot_ids: list[int] = []
    skill_id: str | None = None
    skill_params: dict | None = None


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

    messages = body.messages
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
