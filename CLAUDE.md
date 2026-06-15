# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**pysdp** is a Snapdragon Profiler data analysis and visualization platform. It ingests GPU profiling capture data (Vulkan/GLES), stores it in DuckDB, runs rule-based and LLM-powered analysis pipelines, and exposes everything via a FastAPI backend + vanilla JS single-page app. An external C# binary (SDPCLI) handles device connection and raw data extraction; pysdp sits on top of that.

## Commands

```bash
# Install (also downloads SDPCLI binary via setup.py post-install hook)
pip install -e .

# Start the WebUI server
pysdp --port 8000
python -m pysdp --port 8000
python webui/server.py --port 8000

# With live device capture via SDPCLI server
pysdp --port 8000 --sdpcli http://localhost:5000

# Monorepo dev shortcut (kills stale processes, reinstalls, starts server)
./webui.ps1

# Run tests (no test files exist yet; pytest is the intended runner)
pytest

# API docs (Swagger UI, available while server is running)
# http://localhost:8000/api/docs
```

## Configuration

The config system resolves in priority order: `PYSDP_*` env vars → `.env` file → `config.ini` → monorepo fallback defaults. Copy `.env.example` to `.env` and set API keys there. `config.py` is a thread-safe singleton with 60+ keys; `config.ini` holds the committed defaults (LLM model, VLM endpoint, analysis categories, WebUI defaults).

## Architecture

### Three-tier system

```
Browser (SPA: webui/static/)
    └─ FastAPI backend (webui/server.py)
         ├─ analysis/      Python analysis pipeline services
         ├─ data/          DuckDB data layer
         ├─ chat/          AI GPU profiling assistant
         └─ pysdp/         Standalone sync client library
```

### Key modules

| Module | Role |
|---|---|
| `webui/server.py` | FastAPI app entry point — mounts routes, initializes DuckDB workspace, mounts MCP server |
| `webui/routes/` | 7 routers: proxy (→SDPCLI), snapshot, jobs, files, data, logs, chat |
| `webui/jobs.py` | Background pipeline job manager — runs ingest→label→status→topdc→analysis_md in a thread |
| `webui/events.py` | SSE bus (`publish()`) — all real-time browser updates flow through here |
| `analysis/` | Services: `label_service`, `status_service`, `topdc_service`, `gles_decompile_service`, `vlm_screenshot_service`, `analysis_md_service`, `dashboard_service`, `report_service`, mesh/texture stats |
| `analysis/llm_wrapper.py` | OpenAI-compatible LLM client with SHA-256 ring-pool response cache |
| `analysis/models/` | 4 typed result models used by the chat skills: `category_breakdown`, `label_quality`, `top_bottleneck_dcs`, `base` |
| `data/db.py` | `WorkspaceDB` singleton — DuckDB schema DDL (11+ tables) |
| `data/ingest.py` | Parses C# JSON snapshot outputs → DuckDB (idempotent) |
| `data/query.py` | Typed read API: `get_draw_calls`, `get_metrics`, `get_correlations`, etc. |
| `pysdp/client.py` | `SdpClient` — synchronous blocking API for scripts/CI use; `pysdp/exceptions.py` defines the error hierarchy |
| `chat/` | AI assistant: system prompt builder, async streaming LLM client, sandboxed Python exec, tool definitions |
| `chat/skills/` | 5 deterministic skills invoked by the AI: `bottlenecks`, `breakdown`, `compare`, `correlate`, `mesh_ratio` |

### Analysis pipeline

Triggered by `POST /api/jobs/pipeline`. Steps run in order; each is independently non-fatal:

```
screenshot → mesh_stats → texture_stats → ingest → label → status → topdc → analysis_md → scene_describe
```

Each step publishes SSE events (`label_changed`, `ingest_done`, `pipeline_done`, `report_done`) that the browser consumes via `EventSource` to auto-refresh without polling.

### Frontend

Pure vanilla JS — no build step. `webui/static/app.js` (157KB) and `webui/static/chat.js` (27KB) are edited directly. Charts are generated server-side via matplotlib and served as images.

### LLM integration

`litellm` abstracts all LLM/VLM providers. `label_service` uses both rule-based heuristics (keyword + geometry) and LLM classification with response caching. `gles_decompile_service` converts IR3 disasm → GLSL via LLM. Configure the model and endpoint in `config.ini` or via env vars.

### MCP exposure

The FastAPI app mounts a `fastapi-mcp` server, exposing the `/api/data/*` routes as MCP tools so Claude can query the DuckDB profiling data directly.

## DuckDB Schema

Core tables: `snapshots`, `draw_calls`, `labels`, `metrics` (~50 GPU counter columns), `shader_stages`, `textures`, `meshes`, `dc_render_targets`, `questions`, `dashboards`, `projects`, `versions`. Schema DDL lives in `data/db.py`.
