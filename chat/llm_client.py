"""chat/llm_client.py — Async streaming LLM client with tool-use loop."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import AsyncGenerator

import litellm

from chat.prompts import build_system_prompt
from chat.tools import TOOL_DEFINITIONS, ToolExecutor


@dataclass
class SSEEvent:
    type: str   # token, tool_call, tool_result, done, error
    data: dict


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


def get_client() -> ChatLlmClient:
    global _client
    if _client is None:
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        from analysis.llm_wrapper import _load_config
        cfg = _load_config()
        _client = ChatLlmClient(cfg)
    return _client
