---
type: finding
topic: metrics display mismatch
status: investigated
related_paths:
  - data/query.py
  - webui/static/app.js
  - webui/routes/data.py
related_tags:
  - metrics
  - frontend
  - data-layer
summary: Frontend metrics tab和DC detail面板metrics显示不全的根因是get_draw_calls查询只返回13个硬编码的metric列，而DB实际有17个列有数据，导致5个有数据的列（time_shading_fragments_pct, time_shading_vertices_pct, textures_per_fragment, preemptions, avg_preemption_delay）在metrics tab中无法显示
last_updated: 2026-06-09
---

# Finding: Metrics Display Mismatch

## Problem Statement

用户报告在 snapshot_id=11 中，DB 有 17 个 metric 列有数据，但前端 metrics tab 和 DC detail 面板不显示数据。需要调研数据流从 API 到前端渲染的哪个环节断了，以及前端硬编码的列定义是否与 DB 实际数据对齐。

## Evidence

### 1. Backend Query Layer (`data/query.py`)

**`get_draw_calls` 函数** (lines 50-132) 返回 draw calls 列表时，只查询了 **13 个硬编码的 metric 列**:

```python
# Lines 93-104
m.clocks,
m.fragments_shaded,
m.vertices_shaded,
m.read_total_bytes,
m.write_total_bytes,
m.shaders_busy_pct,
m.shaders_stalled_pct,
m.time_alus_working_pct,
m.tex_fetch_stall_pct,
m.tex_l1_miss_pct,
m.tex_pipes_busy_pct,
m.lrz_pixels_killed
```

**`get_dc_detail` 函数** (lines 199-350) 返回单个 DC 详细信息时，使用 `SELECT * FROM metrics`，会返回 **所有可用的 metric 列**:

```python
# Line 242
metrics_rows = _rows_to_dicts(db.cursor().execute(
    "SELECT * FROM metrics WHERE snapshot_id = ? AND api_id = ?",
    [snapshot_id, api_id],
))
```

### 2. API Layer (`webui/routes/data.py`)

`/api/data/draw_calls` 端点 (lines 164-186) 直接调用 `get_draw_calls`，因此只返回 13 个 metric 列。

`/api/data/dc/{api_id}` 端点 (lines 188-217) 调用 `get_dc_detail`，因此返回所有 metric 列。

### 3. Frontend Layer (`webui/static/app.js`)

**Metrics Tab 列定义** (lines 2535-2549) 硬编码了 **12 个 metric 列** (与 `get_draw_calls` 返回的 13 列对齐，但缺少 `tex_l1_miss_pct` 在显示中被包含):

```javascript
if (tab === 'metrics') return [
  SEQ, NAME,
  { key: 'clocks',                 label: 'Clocks',        val: dc => dc.clocks               ?? '—' },
  { key: 'fragments_shaded',       label: 'Frags',         val: dc => dc.fragments_shaded      ?? '—' },
  { key: 'vertices_shaded',        label: 'Verts',         val: dc => dc.vertices_shaded       ?? '—' },
  { key: 'read_total_bytes',       label: 'Read(B)',       val: dc => dc.read_total_bytes      ?? '—' },
  { key: 'write_total_bytes',      label: 'Write(B)',      val: dc => dc.write_total_bytes     ?? '—' },
  { key: 'shaders_busy_pct',       label: 'ShBusy%',      val: dc => _fmt1(dc.shaders_busy_pct) },
  { key: 'shaders_stalled_pct',    label: 'ShStall%',     val: dc => _fmt1(dc.shaders_stalled_pct) },
  { key: 'time_alus_working_pct',  label: 'ALU%',         val: dc => _fmt1(dc.time_alus_working_pct) },
  { key: 'tex_fetch_stall_pct',    label: 'TexStall%',    val: dc => _fmt1(dc.tex_fetch_stall_pct) },
  { key: 'tex_l1_miss_pct',        label: 'TexL1Miss%',   val: dc => _fmt1(dc.tex_l1_miss_pct) },
  { key: 'tex_pipes_busy_pct',     label: 'TexPipes%',    val: dc => _fmt1(dc.tex_pipes_busy_pct) },
  { key: 'lrz_pixels_killed',      label: 'LRZ',          val: dc => dc.lrz_pixels_killed     ?? '—' },
];
```

**DC Detail Panel Metrics 渲染** (lines 3017-3059) 遍历 `dc.metrics` 对象的所有 key-value pairs，因此能显示所有可用的 metric 列:

```javascript
// Lines 3021-3025
if (dc.metrics && Object.keys(dc.metrics).length > 0) {
  const metricEntries = Object.entries(dc.metrics).filter(([k]) =>
    !['snapshot_id', 'api_id'].includes(k));
  metricEntries.forEach(([k, v]) => {
    // render each metric row with heatmap background
```

### 4. Database Layer (`data/ingest.py`)

`ingest.py` 在导入 metrics 时，会动态发现 metrics.json 中实际存在的列 (lines 518-588)，支持的完整列表包含 50+ 个 metric 列 (lines 530-553)，其中包括:

- `time_shading_fragments_pct`
- `time_shading_vertices_pct`
- `textures_per_fragment`
- `preemptions`
- `avg_preemption_delay`

这些列在 snapshot_id=11 中有数据，但在 `get_draw_calls` 的硬编码列表中缺失。

### 5. DB 实际数据

根据用户提供的信息，snapshot_id=11 有 **17 个 metric 列有数据**:

- clocks ✓ (在 query 中)
- shaders_busy_pct ✓
- shaders_stalled_pct ✓
- time_alus_working_pct ✓
- **time_shading_fragments_pct** ✗ (缺失)
- **time_shading_vertices_pct** ✗ (缺失)
- fragments_shaded ✓
- vertices_shaded ✓
- lrz_pixels_killed ✓
- **textures_per_fragment** ✗ (缺失)
- tex_fetch_stall_pct ✓
- tex_l1_miss_pct ✓
- tex_pipes_busy_pct ✓
- read_total_bytes ✓
- write_total_bytes ✓
- **preemptions** ✗ (缺失)
- **avg_preemption_delay** ✗ (缺失)

13 个列在 `get_draw_calls` 查询中存在，**5 个列缺失**。

## Analysis

### Root Cause

数据流断裂发生在 **`data/query.py` 的 `get_draw_calls` 函数**:

1. **DB 层**: 所有 17 个 metric 列都正确存储在 `metrics` 表中
2. **Query 层断裂**: `get_draw_calls` 只返回 13 个硬编码的 metric 列，丢失 5 个有数据的列
3. **API 层**: `/api/data/draw_calls` 直接返回 query 结果，继承了列缺失问题
4. **Frontend 层**: metrics tab 的列定义硬编码为那 13 个列，无法显示缺失的 5 个列

### Why DC Detail Panel Works

DC detail panel 调用的是 `get_dc_detail` → 使用 `SELECT * FROM metrics` → 返回所有列 → 前端动态遍历 `dc.metrics` 的所有 keys → **因此能正确显示所有 17 个 metric 列**。

### Design Inconsistency

系统存在两种不同的设计模式:

1. **Metrics Tab (列表视图)**: 硬编码列 → 性能好，但不灵活，需手动维护
2. **DC Detail Panel (详情视图)**: 动态列 → 灵活，但需要所有列在 API 响应中

问题在于 `get_draw_calls` 使用了硬编码列模式，但前端 metrics tab 期望显示的列集合 **必须** 匹配 query 返回的列集合，否则会出现:
- 数据存在但不显示 (当前问题)
- 前端定义了列但 API 不返回 (显示 '—')

### No Config-Based Whitelist

`config.ini` 中 **没有 `MetricsWhitelist` 配置项**。代码中提到的 "MetricsWhitelist" 是指 SDPCLI 在 capture 时的配置，决定了哪些 GPU counter 会被采集到 metrics.json 中。

pysdp 的 `ingest.py` 会动态发现 metrics.json 中实际存在的列并导入，但 `get_draw_calls` 没有动态适配，而是硬编码了一个固定子集。

## Impact

### User-Facing Impact

1. **Metrics Tab 数据不完整**: 用户无法在 DC 列表的 metrics tab 中看到 5 个有数据的 metric 列
2. **DC Detail Panel 正常工作**: 单个 DC 详情面板能正确显示所有 17 个 metric 列
3. **数据一致性混淆**: 同一份数据在不同 UI 区域显示不一致，用户体验差

### Affected Metrics (snapshot_id=11)

缺失的 5 个 metric 列:
- `time_shading_fragments_pct` - 重要的 fragment shader 性能指标
- `time_shading_vertices_pct` - 重要的 vertex shader 性能指标
- `textures_per_fragment` - 重要的纹理采样密度指标
- `preemptions` - GPU preemption 次数
- `avg_preemption_delay` - 平均 preemption 延迟

这些都是性能分析中有价值的指标。

### System-Wide Impact

任何未来新增的 GPU counter (通过 SDPCLI 的 MetricsWhitelist 启用) 都会遇到同样的问题，除非手动修改 `get_draw_calls` 和前端列定义。

## Related Context

- Related findings: [FINDING-2026-05-26-path-handling.md](FINDING-2026-05-26-path-handling.md) (path handling logic)
- Related plans: (plan will be created next)
- Related implementations: (none yet)
