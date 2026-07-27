# Plan: Chat Session Management + History

**Status:** COMPLETE (Steps 1-8 done, Step 9 skipped for MVP)  
**Date:** 2026-06-22 (extracted from PLAN-chat-sidebar.md Phase 5)  
**Completed:** 2026-07-06  
**Prerequisite:** Phases 1-4 complete (chat streaming, tools, skills, reports all working)

---

## Status

| Step | Description | Status |
|------|-------------|--------|
| Step 1 | `chat/sessions.py` — session CRUD + tree structure | ✅ Done |
| Step 2 | `fetch_aspect` tool + path normalization | ✅ Done |
| Step 2b | `/api/files/project` endpoint | ✅ Done |
| Step 2c | `compare_snapshots` tool (cross-snapshot comparison) | ✅ Done |
| Step 3 | `prompts.py` — aspect menu + already-fetched + answer/recap | ✅ Done |
| Step 4 | `chat/context.py` — layered-reserve context assembler | ✅ Done |
| Step 4b | Refactor `stream_chat` — session-backed integration spine | ✅ Done |
| Step 4c | `recall_history` tool | ✅ Done |
| Step 5 | Session routes in `routes/chat.py` | ✅ Done |
| Step 6 | Frontend — session list in `chat.js` | ✅ Done |
| Step 7 | Frontend — session bar HTML in `index.html` | ✅ Done |
| Step 8 | CSS for session list | ✅ Done |
| Step 9 | Optional — cross-session search | ⏭️ Skipped (MVP) |

**Dependency order**: Step 1 → Step 2/2b/2c → Step 3 → Step 4 → **Step 4b (highest risk)** → Step 5 → Steps 6-8 → Step 9 (optional).

---

## Context

The chat system (Phases 1-4) is fully functional but **stateless** — messages live only in the browser's `chatState.messages` array and are lost on refresh. The backend receives the full message history on every request. There is no persistence, no session concept, no cross-turn aspect deduplication.

**Current request contract:** `POST /api/chat` with `{messages: list[dict], snapshot_ids, skill_id?, skill_params?}` — frontend sends full history.

**Target contract:** `POST /api/chat` with `{session_id: str|null, message: str, snapshot_ids, skill_id?, skill_params?}` — frontend sends only the new user turn; backend owns history via session tree.

---

## Step 1: Create `pySdp/chat/sessions.py`

Create `pySdp/chat/sessions.py` with:

### Session directory

```python
def _session_root() -> Path:
    from config import get_settings
    cfg = get_settings()
    project = cfg.get("ProjectDir") or str(Path(cfg.get("WorkingDirectory", "")) / "project")
    return Path(project) / "chat" / "sessions"
```

MUST live under `ProjectDir` so `/api/files/project` (Step 2b) can serve session attachments.

### Data model

**Session** dataclass:
- `id: str` — format `s_{unix_timestamp}_{4-char-hex}`
- `title: str` — auto-generated from first user message
- `created_at: str` — ISO timestamp
- `updated_at: str` — ISO timestamp
- `pinned_snapshot_ids: list[int]`
- `active_leaf: str` — current tip message id
- `messages: dict[str, dict]` — message tree keyed by message id

**Message** dataclass:
- `id: str` — monotonic `msg_001`, `msg_002`...
- `parent: str | None` — parent message id (None for root)
- `role: str` — `"user"` | `"assistant"` | `"tool"`
- `type: str` — `"text"` | `"tool_call"` | `"tool_result"`
- `content: str` — text or JSON string
- `timestamp: str` — ISO timestamp
- `metadata: dict` — tool-specific fields (see below)

**Metadata shapes:**
- assistant `tool_call`: `{"tool_calls": [{"id": "call_abc", "type": "function", "function": {"name": ..., "arguments": "<json>"}}]}`
- tool `tool_result`: `{"tool_call_id": "call_abc", "tool_name": "fetch_aspect", "snapshot_id": 1, "aspect": "gpu_timing"}`
- assistant final text: `{"answer": "...", "recap": "..."}` (extracted from 【answer】/【recap】 markers; fallback: truncated text ≤120 chars)

### Functions

- `create_session() -> Session` — generate id, create dir, write initial `session.json`
- `load_session(session_id: str) -> Session | None` — read from `_session_root()/{id}/session.json`
- `save_session(session: Session)` — atomic write (`.tmp` + rename)
- `list_sessions() -> list[dict]` — scan dirs, return `[{id, title, updated_at, pinned_snapshot_ids}]` sorted by `updated_at` desc
- `delete_session(session_id: str)` — remove entire session directory
- `add_message(session, role, content, parent_id, msg_type="text", metadata=None) -> Message` — append to tree, update `active_leaf`
- `attachment_dir(session_id) -> Path` — `_session_root()/{id}/attachments/` (mkdir on demand)
- `get_path(session, leaf_id) -> list[str]` — trace root→leaf, return ordered message ids
- `get_fetched_aspects(session) -> list[dict]` — scan `tool_result` msgs on active path where `metadata.tool_name == "fetch_aspect"`, return `[{snapshot_id, aspect, msg_id}]`
- `auto_title(session)` — set title from first user message (strip specials, truncate 40 chars)

### Answer/recap extraction

When persisting a final assistant text turn, extract:
- `metadata.answer` — text after `【answer】` marker (first line)
- `metadata.recap` — text after `【recap】` marker (last line)
- **Fallback:** if markers absent → whole text truncated to ~120 chars in both fields

Reserve `metadata.embedding` (nullable) for future semantic search upgrade.

### Tool result persistence strategy

| Tool | Store in `content` | Why |
|------|--------------------|-----|
| `fetch_aspect` | Full structured dict (JSON) | Medium cost, reuse across turns |
| `compare_snapshots` | Full structured dict (JSON) | Medium cost, high reuse |
| `execute_python` (chart or >1.5s) | `{output, result, error, attachments: [...]}` | Expensive, images as session attachments |
| `save_report` | `{ok, path, filename}` (reference only) | File is the artifact |
| `get_snapshots` / cheap tools | Not persisted (CACHE_ONLY) | Cheap to re-run |

### Directory layout

```
{ProjectDir}/
  chat/
    sessions/
      s_1749xxx_a3f2/
        session.json
        attachments/
          msg_003_chart_0.png
```

All paths stored in session JSON are relative to `ProjectDir` (forward slashes, never absolute).

---

## Step 2: Add `fetch_aspect` tool to `pySdp/chat/tools.py`

### Tool definition (OpenAI format)

```python
{
    "type": "function",
    "function": {
        "name": "fetch_aspect",
        "description": "Fetch a specific aspect of a snapshot's GPU data. Use when the user asks about a snapshot dimension not yet in the conversation. Do NOT call if the (snapshot_id, aspect) pair is in the Already-Fetched list.",
        "parameters": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "integer", "description": "Integer snapshot id"},
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

### Dispatch

```python
ASPECT_HANDLERS = {
    "gpu_timing":          _aspect_gpu_timing,          # clocks distribution
    "bandwidth":           _aspect_bandwidth,           # read_total_bytes / write_total_bytes
    "draw_call_breakdown": _aspect_draw_call_breakdown, # DC count by category
    "shader_complexity":   _aspect_shader_complexity,   # shaders_busy_pct, stalled, ALU
    "texture_usage":       _aspect_texture_usage,       # textures table: count/format/VRAM
    "triangle_count":      _aspect_triangle_count,      # vertices_shaded by category
    "bottleneck_summary":  _aspect_bottleneck_summary,  # attribution_rules scoring
}
```

Wire into `ToolExecutor.execute()`: `elif name == "fetch_aspect": return await asyncio.to_thread(self._fetch_aspect, args)`

Each handler takes `(db, snapshot_id: int)` and queries DB directly. Returns structured dict with numeric data + key findings.

### Column availability

Known metrics from `prompts.py`: `clocks, fragments_shaded, vertices_shaded, read_total_bytes, write_total_bytes, shaders_busy_pct, shaders_stalled_pct, lrz_pixels_killed`.

Additional from `get_draw_calls` JOIN: `time_alus_working_pct, tex_fetch_stall_pct, tex_l1_miss_pct, tex_pipes_busy_pct`.

Full metrics table has 50+ columns (from `data/db.py`).

### Path normalization (also in this step)

Modify `_save_report` — return project-relative path:
```python
project_dir = Path(cfg.get("ProjectDir") or ...)
rel = str(filepath.relative_to(project_dir)).replace("\\", "/")
return {"ok": True, "path": rel, "filename": f"{filename}.md"}
```

Modify `_execute_python` — convert `image_paths` to project-relative.

---

## Step 2b: Add `/api/files/project` endpoint

Add to `pySdp/webui/routes/files.py`:

```python
@router.get("/project")
def serve_project_file(
    path: str = Query(..., description="Path relative to ProjectDir"),
    download: int = Query(default=0),
):
    # resolve project_dir from config
    # security: path traversal guard
    # return FileResponse
```

Single access point for: reports, chart images, session attachments.

Frontend switches: `/api/files/read?path=<abs>` → `/api/files/project?path=<rel>` for all chat-originated files.

---

## Step 2c: Add `compare_snapshots` tool (Cross-Snapshot Comparison)

### Tool definition

```python
{
    "type": "function",
    "function": {
        "name": "compare_snapshots",
        "description": "Compare performance between two snapshots. Returns category-level deltas, top regressions/improvements, and bottleneck shift analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "baseline_id": {"type": "integer", "description": "Baseline snapshot id (the 'before')"},
                "target_id": {"type": "integer", "description": "Target snapshot id (the 'after')"},
                "focus_category": {"type": "string", "description": "Optional: limit to one category"},
                "top_n": {"type": "integer", "description": "Top N regressed/improved DCs (default 10)"}
            },
            "required": ["baseline_id", "target_id"]
        }
    }
}
```

### Return structure

```python
{
    "summary": {
        "baseline": {"snapshot_id", "sdp_name", "total_clocks", "dc_count"},
        "target": {...},
        "delta_clocks": int,
        "delta_pct": float,
        "verdict": "regression" | "improvement" | "neutral"
    },
    "category_comparison": [
        {"category", "baseline_clocks", "target_clocks", "delta", "delta_pct",
         "baseline_dc_count", "target_dc_count", "new_dcs", "removed_dcs"}
    ],
    "top_regressions": [
        {"category", "subcategory", "api_id_baseline", "api_id_target",
         "match_method": "label", "baseline_clocks", "target_clocks",
         "delta", "delta_pct",
         "bottleneck_shift": {"from": str, "to": str},
         "key_metric_changes": {"metric": {"baseline", "target", "delta"}}}
    ],
    "top_improvements": [...],  # same shape
    "bottleneck_distribution": {
        "baseline": {"bottleneck_type": count},
        "target": {...},
        "shifts": [{"from", "to", "count"}]
    }
}
```

### Implementation layers

1. **Category-level** — reuse `_get_label_agg` × 2, compute delta
2. **DC pairing** — match by `(category, subcategory, detail)` label, greedy by clocks proximity
3. **Attribution scoring** — reuse `topdc_service._Engine` on paired DCs, compare bottleneck shifts
4. **Bottleneck distribution** — aggregate primary_bottleneck counts across all paired DCs

### DC pairing strategy

`api_id` is unstable across snapshots. Pair by label (semantic, stable across versions):
- Phase 1: exact `(category, subcategory, detail)` match, greedy by clocks proximity
- Phase 2: fallback `(category, subcategory)` for remaining unmatched
- Unpaired: classify as `new_dcs` (target-only) or `removed_dcs` (baseline-only)

### Percentile computation from DB

```python
def _build_percentiles(db, snapshot_id) -> dict[str, dict]:
    # get_draw_calls → group by category → compute p50/p70/p80/p90/p95/p99 per metric
    # Returns: {category: {"metrics_p70": {metric: threshold}, ...}}
```

### classify_result

`compare_snapshots` → `PERSIST_FULL` (medium cost, high reuse value)

### Token budget

Typical return ~2150 tokens (8 categories, top_n=10), fits within 12000 context budget.

---

## Step 3: Update `pySdp/chat/prompts.py`

Change signature: `build_system_prompt(snapshot_ids, user_lang="en", session=None)`

Add three sections:

### Static aspect menu (always present)

```
## Available Snapshot Aspects
fetch_aspect(snapshot_id, aspect) — call when user asks about a dimension not yet in context.
  gpu_timing, bandwidth, draw_call_breakdown, shader_complexity,
  texture_usage, triangle_count, bottleneck_summary
```

### Cross-snapshot comparison hint

```
## Cross-Snapshot Comparison
compare_snapshots(baseline_id, target_id) — category deltas, top regressions, bottleneck shifts.
```

### Dynamic already-fetched list (when session provided)

```
## Already Fetched Aspects
  #42 :: gpu_timing         (msg_003)
  #43 :: gpu_timing         (msg_007)
```

Built from `get_fetched_aspects(session)` — omit section if empty.

### Reply structure convention

```
Structure every reply as:
  First line:  【answer】<direct answer>
  Body:        analysis...
  Last line:   【recap】<summary of this round>
```

### History recall hint

```
If missing earlier detail, call recall_history(query="...") to browse trimmed rounds.
```

---

## Step 4: Create `pySdp/chat/context.py`

Layered-reserve assembly. Active path only, no semantic scoring.

```python
RECENCY_WINDOW_K = 3
def estimate_tokens(text): return len(text) // 3
```

### `assemble_context(session, system_prompt, budget=12000)`

Returns: `(messages: list[dict], surviving_aspects: list, excluded_ids: set)`

**Layers (filled in order, each reserves budget):**

- **Layer 0 — Anchors:** system prompt + current user question (never dropped)
- **Layer 1 — Aspect reserve:** ALL `fetch_aspect`/`compare_snapshots` tool pairs on active path (highest priority, never squeezed)
- **Layer 2 — Recency window:** most recent K rounds in FULL
- **Layer 3 — Older rounds demoted:** keep assistant conclusion (answer/recap), fold prose-replaceable details
- **Layer 4 — Final trim:** drop oldest rounds from Layer 3 if still over budget

### Rules

- Tool pairs (`tool_call` + `tool_result`) are atomic — never split
- `surviving_aspects` feeds the already-fetched list (not `get_fetched_aspects` over whole tree)
- Assembly runs FIRST, then already-fetched list injected into system prompt (prompt must not lie about what's in context)

### Round definition

Round = `user` message → its tool pairs → closing assistant text turn. Keyed by user message id.

---

## Step 4b: Refactor `stream_chat` (THE INTEGRATION SPINE)

**Highest risk step.** This wires sessions.py + context.py into the actual request flow.

### New signature

```python
async def stream_chat(self, session, new_user_text, snapshot_ids, skill_context=None):
```

### Flow

1. Persist user turn: `add_message(session, "user", new_user_text, session.active_leaf)`
2. Build system prompt (base) → `assemble_context` → inject already-fetched from `surviving_aspects`
3. Tool-use loop (unchanged structure), but persist each step:
   - `classify_result(name, args, result, duration_ms)` decides durability
   - `PERSIST_FULL` / `PERSIST_REF` → write `tool_call` + `tool_result` to tree (lazy pairing)
   - `CACHE_ONLY` → in-request only, not persisted
4. Persist final assistant text (with answer/recap extraction)
5. `save_session(session)`
6. Yield `SSEEvent("done", {"session_id": session.id})`

### Persistence policy

```python
PERSIST_FULL = "full"
PERSIST_REF  = "ref"
CACHE_ONLY   = "cache"

def classify_result(name, args, result, duration_ms):
    if name == "fetch_aspect":            return PERSIST_FULL
    if name == "compare_snapshots":       return PERSIST_FULL
    if name == "save_report":             return PERSIST_REF
    if name == "execute_python":
        if result.get("image_paths") or duration_ms > 1500:
            return PERSIST_FULL
        return CACHE_ONLY
    if name == "recall_history":          return CACHE_ONLY
    return CACHE_ONLY
```

### Lazy pairing

`tool_call` message is only persisted once at least one of its `tool_result`s is `PERSIST_*`. Prevents orphaned `tool_call_id` in the tree.

### `_persist_tool_attachments`

Copy chart PNGs from `reports/img/` into `attachment_dir(session.id)` as `{msg_id}_chart_{i}.png`.

---

## Step 4c: Add `recall_history` tool

LLM escape hatch for trimmed context. Operates on `excluded_ids` from `assemble_context`.

### Tool definition

```python
{
    "name": "recall_history",
    "parameters": {
        "properties": {
            "query": {"type": "string", "description": "keywords (directory mode)"},
            "round_id": {"type": "string", "description": "expand one round"},
            "direction": {"type": "string", "enum": ["parent", "next"]}
        }
    }
}
```

### Three modes

- **Directory (query):** metadata-only list of matching rounds (answer/recap, no payloads)
- **Expand (round_id):** rebuild one round, capped ~1500 tokens
- **Page (round_id + direction):** adjacent round entry

### Matching

MVP: case-insensitive substring over `answer + recap`. Future: embedding-based via `metadata.embedding`.

`classify_result` → `CACHE_ONLY` (content already in tree, recall just re-surfaces).

---

## Step 5: Add session routes to `pySdp/webui/routes/chat.py`

```python
@router.get("/sessions")          # list_sessions
@router.post("/sessions")         # create_session
@router.get("/sessions/{id}")     # get_session
@router.delete("/sessions/{id}")  # delete_session
@router.patch("/sessions/{id}")   # update_session (title, pinned_snapshot_ids)
```

### Modify `POST /api/chat`

- `ChatRequest`: `session_id: str | None = None`, `message: str` (breaking change)
- Load/create session, call `stream_chat(session, message, ...)`
- `done` SSE event carries `session_id`

---

## Step 6: Frontend — session list in `chat.js`

Add to `chatState`: `sessionId: null`, `sessionList: []`

New functions:
- `loadSessionList()` — GET /api/chat/sessions
- `renderSessionList()` — collapsible list at panel top
- `switchSession(id)` — restore all message types from session tree
- `newSession()` — POST /api/chat/sessions
- `deleteSession(id)` — DELETE

Change request shape: `{session_id, message}` replaces `{messages}`.

On `done` event: capture `session_id`, refresh list.

---

## Step 7: Frontend — session bar HTML in `index.html`

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

Between `.chat-header` and `.chat-messages`.

---

## Step 8: CSS for session list in `style.css`

- `.chat-session-bar` — thin bar, collapsible
- `.session-list` — scrollable, max-height 200px
- `.session-entry` — flex row: title (truncated) + time + delete
- `.session-entry.active` — highlighted

---

## Step 9: Optional — cross-session search

`GET /api/chat/search?q=<keyword>&snapshot_id=<optional>`:
- Scan all session.json files
- Substring match on content
- Return `[{session_id, title, snippet}]`

Lower priority — skip for MVP.

---

## Validation

### Step 1 — sessions.py

```bash
python -c "import sys; sys.path.insert(0,'D:/pysdp'); from chat.sessions import create_session, add_message, save_session, load_session; s=create_session(); add_message(s,'user','hello',s.active_leaf); save_session(s); s2=load_session(s.id); print(s2.messages)"
```

Expected: session JSON written, message visible after reload.

### Step 2 — fetch_aspect

Chat: "snapshot 1 的 GPU timing 是怎样的？" → `fetch_aspect(1, gpu_timing)` called, data returned.
Re-ask same → NO repeat call (already-fetched dedup works).

### Step 2c — compare_snapshots

Chat: "对比 snapshot 1 和 2" → `compare_snapshots(1, 2)` called, structured comparison returned.
Follow-up: "Character 分类具体情况" → `compare_snapshots(1, 2, focus_category="Character")`.

### Step 3 — prompts.py

```python
from chat.prompts import build_system_prompt
prompt = build_system_prompt(snapshot_ids=[42], session=s)
assert "Available Snapshot Aspects" in prompt
assert "Already Fetched" in prompt  # when session has aspects
```

### Step 4 — context.py

Create session with 20+ messages, call `assemble_context` with tight budget:
- tool_result messages survive while older user messages are dropped
- No orphaned `tool_call` without matching `tool_result`

### Step 4b — stream_chat persistence

1. Message triggers `fetch_aspect` → session JSON contains user + tool_call + tool_result + assistant msgs
2. Re-ask same aspect → no duplicate fetch
3. `execute_python` with chart → PNG in attachments dir, `PERSIST_FULL`
4. Trivial `execute_python` → `CACHE_ONLY`, no tool_result in tree
5. `get_snapshots` → streamed to UI but NOT in session JSON

### Step 5 — session routes

```bash
curl http://localhost:8000/api/chat/sessions          # []
curl -X POST http://localhost:8000/api/chat/sessions  # {"id": "s_..."}
curl http://localhost:8000/api/chat/sessions/<id>     # session JSON
```

### Steps 6-8 — frontend

1. Open chat → session list bar visible
2. "+" → new session, welcome message
3. Send message → session persists, list shows auto-title
4. Refresh → session survives, messages restored
5. Click session → switch, all msg types rendered

### End-to-end

```
POST /api/chat {message, snapshot_ids} (no session_id)
  → done event has session_id; session.json created
POST /api/chat {session_id, message}
  → backend rebuilds from tree; tools persist correctly
Refresh → restore → re-ask aspect → no duplicate fetch
```

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Session file corruption | Atomic write (.tmp + rename) |
| Orphaned tool messages (API 400) | Lazy tool_call persist; context.py keeps pairs atomic |
| session_id not captured | `done` SSE event carries it; frontend must persist to localStorage |
| Attachment path drift | All paths project-relative; single `_persist_tool_attachments` choke point |
| Message ordering | Monotonic IDs; single-user assumption acceptable |
| Breaking request contract | Frontend (Step 6) + backend (Step 5/4b) ship together |
| DC pairing accuracy (compare) | Label match + clocks proximity; fallback pipeline_id |

---

## Files Summary

| Path | Action |
|------|--------|
| `pySdp/chat/sessions.py` | **NEW** — session CRUD, tree traversal, get_fetched_aspects, attachment_dir, auto_title |
| `pySdp/chat/context.py` | **NEW** — layered-reserve assembler |
| `pySdp/chat/llm_client.py` | **MODIFY** — stream_chat becomes session-backed (spine) |
| `pySdp/chat/tools.py` | **MODIFY** — add fetch_aspect, compare_snapshots, recall_history; classify_result; path normalization |
| `pySdp/chat/prompts.py` | **MODIFY** — session param, aspect menu, already-fetched, answer/recap, recall hint |
| `pySdp/webui/routes/files.py` | **MODIFY** — add GET /api/files/project endpoint |
| `pySdp/webui/routes/chat.py` | **MODIFY** — 5 session endpoints; ChatRequest breaking change |
| `pySdp/webui/static/chat.js` | **MODIFY** — session list UX, switchSession, request shape change |
| `pySdp/webui/static/index.html` | **MODIFY** — session bar HTML |
| `pySdp/webui/static/style.css` | **MODIFY** — session bar + entry styles |
| `pySdp/chat/skills/compare.md` | **MODIFY** — update to use compare_snapshots tool |
