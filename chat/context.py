"""chat/context.py — Layered-reserve context assembler for chat sessions."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chat.sessions import Session

RECENCY_WINDOW_K = 3
DEFAULT_BUDGET = 12000


def estimate_tokens(text: str) -> int:
    return len(text) // 3


# ── Round extraction ───────────────────────────────────────────────────────────


def _extract_rounds(session: "Session") -> list[dict]:
    """Extract rounds from the active path.

    A round = user msg → its tool pairs → closing assistant text.
    Returns list of dicts ordered root→leaf:
        {"user_id": str, "msg_ids": [str...], "messages": [dict...],
         "has_aspect": bool, "aspect_msg_ids": [str...]}
    """
    from chat.sessions import get_path, extract_answer_recap

    path_ids = get_path(session)
    if not path_ids:
        return []

    rounds: list[dict] = []
    current_round: dict | None = None

    for mid in path_ids:
        msg = session.messages.get(mid)
        if not msg:
            continue
        role = msg.get("role", "")
        mtype = msg.get("type", "")

        if role == "user" and mtype == "text":
            if current_round is not None:
                rounds.append(current_round)
            current_round = {
                "user_id": mid,
                "msg_ids": [mid],
                "messages": [msg],
                "has_aspect": False,
                "aspect_msg_ids": [],
            }
        elif current_round is not None:
            current_round["msg_ids"].append(mid)
            current_round["messages"].append(msg)
            if role == "tool" and mtype == "tool_result":
                meta = msg.get("metadata", {})
                tool_name = meta.get("tool_name", "")
                if tool_name in ("fetch_aspect", "compare_snapshots"):
                    current_round["has_aspect"] = True
                    current_round["aspect_msg_ids"].append(mid)

    if current_round is not None:
        rounds.append(current_round)

    return rounds


def _round_to_openai(rnd: dict) -> list[dict]:
    """Convert a round's messages to OpenAI chat format."""
    out = []
    for msg in rnd["messages"]:
        role = msg.get("role", "user")
        mtype = msg.get("type", "")
        content = msg.get("content", "")

        if role == "user":
            out.append({"role": "user", "content": content})
        elif role == "assistant" and mtype == "text":
            out.append({"role": "assistant", "content": content})
        elif role == "assistant" and mtype == "tool_call":
            meta = msg.get("metadata", {})
            tool_calls = meta.get("tool_calls", [])
            out.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
        elif role == "tool" and mtype == "tool_result":
            meta = msg.get("metadata", {})
            out.append({
                "role": "tool",
                "tool_call_id": meta.get("tool_call_id", ""),
                "content": content,
            })

    return out


def _demote_round(rnd: dict) -> list[dict]:
    """Demote a round to just user question + assistant conclusion (answer/recap)."""
    from chat.sessions import extract_answer_recap

    out = []
    for msg in rnd["messages"]:
        role = msg.get("role", "")
        mtype = msg.get("type", "")
        content = msg.get("content", "")

        if role == "user" and mtype == "text":
            out.append({"role": "user", "content": content})
        elif role == "assistant" and mtype == "text":
            answer, recap = extract_answer_recap(content)
            out.append({"role": "assistant", "content": f"【answer】{answer}\n【recap】{recap}"})
        elif role == "tool" and mtype == "tool_result":
            meta = msg.get("metadata", {})
            tool_name = meta.get("tool_name", "")
            if tool_name in ("fetch_aspect", "compare_snapshots"):
                tool_call_id = meta.get("tool_call_id", "")
                out.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content,
                })
        elif role == "assistant" and mtype == "tool_call":
            meta = msg.get("metadata", {})
            tool_calls = meta.get("tool_calls", [])
            has_aspect_call = any(
                tc.get("function", {}).get("name") in ("fetch_aspect", "compare_snapshots")
                for tc in tool_calls
            )
            if has_aspect_call:
                aspect_calls = [
                    tc for tc in tool_calls
                    if tc.get("function", {}).get("name") in ("fetch_aspect", "compare_snapshots")
                ]
                out.append({"role": "assistant", "content": None, "tool_calls": aspect_calls})

    return out


def _msgs_tokens(msgs: list[dict]) -> int:
    total = 0
    for m in msgs:
        c = m.get("content") or ""
        total += estimate_tokens(c)
        for tc in m.get("tool_calls", []):
            total += estimate_tokens(tc.get("function", {}).get("arguments", ""))
    return total


# ── Main assembler ─────────────────────────────────────────────────────────────


def assemble_context(
    session: "Session",
    system_prompt: str,
    budget: int = DEFAULT_BUDGET,
) -> tuple[list[dict], list[dict], set[str]]:
    """Assemble context messages within token budget.

    Returns:
        (messages, surviving_aspects, excluded_ids)
        - messages: list of OpenAI-format message dicts (no system — caller prepends)
        - surviving_aspects: list of {"snapshot_id", "aspect", "msg_id"} that survived
        - excluded_ids: set of user_id for rounds that were fully dropped
    """
    rounds = _extract_rounds(session)
    if not rounds:
        return [], [], set()

    # Layer 0: anchor — system prompt + current user question (last round's user msg)
    system_tokens = estimate_tokens(system_prompt)
    last_round = rounds[-1]
    last_user_msg = {"role": "user", "content": last_round["messages"][0].get("content", "")}
    anchor_tokens = system_tokens + estimate_tokens(last_user_msg["content"])

    remaining = budget - anchor_tokens

    # Layer 1: aspect reserve — all rounds with aspect data (tool pairs kept intact)
    aspect_rounds_idx: list[int] = []
    aspect_messages: dict[int, list[dict]] = {}
    aspect_tokens = 0

    for i, rnd in enumerate(rounds[:-1]):
        if rnd["has_aspect"]:
            msgs = _round_to_openai(rnd)
            t = _msgs_tokens(msgs)
            aspect_rounds_idx.append(i)
            aspect_messages[i] = msgs
            aspect_tokens += t

    remaining -= aspect_tokens

    # Layer 2: recency window — last K rounds (excluding the final user-only round)
    # The last round is just the user question (anchor), so recency = rounds[-K-1:-1]
    non_aspect_non_last = [
        i for i in range(len(rounds) - 1) if i not in aspect_rounds_idx
    ]
    recency_start = max(0, len(non_aspect_non_last) - RECENCY_WINDOW_K)
    recency_idx = non_aspect_non_last[recency_start:]
    older_idx = non_aspect_non_last[:recency_start]

    recency_messages: dict[int, list[dict]] = {}
    recency_tokens = 0
    for i in recency_idx:
        msgs = _round_to_openai(rounds[i])
        t = _msgs_tokens(msgs)
        recency_messages[i] = msgs
        recency_tokens += t

    remaining -= recency_tokens

    # Layer 3: older rounds demoted (answer/recap + aspect tool pairs only)
    demoted_messages: dict[int, list[dict]] = {}
    demoted_tokens: dict[int, int] = {}
    for i in older_idx:
        msgs = _demote_round(rounds[i])
        t = _msgs_tokens(msgs)
        demoted_messages[i] = msgs
        demoted_tokens[i] = t

    # Layer 4: trim oldest demoted rounds if over budget
    excluded_ids: set[str] = set()
    total_demoted = sum(demoted_tokens.values())

    if total_demoted > remaining:
        for i in older_idx:
            if total_demoted <= remaining:
                break
            total_demoted -= demoted_tokens[i]
            del demoted_messages[i]
            excluded_ids.add(rounds[i]["user_id"])

    # Assemble final message list in chronological order
    final_messages: list[dict] = []
    for i in range(len(rounds) - 1):
        if i in aspect_messages:
            final_messages.extend(aspect_messages[i])
        elif i in recency_messages:
            final_messages.extend(recency_messages[i])
        elif i in demoted_messages:
            final_messages.extend(demoted_messages[i])

    # Append the current user question (anchor)
    final_messages.append(last_user_msg)

    # Compute surviving_aspects
    surviving_aspects: list[dict] = []
    for i in aspect_rounds_idx:
        rnd = rounds[i]
        for mid in rnd["aspect_msg_ids"]:
            msg = session.messages.get(mid)
            if msg:
                meta = msg.get("metadata", {})
                surviving_aspects.append({
                    "snapshot_id": meta.get("snapshot_id"),
                    "aspect": meta.get("aspect") or meta.get("tool_name"),
                    "msg_id": mid,
                })

    return final_messages, surviving_aspects, excluded_ids
