# pysdp

## What is Snapdragon Profiler?

[Snapdragon Profiler](https://developer.qualcomm.com/software/snapdragon-profiler) is Qualcomm's official GPU profiling tool for devices with Adreno GPUs (phones, XR headsets, PCs). It performs frame-level capture and collects per-Draw-Call parameters, GPU hardware counters (clocks, bandwidth, shader stall, etc.), shader source code, textures, and mesh resources.

Output format: `.sdp` file (a ZIP containing capture metadata + binary assets).

## What is pysdp?

pysdp is an **automated analysis platform** built on top of Snapdragon Profiler capture data. It solves three core problems:

- **The official tool can only view, not analyze** — Snapdragon Profiler displays raw data but doesn't tell you where the bottlenecks are or which DCs are problematic
- **Manual analysis is extremely inefficient** — a single frame may have hundreds to thousands of Draw Calls, making manual inspection impractical
- **Requires deep GPU architecture expertise** — understanding Adreno metrics requires specialized knowledge

pysdp automates via rule engine + LLM: Draw Call classification (UI / scene / shadow / post-processing, etc.), performance bottleneck attribution (shader bound / bandwidth bound / geometry bound), Top-N problematic DC identification, and AI-generated optimization reports.

End-to-end flow: **Capture → C# extraction → Python analysis → WebUI visualization + AI Chat interactive queries**.

---

## Requirements

| Requirement | Details |
|-------------|---------|
| OS | Windows 10/11 (x64) |
| Python | 3.10+ |
| ADB | Android Debug Bridge (for device connection) |
| USB | USB debugging enabled on target device |
| LLM API | Any OpenAI-compatible endpoint (for labeling, decompile, chat) |

> SDPCLI (the C# capture/extraction binary) is Windows-only. The Python analysis and WebUI themselves are cross-platform, but the full capture workflow requires Windows.

## Supported Devices

pysdp works with any device running a **Qualcomm Snapdragon SoC with Adreno GPU**. This includes:

**Phones:**
- Snapdragon 8 Elite / 8 Gen 3 / 8 Gen 2 / 8 Gen 1
- Snapdragon 7 series (7s Gen 2, 7+ Gen 2, etc.)
- Snapdragon 888 / 865 / 855 and older (with supported Adreno GPU)

**PC / Always-on-PC:**
- Snapdragon X Elite / X Plus (Adreno X1-85 / X1-45)
- Snapdragon X2 Elite (Adreno X2-85)

**Supported APIs:** Vulkan and GLES (OpenGL ES)

> For the full list of supported Adreno GPUs (with chip IDs for ir3-disasm shader decompilation), see [GLES Shader Decompile](#gles-shader-decompile-ir3-disasm) below.

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
        ├── /api/chat/*      ──►  AI assistant (LLM chat with GPU data context)
        ├── /api/events      ──►  SSE real-time push (data change notifications)
        └── /api/logs/*      ──►  Log streaming

pysdp/
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
#
#    # --- LLM (DrawCall labeling / IR3→GLSL decompile / report generation) ---
#    # High-frequency batch calls (hundreds per frame), use cheapest lightweight model
#    PYSDP_LLM_API_ENDPOINT=https://...
#    PYSDP_LLM_API_KEY=sk-...
#    PYSDP_LLM_MODEL=vertex_ai/gemini-2.5-flash-lite
#
#    # --- VLM (screenshot scene description) ---
#    # Requires vision capability, use a multimodal model that accepts image input
#    PYSDP_VLM_API_ENDPOINT=https://...
#    PYSDP_VLM_API_KEY=sk-...
#    PYSDP_VLM_MODEL=...
#
#    # --- Chat AI (WebUI sidebar assistant) ---
#    # Interactive conversation, needs stronger reasoning, use mid/high-tier model
#    PYSDP_CHAT_API_ENDPOINT=https://...
#    PYSDP_CHAT_API_KEY=sk-...
#    PYSDP_CHAT_MODEL=vertex_ai/gemini-2.5-flash
#
#    See config.ini for all available keys and their defaults.

# 3. Install: creates .venv, installs Python deps, downloads SDPCLI binary
.\install.ps1

# 4. Start
.\webui.ps1
```

`webui.ps1` automatically: kills stale port processes → loads `.env` → starts SDPCLI Server with `-projectdir` (if available) → starts WebUI → opens browser. Press **ESC** to stop all processes.

Open **http://localhost:8000** in your browser.

> API docs (Swagger): **http://localhost:8000/api/docs**

### SDPCLI requirement

pysdp requires SDPCLI for both live capture and offline analysis (C# extraction step). `install.ps1` downloads it automatically; if missing, `webui.ps1` will fail to start.

### Project Directory

`ProjectDir` is pysdp's core working directory — all data lives here:

```
ProjectDir/
├── sdp/              # .sdp files output by SDPCLI capture
└── analysis/         # Python analysis pipeline output (one subdirectory per capture)
    └── <run_name>/
        └── snapshot_N/
            ├── dc.json, metrics.json, label.json, shaders.json, ...
            ├── shaders/          # disasm + glsl files
            ├── textures/         # texture files
            └── meshes/           # OBJ mesh files
```

Configuration:

- **Recommended**: set `PYSDP_PROJECT_DIR=D:/your/project` in `.env`
- **Command-line override**: `.\webui.ps1 -ProjectDir D:\other\project` (takes precedence over .env)

`webui.ps1` reads ProjectDir on startup and passes it to SDPCLI via the `-projectdir` argument automatically.

### Custom ports

```powershell
.\webui.ps1 -Port 8080 -SdpcliPort 5001
```

### SDPCLI binary location

`install.ps1` downloads SDPCLI to `%USERPROFILE%\.pysdp\sdpcli\SDPCLI.exe`.  
To force re-download (e.g. after version bump in `pyproject.toml`): `.\install.ps1 -Force`  
To use a custom binary, set `PYSDP_SDPCLI_PATH=C:\path\to\SDPCLI.exe` in `.env` before running `webui.ps1`.

---

## Quick Start Guide

### Capture + Analysis (WebUI workflow)

1. **Click the "+" card on the Home page** → opens the New Capture modal
2. **Step 0 — Select Project & Version** (for data organization, can be changed later)
3. **Step 1 — Connect** → select device → click Connect (wait for connection)
4. **Step 2 — Launch** → select app package → choose Vulkan/GLES → click Launch
5. **Step 3 — Capture** → navigate the app to the target scene → click Capture (wait for completion)
6. **Click "Analyze →"** → automatically runs C# extraction + Python analysis pipeline

After analysis completes, a thumbnail appears on the Home card. Double-click to enter the Explorer.

### About .sdp files

Capture must be performed using pysdp's built-in workflow (via SDPCLI) in order to run analysis. The resulting `.sdp` files are compatible with the official Snapdragon Profiler and can be opened there as well.

If you already have `.sdp` files captured through pysdp (e.g. received from a colleague):

1. Place the `.sdp` file in `ProjectDir/sdp/`
2. Start WebUI → the Home page auto-scans and displays the file
3. Select the card → click Analyze → wait for completion
4. Double-click the card to enter Explorer

### Explorer UI

- **Left panel — Draw Call list**
  - **Params tab**: DC call parameters, expand to see sub-calls (setpass)
  - **Metrics tab**: GPU hardware counters (clocks, bandwidth, shader busy %, etc.)
  - **Label tab**: AI classification results (category / subcategory / confidence)
  - Category filter dropdown to filter by label
  - Click column headers to sort

- **Right panel — DC detail**
  - Metrics heatmap (red = metric significantly above median)
  - Shader source viewer (GLSL ↔ DISASM toggle, Recompile support)
  - Texture list + preview
  - Mesh 3D preview (OBJ)
  - Render Target info

### AI Chat

Click the Chat button (top-right) to open the sidebar. Supports natural language queries on GPU data:

- "What are the top 5 most expensive DCs in this frame?"
- "How many clocks do UI-category DCs consume?"
- "Which DCs have the worst fragment shader stall?"

You can pin a specific snapshot as the query context.

---

## WebUI Features

### Home Tab

- **SDP Files**: Scans directory and displays `.sdp` file cards; click Explore to open analysis; "+" card opens New Capture modal
- **New Capture Modal**: Four-step workflow (Project & Version → Connect → Launch → Capture)
- **Settings Modal**: Configure SDP / Analysis directories, Snapshot ID, analysis targets
- **Analysis Progress Modal**: Floating progress panel showing stage and percentage

### Explorer Modal (Questions / Explorer / Results sub-tabs)

- **Questions**: R² correlation analysis (0-1 range), Pie / Bar charts, metric button toggles
- **Explorer**: DrawCall list (Category filter, clock bar chart, Params/Metrics/Label tabs); Params tab shows DC parameters with expandable setpass tree; DC Detail panel with Metrics · Textures · OBJ 3D · Shaders (GLSL ↔ DISASM toggle + Recompile / Relabel buttons)
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

---

## Data Layer (data/)

### WorkspaceDB

Singleton DuckDB connection with the following schema:

| Table | Description |
|---|---|
| `snapshots` | Metadata for each ingest (path, sdp_name, snap_index, ingested_at) |
| `draw_calls` | DC base parameters (api_id, vertex count, instance count, parameters, etc.) |
| `setpass` | Per-DC state-setting calls (call_id, call_name, parameters) |
| `labels` | Classification results (category, subcategory, confidence, reason_tags) |
| `metrics` | GPU counters (clocks, fragments_shaded, tex_fetch_stall_pct, ~50 columns) |
| `shader_stages` / `dc_shader_stages` | Pipeline → Shader Stage mapping + per-DC binding |
| `textures` / `dc_textures` / `meshes` | Asset metadata + per-DC binding |
| `dc_render_targets` | Per-DC render target attachments (format, dimensions) |
| `questions` / `dashboards` | User-defined analysis queries |

### ingest.py

`ingest_snapshot(db, snapshot_dir)` — Idempotent, safe to call repeatedly. Reads `dc.json` (including inline `setpass` arrays), `label.json`, `metrics.json`, `shaders.json`, `textures.json`, `buffers.json` and writes to DuckDB. `snapshot_dir` is the unique key; `snap_index` preserves the original C# numbering. Shader stage names are normalized to title-case on ingest to prevent duplicates.

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

See `.env.example` for available variables.

### Log Level

Set `PyLogLevel=debug|info|warning|error` in `config.ini` or `PYSDP_LOG_LEVEL` env var.

---

## Key Constraints

- **DuckDB connection**: `WorkspaceDB` is an in-process singleton; all queries use `db.cursor()` (independent cursors)
- **snapshot_id conflicts**: `snapshot_dir` is the unique key; C# session-local numbering may overlap, ingest auto-assigns globally unique IDs while `snap_index` preserves original numbering
- **Render Targets**: Stored in `dc_render_targets` table; GLES captures correctly distinguish Color / Depth / Stencil attachment types
- **Screenshot**: Prefers analysis directory cache, falls back to extracting from `.sdp` ZIP at `snapshot_N/*.bmp`
- **GLES Shader format**: Vulkan outputs HLSL ([spirv-cross](https://vulkan.lunarg.com/sdk/home)), GLES outputs GLSL (IR3→LLM decompile) or raw IR3 disasm ([Mesa freedreno ir3-disasm](https://gitlab.freedesktop.org/mesa/mesa)); `label_service` handles both transparently
---

## MCP Server

pysdp exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server via [`fastapi-mcp`](https://github.com/tadata-org/fastapi-mcp), allowing AI assistants (Claude, Cursor, etc.) to directly query GPU profiling data.

**Endpoint:** `http://localhost:8000/mcp`

**Available tools (19 read-only operations):**

| Tool | Description |
|------|-------------|
| `get_snapshots` | List all captured snapshots |
| `get_draw_calls` | Query draw calls with filtering/sorting |
| `get_dc_detail` | Full detail for a single draw call (params, metrics, shaders, textures) |
| `get_available_metrics` | List available GPU counter columns |
| `get_label_correlations` | Metric correlations grouped by label category |
| `get_clock_correlation` | Clock cycle correlation analysis |
| `get_label_agg_multi` | Aggregate metrics across multiple labels |
| `get_label_agg` | Aggregate metrics for a single label |
| `get_label_agg_all` | Aggregate metrics for all labels |
| `get_label_metrics` | Per-DC metrics within a label |
| `get_models` | List registered analysis models |
| `get_questions` | List saved questions |
| `get_question` | Execute a saved question query |
| `get_dashboards` | List dashboards |
| `get_dashboard` | Get dashboard detail |
| `get_file_read` | Read text file content |
| `get_file_raw` | Serve raw file (binary) |
| `get_file_image` | Serve image file with optional resize |

**Usage with Claude Desktop / Claude Code:**

Add to your MCP config (`claude_desktop_config.json` or `.mcp.json`):

```json
{
  "mcpServers": {
    "pysdp": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Then you can ask Claude things like "What are the top 5 most expensive draw calls?" or "Show me bandwidth usage by category" — Claude will call the MCP tools to query your profiling data directly.

---

## GLES Shader Decompile (ir3-disasm)

GLES captures use Adreno IR3 disassembly. `ir3-disasm` ([Mesa freedreno](https://gitlab.freedesktop.org/mesa/mesa)) is bundled in the SDPCLI package — no manual configuration needed. The chip ID is detected automatically by SDPCLI at capture time.

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

---

## FAQ

### Installation & Startup

**Q: `install.ps1` fails to download SDPCLI**  
A: Check your network connection. If behind a corporate proxy, set `HTTP_PROXY` / `HTTPS_PROXY` env vars before running. You can also manually download from [sdpcli-releases](https://github.com/mysheng8/sdpcli-releases/releases) and extract to `%USERPROFILE%\.pysdp\sdpcli\`.

**Q: `webui.ps1` reports "port already in use"**  
A: Another process is occupying port 8000 or 5000. Either kill it manually (`netstat -ano | findstr :8000`) or use a different port: `.\webui.ps1 -Port 8080 -SdpcliPort 5001`.

**Q: Python version error or missing modules**  
A: pysdp requires Python 3.10+. Run `python --version` to confirm. If you have multiple Python installations, ensure the correct one is first in PATH. Delete `.venv/` and re-run `.\install.ps1`.

**Q: `.env` changes don't take effect**  
A: `webui.ps1` reads `.env` on every startup — you must restart the server. Make sure the file is named exactly `.env` (not `.env.txt`) and is in the repo root.

**Q: SDPCLI version mismatch after update**  
A: Run `.\install.ps1 -Force` to re-download the version specified in `pyproject.toml`.

### Capture & Analysis

**Q: Device not detected in Step 1 (Connect)**  
A: Ensure USB Debugging is enabled on the device. Run `adb devices` to verify the device is visible. If the device shows as "unauthorized", accept the USB debugging prompt on the device screen.

**Q: Connect succeeds but Launch fails**  
A: The app package name may be wrong — use the refresh button to re-scan. For GLES apps, make sure "GLES" is selected (not Vulkan). Some apps with anti-debugging protection may refuse to launch under profiling.

**Q: Capture hangs or times out**  
A: The app must be actively rendering. If the app is paused or in background, the capture will stall. Navigate to a scene with active rendering, then click Capture.

**Q: Analysis fails at "ingest" step**  
A: Usually means the C# extraction produced incomplete data. Check the Logs tab for details. Try re-running: select the card → Analyze again.

**Q: Shaders show "No shader extracted"**  
A: The device's GPU chip may not be supported by ir3-disasm (GLES), or the capture didn't include shader data. Check the supported GPU list above. For Vulkan captures, shaders should always be available via SPIR-V.

**Q: Only DISASM shown, no GLSL toggle**  
A: GLSL is generated by LLM decompilation (the `gles_decompile` pipeline step). If LLM keys are not configured in `.env`, or the decompile step failed, only raw DISASM will be available. Check Logs for decompile errors. You can also click "Recompile" on a single shader to retry.

**Q: Labels are all "Unknown" or missing**  
A: The label step requires a working LLM endpoint. Verify `PYSDP_LLM_API_ENDPOINT` and `PYSDP_LLM_API_KEY` in `.env`. Check Logs for 401/403/timeout errors.

**Q: Can I use .sdp files captured with the official Snapdragon Profiler?**  
A: No. Only `.sdp` files captured through pysdp's SDPCLI can be analyzed, because the C# extraction step relies on SDPCLI-specific metadata. However, `.sdp` files captured by pysdp *can* be opened in the official Snapdragon Profiler.
