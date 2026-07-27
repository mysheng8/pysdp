"""chat/llm_client.py — Async streaming LLM client with tool-use loop."""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, TYPE_CHECKING

import litellm

from chat.prompts import build_system_prompt
from chat.tools import TOOL_DEFINITIONS, ToolExecutor

if TYPE_CHECKING:
    from chat.sessions import Session


@dataclass
class SSEEvent:
    type: str   # token, tool_call, tool_result, done, error
    data: dict


# ── Persistence policy ─────────────────────────────────────────────────────────

PERSIST_FULL = "full"
PERSIST_REF = "ref"
CACHE_ONLY = "cache"


def classify_result(name: str, args: dict, result: dict | str, duration_ms: int) -> str:
    if name == "fetch_aspect":
        return PERSIST_FULL
    if name == "compare_snapshots":
        return PERSIST_FULL
    if name == "save_report":
        return PERSIST_REF
    if name == "execute_python":
        if isinstance(result, dict) and (result.get("image_paths") or duration_ms > 1500):
            return PERSIST_FULL
        return CACHE_ONLY
    if name == "recall_history":
        return CACHE_ONLY
    return CACHE_ONLY


_client: ChatLlmClient | None = None


class ChatLlmClient:
    def __init__(self, cfg: dict):
        self.endpoint = cfg.get("ChatApiEndpoint", "")
        self.api_key = cfg.get("ChatApiKey", "")
        self._raw_model = cfg.get("ChatModel", "")
        self.max_tokens = int(cfg.get("ChatMaxTokens", "8192"))
        self.timeout = int(cfg.get("ChatTimeoutSeconds", "120"))
        self.executor = ToolExecutor()

    @property
    def is_enabled(self) -> bool:
        return bool(self.endpoint and self.api_key and self._raw_model)

    @property
    def model_name(self) -> str:
        return self._raw_model

    @property
    def model(self) -> str:
        # Always use openai/ provider (custom gateway), pass raw model name to the gateway
        m = self._raw_model
        if m and not m.startswith("openai/"):
            return f"openai/{m}"
        return m

    @property
    def _api_base(self) -> str:
        ep = self.endpoint
        if ep.endswith("/chat/completions"):
            ep = ep[: -len("/chat/completions")]
        return ep

    async def stream_chat(
        self,
        messages: list[dict],
        snapshot_ids: list[int],
        skill_context: str | None = None,
    ) -> AsyncGenerator[SSEEvent, None]:
        from chat.prompts import _detect_language

        self.executor.set_snapshot_ids(snapshot_ids)
        last_user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        user_lang = _detect_language(last_user_msg)
        system_prompt = build_system_prompt(snapshot_ids, user_lang=user_lang)
        if skill_context:
            system_prompt += f"\n\n## Current Skill Task\n{skill_context}"

        full_messages = [{"role": "system", "content": system_prompt}] + messages

        consecutive_errors = 0
        emitted_final = False
        for _iteration in range(6):
            try:
                response = await litellm.acompletion(
                    model=self.model,
                    messages=full_messages,
                    tools=TOOL_DEFINITIONS,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                    stream=False,
                    api_base=self._api_base,
                    api_key=self.api_key,
                )
            except Exception as e:
                yield SSEEvent("error", {"message": f"LLM call failed: {e}"})
                return

            choice = response.choices[0] if response.choices else None
            if not choice:
                print(f"[LLM] iter={_iteration} no choice returned")
                break

            msg = choice.message
            content = msg.content or ""
            finish_reason = choice.finish_reason
            tool_names = [tc.function.name for tc in (msg.tool_calls or [])]
            print(f"[LLM] iter={_iteration} finish={finish_reason} content_len={len(content)} tools={tool_names}")

            if not msg.tool_calls:
                if content:
                    yield SSEEvent("token", {"content": content})
                emitted_final = True
                break

            # Has tool calls — execute them, don't emit intermediate text
            assistant_msg = {"role": "assistant", "content": content or None, "tool_calls": []}
            for tc in msg.tool_calls:
                assistant_msg["tool_calls"].append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                })
            full_messages.append(assistant_msg)

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                yield SSEEvent("tool_call", {"name": name, "args": args})

                t0 = time.time()
                try:
                    result = await self.executor.execute(name, args)
                except Exception as e:
                    result = {"error": str(e)}
                duration_ms = int((time.time() - t0) * 1000)

                images = result.pop("images", []) if isinstance(result, dict) else []

                yield SSEEvent("tool_result", {
                    "name": name,
                    "result": result,
                    "duration_ms": duration_ms,
                    "images": images,
                })

                if isinstance(result, dict) and result.get("error"):
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str, ensure_ascii=False),
                })

            if consecutive_errors >= 2:
                yield SSEEvent("token", {"content": "\n\n_Tool execution failed after multiple attempts._"})
                break

        # Final streaming response after tool results (only if loop didn't already emit text)
        if not emitted_final and full_messages[-1].get("role") == "tool":
            ctx_size = sum(len(m.get("content", "") or "") for m in full_messages)
            print(f"[LLM] post-loop streaming call, context_size={ctx_size} chars, msg_count={len(full_messages)}")
            try:
                stream_resp = await litellm.acompletion(
                    model=self.model,
                    messages=full_messages,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                    stream=True,
                    api_base=self._api_base,
                    api_key=self.api_key,
                )
                async for chunk in stream_resp:
                    ch = chunk.choices[0] if chunk.choices else None
                    if ch and ch.delta and ch.delta.content:
                        yield SSEEvent("token", {"content": ch.delta.content})
            except Exception as e:
                print(f"[LLM] post-loop streaming failed: {e}")

        yield SSEEvent("done", {})

    # ── Session-backed flow ────────────────────────────────────────────────────

    async def stream_chat_session(
        self,
        session: "Session",
        new_user_text: str,
        snapshot_ids: list[int],
        skill_context: str | None = None,
    ) -> AsyncGenerator[SSEEvent, None]:
        from chat.prompts import _detect_language
        from chat.sessions import (
            add_message, save_session, auto_title, attachment_dir,
            extract_answer_recap,
        )
        from chat.context import assemble_context

        self.executor.set_snapshot_ids(snapshot_ids)

        # 1. Persist user turn
        add_message(session, "user", new_user_text, session.active_leaf)
        auto_title(session)

        # 2. Build system prompt → assemble context → inject already-fetched
        user_lang = _detect_language(new_user_text)
        base_prompt = build_system_prompt(snapshot_ids, user_lang=user_lang, session=session)
        if skill_context:
            base_prompt += f"\n\n## Current Skill Task\n{skill_context}"

        ctx_messages, surviving_aspects, excluded_ids = assemble_context(
            session, base_prompt
        )

        full_messages = [{"role": "system", "content": base_prompt}] + ctx_messages

        self.executor.set_session_context(session, excluded_ids)

        # 3. Tool-use loop with persistence
        consecutive_errors = 0
        emitted_final = False
        accumulated_text = ""

        for _iteration in range(6):
            try:
                response = await litellm.acompletion(
                    model=self.model,
                    messages=full_messages,
                    tools=TOOL_DEFINITIONS,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                    stream=False,
                    api_base=self._api_base,
                    api_key=self.api_key,
                )
            except Exception as e:
                yield SSEEvent("error", {"message": f"LLM call failed: {e}"})
                save_session(session)
                return

            choice = response.choices[0] if response.choices else None
            if not choice:
                break

            msg = choice.message
            content = msg.content or ""
            tool_names = [tc.function.name for tc in (msg.tool_calls or [])]
            print(f"[LLM-session] iter={_iteration} finish={choice.finish_reason} tools={tool_names}")

            if not msg.tool_calls:
                if content:
                    yield SSEEvent("token", {"content": content})
                    accumulated_text += content
                emitted_final = True
                break

            # Build assistant tool_call message
            tc_list = []
            for tc in msg.tool_calls:
                tc_list.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                })
            assistant_msg = {"role": "assistant", "content": content or None, "tool_calls": tc_list}
            full_messages.append(assistant_msg)

            # Track which tool_calls need persisting (lazy pairing)
            persist_tool_call = False

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                yield SSEEvent("tool_call", {"name": name, "args": args})

                t0 = time.time()
                try:
                    result = await self.executor.execute(name, args)
                except Exception as e:
                    result = {"error": str(e)}
                duration_ms = int((time.time() - t0) * 1000)

                images = result.pop("images", []) if isinstance(result, dict) else []

                yield SSEEvent("tool_result", {
                    "name": name,
                    "result": result,
                    "duration_ms": duration_ms,
                    "images": images,
                })

                if isinstance(result, dict) and result.get("error"):
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

                result_json = json.dumps(result, default=str, ensure_ascii=False)
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_json,
                })

                # Persistence decision
                policy = classify_result(name, args, result, duration_ms)
                if policy in (PERSIST_FULL, PERSIST_REF):
                    persist_tool_call = True
                    # Persist tool_result
                    meta = {"tool_call_id": tc.id, "tool_name": name}
                    if name == "fetch_aspect":
                        meta["snapshot_id"] = args.get("snapshot_id")
                        meta["aspect"] = args.get("aspect")
                    tr_msg = add_message(
                        session, "tool", result_json, session.active_leaf,
                        msg_type="tool_result", metadata=meta,
                    )
                    # Copy chart attachments
                    if images:
                        att_dir = attachment_dir(session.id)
                        for i, img_path in enumerate(images):
                            src = Path(img_path)
                            if src.exists():
                                dst = att_dir / f"{tr_msg.id}_chart_{i}.png"
                                shutil.copy2(src, dst)

            # Lazy pairing: persist tool_call message if any result was PERSIST_*
            if persist_tool_call:
                add_message(
                    session, "assistant", "", session.active_leaf,
                    msg_type="tool_call",
                    metadata={"tool_calls": tc_list},
                )

            if consecutive_errors >= 2:
                accumulated_text += "\n\n_Tool execution failed after multiple attempts._"
                yield SSEEvent("token", {"content": "\n\n_Tool execution failed after multiple attempts._"})
                break

        # Post-loop streaming if needed
        if not emitted_final and full_messages[-1].get("role") == "tool":
            try:
                stream_resp = await litellm.acompletion(
                    model=self.model,
                    messages=full_messages,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                    stream=True,
                    api_base=self._api_base,
                    api_key=self.api_key,
                )
                async for chunk in stream_resp:
                    ch = chunk.choices[0] if chunk.choices else None
                    if ch and ch.delta and ch.delta.content:
                        yield SSEEvent("token", {"content": ch.delta.content})
                        accumulated_text += ch.delta.content
            except Exception as e:
                print(f"[LLM-session] post-loop streaming failed: {e}")

        # 4. Persist final assistant text
        if accumulated_text.strip():
            answer, recap = extract_answer_recap(accumulated_text)
            add_message(
                session, "assistant", accumulated_text, session.active_leaf,
                msg_type="text",
                metadata={"answer": answer, "recap": recap},
            )

        # 5. Save session
        save_session(session)

        # 6. Done event with session_id
        yield SSEEvent("done", {"session_id": session.id})


def get_client() -> ChatLlmClient:
    global _client
    if _client is None:
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        from analysis.llm_wrapper import _load_config
        cfg = _load_config()
        _client = ChatLlmClient(cfg)
    return _client
