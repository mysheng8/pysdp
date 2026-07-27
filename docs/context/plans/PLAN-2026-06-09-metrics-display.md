---
type: plan
topic: fix metrics display and make system dynamic
status: proposed
based_on:
  - FINDING-2026-06-09-metrics-display.md
related_paths:
  - data/query.py
  - webui/static/app.js
  - webui/routes/data.py
related_tags:
  - metrics
  - frontend
  - data-layer
summary: 修复 metrics tab 显示缺失问题，并将系统从硬编码列模式改为动态列模式，使前端能自动适配 DB 中实际存在的 metric 列
last_updated: 2026-06-09
---

# Plan: Fix Metrics Display and Make System Dynamic

## Goal

1. 修复 metrics tab 不显示 5 个有数据的 metric 列的问题
2. 将系统从硬编码列模式改为动态列模式，使前端能自动适配任何 DB 中存在的 metric 列
3. 保持性能优化 (避免在高频 API 调用中每次动态查询 schema)

## Context

根据 [FINDING-2026-06-09-metrics-display.md](../findings/FINDING-2026-06-09-metrics-display.md):

- DB 中 snapshot_id=11 有 17 个 metric 列有数据
- `get_draw_calls` 只返回 13 个硬编码的 metric 列，缺失 5 个有数据的列:
  - `time_shading_fragments_pct`
  - `time_shading_vertices_pct`
  - `textures_per_fragment`
  - `preemptions`
  - `avg_preemption_delay`
- 前端 metrics tab 硬编码了 12 个列定义，与 `get_draw_calls` 对齐
- DC detail panel 使用 `SELECT * FROM metrics` + 动态遍历，因此能正确显示所有列

问题根因: `get_draw_calls` 硬编码列列表与 DB 实际存在的列不匹配。

## Approach

选择 **Option 2: Backend 查询所有列 + Frontend 动态列定义**，原因:

- 与 `get_dc_detail` 的设计一致 (都用 `SELECT *`)
- 支持未来新增 GPU counter 无需改代码
- 性能影响可忽略 (metrics 表列数固定，DuckDB `SELECT *` 很快)

### Technical Design

1. **Backend**: 修改 `get_draw_calls` 使用 `SELECT m.*` (排除 snapshot_id/api_id)
2. **Frontend**: 修改 metrics tab 列定义为动态生成 (从第一个 DC 的 keys 中提取)
3. **列顺序**: 保持用户友好的顺序 (clocks 优先，百分比指标分组，字节数分组)
4. **格式化**: 保持现有格式化逻辑 (百分比用 `_fmt1`, 整数用 `toLocaleString`)

## Steps

### Step 1: 修改 `data/query.py` 的 `get_draw_calls` 函数

**File**: `D:\pysdp\data\query.py`

**Action**: 将 lines 93-104 的硬编码列列表替换为 `m.*` (排除 PK 列):

Before:
```python
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

After:
```python
            m.* EXCLUDE (snapshot_id, api_id)
```

**Exact Edit**:

在 line 70 附近，将 SQL SELECT 子句从:

```python
    sql = f"""
        SELECT
            dc.api_id,
            dc.dc_id,
            dc.api_name,
            dc.pipeline_id,
            dc.parameters,
            dc.vertex_count,
            dc.index_count,
            dc.instance_count,
            dc.first_vertex,
            dc.first_index,
            dc.vertex_offset,
            dc.first_instance,
            dc.draw_count,
            dc.group_count_x,
            dc.group_count_y,
            dc.group_count_z,
            lb.category,
            lb.subcategory,
            lb.detail,
            lb.confidence,
            lb.label_source,
            lb.reason_tags,
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
        FROM draw_calls dc
```

改为:

```python
    sql = f"""
        SELECT
            dc.api_id,
            dc.dc_id,
            dc.api_name,
            dc.pipeline_id,
            dc.parameters,
            dc.vertex_count,
            dc.index_count,
            dc.instance_count,
            dc.first_vertex,
            dc.first_index,
            dc.vertex_offset,
            dc.first_instance,
            dc.draw_count,
            dc.group_count_x,
            dc.group_count_y,
            dc.group_count_z,
            lb.category,
            lb.subcategory,
            lb.detail,
            lb.confidence,
            lb.label_source,
            lb.reason_tags,
            m.* EXCLUDE (snapshot_id, api_id)
        FROM draw_calls dc
```

**DuckDB Compatibility**: DuckDB 支持 `SELECT * EXCLUDE (cols)` 语法 (从 0.3.0 开始)，pysdp 使用的 DuckDB 版本满足要求。

---

### Step 2: 修改 `webui/static/app.js` 的 metrics tab 列定义

**File**: `D:\pysdp\webui\static\app.js`

**Action**: 将 lines 2535-2549 的硬编码列定义改为动态生成。

**Current Code** (lines 2535-2549):

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

**New Approach**:

1. 在 `loadExplorerDCs` 成功加载数据后，从第一个 DC 中提取所有 metric keys
2. 缓存到 `ts.explorerState.availableMetricCols`
3. 在 `_dcColDefs` 中动态生成列定义

**Implementation**:

**2a. 在 `loadExplorerDCs` 中提取 metric columns (在 line ~2484 附近，`ts.explorerState.dcs = res.data || [];` 之后):**

```javascript
    ts.explorerState.dcs = res.data || [];

    // Discover available metric columns from first DC
    if (ts.explorerState.dcs.length > 0) {
      const firstDC = ts.explorerState.dcs[0];
      const metricKeys = Object.keys(firstDC).filter(k => 
        !['api_id', 'dc_id', 'api_name', 'pipeline_id', 'parameters',
          'vertex_count', 'index_count', 'instance_count',
          'first_vertex', 'first_index', 'vertex_offset', 'first_instance',
          'draw_count', 'group_count_x', 'group_count_y', 'group_count_z',
          'category', 'subcategory', 'detail', 'confidence', 'label_source',
          'reason_tags', 'snapshot_id'].includes(k)
      );
      ts.explorerState.availableMetricCols = metricKeys;
    } else {
      ts.explorerState.availableMetricCols = [];
    }
```

**2b. 修改 `_dcColDefs` 中的 metrics tab 分支 (lines 2535-2549):**

Before:
```javascript
  if (tab === 'metrics') return [
    SEQ, NAME,
    { key: 'clocks',                 label: 'Clocks',        val: dc => dc.clocks               ?? '—' },
    // ... 硬编码列
  ];
```

After:
```javascript
  if (tab === 'metrics') {
    // Get available metric columns from tab state
    const ts = getTabState(currentTabId); // Need to pass tabId or use global
    const metricKeys = (ts && ts.explorerState && ts.explorerState.availableMetricCols) || [];
    
    // Define display order and labels
    const METRIC_ORDER = {
      'clocks': { label: 'Clocks', fmt: v => v ?? '—' },
      'preemptions': { label: 'Preempt', fmt: v => v ?? '—' },
      'avg_preemption_delay': { label: 'PreemptDly', fmt: v => v ?? '—' },
      'fragments_shaded': { label: 'Frags', fmt: v => v ?? '—' },
      'vertices_shaded': { label: 'Verts', fmt: v => v ?? '—' },
      'read_total_bytes': { label: 'Read(B)', fmt: v => v ?? '—' },
      'write_total_bytes': { label: 'Write(B)', fmt: v => v ?? '—' },
      'shaders_busy_pct': { label: 'ShBusy%', fmt: _fmt1 },
      'shaders_stalled_pct': { label: 'ShStall%', fmt: _fmt1 },
      'time_alus_working_pct': { label: 'ALU%', fmt: _fmt1 },
      'time_shading_fragments_pct': { label: 'FragShd%', fmt: _fmt1 },
      'time_shading_vertices_pct': { label: 'VertShd%', fmt: _fmt1 },
      'tex_fetch_stall_pct': { label: 'TexStall%', fmt: _fmt1 },
      'tex_l1_miss_pct': { label: 'TexL1Miss%', fmt: _fmt1 },
      'tex_pipes_busy_pct': { label: 'TexPipes%', fmt: _fmt1 },
      'textures_per_fragment': { label: 'Tex/Frag', fmt: _fmt1 },
      'lrz_pixels_killed': { label: 'LRZ', fmt: v => v ?? '—' },
    };

    const cols = [SEQ, NAME];
    // Use preferred order first, then append any unknown columns
    const ordered = Object.keys(METRIC_ORDER).filter(k => metricKeys.includes(k));
    const unordered = metricKeys.filter(k => !METRIC_ORDER[k]);
    
    [...ordered, ...unordered].forEach(key => {
      const cfg = METRIC_ORDER[key] || { label: key, fmt: v => v ?? '—' };
      cols.push({
        key,
        label: cfg.label,
        val: dc => cfg.fmt(dc[key])
      });
    });
    
    return cols;
  }
```

**Note**: 上述代码需要访问 `tabId`，但 `_dcColDefs` 当前只接收 `tab` 参数。有两个解决方案:

- **方案 A** (推荐): 修改 `_dcColDefs(tab, tabId)` 签名，传入 `tabId`
- **方案 B**: 在 `renderExplorerDCTable` 中提前计算列定义并缓存到 `ts.explorerState.metricColDefs`

选择 **方案 A**，因为更清晰，调用点很少。

**修改后的完整实现**:

**Step 2a**: 修改 `_dcColDefs` 函数签名 (line ~2524):

```javascript
function _dcColDefs(tab, tabId) {
  const SEQ   = { key: '_seq',     label: '#',        val: (dc, i) => i + 1 };
  const APID  = { key: 'api_id',   label: 'API ID',   val: dc => dc.api_id };
  const NAME  = { key: 'api_name', label: 'API Name', val: dc => dc.api_name || '—' };

  if (tab === 'params') return [
    { key: 'dc_id', label: 'DC', val: dc => dc.dc_id ?? '—' },
    NAME,
    { key: 'parameters', label: 'Parameters', val: dc => dc.parameters || '—', wide: true },
  ];

  if (tab === 'metrics') {
    const ts = getTabState(tabId);
    const metricKeys = (ts && ts.explorerState && ts.explorerState.availableMetricCols) || [];
    
    const METRIC_ORDER = {
      'clocks': { label: 'Clocks', fmt: v => v ?? '—' },
      'preemptions': { label: 'Preempt', fmt: v => v ?? '—' },
      'avg_preemption_delay': { label: 'PreemptDly', fmt: v => v ?? '—' },
      'fragments_shaded': { label: 'Frags', fmt: v => v ?? '—' },
      'vertices_shaded': { label: 'Verts', fmt: v => v ?? '—' },
      'read_total_bytes': { label: 'Read(B)', fmt: v => v ?? '—' },
      'write_total_bytes': { label: 'Write(B)', fmt: v => v ?? '—' },
      'shaders_busy_pct': { label: 'ShBusy%', fmt: _fmt1 },
      'shaders_stalled_pct': { label: 'ShStall%', fmt: _fmt1 },
      'time_alus_working_pct': { label: 'ALU%', fmt: _fmt1 },
      'time_shading_fragments_pct': { label: 'FragShd%', fmt: _fmt1 },
      'time_shading_vertices_pct': { label: 'VertShd%', fmt: _fmt1 },
      'tex_fetch_stall_pct': { label: 'TexStall%', fmt: _fmt1 },
      'tex_l1_miss_pct': { label: 'TexL1Miss%', fmt: _fmt1 },
      'tex_pipes_busy_pct': { label: 'TexPipes%', fmt: _fmt1 },
      'textures_per_fragment': { label: 'Tex/Frag', fmt: _fmt1 },
      'lrz_pixels_killed': { label: 'LRZ', fmt: v => v ?? '—' },
    };

    const cols = [SEQ, NAME];
    const ordered = Object.keys(METRIC_ORDER).filter(k => metricKeys.includes(k));
    const unordered = metricKeys.filter(k => !METRIC_ORDER[k]);
    
    [...ordered, ...unordered].forEach(key => {
      const cfg = METRIC_ORDER[key] || { label: key, fmt: v => v ?? '—' };
      cols.push({ key, label: cfg.label, val: dc => cfg.fmt(dc[key]) });
    });
    
    return cols;
  }

  // label tab
  return [
    SEQ, NAME,
    { key: 'category',     label: 'Category',   val: dc => dc.category     || '—' },
    { key: 'subcategory',  label: 'Subcategory',val: dc => dc.subcategory  || '—' },
    { key: 'detail',       label: 'Detail',     val: dc => dc.detail       || '—' },
    { key: 'confidence',   label: 'Conf',       val: dc => dc.confidence != null ? dc.confidence.toFixed(2) : '—' },
    { key: 'label_source', label: 'Source',     val: dc => dc.label_source || '—' },
  ];
}
```

**Step 2b**: 更新所有 `_dcColDefs` 调用点传入 `tabId`:

搜索 `_dcColDefs(` → 找到所有调用点 → 添加 `tabId` 参数。

主要调用点在 `renderExplorerDCTable` (line ~2574):

```javascript
const cols = _dcColDefs(ts.explorerState.colTab, tabId);
```

以及 line ~2585 附近的 sort fallback:

```javascript
for (const t of ['params', 'metrics', 'label']) {
  colDef = _dcColDefs(t, tabId).find(c => c.key === sortCol);
  if (colDef) break;
}
```

---

### Step 3: 在 `loadExplorerDCs` 中提取并缓存 metric columns

**File**: `D:\pysdp\webui\static\app.js`

**Location**: Line ~2484, 在 `ts.explorerState.dcs = res.data || [];` 之后

**Exact Edit**:

After:
```javascript
    ts.explorerState.dcs = res.data || [];
```

Add:
```javascript
    // Discover available metric columns from first DC
    if (ts.explorerState.dcs.length > 0) {
      const firstDC = ts.explorerState.dcs[0];
      const DC_FIELDS = new Set([
        'api_id', 'dc_id', 'api_name', 'pipeline_id', 'parameters',
        'vertex_count', 'index_count', 'instance_count',
        'first_vertex', 'first_index', 'vertex_offset', 'first_instance',
        'draw_count', 'group_count_x', 'group_count_y', 'group_count_z',
        'category', 'subcategory', 'detail', 'confidence', 'label_source',
        'reason_tags', 'snapshot_id'
      ]);
      const metricKeys = Object.keys(firstDC).filter(k => !DC_FIELDS.has(k));
      ts.explorerState.availableMetricCols = metricKeys;
    } else {
      ts.explorerState.availableMetricCols = [];
    }
```

---

## Validation

### Step 1 Validation

**Backend Query Test**:

```bash
# Start the server
python -m pysdp --port 8000

# Query the API
curl "http://localhost:8000/api/data/draw_calls?snapshot_id=11" | python -m json.tool | head -50
```

**Expected Output**: 每个 DC 对象应包含所有 17 个 metric 字段，包括:
- `time_shading_fragments_pct`
- `time_shading_vertices_pct`
- `textures_per_fragment`
- `preemptions`
- `avg_preemption_delay`

**SQL Test** (可选):

```python
from data.db import WorkspaceDB
from data.query import get_draw_calls

db = WorkspaceDB.get()
dcs = get_draw_calls(db, 11)
print("Keys in first DC:", list(dcs[0].keys()))
print("Metric keys:", [k for k in dcs[0].keys() if k not in [
    'api_id', 'dc_id', 'api_name', 'pipeline_id', 'parameters',
    'vertex_count', 'index_count', 'instance_count',
    'first_vertex', 'first_index', 'vertex_offset', 'first_instance',
    'draw_count', 'group_count_x', 'group_count_y', 'group_count_z',
    'category', 'subcategory', 'detail', 'confidence', 'label_source', 'reason_tags'
]])
```

**Expected**: 17 个 metric keys.

---

### Step 2-3 Validation

**Frontend Test**:

1. 打开浏览器: `http://localhost:8000`
2. 选择 snapshot_id=11
3. 切换到 Explorer → Metrics tab
4. 验证表格显示 **17 个 metric 列** (除了 # 和 API Name)
5. 验证列顺序符合 `METRIC_ORDER` 定义
6. 点击任意 DC → 查看 DC detail panel
7. 验证 detail panel 的 metrics 区域显示所有 17 个 metric

**Expected Columns** (按顺序):

1. # (序号)
2. API Name
3. Clocks
4. Preempt
5. PreemptDly
6. Frags
7. Verts
8. Read(B)
9. Write(B)
10. ShBusy%
11. ShStall%
12. ALU%
13. FragShd%
14. VertShd%
15. TexStall%
16. TexL1Miss%
17. TexPipes%
18. Tex/Frag
19. LRZ

**Console Check**:

```javascript
// In browser console
const ts = window._tabStates[Object.keys(window._tabStates)[0]];
console.log("Available metric cols:", ts.explorerState.availableMetricCols);
// Expected: Array of 17 metric column names
```

---

### End-to-End Validation

**Scenario**: 新增一个 snapshot，启用了新的 GPU counter (例如 `tex_l2_miss_pct`)

**Steps**:
1. Ingest 新 snapshot
2. 打开 Explorer → Metrics tab
3. 验证新列自动出现在表格末尾

**Expected**: 无需修改代码，新列自动显示。

---

## Alternatives Considered

### Alternative 1: Backend 仍硬编码，手动添加缺失的 5 列

**Approach**: 在 `get_draw_calls` 的 SELECT 子句中添加:
```python
m.time_shading_fragments_pct,
m.time_shading_vertices_pct,
m.textures_per_fragment,
m.preemptions,
m.avg_preemption_delay,
```

前端添加对应的 5 个列定义。

**Pros**:
- 改动最小
- 性能无影响

**Cons**:
- 未来新增 counter 仍需手动维护
- 与 `get_dc_detail` 的设计不一致 (一个硬编码，一个动态)
- 维护负担高

**Rejected**: 不解决根本问题。

---

### Alternative 2: Backend 动态 + Frontend 硬编码

**Approach**: 
- Backend 用 `SELECT m.*`
- Frontend 保持硬编码列定义，只渲染预定义的列

**Pros**:
- Backend 灵活
- Frontend 简单

**Cons**:
- Frontend 列定义仍需手动维护
- API 返回的数据有浪费 (前端不用的列也传输了)

**Rejected**: 部分解决问题，但前端仍不灵活。

---

### Alternative 3: 添加 `/api/data/available_metrics` 端点

**Approach**:
- 新增 API 端点返回 snapshot 可用的 metric 列
- 前端在 load snapshot 时先调用该端点，缓存列列表

**Pros**:
- 前端可动态适配
- 列列表缓存在前端，不影响后续 DC 查询性能

**Cons**:
- 新增 API 端点，增加复杂度
- 额外一次 HTTP 请求

**Rejected**: 当前方案更简单 (从第一个 DC 提取列即可)。

---

## Risks

### Risk 1: DuckDB `EXCLUDE` 语法兼容性

**Mitigation**: DuckDB 从 0.3.0 开始支持 `EXCLUDE` 语法，pysdp 依赖的 DuckDB 版本 >= 0.8.0 (通过 `requirements.txt` 检查)。

**Fallback**: 如果 `EXCLUDE` 不支持，使用完整列名列表 (从 `ingest.py` 的 `_ORDERED` 复制)。

---

### Risk 2: 性能影响 (SELECT * vs 硬编码列)

**Analysis**:
- `SELECT *` 对 DuckDB 几乎无性能影响 (列数固定，~50 列)
- 网络传输影响可忽略 (增加 5 个列 × 平均 8 bytes/列 × 1000 DCs = 40KB)
- 前端渲染影响可忽略 (只渲染用户可见的列)

**Mitigation**: 如果发现性能问题，可在 backend 添加列白名单配置项 (但默认返回所有列)。

---

### Risk 3: 未知列的显示格式

**Issue**: 新增的未在 `METRIC_ORDER` 中定义的列，使用默认格式 `v ?? '—'`，可能不是最佳显示格式 (例如百分比列应该用 `_fmt1`)。

**Mitigation**:
- 在 `METRIC_ORDER` 中预定义所有 `ingest.py` 支持的 50+ 列
- 使用命名约定自动判断格式 (例如 `*_pct` → `_fmt1`, `*_bytes` → `toLocaleString`)

**Implementation**: 在 Step 2 中添加格式自动判断逻辑:

```javascript
const cfg = METRIC_ORDER[key] || {
  label: key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
  fmt: key.endsWith('_pct') ? _fmt1 : (v => v ?? '—')
};
```

---

### Risk 4: 列顺序不稳定

**Issue**: 从 `Object.keys(firstDC)` 提取的列顺序取决于 DuckDB 返回的列顺序，可能不是用户期望的顺序。

**Mitigation**: 使用 `METRIC_ORDER` 定义优先顺序，未定义的列追加到末尾。已在 Step 2 中实现。

---

## Implementation Notes

### For the Executor Agent

1. **DuckDB Syntax**: `SELECT m.* EXCLUDE (snapshot_id, api_id)` 是 DuckDB 特有语法，确保 DuckDB 版本 >= 0.3.0
2. **Frontend State**: `ts.explorerState.availableMetricCols` 需要在 `loadExplorerDCs` 成功后立即设置，避免 race condition
3. **Fallback**: 如果 `availableMetricCols` 为空 (例如 snapshot 无 metrics 数据)，metrics tab 应显示 "No metrics" 而不是报错
4. **Testing**: 需要测试以下场景:
   - snapshot 有 0 个 DC
   - snapshot 有 DC 但无 metrics
   - snapshot 有 metrics 但缺少某些列 (NULL)
   - snapshot 有所有 17 个列

### Code Review Checklist

- [ ] `get_draw_calls` 返回所有 metric 列
- [ ] API `/api/data/draw_calls` 返回包含所有 metric 列的 JSON
- [ ] Frontend `availableMetricCols` 正确提取
- [ ] Frontend metrics tab 列定义动态生成
- [ ] 列顺序符合 `METRIC_ORDER`
- [ ] 未定义列使用默认格式
- [ ] DC detail panel 仍正常工作
- [ ] 空数据场景不报错
