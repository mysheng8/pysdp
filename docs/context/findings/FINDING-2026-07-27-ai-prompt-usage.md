---
type: finding
topic: AI Prompt Usage Inventory
status: investigated
related_paths:
  - analysis/label_service.py
  - analysis/gles_decompile_service.py
  - analysis/vlm_screenshot_service.py
  - analysis/report_service.py
  - analysis/analysis_md_service.py
  - chat/prompts.py
  - chat/llm_client.py
  - config.ini
related_tags: [ai, llm, vlm, prompt, configuration]
summary: Complete inventory of all AI model usage points in pysdp and their prompt sources
last_updated: 2026-07-27
---

# Finding: AI Prompt Usage in pysdp

## Problem Statement

用户希望能够在 WebUI 中自定义系统所有 AI 步骤的提示词，以便根据具体项目和需求调整 AI 行为。为此需要首先定位系统中所有使用 LLM/VLM 的位置，了解提示词的存储方式和调用流程。

## Evidence

### 1. AI 使用点清单

pysdp 系统中共有 **6 个主要 AI 使用点**：

| 功能 | 文件位置 | LLM/VLM | 提示词来源 | 当前可配置性 |
|------|---------|---------|-----------|-------------|
| **DrawCall 标签分类** | `analysis/label_service.py` | LLM (batch) | 硬编码在 `_build_llm_prompt()` 函数（286-411行） | ❌ 不可配置 |
| **GLES Shader 反编译** | `analysis/gles_decompile_service.py` | LLM (batch) | 硬编码在 `_SYSTEM_PREAMBLE` 常量（94-130行） | ❌ 不可配置 |
| **截图场景描述** | `analysis/vlm_screenshot_service.py` | VLM | 硬编码在 `generate_scene_description()` 函数（170-179行） | ❌ 不可配置 |
| **GPU 性能报告生成** | `analysis/report_service.py` | LLM + VLM | 硬编码在 `_build_prompt()` 函数（121-172行）和 VLM 调用处（211-216行） | ❌ 不可配置 |
| **分析报告生成** | `analysis/analysis_md_service.py` | LLM (可选) | 通过 `llm_fn` 回调传入，目前未使用 | ⚠️ 预留接口但未实现 |
| **AI 聊天助手** | `chat/prompts.py` + `chat/llm_client.py` | LLM (chat) | 硬编码在 `build_system_prompt()` 函数（33-112行） | ❌ 不可配置 |

### 2. 提示词详细分析

#### 2.1 DrawCall 标签分类 (`label_service.py`)

**用途**: 根据 shader 代码、渲染目标、几何参数等信息，将每个 DrawCall 分类为 Scene/Character/UI/PostProcess/VFX/Shadow 等类别。

**提示词长度**: ~125 行，约 3500 字符

**关键内容**:
- 10 种分类定义（Scene, Terrain, Character, PostProcess, VFX, UI, Shadow 等）
- 分层规则系统（R1 Render Target 优先级 → R2 Shader main() 分析 → R3 cbuffer vs texture 优先级）
- 输出格式规范（JSON schema）

**示例片段**:
```python
"Rules (apply in order):",
"R1 [Render targets first — HIGHEST PRIORITY, overrides everything else]",
"  ** RULE R1a: Depth-only RT, no Color RT → SHADOW MAP PASS.",
"R2 [Shader main() for Scene/Character/Terrain]",
"  Scene:     lightmap textures sampled in main() using TEXCOORD2 UVs.",
"  Character: per-object SH probe Buffer<float4> loaded via per-instance offset.",
```

**调用频率**: 每帧数百到数千次（每个 DrawCall pipeline 一次，有缓存）

**依赖数据**: DrawCall 参数、shader 代码、mesh 统计、texture 描述

---

#### 2.2 GLES Shader 反编译 (`gles_decompile_service.py`)

**用途**: 将 Adreno IR3 汇编代码反编译为人类可读的 GLSL ES 3.0 代码。

**提示词长度**: ~37 行，约 2300 字符

**关键内容**:
- IR3 指令集快速参考（bary.f, sam, mad.f32 等）
- Vertex shader 输出约定（gl_Position 赋值规则、Skinning 模式识别）
- Fragment shader 输出约定（r0.xyzw 为颜色输出）
- 输出格式约束（纯 GLSL，有意义的变量名，gl_Position 必须最后赋值）

**示例片段**:
```python
_SYSTEM_PREAMBLE = (
    "You are an expert in Adreno GPU IR3 assembly (Qualcomm freedreno). "
    "Reconstruct the GLSL ES 3.0 shader source from the IR3 disassembly below.\n\n"
    "IR3 quick reference:\n"
    "  bary.f rD,N,r0.x — interpolate varying slot N (fragment: read varying)\n"
    "  sam/isam (xyzw)rD,rS,s#N,t#N — texture sample\n"
    ...
)
```

**调用频率**: 每帧数十到数百次（每个 unique shader × stage，有缓存）

**依赖数据**: IR3 disassembly 文本（从 sdp.db 读取）

---

#### 2.3 截图场景描述 (`vlm_screenshot_service.py`)

**用途**: 使用 VLM 分析截图，描述场景内容、视觉风格和渲染特征。

**提示词长度**: ~7 行，约 300 字符 + GPU 数据摘要

**关键内容**:
- 任务说明（graphics engineer 视角）
- 要求的描述维度（scene content, visual style, rendering features）
- 结合 GPU profiling 数据的关联分析
- 输出长度约束（200-400 词，英文）

**示例片段**:
```python
prompt = (
    "You are a graphics engineer analyzing a mobile game GPU capture.\n"
    "Describe what you see in this screenshot: the scene content, visual style, "
    "and key rendering features (lighting, shadows, transparency, post-effects, UI, etc.).\n"
    "Then, given the GPU profiling data below, briefly note which visible elements "
    "are likely responsible for the highest GPU cost.\n"
    "Be concise (200–400 words). Write in English.\n\n"
)
```

**调用频率**: 每个 snapshot 一次

**依赖数据**: 截图文件（PNG/JPG/BMP）、label.json、metrics.json

---

#### 2.4 GPU 性能报告生成 (`report_service.py`)

**用途**: 生成完整的中文 GPU 性能分析报告（Markdown 格式）。

**提示词长度**: ~51 行，约 2600 字符 + JSON 数据

**关键内容**:
- 报告结构模板（总览、分类分析、优化建议三部分）
- 每个部分的具体格式要求（表格、列表、小结）
- 数据解读指导（top metrics 含义、瓶颈判断标准）
- 输出语言约束（必须用中文）

**示例片段**:
```python
return f"""You are a GPU performance engineer analyzing Snapdragon Adreno profiling data...

Generate a detailed GPU performance analysis report in Markdown. The report MUST be written in Chinese and follow this exact structure:

# GPU 性能分析报告 — {{sdp_name}}

## 1. 总览
Describe the frame in 2-3 sentences...

## 2. 分类分析
For each category in categories (ordered by clocks_pct descending)...

## 3. 优化建议
Based on the data, provide 4-6 specific, actionable optimization recommendations...
"""
```

**调用频率**: 每个 snapshot 一次（用户手动触发）

**依赖数据**: status.json、topdc.json、screenshot（VLM 描述）

---

#### 2.5 AI 聊天助手 (`chat/prompts.py`)

**用途**: 构建聊天 AI 的系统提示词，提供数据查询 API 和交互规范。

**提示词长度**: ~80 行，约 1500 字符（动态拼接）

**关键内容**:
- 角色定义（GPU profiling assistant）
- 可用工具列表（execute_python, fetch_aspect, compare_snapshots, recall_history）
- 数据查询 API 文档（data_query 模块函数签名）
- 回复格式规范（【answer】...【recap】结构）
- Active snapshots 列表（动态注入）
- Already-fetched aspects 列表（session-aware，避免重复查询）

**示例片段**:
```python
parts = [
    f"GPU profiling assistant. {lang_instruction}",
    "Use execute_python for all computation/charts.",
    "Bindings: db, snapshot_id, data_query (module).",
    "data_query API:",
    "  get_draw_calls(db,sid,category=,tags=) → [dict] with draw_call fields + metrics joined.",
    ...
    "## Reply Structure",
    "Structure every reply as:",
    "  First line:  【answer】<direct answer>",
    "  Body:        analysis...",
    "  Last line:   【recap】<summary of this round>",
]
```

**调用频率**: 每次聊天会话的每一轮

**依赖数据**: snapshot_ids（当前 pinned snapshots）、session history（已提取的 aspects）

---

### 3. LLM/VLM 配置系统

当前配置项（`config.ini`）：

```ini
# LLM (DrawCall labeling / analysis)
# LlmApiEndpoint=
# LlmApiKey=
# LlmModel=vertex_ai/gemini-2.5-flash-lite
LlmTimeoutSeconds=300
LlmMaxOutputTokens=16000
LlmMaxShaderChars=20000
LlmMaxConcurrentRequests=16

# VLM (Vision / screenshot analysis)
# VlmApiEndpoint=
# VlmApiKey=
# VlmModel=
VlmTimeoutSeconds=60
VlmMaxOutputTokens=2000

# Chat AI (WebUI sidebar)
# ChatApiEndpoint=
# ChatApiKey=
# ChatModel=vertex_ai/gemini-2.5-flash
ChatMaxTokens=8192
ChatTimeoutSeconds=120

# LLM Cache
LlmCacheEnabled=true
LlmCacheSize=512
```

**可配置的**:
- ✅ API endpoint、API key、model name
- ✅ Timeout、max tokens、并发数

**不可配置的**:
- ❌ 所有提示词内容
- ❌ 提示词中的规则、分类定义、输出格式

---

## Analysis

### 提示词特征分析

1. **硬编码程度**: 所有提示词都直接硬编码在 Python 源文件中，无外部配置文件。

2. **复杂度分级**:
   - **高复杂度** (>2000 字符): label_service（分类规则）、gles_decompile（IR3 指令集）、report_service（报告模板）
   - **中复杂度** (500-2000 字符): chat/prompts（API 文档）
   - **低复杂度** (<500 字符): vlm_screenshot_service（简单描述任务）

3. **结构化程度**:
   - **高结构化**: label_service（分层规则 R1/R2/R3）、report_service（固定章节结构）
   - **半结构化**: gles_decompile（指令参考 + 输出约束）
   - **自由文本**: vlm_screenshot_service、chat/prompts

4. **调用频率分级**:
   - **高频** (每帧数百次): label_service, gles_decompile
   - **低频** (每帧 1 次): vlm_screenshot_service, report_service
   - **交互式** (用户触发): chat/prompts

5. **缓存策略**:
   - **Pipeline-level cache**: label_service（相同 pipeline → 相同结果）
   - **Content-based cache**: gles_decompile（SHA-256 of IR3 disasm）
   - **SHA-256 ring-pool**: llm_wrapper 全局缓存（512 slots，所有 LLM 调用共享）
   - **无缓存**: chat/prompts（每次对话都新建）

---

## Impact

### 当前限制

1. **无法调优 AI 行为**: 用户无法针对特定项目调整分类规则或描述粒度。

2. **多语言支持受限**: report_service 强制中文输出，其他服务强制英文，无法适配其他语言。

3. **模板维护困难**: 修改提示词需要修改源代码并重新部署。

4. **无法 A/B 测试**: 无法在不同提示词变体之间快速切换对比效果。

5. **缺乏版本管理**: 提示词变更历史无法追踪，容易引入回归。

### 潜在风险

1. **SQL 注入风险**: 如果允许用户自定义提示词，需要防止通过提示词注入攻击 execute_python 等工具。

2. **输出格式破坏**: 某些提示词（如 label_service）依赖严格的 JSON 输出格式，用户修改可能导致解析失败。

3. **性能退化**: 修改 label_service 提示词可能导致 LLM 输出冗长，降低批量标注速度。

4. **缓存失效**: 修改提示词会使所有基于 content-hash 的缓存失效，需要重新调用 LLM。

---

## Related Context

- **配置系统**: `config.py` + `config.ini` + `.env` 三层优先级
- **LLM wrapper**: `analysis/llm_wrapper.py` — 统一的 LLM/VLM 调用封装
- **Chat client**: `chat/llm_client.py` — 基于 litellm 的异步流式客户端
- **数据持久化**: DuckDB (`data/db.py`) — 不涉及提示词存储
