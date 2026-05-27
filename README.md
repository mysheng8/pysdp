# pySdp

Snapdragon Profiler data analysis platform — Python layer with WebUI, data layer, analysis services, and a standalone Python client library. Supports visualization of both **Vulkan** and **OpenGL ES** GPU profiling results.

---

## Architecture Overview

```
Browser (localhost:8000)
  └── WebUI SPA (index.html + app.js)
        │
        ├── /api/sdpcli/*    ──proxy──►  SDPCLI Server (localhost:5000)   [frontend only]
        ├── /api/snapshot/*  ──►  Device/app discovery + snapshot workflow
        ├── /api/jobs/*      ──►  C# extraction + Python analysis triggers
        ├── /api/files/*     ──►  Local file serving (read-only)
        ├── /api/data/*      ──►  DuckDB data queries (MCP-exposed)
        ├── /api/events      ──►  SSE real-time push (data change notifications)
        └── /api/logs/*      ──►  Log streaming

pySdp/
  webui/        FastAPI application (backend + static assets + SSE real-time push)
  analysis/     Python analysis services (labeling, status, topdc, etc.)
  data/         DuckDB data layer (ingest, query, models, questions)
  pysdp/        Standalone Python client package (for scripts/CI)
```

---

## Installation & Quick Start

### Windows

```powershell
# 1. Clone
git clone https://github.com/mysheng8/pysdp && cd pysdp

# 2. Configure — create a .env file in the repo root before installing:
#
#    # --- Paths ---
#    PYSDP_PROJECT_DIR=D:/your/project           # SDP files and analysis output
#    PYSDP_VULKAN_SDK_PATH=C:/VulkanSDK/1.x.x    # Vulkan SPIR-V → HLSL via spirv-cross
#                                                 # Download: https://vulkan.lunarg.com/sdk/home
#    PYSDP_IR3_DISASM_PATH=C:/path/to/ir3-disasm.exe  # GLES IR3 disassembly (optional)
#                                                 # Build from Mesa freedreno:
#                                                 # https://gitlab.freedesktop.org/mesa/mesa
#
#    # --- LLM (DrawCall labeling / report generation) ---
#    PYSDP_LLM_API_ENDPOINT=https://...
#    PYSDP_LLM_API_KEY=sk-...
#    PYSDP_LLM_MODEL=vertex_ai/gemini-2.5-flash-lite
#
#    # --- VLM (screenshot description) ---
#    PYSDP_VLM_API_ENDPOINT=https://...
#    PYSDP_VLM_API_KEY=sk-...
#    PYSDP_VLM_MODEL=...
#
#    # --- Chat AI (WebUI sidebar) ---
#    PYSDP_CHAT_API_ENDPOINT=https://...
#    PYSDP_CHAT_API_KEY=sk-...
#    PYSDP_CHAT_MODEL=vertex_ai/gemini-2.5-flash
#
#    See config.ini for all available keys and their defaults.

# 3. Install: creates .venv, installs Python deps, downloads SDPCLI binary,
#             seeds SDPCLI config.ini with paths from .env
.\install.ps1

# 4. Start
.\webui.ps1
```

`webui.ps1` automatically: kills stale port processes → syncs `.env` paths into SDPCLI config → starts SDPCLI Server (if available) → starts WebUI → opens browser. Press **ESC** to stop all processes.

Open **http://localhost:8000** in your browser.

> API docs (Swagger): **http://localhost:8000/api/docs**

### Without SDPCLI (offline / analysis-only)

If SDPCLI binary is not found, `webui.ps1` starts in offline mode — all analysis features work, only live device capture is unavailable.

### Custom ports

```powershell
.\webui.ps1 -Port 8080 -SdpcliPort 5001
```

### SDPCLI binary location

`install.ps1` downloads SDPCLI to `%USERPROFILE%\.pysdp\sdpcli\SDPCLI.exe`.  
To use a custom binary, set `PYSDP_SDPCLI_PATH=C:\path\to\SDPCLI.exe` in `.env` before running `webui.ps1`.

---

## Directory Structure

```
pySdp/
├── webui/
│   ├── server.py              # FastAPI entry point + uvicorn
│   ├── routes/
│   │   ├── proxy.py           # /api/sdpcli/*      → SDPCLI Server passthrough (frontend SPA only)
│   │   ├── snapshot_router.py # /api/snapshot/*    → Device/app discovery + snapshot workflow
│   │   ├── jobs_router.py     # /api/jobs/*        → Extraction/analysis step triggers (C# + Python)
│   │   ├── files.py           # /api/files/*       → File browsing and serving (read-only)
│   │   ├── data.py            # /api/data/*        → DuckDB query endpoints (MCP-exposed)
│   │   └── logs.py            # /api/logs/*        → WebUI log streaming
│   ├── events.py              # SSE event bus (publish/subscribe + /api/events endpoint)
│   ├── jobs.py                # Server-side Pipeline Job (background threads + state management)
│   ├── logger.py              # WebUI logging module
│   └── static/
│       ├── index.html         # Single-page HTML
│       ├── app.js             # Frontend logic (vanilla JS, no build step)
│       └── style.css
├── analysis/
│   ├── label_service.py       # DrawCall rule-based classification → label.json + DB
│   ├── status_service.py      # Percentile statistics → status.json + DB
│   ├── topdc_service.py       # Top-DC bottleneck attribution → topdc.json
│   ├── dashboard_service.py   # Mermaid charts → dashboard.md
│   ├── report_service.py      # LLM analysis report → snapshot_N_report.md
│   ├── mesh_stats_service.py  # OBJ parsing → meshes.json
│   ├── texture_stats_service.py # Texture dimensions → textures.json
│   ├── gles_decompile_service.py # IR3 disasm → GLSL via LLM (batch + single-file recompile)
│   ├── vlm_screenshot_service.py # VLM scene description → scene_description.md
│   ├── llm_wrapper.py         # LLM HTTP client
│   └── models/
│       ├── base.py
│       ├── category_breakdown.py
│       ├── label_quality.py
│       └── top_bottleneck_dcs.py
├── data/
│   ├── db.py                  # WorkspaceDB (DuckDB connection + Schema DDL)
│   ├── ingest.py              # snapshot_dir → DuckDB (idempotent)
│   ├── query.py               # Typed Read API
│   ├── model_registry.py      # Analysis model registry
│   ├── questions.py           # Questions CRUD
│   └── dashboards.py          # Dashboards CRUD
├── pysdp/
│   ├── client.py              # SdpClient (synchronous blocking API)
│   ├── _jobs.py               # JobPoller
│   ├── _models.py             # JobStatus / DeviceInfo data classes
│   └── exceptions.py          # Exception hierarchy
├── examples/
│   ├── snapshot.py
│   └── batch_analysis.py
└── pyproject.toml
```

---

## WebUI Features

### Home Tab

- **SDP Files**: Scans directory and displays `.sdp` file cards; click Explore to open analysis; "+" card opens New Capture modal
- **New Capture Modal**: Three-step workflow (Connect → Launch → Capture)
- **Settings Modal**: Configure SDP / Analysis directories, Snapshot ID, analysis targets
- **Analysis Progress Modal**: Floating progress panel showing stage and percentage

### Explorer Modal (Questions / Explorer / Results sub-tabs)

- **Questions**: R² correlation analysis (0-1 range), Pie / Bar charts, metric button toggles
- **Explorer**: DrawCall list (Category filter, clock bar chart); DC Detail panel with Metrics · Textures · OBJ 3D · Shaders (GLSL ↔ DISASM toggle + Recompile / Relabel buttons)
- **Results**: Snapshot file viewer, inline preview for JSON / Markdown
- Smart screenshot rotation: only portrait screenshots are rotated to landscape

### Logs Tab

WebUI server-side logs with Error / Warning / Info filtering.

### Real-time Updates (SSE)

The page subscribes to data changes via `EventSource('/api/events')`:
- `label_changed` / `labels_changed` — Auto-refresh charts and DC list after relabel
- `ingest_done` — Refresh all views after data import
- `pipeline_done` — Refresh file list + data views after full pipeline completes
- `report_done` — Refresh Questions after analysis report generation

---

## API Route Overview

Full documentation at Swagger: **http://localhost:8000/api/docs**

Routes are grouped by purpose:

| Prefix | Tag | Purpose |
|---|---|---|
| `/api/sdpcli/*` | `frontend` | Frontend SPA passthrough, not documented |
| `/api/snapshot/*` | `snapshot` | Device discovery, snapshot workflow (typed + docs) |
| `/api/jobs/*` | `jobs` | Trigger C# extraction + Python analysis steps |
| `/api/files/*` | `files` | Read-only file serving |
| `/api/data/*` | `data` | DuckDB data queries (MCP-exposed) |
| `/api/events` | — | SSE real-time push (data changes → browser auto-refresh) |

See [docs/explanations/EXPLAIN-api.md](../docs/explanations/EXPLAIN-api.md) for detailed endpoint list.

---

## Data Layer (data/)

### WorkspaceDB

Singleton DuckDB connection with the following schema:

| Table | Description |
|---|---|
| `snapshots` | Metadata for each ingest (path, sdp_name, snap_index, ingested_at) |
| `draw_calls` | DC base parameters (api_id, vertex count, instance count, etc.) |
| `labels` | Classification results (category, subcategory, confidence, reason_tags) |
| `metrics` | GPU counters (clocks, fragments_shaded, tex_fetch_stall_pct, ~50 columns) |
| `shader_stages` | Pipeline → Shader Stage mapping |
| `textures` / `meshes` | Asset paths |
| `questions` / `dashboards` | User-defined analysis queries |

### ingest.py

`ingest_snapshot(db, snapshot_dir)` — Idempotent, safe to call repeatedly. Reads `dc.json`, `label.json`, `metrics.json`, `shaders.json`, `textures.json`, `buffers.json` and writes to DuckDB. `snapshot_dir` is the unique key; `snap_index` preserves the original C# numbering.

---

## Analysis Services (analysis/)

Python pipeline execution order (runs after C# writes JSON outputs):

```
screenshot → mesh_stats → texture_stats → gles_decompile → ingest → label → status → topdc → describe → report
```

Each step is non-fatal: a single step failure does not affect already-completed steps.

| Service | Input | Output | Writes DB |
|---|---|---|---|
| `mesh_stats_service` | `meshes/*.obj` | `meshes/meshes.json` | ✓ (re-ingest) |
| `texture_stats_service` | `textures/` | `textures/textures.json` | ✓ (re-ingest) |
| `gles_decompile_service` | `shaders.json` (IR3 disasm) | per-shader `.glsl` files | — |
| `label_service` | `dc.json` + `shaders.json` | `label.json` | ✓ |
| `status_service` | `dc.json` + `label.json` + `metrics.json` | `status.json` | ✓ |
| `topdc_service` | `status.json` + `attribution_rules.json` | `topdc.json` | — |
| `vlm_screenshot_service` | screenshot + label/metrics | `scene_description.md` | ✓ |
| `report_service` | screenshot + `status.json` + `topdc.json` | `snapshot_N_report.md` | — |

**GLES-specific**: Shaders in `shaders.json` come from IR3 disassembly or LLM-reconstructed GLSL (not Vulkan's SPIR-V→HLSL). `label_service` handles both formats transparently without needing to distinguish API type.

---

## pysdp Client Package

Standalone package, no WebUI dependency — use directly in scripts or CI:

```python
from pysdp import SdpClient

client = SdpClient("http://localhost:5000")
client.connect()
client.launch("com.example.app/.MainActivity")
result = client.capture()
analysis = client.analyze(
    sdp_path=result["sdpPath"],
    snapshot_id=result["captureId"],
    targets="label,metrics,status,topdc",
)
print(analysis["captureDir"])
```

| Method | Description |
|---|---|
| `connect(device_id=None)` | Connect to device |
| `launch(package_activity)` | Launch app |
| `capture(output_dir=None, label=None)` | Trigger frame capture |
| `analyze(sdp_path, snapshot_id, targets=None)` | Offline analysis |
| `disconnect()` | Disconnect device |
| `device_status()` | Query device status |

Exceptions: `SdpStateError` / `SdpJobError` / `SdpTimeoutError` / `SdpConnectionError`

---

## Configuration

Settings are resolved in priority order:

1. **Environment variables** (`PYSDP_*`) — highest priority
2. **`.env` file** — for local development secrets (git-ignored)
3. **`config.ini`** — committed defaults (no secrets)
4. **`../SDPCLI/config.ini` + `secrets.ini`** — monorepo fallback (auto-detected)

See `.env.example` for available variables.

### Log Level

Set `PyLogLevel=debug|info|warning|error` in `config.ini` or `PYSDP_LOG_LEVEL` env var.

---

## Key Constraints

- **DuckDB connection**: `WorkspaceDB` is an in-process singleton; all queries use `db.cursor()` (independent cursors)
- **snapshot_id conflicts**: `snapshot_dir` is the unique key; C# session-local numbering may overlap, ingest auto-assigns globally unique IDs while `snap_index` preserves original numbering
- **Render Targets**: Not stored in DuckDB, read at runtime from `dc.json`; GLES captures correctly distinguish Color / Depth / Stencil attachment types
- **Screenshot**: Prefers analysis directory cache, falls back to extracting from `.sdp` ZIP at `snapshot_N/*.bmp`
- **GLES Shader format**: Vulkan outputs HLSL ([spirv-cross](https://vulkan.lunarg.com/sdk/home)), GLES outputs GLSL (IR3→LLM decompile) or raw IR3 disasm ([Mesa freedreno ir3-disasm](https://gitlab.freedesktop.org/mesa/mesa)); `label_service` handles both transparently
- **MCP**: Exposes 19 read-only query endpoints via `fastapi-mcp`; mount point `/mcp`

---

## GLES Shader Decompile (ir3-disasm)

GLES captures use Adreno IR3 disassembly. `ir3-disasm` is built from [Mesa freedreno](https://gitlab.freedesktop.org/mesa/mesa) and must be configured via `PYSDP_IR3_DISASM_PATH`. The chip ID is auto-detected at runtime from `dc.json`; override with `PYSDP_IR3_CHIP_ID` if needed.

Supported Adreno GPUs:

| Chip ID | GPU | Device examples |
|---|---|---|
| 0x06030001 | Adreno 660 | Snapdragon 888 |
| 0x06030500 | Adreno 7c+ Gen 3 / 8c Gen 3 | Snapdragon 7c+ Gen 3, QCM6490 |
| 0x06060201 | Adreno 662 (FD644) | — |
| 0x06060300 | FD663 | — |
| 0x07002000 | FD702 | QRB2210 |
| 0x07030001 | Adreno 730 | Snapdragon 8 Gen 1 |
| 0x07030002 | Adreno 725 | Snapdragon 7s Gen 2 |
| 0x43030B00 | FD735 | — |
| 0x43030c00 | Adreno X1-45 | Snapdragon X Plus |
| 0x43050a00 | Adreno A32 | G3x Gen 2 |
| 0x43050a01 | Adreno 740 | Snapdragon 8 Gen 2 |
| 0x43050b00 | Adreno 740 v3 | Meta Quest 3 |
| 0x43050c01 | Adreno X1-85 | Snapdragon X Elite |
| 0x43051401 | Adreno 750 | Snapdragon 8 Gen 3 |
| 0x44010000 | Adreno 810 | Snapdragon 8 Elite |
| 0x44030a20 | Adreno 829 | — |
| 0x44050001 | Adreno 830 | Snapdragon 8 Elite (variant) |
| 0x44050A31 | Adreno 840 | — |
| 0x44070041 | Adreno X2-85 | Snapdragon X2 Elite |
