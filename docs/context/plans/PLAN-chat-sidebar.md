# Plan: AI Chat Sidebar for pySdp WebUI

**Status:** Phases 1-4 Complete / Phase 5 extracted to [PLAN-chat-session.md](PLAN-chat-session.md)
**Date:** 2026-05-14
**Updated:** 2026-07-02 (Phase 5 extracted to standalone plan)

---

## Status

| Phase | Steps | Status |
|-------|-------|--------|
| Phase 1: Minimum Viable Chat | Steps 1-6 | ✅ DONE |
| Phase 2: Skills + Snapshot Pinning | Steps 7-10 | ✅ DONE |
| Phase 3: Code Execution + Skill Authoring | `execute_python`, `create_skill` | ✅ DONE |
| Phase 4: Report Generation + Data Visualization | `save_report`, matplotlib charts | ✅ DONE |
| Phase 5: Session Management + History | → [PLAN-chat-session.md](PLAN-chat-session.md) | ❌ TODO (extracted) |

**Phase 5 dependency order**: Step 1 (sessions.py) → Step 2/2b (tools + file endpoint) → Step 3 (prompts) → Step 4 (context.py) → **Step 4b (refactor `stream_chat` — the integration spine that wires everything together)** → Step 5 (routes) → Steps 6-8 (frontend) → Step 9 (optional search). Step 4b is where sessions.py + context.py actually get used and tool results get persisted; it's the highest-risk step.

**Implementation records**: IMPL-2026-05-16-ai-chat-sidebar.md, IMPL-2026-05-16-chat-phase2-skills-pinning.md, IMPL-2026-05-16-chat-phase3-code-execution.md

---

## Context

The pySdp WebUI currently provides visual exploration of GPU profiling data (draw calls, metrics, correlations), but users cannot ask freeform questions or run comparative analysis across snapshots without manually navigating between views. Adding an AI chat panel allows natural-language queries like "where's the bottleneck?" or "compare character triangle counts between these two snapshots" — backed by our existing DuckDB data layer and analysis models, accessed via tool calls.

## Design Decisions

- **LLM**: Separate `ChatApiEndpoint`/`ChatApiKey`/`ChatModel` config (distinct from label/analysis LLM)
- **Streaming**: SSE token-by-token via `fetch()` + `ReadableStream`
- **Snapshot context**: Auto-attach current Explorer snapshot; user can pin additional snapshots (like GitHub Copilot pinned files)
- **Skills**: Both quick-action buttons and `/slash` commands; defined as `.md` files (like Claude Code skills), not Python code
- **Tools**: Auto-derived from MCP operations (same `include_operations` list) — no hand-crafted subset

## Architecture

### Module Boundary

`chat/` is an **independent module** — does not depend on `webui/`, but directly depends on `data/` (same data system, no need for abstraction layer).

```
pySdp/
├── chat/                          # independent chat module
│   ├── __init__.py                # init(db) one-time injection of WorkspaceDB
│   ├── llm_client.py             # litellm wrapper (streaming + tool loop) — DONE
│   ├── tools.py                  # tool registry + executor — DONE
│   ├── prompts.py                # system prompt builder — DONE
│   ├── sandbox.py                # restricted exec sandbox — DONE
│   ├── skills.py                 # skill .md/.py loader — DONE
│   ├── sessions.py               # session CRUD (tree structure) — TODO
│   ├── context.py                # aspect-aware context assembler — TODO
│   └── skills/                   # skill definitions (.md + .py) — DONE
├── data/
├── webui/
│   ├── routes/chat.py            # route + session orchestration — DONE (no session orchestration yet)
│   └── static/chat.js            # frontend sidebar — DONE (no session UX)

# Session data files live under ProjectDir, NOT under pySdp/chat/:
{ProjectDir}/chat/sessions/{session_id}/session.json + attachments/
```

### Configuration (IMPLEMENTED — architecture changed from original plan)

Config keys live in `pySdp/config.ini` and `pySdp/config.py` (not `SDPCLI/config.ini` as originally planned):

```ini
# pySdp/config.ini
# ChatApiEndpoint=
# ChatApiKey=
# ChatModel=vertex_ai/gemini-2.5-flash
ChatMaxTokens=8192
ChatTimeoutSeconds=120
```

Env var aliases (from `pySdp/config.py`):
- `PYSDP_CHAT_ENDPOINT`, `PYSDP_CHAT_KEY`, `PYSDP_CHAT_MODEL`, `PYSDP_CHAT_MAX_TOKENS`, `PYSDP_CHAT_TIMEOUT`

The original plan's `SDPCLI/secrets.ini.example` step is **superseded** — config loading goes through `pySdp/config.py` which already reads `SDPCLI/secrets.ini` as a monorepo fallback.

---

## Phase 1: Minimum Viable Chat (streaming + tool use) — ✅ DONE

### Step 1: Config — add Chat LLM keys ✅ DONE

Config keys exist in `pySdp/config.ini` (lines 48-52) and `pySdp/config.py` (lines 38-42).
Architecture differs from original plan: keys live in pySdp config, not SDPCLI/config.ini.
`SDPCLI/secrets.ini.example` was NOT created — not needed given the pySdp config system.

### Step 2: Backend — `pySdp/chat/` package ✅ DONE

All planned files exist and are fully implemented:
- `pySdp/chat/__init__.py` — `init(db)` + `get_db()` injection
- `pySdp/chat/llm_client.py` — `ChatLlmClient` with litellm async, tool-use loop (6 iterations), streaming post-loop call
- `pySdp/chat/tools.py` — `TOOL_DEFINITIONS` + `ToolExecutor`; tools: `execute_python`, `get_snapshots`, `save_report`; extended: `create_skill`
- `pySdp/chat/prompts.py` — `build_system_prompt()` with language detection and active snapshot metadata

Note: Tool set differs from original plan (no `get_draw_calls`/`get_dc_detail`/`get_label_agg` as first-class LLM tools — these are accessed via `execute_python` sandbox instead).

### Step 3: Backend — SSE streaming route ✅ DONE

`pySdp/webui/routes/chat.py` implements:
- `POST /api/chat` (no session_id parameter — sessions not yet implemented)
- `GET /api/chat/status`
- `GET /api/chat/skills`

`pySdp/webui/server.py` registers `_chat_router` at `/api/chat`.

### Step 4: Frontend — chat panel HTML + CSS ✅ DONE

`pySdp/webui/static/index.html`:
- Chat toggle button in header (`#chat-toggle-btn`)
- `<aside id="chat-panel" class="chat-panel">` with header, context bar (chips + snapshot picker), messages area, status line, input area

`pySdp/webui/static/style.css`:
- `.chat-panel` — fixed right sidebar, 600px wide (wider than planned 380px)
- `body.chat-open` — shifts main content, home-detail-bar adjusts
- Full message bubble, tool-call indicator, input box styles

### Step 5: Frontend — chat.js (streaming + rendering) ✅ DONE

`pySdp/webui/static/chat.js` implements:
- `toggleChatPanel()` — show/hide + `body.chat-open` class toggle
- `sendChatMessage()` — slash command detection, active skill routing, plain message
- `streamWithSkill()` — skill-context-aware SSE stream
- `streamChat()` — plain SSE stream
- `readSSEStream()` — `fetch()` + `ReadableStream`, SSE event parsing, incremental render
- `appendMessageBubble()` — markdown rendering (marked.js)
- Tool call indicators in `handleSSEEvent()`
- Auto-scroll, image zoom overlay

### Step 6: Wire up active snapshot context ✅ DONE

`pySdp/webui/static/app.js`:
- `chatState.activeSnapshotId` updated when Explorer snapshot changes (line ~2239)
- `updateChatContextBar()` shows active snapshot chip
- Pin/unpin buttons on snapshot cards (SVG icon pair)

---

## Phase 2: Skills + Snapshot Pinning — ✅ DONE

### Step 7: Backend — markdown-based skills system ✅ DONE

`pySdp/chat/skills.py` — `Skill` dataclass, `load_skills()`, `get_skills()`, `get_skill_by_id()`, `execute_skill()`, hot-reload on file mtime change.

Built-in skills in `pySdp/chat/skills/`:
- `breakdown.md` + `breakdown.py`
- `bottlenecks.md` + `bottlenecks.py`
- `correlate.md` + `correlate.py`
- `compare.md` + `compare.py`
- `explain.md` (no .py)
- `mesh_ratio.md` + `mesh_ratio.py` (LLM-generated skill, extra beyond original plan)

### Step 8: Frontend — skill buttons + slash commands ✅ DONE

- Skills dropdown loaded from `GET /api/chat/skills`
- Skill button row above input (pill-shaped, scrollable)
- Slash command detection in `sendChatMessage()` (types `/cmd` → auto-invoke)
- Active skill badge in control bar

### Step 9: Frontend — snapshot pinning ✅ DONE

- Pin buttons on snapshot cards in Home tab
- Pinned snapshots shown as chips in chat context bar (removable)
- Active snapshot chip auto-tracked (non-removable)
- `chatState.pinnedSnapshotIds` persisted in `localStorage`
- Snapshot picker dropdown from chat header `+` button

### Step 10: Polish ✅ DONE

- Welcome message via `renderWelcome()` on first open
- Error states: connection failed, tool error display
- Status line with contextual messages and elapsed timer
- `startChatTimer()` / `setChatStatusDone()` / `updateStatusText()`
- Enter to send, Shift+Enter for newline
- Auto-resize textarea

---

## Phase 3: Code Execution + Skill Authoring — ✅ DONE

`execute_python` tool implemented in `pySdp/chat/sandbox.py`:
- AST validation (`_validate_code`)
- Restricted globals: allowed builtins + ALLOWED_MODULES whitelist
- BLOCKED_NAMES enforcement
- 30s timeout via `asyncio.wait_for`
- Pre-bound `db`, `snapshot_id`, `snapshot_ids`, `data_query`
- stdout capture → `output` field
- Last expression returned as `result`
- `pandas` in ALLOWED_MODULES (beyond original plan)

`create_skill` tool implemented in `pySdp/chat/tools.py` (`_create_skill`):
- Syntax validation via `ast.parse`
- Writes `.md` (YAML frontmatter + prompt_template) + `.py` (wraps code in `run()`)
- Triggers `load_skills()` hot-reload
- Returns confirmation

---

## Phase 4: Report Generation + Data Visualization — ✅ DONE

`save_report` tool in `pySdp/chat/tools.py` (`_save_report`):
- Saves `.md` to `{ProjectDir}/reports/` directory
- Header with title, timestamp, snapshot IDs
- Report filename auto-generated from title
- Returns path for frontend link rendering

Matplotlib chart support in `pySdp/chat/sandbox.py`:
- `matplotlib.use("Agg")` non-interactive backend
- `_capture_figures()` — captures all open figures as base64 PNG + saves to `reports/img/`
- `chat.js` renders inline `<img>` in assistant message bubbles
- Click-to-zoom overlay (`openImageZoom`)
- "Report saved" link in chat when `save_report` called

---

## Phase 5: Session Management + History — ❌ TODO

No session infrastructure exists. Current state:
- `chatState.messages` — ephemeral in-memory array (lost on page refresh)
- No `session_id` in POST /api/chat request body
- No session list UI
- No `pySdp/chat/sessions.py`
- No `pySdp/chat/context.py`
- No session-related routes in `routes/chat.py`

### Session Data Model (revised — aspect-as-message design)

File-based sessions at `{ProjectDir}/chat/sessions/{session_id}/session.json`. Tree-based message structure with `parent` pointer and `active_leaf`. **No `summaries` field** — snapshot data is captured as `tool_result` messages (from `fetch_aspect`/`execute_python`) that live directly in the message tree, so the LLM sees the raw structured data it fetched rather than a lossy summary. See Step 1 for the full dataclass schema.

---

## Implementation Status Summary (2026-06-10)

### Phase 1-4: ✅ FULLY IMPLEMENTED

**Verified files exist and functional:**
- `D:/pysdp/chat/__init__.py` — db injection working
- `D:/pysdp/chat/llm_client.py` — `stream_chat()` with tool loop (line 56-154)
- `D:/pysdp/chat/tools.py` — `execute_python`, `get_snapshots`, `save_report`, `create_skill`
- `D:/pysdp/chat/prompts.py` — `build_system_prompt()` with language detection
- `D:/pysdp/chat/sandbox.py` — restricted exec + matplotlib capture
- `D:/pysdp/chat/skills.py` — markdown skill loader with hot-reload
- `D:/pysdp/chat/skills/` — 6 skills: breakdown, bottlenecks, correlate, compare, explain, mesh_ratio
- `D:/pysdp/webui/routes/chat.py` — `/api/chat` (POST streaming), `/status`, `/skills`
- `D:/pysdp/webui/static/chat.js` — 727 lines, full client: streaming, skills, pins, markdown render
- `D:/pysdp/webui/static/index.html` — chat panel HTML at line 446+
- `D:/pysdp/webui/static/style.css` — chat panel styles (600px sidebar)
- `D:/pysdp/config.ini` — ChatApiEndpoint/Key/Model/MaxTokens/Timeout (lines 48-52)

**Request contract:** `POST /api/chat` with `{messages: list[dict], snapshot_ids, skill_id?, skill_params?}` — frontend sends full history, backend is stateless (no sessions yet).

### Phase 5: ❌ NOT STARTED

**Missing files (0 implemented):**
- `D:/pysdp/chat/sessions.py` — does NOT exist
- `D:/pysdp/chat/context.py` — does NOT exist

**No session infrastructure in existing code:**
- `routes/chat.py` (line 13-17): `ChatRequest` has `messages: list[dict]`, NO `session_id` field
- `llm_client.py` (line 56-61): `stream_chat(messages, snapshot_ids, skill_context)` — no session parameter, no persistence
- `chat.js` (line 4-14): `chatState.messages` is in-memory only, no `sessionId` field
- `index.html`: no session list UI (no `chat-session-bar` element found)
- No `fetch_aspect` tool (grep returned 0 matches in chat/)
- No `/api/chat/sessions` routes (grep returned 0 matches)
- No `/api/files/project` endpoint

**Consequence:** All 9 Phase 5 steps (Step 1 through Step 9) remain unimplemented. The plan accurately reflects reality.

---

## Remaining Work

All remaining work is Phase 5: Session Management + History Browsing.

### Step 1: Create `pySdp/chat/sessions.py` — ❌ NOT STARTED

Create `pySdp/chat/sessions.py` with:
- `SESSION_DIR` resolved from config, NOT relative to this file:
  ```python
  def _session_root() -> Path:
      from config import get_settings
      cfg = get_settings()
      project = cfg.get("ProjectDir") or str(Path(cfg.get("WorkingDirectory", "")) / "project")
      return Path(project) / "chat" / "sessions"
  ```
  This MUST live under `ProjectDir` so the `/api/files/project` endpoint (Step 2b) can serve session attachments.
- `Session` dataclass: `id`, `title`, `created_at`, `updated_at`, `pinned_snapshot_ids: list[int]`, `active_leaf`, `messages: dict[str, dict]`
  - Note: no `summaries` field — aspect data lives as `tool_result` messages in the tree, not as a separate store
- `Message` dataclass: `id`, `parent`, `role`, `type`, `content`, `timestamp`, `metadata: dict`
  - `role` is one of: `"user"`, `"assistant"`, `"tool"` (OpenAI convention)
  - `type` is one of: `"text"`, `"tool_call"`, `"tool_result"`
  - **`metadata` must carry the fields needed to rebuild a valid OpenAI tool-use exchange:**
    - assistant message with type `tool_call`: `{"tool_calls": [{"id": "call_abc", "type": "function", "function": {"name": ..., "arguments": "<json str>"}}]}`
    - tool message with type `tool_result`: `{"tool_call_id": "call_abc", "tool_name": "fetch_aspect", "snapshot_id": 1, "aspect": "gpu_timing"}`
  - `snapshot_id` is an **int** everywhere (the codebase uses integer snapshot IDs, e.g. `#42`, `WHERE snapshot_id IN (?)`), never a string like `"snap001"`.
  - Note on OpenAI shape: a single assistant turn may emit multiple `tool_calls`, each followed by its own `tool` message keyed by `tool_call_id`. `context.py` (Step 4) relies on `tool_call_id` to pair them when rebuilding API messages.

**Per-round answer/recap (for `recall_history` matching) — extracted at write time, zero extra LLM call:**

When persisting a final assistant **text** turn, extract two short, semantically-dense sentences and store them in `metadata`:
- `metadata.answer` — the direct one-line answer to the user's question
- `metadata.recap` — the one-line summary of what this round did / concluded

These come from a prompt convention (Step 3) where the assistant is asked to start with an `【answer】` line and end with a `【recap】` line. Extraction is mechanical (locate the marked lines):
- **Fallback B (chosen)**: if the markers are absent (LLM didn't comply, short reply, non-English), fall back to the whole assistant text truncated to ~120 chars stored in BOTH `answer` and `recap`. This never fails and always yields a value — at worst the match quality for that round degrades to the truncated-text baseline. (Rejected fallback A — "first sentence + last sentence by period split" — because it can capture half-sentences.)
- These two sentences are what `recall_history` (Step 4c) uses as a round's `summary` AND as its keyword-match domain — NOT mechanically-scraped tags from code (dropped: low semantic value, noisy).
- **Embedding hook (not implemented in MVP)**: reserve `metadata.embedding` (nullable). MVP leaves it null and matches on `answer`/`recap` text via substring. A future upgrade can embed `answer + " " + recap` and swap the match function (see Step 4c `_match`). No schema change needed to add it later.

**Tool result persistence strategy** — what gets stored in `content` varies by tool cost:

| Tool | Store in `content` | Why |
|------|--------------------|-----|
| `fetch_aspect` | Full structured dict (JSON) | Medium cost DB query, avoid re-running |
| `execute_python` | `{output, result, error, attachments: [...rel_paths]}` | Variable cost, may be expensive; images saved to session attachments dir |
| `save_report` | `{ok, path: "reports/foo.md", filename: "foo.md"}` | Path is relative to ProjectDir; file is already the persistent artifact |
| `get_snapshots` / `get_draw_calls` / etc. | Not persisted — these messages are strategy C (don't store) | Cheap DB queries, re-run on restore |

**Session directory layout** (all paths relative to `ProjectDir`):
```
ProjectDir/
  sdp/
  analysis/
  reports/
    foo.md                       ← save_report output
    img/
      chart_1749xxx_0.png        ← execute_python matplotlib (also referenced by session)
  chat/
    sessions/
      s_1749xxx_a3f2/
        session.json
        attachments/
          msg_003_chart_0.png    ← copy of chart PNG, session-owned (survives reports/img cleanup)
```

**Path convention**: all paths stored in `content` and `metadata` are relative to `ProjectDir` (forward slashes). Never store absolute paths in session JSON. Conversion happens at write time in `tools.py` and at read time in the frontend via `/api/files/project?path=<rel>`.

**execute_python image handling**: `image_paths` from sandbox are absolute (written to `reports/img/`). At persist time, copy each image to `session_dir/attachments/msg_{id}_chart_{i}.png` and store the session-relative path `chat/sessions/{id}/attachments/msg_{id}_chart_{i}.png`. The `reports/img/` copy can be used for report embedding; the `attachments/` copy is the session-owned canonical reference.
- `create_session() -> Session` — generates `s_{timestamp}_{rand4}` id, creates `_session_root()/{id}/`, writes `session.json`
- `load_session(session_id: str) -> Session | None` — reads JSON from `_session_root()/{session_id}/session.json`
- `save_session(session: Session)` — atomic write (write to `.tmp` then rename)
- `list_sessions() -> list[dict]` — scans `_session_root()`, returns `[{id, title, updated_at, pinned_snapshot_ids}]` sorted by `updated_at` desc
- `delete_session(session_id: str)` — removes session directory (incl. `attachments/`)
- `add_message(session, role, content, parent_id, msg_type="text", metadata=None) -> Message` — assigns monotonic id `msg_001`, `msg_002`..., appends to `session.messages`, updates `active_leaf` when role is `"assistant"` (final text turn). Used by the route/`stream_chat` for ALL message types: user text, assistant `tool_call`, `tool_result`, and assistant final text.
- `attachment_dir(session_id) -> Path` — returns `_session_root()/{session_id}/attachments/` (created on demand); used when persisting `execute_python` chart PNGs
- `get_path(session: Session, leaf_id: str) -> list[str]` — trace from leaf to root, return ordered `[root, ..., leaf]`
- `get_fetched_aspects(session: Session) -> list[dict]` — scan `tool_result` messages on the active path where `metadata.tool_name == "fetch_aspect"`, return `[{snapshot_id, aspect, msg_id}]` — used by prompts.py to inject the "already fetched" list
- `auto_title(session: Session)` — set title from first user message (truncate to 40 chars) if title is still the default

### Step 2: Add `fetch_aspect` tool to `pySdp/chat/tools.py` — ❌ NOT STARTED

Add a new tool `fetch_aspect` to `TOOL_DEFINITIONS` and a dispatch handler in `ToolExecutor`.

**Tool definition** — MUST match the existing OpenAI-style shape in `TOOL_DEFINITIONS` (`{"type":"function","function":{...,"parameters":{...}}}`), NOT Anthropic `input_schema`:
```python
{
    "type": "function",
    "function": {
        "name": "fetch_aspect",
        "description": "Fetch a specific aspect of a snapshot's GPU data. Use when the user asks about a snapshot dimension not yet in the conversation. Do NOT call if the (snapshot_id, aspect) pair is in the Already-Fetched list.",
        "parameters": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "integer", "description": "Integer snapshot id, e.g. 42"},
                "aspect": {
                    "type": "string",
                    "enum": ["gpu_timing", "bandwidth", "draw_call_breakdown",
                             "shader_complexity", "texture_usage", "triangle_count",
                             "bottleneck_summary"]
                }
            },
            "required": ["snapshot_id", "aspect"]
        }
    }
}
```

**Dispatch table** in `ToolExecutor`:
```python
ASPECT_HANDLERS = {
    "gpu_timing":          _aspect_gpu_timing,          # GPU pass timing, frame time
    "bandwidth":           _aspect_bandwidth,           # memory bandwidth read/write
    "draw_call_breakdown": _aspect_draw_call_breakdown, # DC count by category
    "shader_complexity":   _aspect_shader_complexity,   # ALU pressure, register use
    "texture_usage":       _aspect_texture_usage,       # texture count/format/VRAM
    "triangle_count":      _aspect_triangle_count,      # triangle count by category
    "bottleneck_summary":  _aspect_bottleneck_summary,  # attribution_rules scoring
}
```

Wire `fetch_aspect` into `ToolExecutor.execute()` (the dispatch switch at tools.py:113) alongside the existing `execute_python`/`save_report` branches, e.g. `elif name == "fetch_aspect": return await asyncio.to_thread(self._fetch_aspect, args)`.

Each `_aspect_*` function takes `(db, snapshot_id: int)` and queries the DB directly, returning a structured dict with numeric data + key findings. The *persistence* of this result as a `tool_result` message is NOT done here — it happens in the `stream_chat` refactor (Step 4b), which calls `add_message(type="tool_result", metadata={"tool_call_id":..., "tool_name":"fetch_aspect", "snapshot_id":..., "aspect":...})`.

**Column availability caveat**: aspect handlers must only use metric columns that actually exist. The known metric keys (from `prompts.py`) are: `clocks, fragments_shaded, vertices_shaded, read_total_bytes, write_total_bytes, shaders_busy_pct, shaders_stalled_pct, lrz_pixels_killed`. Before implementing, verify each aspect is derivable:
- `gpu_timing` ← `clocks`; `bandwidth` ← `read_total_bytes`/`write_total_bytes`; `shader_complexity` ← `shaders_busy_pct`/`shaders_stalled_pct`/`vertices_shaded`/`fragments_shaded`.
- `texture_usage`, `triangle_count`, `bottleneck_summary` may need the `textures`/`draw_calls` tables or the `attribution_rules.json` engine — confirm the source exists per aspect; if an aspect can't be backed by real data, drop it from the enum rather than returning fabricated numbers.

**Also modify `_save_report`** — return relative path instead of absolute:
```python
# resolve ProjectDir from config
project_dir = Path(cfg.get("ProjectDir") or Path(cfg.get("WorkingDirectory","")) / "project")
rel = str(filepath.relative_to(project_dir)).replace("\\", "/")
return {"ok": True, "path": rel, "filename": f"{filename}.md"}
# e.g. {"ok": true, "path": "reports/shadow_pass.md", "filename": "shadow_pass.md"}
```

**Also modify `_execute_python`** — convert `image_paths` to project-relative paths:
```python
# after sandbox returns, replace absolute image_paths with relative
project_dir = Path(cfg.get("ProjectDir") or ...)
result["image_paths"] = [
    str(Path(p).relative_to(project_dir)).replace("\\", "/")
    for p in result.get("image_paths", [])
]
```

These changes make all stored paths portable — `ProjectDir` is the single anchor.

### Step 2b: Add `/api/files/project` endpoint to `pySdp/webui/routes/files.py` — ❌ NOT STARTED

Add a new route to `files.py` that resolves ProjectDir-relative paths:

```python
@router.get("/project")
def serve_project_file(
    path: str = Query(..., description="Path relative to ProjectDir"),
    download: int = Query(default=0),
):
    from config import get_settings
    cfg = get_settings()
    project_dir = cfg.get("ProjectDir") or str(Path(cfg.get("WorkingDirectory","")) / "project")
    full = Path(project_dir) / path
    # security: prevent path traversal outside project_dir
    if not str(full.resolve()).startswith(str(Path(project_dir).resolve())):
        return JSONResponse({"ok": False, "error": "Path outside project dir"}, status_code=400)
    if not full.exists() or not full.is_file():
        return JSONResponse({"ok": False, "error": f"Not found: {path}"}, status_code=404)
    headers = {"Content-Disposition": f'attachment; filename="{full.name}"'} if download else {}
    return FileResponse(str(full), headers=headers)
```

This endpoint is the single access point for all project-relative assets: reports, chart images, and session attachments. The frontend switches from `/api/files/raw?path=<abs>` to `/api/files/project?path=<rel>` for all chat-originated files.

**Also update `chat.js` `openReportTab()`** — change the fetch URL:
```js
// Before:
fetch(`/api/files/read?path=${encodeURIComponent(filepath)}`)
// After (filepath is now "reports/foo.md", a project-relative path):
fetch(`/api/files/project?path=${encodeURIComponent(filepath)}`)
// and image src rewrite:
// Before: src="/api/files/raw?path=<abs>"
// After:  src="/api/files/project?path=<rel>"
```

Inline chart images rendered in chat bubbles also switch to `/api/files/project?path=` when restored from session history (base64 used for live streaming, project-relative URL used for session restore).

### Step 3: Update `pySdp/chat/prompts.py` — ❌ NOT STARTED

Change the signature from `build_system_prompt(snapshot_ids, user_lang="en")` to `build_system_prompt(snapshot_ids, user_lang="en", session=None)`. When `session` is provided, append the dynamic already-fetched list built from `get_fetched_aspects(session)`. Keep `session=None` working (Phase 1-4 callers and the no-session path).

Modify it to include two new sections:

**Static aspect menu** (always present):
```
## Available Snapshot Aspects

You can retrieve specific data from any snapshot using fetch_aspect(snapshot_id, aspect).
Available aspects:

  gpu_timing         — GPU pass timing distribution, overall frame time
  bandwidth          — Memory bandwidth usage, read/write ratio
  draw_call_breakdown — DrawCall count by category
  shader_complexity  — ALU pressure, register usage, instruction count
  texture_usage      — Texture count, format, VRAM footprint
  triangle_count     — Triangle count by category/object
  bottleneck_summary — Composite bottleneck score (based on attribution rules)

Call fetch_aspect when the user asks about a snapshot dimension not yet in context.
Do NOT call fetch_aspect if the same (snapshot_id, aspect) pair already appears in
the "Already fetched" list below — the data is already in this conversation.
```

**Dynamic already-fetched list** (built from `get_fetched_aspects(session)` at request time):
```
## Already Fetched Aspects

  snap001 :: gpu_timing         (msg_003)
  snap002 :: gpu_timing         (msg_007)
  snap001 :: texture_usage      (msg_011)
```

When the list is empty, omit this section entirely. This prevents redundant `fetch_aspect` calls.

**Reply structure convention** (enables free answer/recap extraction — see Step 1):
```
Structure every reply as:
  First line:  【answer】<one-line direct answer to the user's question>
  Body:        analysis, data, chart descriptions…
  Last line:   【recap】<one-line summary of what this round did / concluded>
```
The persistence layer extracts these two lines into `metadata.answer`/`metadata.recap` (fallback B if absent). They power `recall_history` matching and round summaries.

**History recall hint** (so the LLM knows omitted history is reachable):
```
Older turns may be summarized or omitted from your current context. If you sense
you're missing detail from earlier in THIS conversation, call recall_history:
  - recall_history(query="...")           → browse a directory of matching rounds
  - recall_history(round_id="...")         → expand one round's full detail
  - recall_history(round_id="...", direction="parent"|"next")  → page to adjacent rounds
Do NOT recall rounds already visible in your context.
```

### Step 4: Create `pySdp/chat/context.py` — ❌ NOT STARTED

Create `pySdp/chat/context.py` with **layered-reserve assembly** — NOT a flat priority sort. There is no semantic relevance scoring (deferred) and no cross-branch injection (deferred); only the active path is used. Recency is a free structural property (position in the ordered path), not a computed score.

```python
RECENCY_WINDOW_K = 3   # most recent K rounds kept in FULL (details included)
def estimate_tokens(text): return len(text) // 3
```

**Round** = one segment of the active path from a `user` message up to (and including) its closing assistant **text** turn. Identified by the user message id. Used by both truncation and `recall_history` (Step 4c).

**`assemble_context(session, system_prompt, budget=12000) -> (messages, surviving_aspects, excluded_ids)`** — fills the budget in layers, then prepends `system_prompt`:

- **Layer 0 — anchors (never dropped):** the system prompt + the current (leaf) user question.
- **Layer 1 — aspect reserve:** ALL `fetch_aspect` tool pairs on the active path (already deduped, so bounded and small). Reserved *before* recency competition so a long chat can never squeeze out "I already fetched snap42 gpu_timing." Charts/`PERSIST_FULL` execute_python results that are NOT prose-replaceable also belong here.
- **Layer 2 — recency window:** the most recent `RECENCY_WINDOW_K` rounds kept in FULL (user + tool pairs + assistant), for current-topic continuity. Walk rounds backward from the leaf.
- **Layer 3 — older rounds, detail-demoted:** for rounds older than the window, keep the assistant **conclusion** (answer/recap + text) but **fold away** details that are (a) already summarized by that conclusion AND (b) prose-replaceable (ordinary execute_python text output). Non-replaceable details (aspects, charts) were already secured in Layer 1.
- **Layer 4 — final trim:** if still over budget, drop whole older rounds from Layer 3, oldest first.

**Why this beats flat recency** (the "near detail vs farther conclusion" problem): a nearer round's bulky detail is NOT automatically preferred over a slightly-older round's conclusion. The choice uses two free structural signals, no scoring:
1. **summary-coverage** — is there a later assistant conclusion that already digested this detail? (just "is there an assistant turn after it" — a structural fact)
2. **prose-replaceability** — `classify_result` already told us: aspects/charts are `PERSIST_FULL` (not replaceable → never demoted); ordinary text output is replaceable → demote-able once covered.

So: detail inside the window → keep; detail outside window but covered+replaceable → fold to conclusion; aspect/chart → kept regardless of distance (Layer 1).

**Return values:**
- `messages`: OpenAI-format list. Reconstruction from the tree:
  - `user`/`assistant` text → `{"role", "content"}`
  - assistant `tool_call` → `{"role":"assistant", "content": null, "tool_calls": metadata["tool_calls"]}`
  - `tool_result` → `{"role":"tool", "tool_call_id": metadata["tool_call_id"], "content": <json string>}`
  - Pairing rule (Step 1): an assistant `tool_call` and its `tool_result`(s) are kept or dropped **together**, never split, or the API 400s.
- `surviving_aspects`: the `(snapshot_id, aspect, msg_id)` list for aspects that ACTUALLY made it into `messages`. **This — not `get_fetched_aspects(session)` over the whole tree — is what feeds the already-fetched list in the system prompt** (see ordering fix below).
- `excluded_ids`: set of message ids on the active path that did NOT make it into `messages` (folded details + trimmed rounds). This is the search domain for `recall_history` (Step 4c) — so recall never re-surfaces something already in context.

**Ordering fix (prompt must not lie):** `assemble_context` runs FIRST, then the already-fetched list is built from `surviving_aspects` and injected into the system prompt — not the other way around. If an aspect got trimmed, it drops off the already-fetched list, so the LLM will simply re-`fetch_aspect` (safe — recomputable). This prevents the prompt from claiming data is present when truncation removed it.

Step 4b consequence: build the prompt in two passes — a base prompt for assembly, then re-inject the already-fetched section from `surviving_aspects` before the LLM call. (Or pass a placeholder the route fills after assembly.)

### Step 4b: Refactor `stream_chat` to be session-backed (THE INTEGRATION SPINE) — ❌ NOT STARTED

This is the core wiring of Phase 5 — without it, sessions.py and context.py are never used and tool results are never persisted. Currently `stream_chat(messages, snapshot_ids, skill_context)` (llm_client.py:56) does `full_messages = [system] + messages` (line 71), trusting the frontend's array, and builds `tool_call`/`tool_result` only in the throwaway local `full_messages`.

Change `stream_chat` to accept a `session` and own persistence:

```python
async def stream_chat(self, session, new_user_text, snapshot_ids, skill_context=None):
    # 1. persist the incoming user turn
    add_message(session, "user", new_user_text, session.active_leaf, msg_type="text")
    # 2. build system prompt WITH already-fetched list, then assemble context from the tree
    system_prompt = build_system_prompt(snapshot_ids, user_lang, session=session)
    if skill_context: system_prompt += f"\n\n## Current Skill Task\n{skill_context}"
    full_messages = assemble_context(session, system_prompt, budget=...)
    # 3. tool-use loop (unchanged structure), BUT persist each step into the tree:
    for _iteration in range(6):
        response = await litellm.acompletion(..., messages=full_messages, ...)
        msg = choice.message
        if not msg.tool_calls:
            add_message(session, "assistant", content, session.active_leaf, msg_type="text")  # final turn
            yield SSEEvent("token", {"content": content}); break
        # assistant tool_call turn — build payload now, but persist LAZILY (see pairing rule below):
        # only commit tcall_msg to the tree once a tool_result for it is also being persisted.
        tc_payload = [{"id": tc.id, "type":"function", "function":{...}} for tc in msg.tool_calls]
        full_messages.append({"role":"assistant","content":content or None,"tool_calls":tc_payload})
        tcall_msg = None  # lazily created via _ensure_tcall_msg() on first PERSIST_* result
        def _ensure_tcall_msg():
            nonlocal tcall_msg
            if tcall_msg is None:
                tcall_msg = add_message(session, "assistant", content or "", session.active_leaf,
                                        msg_type="tool_call", metadata={"tool_calls": tc_payload})
            return tcall_msg
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            yield SSEEvent("tool_call", {...})
            t0 = time.time()
            result = await self.executor.execute(name, args)
            duration_ms = int((time.time() - t0) * 1000)
            images = result.pop("images", [])            # base64 — used for live SSE only
            # 3a. append to in-request context so the model can answer NOW (always)
            full_messages.append({"role":"tool","tool_call_id":tc.id,"content":json.dumps(result, default=str)})
            yield SSEEvent("tool_result", {"name":name,"result":result,"images":images, ...})
            # 3b. DURABILITY decision — persist to tree only if worth re-using (see classify_result)
            policy = classify_result(name, args, result, duration_ms)
            if policy in (PERSIST_FULL, PERSIST_REF):
                parent = _ensure_tcall_msg()  # commit the assistant tool_call turn now (keeps the pair intact)
                if policy == PERSIST_FULL:
                    self._persist_tool_attachments(session, parent, result)  # copy chart PNGs into session
                    stored = json.dumps(result, default=str)
                else:  # PERSIST_REF — store only the reference, not the payload
                    stored = json.dumps({k: result.get(k) for k in ("ok", "path", "filename")})
                meta = {"tool_call_id": tc.id, "tool_name": name}
                if name == "fetch_aspect": meta |= {"snapshot_id": args["snapshot_id"], "aspect": args["aspect"]}
                add_message(session, "tool", stored, parent.id, msg_type="tool_result", metadata=meta)
            # CACHE_ONLY → nothing written; the exchange lived only in this request's full_messages
    save_session(session)
    yield SSEEvent("done", {"session_id": session.id})
```

#### Persistence policy — two independent axes

The single `PERSIST_TOOLS` set was wrong because it conflated two separate questions. Decide them independently:

| Axis | Question | Decided by |
|------|----------|------------|
| **Durability** | Write the result to `session.json` so it survives refresh/restart? | `classify_result()` below |
| **Context replay** | Re-send it to the LLM on the next turn (costs tokens)? | `context.py` truncation (Step 4) |

Key insight: **the assistant's text reply is itself persisted and already summarizes every tool result.** So a *raw payload* only needs durable storage when prose can't substitute for it — i.e. when later turns need the exact numbers/rows/chart AND re-deriving them is expensive. Everything else is "cache-only": the result lives in this request's `full_messages` so the model can answer now, then is discarded; if a future turn needs it, the model just re-calls the (cheap) tool.

```python
PERSIST_FULL = "full"   # write whole payload to tree (durable + replayed, top truncation priority)
PERSIST_REF  = "ref"    # write only a reference ({ok,path,filename}); the file IS the artifact
CACHE_ONLY   = "cache"  # within-request scratch only; never written, re-run if needed later

def classify_result(name, args, result, duration_ms):
    if name == "fetch_aspect":            return PERSIST_FULL   # medium cost, exact data reused across turns
    if name == "save_report":             return PERSIST_REF    # report file persists itself on disk
    if name == "execute_python":
        # measured, not guessed: persist only artifacts or genuinely expensive runs
        if result.get("image_paths") or duration_ms > 1500:
            return PERSIST_FULL
        return CACHE_ONLY                                       # trivial calc — assistant's prose covers it
    if name == "recall_history":          return CACHE_ONLY     # content already in tree; recall just re-surfaces it
    return CACHE_ONLY                                           # get_snapshots, get_draw_calls, … cheap re-run
```

This directly implements the earlier principle *"存不存要看 tool 的代价"* using the **actually-measured** `duration_ms` (we already compute it at `llm_client.py:132`) plus artifact presence, rather than a hardcoded allowlist.

Resulting matrix:

| Tool | Durable? | Replayed next turn? |
|------|----------|---------------------|
| `fetch_aspect` | yes (full) | yes — highest truncation priority |
| `execute_python` (chart or >1.5s) | yes (full) | yes |
| `execute_python` (trivial/fast) | no | within-request only |
| `save_report` | reference (`{path}`) only | reference only |
| `get_snapshots` / `get_draw_calls` / … | no | within-request only |

**Pairing safety**: when a result is `CACHE_ONLY`, neither its `tool_call` nor its `tool_result` is persisted, so the tree never contains an orphaned `tool_call_id`. Note this means the `tool_call` assistant turn (`tcall_msg`) is persisted eagerly above, but a turn whose tool calls are ALL cache-only would leave a `tool_call` with no `tool_result` in the tree. **Implementation rule**: persist the `tcall_msg` lazily — only write it once at least one of its tool results is being persisted; otherwise keep the whole exchange out of the tree (the assistant's later text turn is what gets persisted instead).

`_persist_tool_attachments(session, tcall_msg, result)`: for `execute_python` results carrying `image_paths` (now project-relative from Step 2), copy each PNG into `attachment_dir(session.id)` as `{msg_id}_chart_{i}.png` and rewrite `result["image_paths"]` (or an `attachments` key) to the session-relative path `chat/sessions/{id}/attachments/{msg_id}_chart_{i}.png`. This is what makes images survive a `reports/img/` cleanup.

**Decision (single source of truth)**: the frontend no longer sends the full history. With `session_id` present, the request body carries only `session_id` + the new user text. `stream_chat` rebuilds all prior turns from the session tree via `assemble_context`. (See Step 5/Step 6.)

**Skill turns**: `streamWithSkill` also routes through `stream_chat`; the skill's `new_user_text` (the filled prompt/`user_prompt`) is persisted as the user turn like any other, so skill conversations are part of the session history too.

### Step 4c: Add `recall_history` tool (active recall of trimmed conversation) — ❌ NOT STARTED

Truncation (Step 4) is lossy by design. `recall_history` is the LLM's escape hatch: when it senses missing detail, it pulls back rounds that were folded/trimmed. Symmetric to `fetch_aspect` (fetch data I don't have) — this fetches *turns* I can't see. Classified `CACHE_ONLY` (the content is already in the tree; recall just temporarily re-surfaces it for this request).

**Round model** (shared with Step 4): a round = `user` message → its tool pairs → closing assistant text, keyed by the user message id, numbered by position.

**Search domain = trimmed-only.** `recall_history` operates on `excluded_ids` (returned by `assemble_context`) — i.e. the rounds/details NOT in the current context. Rounds already visible are returned in the directory marked `in_context: true` with NO body (don't re-feed what the LLM already sees).

**One tool, three modes** (directory → expand → page — never a blind content dump):

```python
{
  "type": "function",
  "function": {
    "name": "recall_history",
    "description": "Search/browse earlier rounds of THIS conversation not in your current context. Start with query to get a directory, then expand a round_id for detail, or page with direction.",
    "parameters": {
      "type": "object",
      "properties": {
        "query":     {"type": "string", "description": "keywords to find matching rounds (directory mode)"},
        "round_id":  {"type": "string", "description": "expand this round's full detail (expand mode)"},
        "direction": {"type": "string", "enum": ["parent", "next"], "description": "page to adjacent round relative to round_id"}
      }
    }
  }
}
```

- **Mode A — directory (query given):** returns a list of matching rounds as **metadata only, no payloads** (the first guard against over-recall):
  ```json
  [{"round": 5, "round_id": "msg_011",
    "answer": "<metadata.answer of the round's assistant turn>",
    "recap":  "<metadata.recap>",
    "details": [{"type":"tool_result","tool":"execute_python","tokens":2100,"has_chart":true},
                {"type":"tool_result","tool":"fetch_aspect","aspect":"triangle_count","tokens":300}],
    "in_context": false}]
  ```
  `answer`/`recap` come straight from `metadata` (Step 1) — the round's "summary", no generation.
- **Mode B — expand (round_id given):** rebuild that ONE round in time order, each message truncated and the whole round capped (e.g. ≤1500 tokens) — the second guard. Layout: `[user] full · [tool_call] name+args only · [tool_result] content truncated (skip if in_context) · [assistant] conclusion full`.
- **Mode C — page (round_id + direction):** return the directory entry for the parent (older) or next (newer) round. Lets the LLM walk the log without re-querying; expand only when it wants detail.

**Matching (`_match(query, rounds)`):** MVP = case-insensitive substring over each round's `answer` + `recap` (NOT raw transcript, NOT scraped tags). Abstract this into one function so the embedding upgrade (Step 1's `metadata.embedding` hook) is a drop-in replacement, no structural change.

**Wiring:** add a `recall_history` branch in `ToolExecutor.execute()` that receives the `session` + current `excluded_ids` (thread these through from `stream_chat`, which holds both). `classify_result` returns `CACHE_ONLY` for it.

### Step 5: Add session routes to `pySdp/webui/routes/chat.py` — ❌ NOT STARTED

Add to existing `routes/chat.py`:

```python
@router.get("/sessions")
def list_sessions(): ...

@router.post("/sessions")
def create_session(): ...

@router.get("/sessions/{session_id}")
def get_session(session_id: str): ...

@router.delete("/sessions/{session_id}")
def delete_session(session_id: str): ...

@router.patch("/sessions/{session_id}")
def update_session(session_id: str, body: SessionUpdate): ...
```

Also modify `POST /api/chat` (`chat_stream`) and `ChatRequest`:
- `ChatRequest` changes from `messages: list[dict]` to `session_id: str | None = None` + `message: str` (the new user turn only). The frontend no longer sends full history (Step 4b decision).
- In `chat_stream`: load session if `session_id` given, else `create_session()`. Set `session.pinned_snapshot_ids = body.snapshot_ids`.
- Call `client.stream_chat(session, body.message, body.snapshot_ids, skill_context=...)` — `stream_chat` now owns user-turn persistence, the tool loop, tool-result persistence, and `save_session()`.
- After streaming, `auto_title(session)` is already triggered inside `stream_chat` on the first assistant turn (or call it in the route after the generator drains — pick one and state it). The `done` SSE event already carries `session_id` (emitted by `stream_chat`).
- Skill branch: build `skill_context` as today, but pass the filled prompt as `body.message`-equivalent so it's persisted as the user turn.

Note: this is a **breaking change** to the request contract — Step 6 (chat.js) must be updated in lockstep. There is no backward-compatible `messages` path; the in-memory `chatState.messages` array stops being the source of truth.

### Step 6: Frontend — session list panel in chat.js — ❌ NOT STARTED

Add to `chatState`:
```js
const chatState = {
  ...existing fields...,
  sessionId: null,
  sessionList: [],
};
```

Add to `pySdp/webui/static/chat.js`:
- `loadSessionList()` — `GET /api/chat/sessions` → populate `chatState.sessionList`
- `renderSessionList()` — collapsible `<div id="chat-session-list">` at top of `#chat-panel` showing session entries sorted by `updated_at`; each entry: title + relative time + "×" delete button
- `switchSession(sessionId)` — `GET /api/chat/sessions/{id}` → walk the active path (root→`active_leaf`) and re-render bubbles. Must handle ALL persisted message types, not just text:
  - `user`/assistant text → normal bubble
  - `tool_call` → tool indicator (name + args)
  - `tool_result` → render per tool: `save_report` → report link (via `openReportTab`), `execute_python` with attachments → `<img src="/api/files/project?path=...">`, `fetch_aspect` → collapsed data block
  - set `chatState.sessionId`
- `newSession()` — `POST /api/chat/sessions` → set `chatState.sessionId`, clear UI, call `renderWelcome()`
- `deleteSession(sessionId)` — `DELETE /api/chat/sessions/{id}` → refresh session list
- **Change request shape** in `sendChatMessage`/`streamChat`/`streamWithSkill`: POST body becomes `{ session_id: chatState.sessionId, message: <new user text>, snapshot_ids, skill_id?, skill_params? }` — NOT `messages: chatState.messages`. The frontend keeps `chatState.messages` only for in-session live rendering; the backend session tree is authoritative.
- On `done` event, capture `data.session_id`, set `chatState.sessionId`, and refresh `loadSessionList()` so the new title appears.
- On panel open, call `loadSessionList()`. If a `chatState.sessionId` is persisted (e.g. in `localStorage`), auto-`switchSession` to restore the last conversation across refresh.
- "+" button in session list header calls `newSession()`

### Step 7: Frontend — session list HTML in index.html — ❌ NOT STARTED

Add to `#chat-panel` in `pySdp/webui/static/index.html`, between `.chat-header` and `.chat-messages`:
```html
<div class="chat-session-bar" id="chat-session-bar">
  <div class="session-list-header">
    <span>Sessions</span>
    <button onclick="newSession()">+</button>
    <button onclick="toggleSessionList()">&#9660;</button>
  </div>
  <div class="session-list" id="chat-session-list" style="display:none"></div>
</div>
```

### Step 8: CSS for session list in style.css — ❌ NOT STARTED

Add to `pySdp/webui/static/style.css`:
- `.chat-session-bar` — thin bar below chat header, collapsible
- `.session-list` — scrollable list, max-height 200px
- `.session-entry` — flex row: title (truncated), relative time, delete button
- `.session-entry.active` — highlighted current session

### Step 9: Optional — cross-session search — ❌ NOT STARTED

Add `GET /api/chat/search?q=<keyword>&snapshot_id=<optional>` to `routes/chat.py`:
- Scan all `SESSION_DIR/*/session.json` files
- Case-insensitive substring match on message content
- Return `[{session_id, title, snippet}]`

Frontend: search icon in session list header → expand search input → results grouped by session.

This step is lower priority — skip for MVP if development bandwidth is limited.

---

## Validation

### Step 1 — sessions.py

```
python -c "import sys; sys.path.insert(0,'D:/snapdragon/pySdp'); from chat.sessions import create_session, add_message, save_session, load_session; s=create_session(); add_message(s,'user','hello',s.active_leaf); save_session(s); s2=load_session(s.id); print(s2.messages)"
```
Expected: session JSON written under `{ProjectDir}/chat/sessions/`, message visible after reload.

### Step 2 — fetch_aspect tool

Start server with a valid snapshot in DB, then in chat ask:
```
"snap001 的 GPU timing 是怎样的？"
```
Expected: assistant calls `fetch_aspect(snap001, gpu_timing)`, tool_call + tool_result appear in message stream, assistant answers with actual numbers.

Second question on same snapshot:
```
"snap001 的 bandwidth 呢？"
```
Expected: new `fetch_aspect(snap001, bandwidth)` call, but NOT a repeat of `gpu_timing`.

Third: re-ask about gpu_timing:
```
"再说说 snap001 的 GPU timing"
```
Expected: assistant answers WITHOUT calling `fetch_aspect` again — uses data already in context.

### Step 3 — prompts.py aspect menu

```python
python -c "
import sys; sys.path.insert(0,'D:/snapdragon/pySdp')
from chat.sessions import create_session, add_message, save_session
from chat.prompts import build_system_prompt
s = create_session()
# a tool_result is role='tool' (NOT 'assistant'), with tool_call_id in metadata
add_message(s, 'tool', '{}', s.active_leaf, msg_type='tool_result',
            metadata={'tool_call_id':'call_1','tool_name':'fetch_aspect','snapshot_id':42,'aspect':'gpu_timing'})
save_session(s)
prompt = build_system_prompt(snapshot_ids=[42], session=s)
print('gpu_timing' in prompt, 'Already Fetched' in prompt, '42' in prompt)
"
```
Expected: `True True True`

### Step 4 — context.py aspect-aware truncation

```python
python -c "
import sys; sys.path.insert(0,'D:/snapdragon/pySdp')
from chat.context import estimate_tokens, assemble_context
print(estimate_tokens('hello world'))
"
```
Expected: prints integer ~3.

Truncation priority test: create a session with 20+ messages including tool_result pairs, call `assemble_context` with a tight budget — verify tool_result messages survive while user messages are dropped first, and that no assistant `tool_call` is ever returned without its matching `tool_result` (no orphaned `tool_call_id`).

### Step 4b — session-backed stream_chat (persistence + dedup)

1. Send a message that triggers `fetch_aspect(42, gpu_timing)`. After the response, load the session JSON and verify it contains: a `user` text msg, an assistant `tool_call` msg (with `tool_calls` in metadata), a `tool_result` msg (role=`tool`, `tool_call_id` matching), and a final assistant text msg.
2. Send a second message about gpu_timing on the same snapshot → assistant does NOT re-call `fetch_aspect` (already-fetched list works end-to-end through persistence).
3. Trigger `execute_python` that draws a chart → `classify_result` returns `PERSIST_FULL`; verify a PNG appears in `{ProjectDir}/chat/sessions/{id}/attachments/` and the `tool_result` content references the session-relative path.
4. Trigger a trivial `execute_python` (e.g. `len(...)`, no chart, sub-second) → `classify_result` returns `CACHE_ONLY`; verify NO `tool_result` (and no orphan `tool_call`) is written to the tree, but the assistant's text answer IS persisted.
5. Verify a `get_snapshots` call is streamed to the UI but produces NO `tool_result` message in the session JSON (strategy C — not persisted).
6. `save_report` → verify the persisted `tool_result` content is the reference only (`{ok, path, filename}`), not the full markdown body.

### Step 5 — session routes

Start server, then:
```
curl http://localhost:8000/api/chat/sessions
# Expected: {"sessions": []}

curl -X POST http://localhost:8000/api/chat/sessions
# Expected: {"id": "s_..."}

curl http://localhost:8000/api/chat/sessions/<id>
# Expected: session JSON with empty messages
```

### Step 6+7+8 — frontend session UX

1. Open chat panel — session list bar appears at top (collapsed by default)
2. Click "+" — new session created, welcome message shows
3. Send a message — session persists, session list shows entry with auto-title after response
4. Refresh page — reopen chat panel, session list still shows previous session
5. Click session entry — previous messages restored

### End-to-end validation

```
# Phase 1-4 (already working)
1. GET /api/chat/status → {"enabled": true} (requires ChatApiEndpoint/Key/Model in config)
2. Open chat panel, ask "list snapshots" → streaming tokens appear + tool_call indicator + final answer
3. Ask "draw a bar chart of categories by clocks" → inline PNG image rendered in chat
4. Click "Bottlenecks" skill button → streaming analysis appears
5. Ask "save a report of the analysis" → report link appears, file written to project/reports/

# Phase 5 (new)
6. POST /api/chat with {message, snapshot_ids} (no session_id) → done event contains a new session_id; session JSON created under {ProjectDir}/chat/sessions/
7. POST /api/chat with that session_id + a new message → backend rebuilds history from the tree (frontend sends only the new turn); tool_call/tool_result persisted
8. Refresh page → reopen chat → session list shows previous session → click → switchSession restores text, tool indicators, charts (via /api/files/project), and report links
9. Re-ask an already-fetched aspect → no duplicate fetch_aspect call
10. "+" button creates fresh session
```

---

## Alternatives Considered

- **Per-snapshot summary dict (original plan)**: Storing a separate `summaries: dict[snapshot_id, text]` in the session. Replaced by aspect-as-message design — aspect data lives as `tool_result` nodes in the message tree, not as a parallel store. Advantages: no separate summarization step, aspect data is directly visible in conversation history, LLM sees the raw structured data it fetched.
- **Full 3-layer context assembler (original plan)**: PathManager + SummaryManager + RelevanceScorer. Deferred — aspect-aware priority truncation (keep tool pairs, drop user msgs first) covers the main need without the complexity.
- **DuckDB session storage**: Simpler for cross-session search. Deferred — file-based JSON is zero-dependency and easy to inspect/debug. Migrate when cross-session search becomes a real need.
- **E2E branch navigation UI**: `◀ 1/2 ▶` at fork points. Deferred — tree branching only matters after users edit messages, which requires additional frontend message editing UX. Not in scope for this phase.

## Risks

- **Session file corruption**: `save_session()` must write atomically (`.tmp` + rename) to avoid corrupt JSON on crash. Persist once at end of `stream_chat`, or after each `add_message` if mid-stream crashes must be survivable.
- **Orphaned tool messages (API 400)**: every `tool_result` must reference a `tool_call_id` from a preceding assistant turn. Both `context.py` truncation and partial-failure paths in `stream_chat` (e.g. tool errors, the `consecutive_errors >= 2` break at llm_client.py:154) must not leave a persisted `tool_call` without its `tool_result`, or the next turn's `assemble_context` will produce an invalid request.
- **`session_id` not returned from streaming**: The `done` SSE event carries `session_id`. Frontend must capture this to persist the session reference across page refreshes.
- **Attachment path drift**: if `ProjectDir` changes between sessions, old session-relative attachment paths still resolve (they're under ProjectDir). But absolute paths must never be written — enforce relative-path conversion at the single `_persist_tool_attachments` choke point.
- **Message ordering**: `add_message()` must maintain monotonic message IDs; parallel requests to the same session could corrupt ordering — single-user assumption makes this acceptable for now.
- **Breaking request contract**: frontend (Step 6) and backend (Step 5/4b) must ship together — there is no `messages`-array fallback.

## Implementation Notes

- Sessions directory: `{ProjectDir}/chat/sessions/` (NOT under `pySdp/chat/` — must be under ProjectDir so `/api/files/project` can serve attachments). Gitignored.
- Session ID format: `s_{unix_timestamp}_{4-char-hex}` — sortable by time, unique
- `active_leaf` advances on every `add_message` (it's the current tip of the tree, whatever the role); the *final assistant text* turn is the one users branch from. Each new message's `parent` is the prior `active_leaf`.
- OpenAI message integrity: an assistant `tool_call` and its `tool_result`(s) form an atomic unit. `context.py` truncation and `switchSession` rendering must never split them, and every `tool_result` must reference a `tool_call_id` present in a preceding assistant turn — else the API 400s.
- `auto_title` runs after first assistant response: take first user message, strip special chars, truncate to 40 chars
- `POST /api/chat` is a **breaking** contract change (`messages` → `session_id` + `message`); frontend and backend must ship together. Omitting `session_id` creates a new session.

---

## Files Summary

### Already Implemented

| Path | Status |
|------|--------|
| `pySdp/chat/__init__.py` | ✅ Done |
| `pySdp/chat/llm_client.py` | ✅ Done |
| `pySdp/chat/tools.py` | ✅ Done |
| `pySdp/chat/prompts.py` | ✅ Done |
| `pySdp/chat/sandbox.py` | ✅ Done |
| `pySdp/chat/skills.py` | ✅ Done |
| `pySdp/chat/skills/*.md` + `.py` (6 skills) | ✅ Done |
| `pySdp/webui/routes/chat.py` | ✅ Done (partial — no session routes) |
| `pySdp/webui/static/chat.js` | ✅ Done (partial — no session UX) |
| `pySdp/webui/static/index.html` | ✅ Done (partial — no session bar) |
| `pySdp/webui/static/style.css` | ✅ Done (partial — no session styles) |
| `pySdp/webui/server.py` | ✅ Done |
| `pySdp/config.ini` (Chat keys) | ✅ Done |

### Remaining (Phase 5)

| Path | Action |
|------|--------|
| `pySdp/chat/sessions.py` | NEW — session CRUD, tree traversal, `get_fetched_aspects()`, `attachment_dir()`, auto-title; session dir under `{ProjectDir}/chat/sessions/` |
| `pySdp/chat/context.py` | NEW — layered-reserve assembler (anchors / aspect reserve / recency window K / detail-demotion); returns `(messages, surviving_aspects, excluded_ids)`; keeps tool pairs atomic |
| `pySdp/chat/llm_client.py` | **MODIFY (spine)** — `stream_chat` becomes session-backed: persists turns, extracts answer/recap, copies chart attachments, classify_result durability, `save_session` |
| `pySdp/chat/tools.py` | MODIFY — add `fetch_aspect` + `recall_history` (OpenAI-format, int snapshot_id) + handlers + `classify_result()`; `_save_report`/`_execute_python` return project-relative paths |
| `pySdp/chat/prompts.py` | MODIFY — add `session` param; aspect menu + already-fetched list + reply-structure (answer/recap) convention + recall hint |
| `pySdp/webui/routes/files.py` | MODIFY — add `GET /api/files/project?path=<rel>` endpoint (ProjectDir-anchored, path-traversal guard) |
| `pySdp/webui/routes/chat.py` | MODIFY — 5 session endpoints; `ChatRequest` → `{session_id, message}`; call session-backed `stream_chat` |
| `pySdp/webui/static/chat.js` | MODIFY — session list UX, `switchSession` restores all msg types, request shape `{session_id, message}`, `/api/files/project` links |
| `pySdp/webui/static/index.html` | MODIFY — session bar HTML |
| `pySdp/webui/static/style.css` | MODIFY — session bar + entry styles |

---

## Open Questions

(Resolved or deferred — no blocking open questions for Phase 5 MVP implementation)

---

## Phase 5 Extension: Cross-Snapshot Performance Comparison

**Status:** DESIGN  
**Date:** 2026-06-22  
**Dependency:** Phase 5 Step 2 (`fetch_aspect`) must be implemented first — this extension builds on the same tool dispatch pattern.

### Motivation

现有 compare skill 只做分类级 clocks 汇总对比（`_get_label_agg` × 2 → delta table），无法回答：
- "哪些 DC 在新 snapshot 中变慢了？"
- "瓶颈类型从 texture_bound 变成了 shader_alu，为什么？"
- "Character 分类新增了哪些 DC，占多少开销？"

用户需要的是 **结构化的多层对比**，不只是汇总数字。

### Design Principles

1. **一次调用，多层结果** — 不让 LLM 自己拼凑多次 `fetch_aspect`，而是一个 `compare_snapshots` 工具直接返回分层对比结构
2. **利用已有引擎** — 复用 `_get_label_agg`（分类汇总）+ `topdc_service._Engine`（attribution 打分）+ `query_dcs`（DC 级数据）
3. **Token 友好** — 返回精简摘要（top-N regression/improvement），不是全量 DC 列表
4. **可组合** — LLM 拿到摘要后可以用 `execute_python` 深入任意维度

### Tool Definition

```python
{
    "type": "function",
    "function": {
        "name": "compare_snapshots",
        "description": "Compare performance between two snapshots. Returns category-level deltas, top regressions/improvements, and bottleneck shift analysis. Use when the user asks to compare, diff, or find what changed between snapshots.",
        "parameters": {
            "type": "object",
            "properties": {
                "baseline_id": {"type": "integer", "description": "Baseline snapshot id (the 'before')"},
                "target_id": {"type": "integer", "description": "Target snapshot id (the 'after')"},
                "focus_category": {
                    "type": "string",
                    "description": "Optional: limit comparison to one category (e.g. 'Character', 'Scene')"
                },
                "top_n": {
                    "type": "integer",
                    "description": "Number of top regressed/improved DCs to return (default 10)"
                }
            },
            "required": ["baseline_id", "target_id"]
        }
    }
}
```

### Return Structure

```python
{
    "summary": {
        "baseline": {"snapshot_id": 1, "sdp_name": "game.apk", "total_clocks": 12000000, "dc_count": 450},
        "target":   {"snapshot_id": 2, "sdp_name": "game.apk", "total_clocks": 14500000, "dc_count": 470},
        "delta_clocks": 2500000,
        "delta_pct": 20.8,
        "verdict": "regression"  # "regression" | "improvement" | "neutral" (±5%)
    },
    "category_comparison": [
        {
            "category": "Character",
            "baseline_clocks": 3000000, "target_clocks": 4200000,
            "delta": 1200000, "delta_pct": 40.0,
            "baseline_dc_count": 50, "target_dc_count": 55,
            "new_dcs": 5,          # DCs in target not in baseline (by label matching)
            "removed_dcs": 0
        },
        # ... sorted by abs(delta) DESC
    ],
    "top_regressions": [
        {
            "category": "Character", "subcategory": "Skin",
            "api_id_baseline": 142, "api_id_target": 148,
            "match_method": "label",  # how we paired them
            "baseline_clocks": 50000, "target_clocks": 120000,
            "delta": 70000, "delta_pct": 140.0,
            "bottleneck_shift": {"from": "shader_alu", "to": "texture_bound"},
            "key_metric_changes": {
                "tex_fetch_stall_pct": {"baseline": 12.0, "target": 45.0, "delta": 33.0},
                "fragments_shaded": {"baseline": 8000, "target": 22000, "delta": 14000}
            }
        },
        # ... top_n entries, sorted by delta DESC
    ],
    "top_improvements": [
        # same shape, sorted by delta ASC (biggest improvement first)
    ],
    "bottleneck_distribution": {
        "baseline": {"texture_bound": 12, "shader_alu": 8, "overdraw": 5, "bandwidth": 3},
        "target":   {"texture_bound": 18, "shader_alu": 6, "overdraw": 7, "bandwidth": 5},
        "shifts": [
            {"from": "shader_alu", "to": "texture_bound", "count": 4},
            {"from": "none", "to": "overdraw", "count": 3}
        ]
    }
}
```

### Implementation: `_compare_snapshots` handler

位于 `chat/tools.py`，在 `ToolExecutor` 类中：

```python
def _compare_snapshots(self, db, args: dict) -> dict:
    baseline_id = args["baseline_id"]
    target_id = args["target_id"]
    focus_category = args.get("focus_category")
    top_n = args.get("top_n", 10)

    # ── Layer 1: Category-level comparison ────────────────────────────
    base_agg = self._get_label_agg(db, {"snapshot_id": baseline_id, "metric": "clocks", "agg": "sum"})
    tgt_agg  = self._get_label_agg(db, {"snapshot_id": target_id, "metric": "clocks", "agg": "sum"})
    category_comparison = _build_category_comparison(base_agg, tgt_agg, focus_category)

    # ── Layer 2: DC-level pairing + delta ─────────────────────────────
    from data.query import get_draw_calls
    base_dcs = get_draw_calls(db, baseline_id, category=focus_category)
    tgt_dcs  = get_draw_calls(db, target_id, category=focus_category)
    paired, new_dcs, removed_dcs = _pair_draw_calls(base_dcs, tgt_dcs)

    # ── Layer 3: Attribution scoring for paired DCs ───────────────────
    regressions, improvements = _rank_paired_dcs(paired, db, baseline_id, target_id, top_n)

    # ── Layer 4: Bottleneck distribution shift ────────────────────────
    bottleneck_dist = _bottleneck_distribution(paired, base_dcs, tgt_dcs)

    # ── Summary ───────────────────────────────────────────────────────
    base_total = sum(r["value"] for r in base_agg)
    tgt_total  = sum(r["value"] for r in tgt_agg)
    delta = tgt_total - base_total
    delta_pct = (delta / base_total * 100) if base_total else 0

    return {
        "summary": { ... },
        "category_comparison": category_comparison,
        "top_regressions": regressions[:top_n],
        "top_improvements": improvements[:top_n],
        "bottleneck_distribution": bottleneck_dist,
    }
```

### DC Pairing Strategy (`_pair_draw_calls`)

跨 snapshot 的 DC 没有稳定 ID（`api_id` 随抓取变化）。配对策略：

```python
def _pair_draw_calls(base_dcs: list[dict], tgt_dcs: list[dict]) -> tuple:
    """Pair DCs across snapshots using label matching.
    
    Strategy (priority order):
    1. Exact match: same (category, subcategory, detail) — 1:1 greedy by clocks proximity
    2. Category match: same (category, subcategory) — 1:1 greedy by clocks proximity
    3. Unmatched: new_dcs (target only) / removed_dcs (baseline only)
    """
    paired = []       # [(base_dc, tgt_dc)]
    new_dcs = []      # target DCs with no baseline match
    removed_dcs = []  # baseline DCs with no target match
    
    # Group by (category, subcategory, detail) for exact matching
    base_by_label = _group_by_label(base_dcs)
    tgt_by_label  = _group_by_label(tgt_dcs)
    
    # Phase 1: Exact label match — pair by closest clocks value
    for key in set(base_by_label) & set(tgt_by_label):
        b_list = sorted(base_by_label[key], key=lambda d: d.get("clocks") or 0, reverse=True)
        t_list = sorted(tgt_by_label[key], key=lambda d: d.get("clocks") or 0, reverse=True)
        for b, t in zip(b_list, t_list):
            paired.append((b, t))
        # Excess goes to new/removed
        if len(t_list) > len(b_list):
            new_dcs.extend(t_list[len(b_list):])
        elif len(b_list) > len(t_list):
            removed_dcs.extend(b_list[len(t_list):])
    
    # Phase 2: Remaining unmatched — try (category, subcategory) only
    # ... (similar greedy matching on remaining DCs)
    
    return paired, new_dcs, removed_dcs
```

**为什么用 label matching 而非 pipeline_id**：
- `pipeline_id` 跨 snapshot 不稳定（随 driver state 变化）
- label 是语义标注（"Character/Skin/Body"），跨版本稳定
- 同一 label 可能有多个 DC，用 clocks 大小做 greedy 配对足够准确

### Attribution Scoring for Comparison

复用 `topdc_service._Engine`，对每个 paired DC 分别跑 attribution，然后比较 bottleneck 变化：

```python
def _rank_paired_dcs(paired, db, baseline_id, target_id, top_n):
    from analysis.topdc_service import _Engine, _RULES_PATH
    import json
    
    rules = json.loads(_RULES_PATH.read_text(encoding="utf-8-sig"))
    engine = _Engine(rules)
    
    # Build category percentile lookups from DB (same logic as topdc_service)
    base_pcts = _build_percentiles(db, baseline_id)
    tgt_pcts  = _build_percentiles(db, target_id)
    
    deltas = []
    for base_dc, tgt_dc in paired:
        if not base_dc.get("clocks") and not tgt_dc.get("clocks"):
            continue
        
        base_clocks = base_dc.get("clocks") or 0
        tgt_clocks  = tgt_dc.get("clocks") or 0
        delta = tgt_clocks - base_clocks
        
        # Run attribution on both
        cat = tgt_dc.get("category") or "Other"
        base_attr = engine.attribute(
            {"metrics": {k: base_dc.get(k) for k in engine.layer1 if base_dc.get(k) is not None}},
            base_pcts.get(cat, {}), True
        )
        tgt_attr = engine.attribute(
            {"metrics": {k: tgt_dc.get(k) for k in engine.layer1 if tgt_dc.get(k) is not None}},
            tgt_pcts.get(cat, {}), True
        )
        
        # Identify key metric changes (top 3 by absolute delta)
        key_changes = _top_metric_changes(base_dc, tgt_dc, n=3)
        
        deltas.append({
            "category": cat,
            "subcategory": tgt_dc.get("subcategory", ""),
            "api_id_baseline": base_dc["api_id"],
            "api_id_target": tgt_dc["api_id"],
            "match_method": "label",
            "baseline_clocks": base_clocks,
            "target_clocks": tgt_clocks,
            "delta": delta,
            "delta_pct": round(delta / max(base_clocks, 1) * 100, 1),
            "bottleneck_shift": {
                "from": base_attr["primary_bottleneck"],
                "to": tgt_attr["primary_bottleneck"],
            },
            "key_metric_changes": key_changes,
        })
    
    deltas.sort(key=lambda d: d["delta"], reverse=True)
    regressions  = [d for d in deltas if d["delta"] > 0]
    improvements = [d for d in deltas if d["delta"] < 0]
    improvements.sort(key=lambda d: d["delta"])  # most improved first
    
    return regressions, improvements
```

### Percentile Lookup from DB (`_build_percentiles`)

`topdc_service` 原本从 `status.json` 文件读 percentile 数据。对比工具需要直接从 DB 计算：

```python
def _build_percentiles(db, snapshot_id: int) -> dict[str, dict]:
    """Build per-category percentile thresholds from live DB data.
    
    Returns: {category: {"metrics_p70": {metric: threshold}, "metrics_p90": ...}}
    """
    from data.query import get_draw_calls
    
    dcs = get_draw_calls(db, snapshot_id)
    by_cat = defaultdict(list)
    for dc in dcs:
        cat = dc.get("category") or "Other"
        by_cat[cat].append(dc)
    
    METRIC_KEYS = ["clocks", "fragments_shaded", "vertices_shaded", "read_total_bytes",
                   "write_total_bytes", "shaders_busy_pct", "shaders_stalled_pct",
                   "tex_fetch_stall_pct", "tex_l1_miss_pct", "tex_pipes_busy_pct"]
    TIERS = [("metrics_p50", 0.5), ("metrics_p70", 0.7), ("metrics_p80", 0.8),
             ("metrics_p90", 0.9), ("metrics_p95", 0.95), ("metrics_p99", 0.99)]
    
    result = {}
    for cat, cat_dcs in by_cat.items():
        cat_pcts = {}
        for tier_name, threshold in TIERS:
            tier_vals = {}
            for metric in METRIC_KEYS:
                vals = sorted(v for dc in cat_dcs if (v := dc.get(metric)) is not None)
                if len(vals) >= 5:
                    idx = int(len(vals) * threshold)
                    tier_vals[metric] = vals[min(idx, len(vals) - 1)]
            cat_pcts[tier_name] = tier_vals
        result[cat] = cat_pcts
    
    return result
```

### Integration with Phase 5 Session System

- **`classify_result("compare_snapshots", ...)`** → `PERSIST_FULL` — 对比结果数据量中等（~2-4KB JSON），且复用价值高（用户会追问 "那 Character 具体是怎么回事"）
- **`fetch_aspect` 不冲突** — `compare_snapshots` 是独立工具，不走 aspect 系统。但 LLM 可以先 `compare_snapshots` 获取全局视图，再用 `fetch_aspect` 深入单个 snapshot 的某个维度
- **System prompt 提示** — 在 Step 3 的 aspect menu 下方加一段：

```
## Cross-Snapshot Comparison

When the user asks to compare or diff snapshots, use compare_snapshots(baseline_id, target_id).
This returns category deltas, top regressions/improvements with bottleneck shift analysis.
For deeper investigation of a specific DC after comparison, use execute_python with get_dc_detail().
```

### Token Budget Analysis

典型返回大小估算（2 snapshot, ~400 DCs each, top_n=10）：

| Section | Est. tokens |
|---------|-------------|
| summary | ~50 |
| category_comparison (8 categories) | ~300 |
| top_regressions (10 entries) | ~800 |
| top_improvements (10 entries) | ~800 |
| bottleneck_distribution | ~200 |
| **Total** | **~2150** |

在 12000 token context budget 中占 ~18%，可接受。如果用户 focus 单个 category，结果更小。

### Comparison Skill Update

更新 `chat/skills/compare.md` 从使用 `_get_label_agg` 切换到 `compare_snapshots`：

```markdown
---
name: Compare Snapshots
slash_command: /compare
button_label: Compare
icon: "\U0001F504"
description: Compare performance between two snapshots — find regressions and improvements
---

Compare snapshots {snapshot_ids} using compare_snapshots tool.
Present the results as:
1. Overall verdict (regression/improvement/neutral with %)
2. Category breakdown table (category, baseline, target, delta%, new/removed DCs)
3. Top 5 regressions with bottleneck shift explanation
4. Top 5 improvements
5. Bottleneck distribution shift summary
6. Actionable recommendation: which category/DC to investigate first
```

`compare.py` 也可保留为 deterministic fallback（不需要 LLM 的快速对比），但主路径改为 LLM + `compare_snapshots` tool。

### Validation

```bash
# 1. Tool returns valid structure
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "对比 snapshot 1 和 2 的性能", "snapshot_ids": [1, 2]}'
# Expected: assistant calls compare_snapshots(1, 2), returns structured comparison

# 2. Focus category
"Character 分类在新版本变慢了多少？"
# Expected: compare_snapshots(1, 2, focus_category="Character")

# 3. Follow-up drill-down
"最大的 regression DC#148 具体是什么情况？"
# Expected: execute_python with get_dc_detail(db, 2, 148)

# 4. Session persistence
# After comparison, reload session → compare result should be in context (PERSIST_FULL)
# Re-asking the same comparison → assistant uses existing data, does NOT re-call
```

### Files Affected

| Path | Action |
|------|--------|
| `pySdp/chat/tools.py` | ADD `compare_snapshots` to TOOL_DEFINITIONS; ADD `_compare_snapshots`, `_pair_draw_calls`, `_rank_paired_dcs`, `_build_percentiles`, `_bottleneck_distribution` methods |
| `pySdp/chat/tools.py` | MODIFY `classify_result` — add `"compare_snapshots": PERSIST_FULL` |
| `pySdp/chat/prompts.py` | MODIFY — add Cross-Snapshot Comparison section to system prompt |
| `pySdp/chat/skills/compare.md` | MODIFY — update prompt to use compare_snapshots tool |
| `pySdp/chat/skills/compare.py` | OPTIONAL — keep as deterministic fallback or remove |

### Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| DC pairing accuracy — label matching may miss renamed categories | Fallback: if no label match, try pipeline_id match (less stable but catches renames) |
| Performance — `get_draw_calls` × 2 + attribution scoring | Cap at top_n early; `_build_percentiles` uses in-memory sort not DB percentile functions |
| Token overflow — user pins 3+ snapshots and asks "compare all" | `compare_snapshots` only accepts 2 IDs (baseline vs target); for 3+ snapshots, LLM should call multiple times |
| Attribution rules unavailable | Graceful fallback: skip bottleneck scoring, return only clocks delta |

### Implementation Order (relative to Phase 5 steps)

This extension slots into Phase 5 at **Step 2** — it's a sibling of `fetch_aspect`, sharing the same infrastructure:

```
Step 1: sessions.py           ← prerequisite (session persistence)
Step 2: fetch_aspect tool     ← sibling (same dispatch pattern)
Step 2c: compare_snapshots    ← THIS EXTENSION (new)
Step 3: prompts.py            ← update to include comparison hint
Step 4: context.py            ← classify_result handles compare_snapshots
...rest of Phase 5 unchanged...
```
