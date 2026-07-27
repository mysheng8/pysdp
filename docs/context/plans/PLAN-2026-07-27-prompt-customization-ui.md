---
type: plan
topic: AI Prompt Customization System
status: proposed
based_on:
  - FINDING-2026-07-27-ai-prompt-usage.md
related_paths:
  - analysis/label_service.py
  - analysis/gles_decompile_service.py
  - analysis/vlm_screenshot_service.py
  - analysis/report_service.py
  - chat/prompts.py
  - webui/routes/settings.py
  - webui/static/app.js
  - config.py
related_tags: [ai, ui, configuration, prompts]
summary: Design and implement a user-friendly UI system for customizing AI prompts across all pysdp services
last_updated: 2026-07-27
---

# Plan: AI Prompt Customization System

## Goal

为 pysdp 系统设计并实现一套完整的 AI 提示词自定义功能，让用户无需修改代码即可：
1. 在 WebUI 中查看和编辑所有 AI 步骤的提示词
2. 实时预览提示词效果
3. 导出/导入提示词配置以便团队共享
4. 一键恢复系统默认提示词

## Context

根据 `FINDING-2026-07-27-ai-prompt-usage.md` 的调研结果：

- 系统中有 **6 个 AI 使用点**：DrawCall 标签分类、GLES Shader 反编译、截图场景描述、GPU 性能报告生成、AI 聊天助手
- 所有提示词均**硬编码在 Python 源文件中**，无法在运行时修改
- 提示词复杂度差异大（300-3500 字符），有些高度结构化（label_service），有些是自由文本
- 高频调用的提示词（label, decompile）有缓存机制，修改提示词会使缓存失效

## Approach

采用 **配置文件 + UI 双层架构**：

1. **数据层**: 使用独立的 JSON 配置文件存储提示词（`prompts.json`）
2. **逻辑层**: 修改各 service 以优先读取配置文件，不存在时 fallback 到硬编码默认值
3. **UI 层**: 在 WebUI Settings 面板中新增 "AI Prompts" 标签页

**关键设计决策**：

- **存储格式**: JSON 而非数据库（易于版本控制、备份、手动编辑）
- **变量系统**: 提示词使用 `{variable}` 占位符，运行时替换（而非完全自由文本）
- **验证机制**: 输出格式验证 + 危险关键词检测
- **向后兼容**: 配置文件缺失时自动使用硬编码默认值

---

## Steps

### Step 1: 定义提示词配置文件格式和默认值

**1.1** 在项目根目录创建 `prompts.json` 配置文件，定义 schema：

```json
{
  "schema_version": "1.0",
  "prompts": {
    "label_dc": {
      "enabled": true,
      "description": "DrawCall classification based on shader code and render targets",
      "model_override": null,
      "system_prompt": "Classify this Vulkan draw call. Reply with JSON only.",
      "user_template": "API:{api_name}\nVerts:{vertex_count}...\n{shader_code}\n...",
      "variables": ["api_name", "vertex_count", "instance_count", "shader_code", "render_targets", "category_list"],
      "output_format": "json",
      "validation_schema": {"type": "object", "required": ["category", "subcategory", "confidence"]},
      "cache_key_includes_prompt": true
    },
    "gles_decompile": {
      "enabled": true,
      "description": "Decompile Adreno IR3 assembly to GLSL ES 3.0",
      "system_prompt": "You are an expert in Adreno GPU IR3 assembly...\n{ir3_reference}",
      "user_template": "// {stage} shader\n{disasm}\n\nReconstruct this as a clean GLSL ES 3.0 {stage} shader:",
      "variables": ["ir3_reference", "stage", "disasm"],
      "output_format": "glsl",
      "validation_schema": null,
      "cache_key_includes_prompt": true
    },
    "scene_description": {
      "enabled": true,
      "description": "VLM-based screenshot scene description",
      "model_override": null,
      "user_template": "You are a graphics engineer analyzing a mobile game GPU capture.\n{task_description}\n{gpu_summary}",
      "variables": ["task_description", "gpu_summary"],
      "output_format": "markdown",
      "cache_key_includes_prompt": false
    },
    "report_generation": {
      "enabled": true,
      "description": "Generate full GPU performance analysis report in Markdown",
      "user_template": "You are a GPU performance engineer...\n{report_structure}\n{data_json}",
      "variables": ["report_structure", "scene_desc", "data_json", "sdp_name"],
      "output_format": "markdown",
      "validation_schema": null
    },
    "chat_system": {
      "enabled": true,
      "description": "AI chat assistant system prompt",
      "system_prompt": "GPU profiling assistant. {lang_instruction}\n{tools_doc}\n{active_snapshots}\n{reply_format}",
      "variables": ["lang_instruction", "tools_doc", "active_snapshots", "reply_format", "fetched_aspects"],
      "output_format": "markdown"
    }
  }
}
```

**1.2** 在 `analysis/prompt_defaults.py` 中将所有硬编码的提示词提取为 Python 常量：

```python
"""Default prompt templates for all AI services."""

DEFAULT_PROMPTS = {
    "label_dc": {
        "system_prompt": "Classify this Vulkan draw call. Reply with JSON only.",
        "user_template": """API:{api_name}
Verts:{vertex_count}  Indices:{index_count}  Instances:{instance_count}...
{shader_code}

Categories: {category_list}
Output JSON only:
{{"category":"<category>","subcategory":"<subcategory>","confidence":0.9}}""",
        "variables": {...},
    },
    "gles_decompile": {
        "system_prompt": IR3_REFERENCE,  # 从 gles_decompile_service.py 迁移过来
        "user_template": "// {stage} shader\n{disasm}\n\nReconstruct...",
    },
    # ... 其他默认值
}
```

**1.3** 创建 `config/prompt_manager.py` 提示词管理模块：

```python
"""Prompt configuration manager — load from prompts.json with fallback to defaults."""

import json
from pathlib import Path
from typing import Any

from analysis.prompt_defaults import DEFAULT_PROMPTS

class PromptManager:
    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or Path("prompts.json")
        self._data = self._load()
    
    def _load(self) -> dict:
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"schema_version": "1.0", "prompts": {}}
    
    def get_prompt(self, prompt_id: str) -> dict:
        """Return prompt config dict with fallback to default."""
        custom = self._data.get("prompts", {}).get(prompt_id, {})
        default = DEFAULT_PROMPTS.get(prompt_id, {})
        
        # Merge: custom overrides default
        merged = {**default, **custom}
        
        # If disabled, return empty template
        if not merged.get("enabled", True):
            return None
        
        return merged
    
    def render_prompt(self, prompt_id: str, variables: dict[str, Any]) -> tuple[str, str]:
        """Render prompt template with variables. Returns (system_prompt, user_prompt)."""
        cfg = self.get_prompt(prompt_id)
        if cfg is None:
            raise ValueError(f"Prompt {prompt_id} is disabled")
        
        system = cfg.get("system_prompt", "").format(**variables)
        user = cfg.get("user_template", "").format(**variables)
        return system, user
    
    def save(self, data: dict) -> None:
        """Save prompt configuration to disk."""
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._data = data
```

---

### Step 2: 重构各 service 以使用 PromptManager

**2.1** 修改 `analysis/label_service.py`:

将 `_build_llm_prompt()` 函数改为使用 PromptManager：

```python
from config.prompt_manager import PromptManager

_prompt_mgr = PromptManager()

def _build_llm_prompt_v2(dc: dict, shader_code: str) -> str:
    """Use PromptManager to render label_dc prompt."""
    variables = {
        "api_name": dc.get("api_name", ""),
        "vertex_count": dc.get("vertex_count", 0),
        "index_count": dc.get("index_count", 0),
        "instance_count": dc.get("instance_count", 0),
        "shader_code": shader_code,
        "render_targets": _format_render_targets(dc.get("render_targets") or []),
        "category_list": "/".join(_ALLOWED_CATEGORIES),
        # ... 其他变量
    }
    system, user = _prompt_mgr.render_prompt("label_dc", variables)
    return system + "\n\n" + user
```

保留原 `_build_llm_prompt()` 作为 fallback（向后兼容）。

**2.2** 修改 `analysis/gles_decompile_service.py`:

将 `_SYSTEM_PREAMBLE` 改为从 PromptManager 读取：

```python
from config.prompt_manager import PromptManager

_prompt_mgr = PromptManager()

def decompile_shaders(snapshot_dir: str | Path) -> dict:
    # ...
    cfg = _prompt_mgr.get_prompt("gles_decompile")
    if cfg is None:
        return {"skipped_reason": "gles_decompile prompt is disabled"}
    
    system_prompt = cfg["system_prompt"].format(
        ir3_reference=IR3_INSTRUCTION_REFERENCE  # 从 prompt_defaults.py 导入
    )
    
    # ... 其余逻辑不变
```

**2.3** 修改 `analysis/vlm_screenshot_service.py`:

```python
from config.prompt_manager import PromptManager

_prompt_mgr = PromptManager()

def generate_scene_description(snapshot_dir: str | Path, db=None) -> Path:
    # ...
    gpu_summary = _build_gpu_summary(label_data, metrics_data)
    
    variables = {
        "task_description": "Describe what you see in this screenshot...",
        "gpu_summary": gpu_summary,
    }
    _, user_prompt = _prompt_mgr.render_prompt("scene_description", variables)
    
    response = vlm.describe_image(screenshot, user_prompt)
    # ...
```

**2.4** 修改 `analysis/report_service.py`:

```python
from config.prompt_manager import PromptManager

_prompt_mgr = PromptManager()

def generate_report(snapshot_dir: str | Path) -> Path:
    # ...
    data = _build_data_summary(status, topdc)
    
    variables = {
        "report_structure": REPORT_STRUCTURE_TEMPLATE,  # 从 prompt_defaults.py 导入
        "scene_desc": scene_desc,
        "data_json": json.dumps(data, ensure_ascii=False, indent=2),
        "sdp_name": sdp_name,
    }
    _, prompt = _prompt_mgr.render_prompt("report_generation", variables)
    
    result = llm.chat(prompt)
    # ...
```

**2.5** 修改 `chat/prompts.py`:

```python
from config.prompt_manager import PromptManager

_prompt_mgr = PromptManager()

def build_system_prompt(
    snapshot_ids: list[int],
    user_lang: str = "en",
    session: "Session | None" = None,
) -> str:
    # ... 构建动态变量
    variables = {
        "lang_instruction": f"Respond in {_LANG_NAMES.get(user_lang, 'English')}.",
        "tools_doc": TOOLS_DOCUMENTATION,  # 从 prompt_defaults.py 导入
        "active_snapshots": _format_snapshots(snapshot_ids),
        "reply_format": REPLY_FORMAT_CONVENTION,
        "fetched_aspects": _format_fetched_aspects(session),
    }
    
    system_prompt, _ = _prompt_mgr.render_prompt("chat_system", variables)
    return system_prompt
```

---

### Step 3: 创建后端 API 端点

**3.1** 在 `webui/routes/` 下创建 `prompts.py` 路由模块：

```python
"""Prompt configuration API routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.prompt_manager import PromptManager
from analysis.prompt_defaults import DEFAULT_PROMPTS

router = APIRouter(prefix="/api/prompts", tags=["prompts"])

_mgr = PromptManager()

class PromptConfig(BaseModel):
    prompt_id: str
    enabled: bool
    system_prompt: str | None = None
    user_template: str | None = None
    description: str | None = None
    model_override: str | None = None

@router.get("/list")
def list_prompts():
    """List all available prompts with their current config."""
    prompts = {}
    for prompt_id, default in DEFAULT_PROMPTS.items():
        custom = _mgr.get_prompt(prompt_id) or {}
        prompts[prompt_id] = {
            "id": prompt_id,
            "description": custom.get("description", default.get("description", "")),
            "enabled": custom.get("enabled", True),
            "is_customized": prompt_id in _mgr._data.get("prompts", {}),
            "variables": default.get("variables", []),
            "output_format": default.get("output_format", "text"),
        }
    return {"prompts": prompts}

@router.get("/{prompt_id}")
def get_prompt(prompt_id: str):
    """Get prompt configuration (merged with default)."""
    cfg = _mgr.get_prompt(prompt_id)
    if cfg is None:
        raise HTTPException(404, f"Prompt {prompt_id} not found or disabled")
    return cfg

@router.get("/{prompt_id}/default")
def get_default_prompt(prompt_id: str):
    """Get the hardcoded default prompt (for reset functionality)."""
    default = DEFAULT_PROMPTS.get(prompt_id)
    if default is None:
        raise HTTPException(404, f"Prompt {prompt_id} not found")
    return default

@router.put("/{prompt_id}")
def update_prompt(prompt_id: str, config: PromptConfig):
    """Update prompt configuration."""
    if prompt_id not in DEFAULT_PROMPTS:
        raise HTTPException(404, f"Unknown prompt_id: {prompt_id}")
    
    # Validate output format if prompt expects JSON
    default = DEFAULT_PROMPTS[prompt_id]
    if default.get("output_format") == "json":
        # TODO: dry-run validation with a test input
        pass
    
    # Update config
    data = _mgr._data
    if "prompts" not in data:
        data["prompts"] = {}
    data["prompts"][prompt_id] = {
        "enabled": config.enabled,
        "system_prompt": config.system_prompt,
        "user_template": config.user_template,
        "description": config.description,
        "model_override": config.model_override,
    }
    _mgr.save(data)
    
    return {"ok": True, "message": f"Prompt {prompt_id} updated"}

@router.post("/{prompt_id}/reset")
def reset_prompt(prompt_id: str):
    """Reset prompt to default (remove custom config)."""
    if prompt_id not in DEFAULT_PROMPTS:
        raise HTTPException(404, f"Unknown prompt_id: {prompt_id}")
    
    data = _mgr._data
    if "prompts" in data and prompt_id in data["prompts"]:
        del data["prompts"][prompt_id]
        _mgr.save(data)
    
    return {"ok": True, "message": f"Prompt {prompt_id} reset to default"}

@router.get("/export")
def export_config():
    """Export entire prompts.json for backup/sharing."""
    return _mgr._data

@router.post("/import")
def import_config(data: dict):
    """Import prompts.json from another instance."""
    if data.get("schema_version") != "1.0":
        raise HTTPException(400, "Invalid schema version")
    _mgr.save(data)
    return {"ok": True, "message": "Prompts imported successfully"}
```

**3.2** 在 `webui/server.py` 中注册路由：

```python
from webui.routes import prompts as prompts_router

app.include_router(prompts_router.router)
```

---

### Step 4: 实现前端 UI

**4.1** 在 `webui/static/index.html` 中添加 "AI Prompts" 标签页：

在 Settings Modal 中新增一个 tab：

```html
<div id="settingsModal" class="modal">
  <div class="modal-content">
    <div class="tabs">
      <button class="tab-btn active" data-tab="general">General</button>
      <button class="tab-btn" data-tab="prompts">AI Prompts</button>
      <!-- 其他 tabs -->
    </div>
    
    <div id="prompts-tab" class="tab-content">
      <h3>AI Prompt Configuration</h3>
      <p class="hint">Customize the prompts used by all AI services. Changes take effect immediately.</p>
      
      <div class="prompts-toolbar">
        <button id="export-prompts-btn">Export Config</button>
        <button id="import-prompts-btn">Import Config</button>
        <input type="file" id="import-prompts-file" accept=".json" style="display:none">
      </div>
      
      <div id="prompts-list"></div>
    </div>
  </div>
</div>
```

**4.2** 在 `webui/static/app.js` 中实现提示词列表和编辑器：

```javascript
// Load prompts list
async function loadPromptsList() {
    const resp = await fetch('/api/prompts/list');
    const data = await resp.json();
    
    const container = document.getElementById('prompts-list');
    container.innerHTML = '';
    
    for (const [id, prompt] of Object.entries(data.prompts)) {
        const card = document.createElement('div');
        card.className = 'prompt-card';
        card.innerHTML = `
            <div class="prompt-header" onclick="togglePromptCard('${id}')">
                <h4>${prompt.description || id}</h4>
                <span class="badge ${prompt.is_customized ? 'customized' : 'default'}">
                    ${prompt.is_customized ? 'Customized' : 'Default'}
                </span>
                <label class="switch">
                    <input type="checkbox" ${prompt.enabled ? 'checked' : ''} 
                           onchange="togglePromptEnabled('${id}', this.checked)">
                    <span class="slider"></span>
                </label>
            </div>
            <div class="prompt-body" id="prompt-body-${id}" style="display:none">
                <div class="prompt-meta">
                    <p>Variables: <code>${prompt.variables.join(', ')}</code></p>
                    <p>Output format: <code>${prompt.output_format}</code></p>
                </div>
                <div class="prompt-editor">
                    <label>System Prompt:</label>
                    <textarea id="system-${id}" rows="8"></textarea>
                    
                    <label>User Template:</label>
                    <textarea id="user-${id}" rows="12"></textarea>
                    
                    <div class="preview-section">
                        <button onclick="previewPrompt('${id}')">Preview with Sample Data</button>
                        <pre id="preview-${id}"></pre>
                    </div>
                </div>
                <div class="prompt-actions">
                    <button onclick="savePrompt('${id}')">Save</button>
                    <button onclick="resetPrompt('${id}')">Reset to Default</button>
                </div>
            </div>
        `;
        container.appendChild(card);
    }
}

function togglePromptCard(id) {
    const body = document.getElementById(`prompt-body-${id}`);
    if (body.style.display === 'none') {
        // Load current config
        fetch(`/api/prompts/${id}`)
            .then(r => r.json())
            .then(cfg => {
                document.getElementById(`system-${id}`).value = cfg.system_prompt || '';
                document.getElementById(`user-${id}`).value = cfg.user_template || '';
                body.style.display = 'block';
            });
    } else {
        body.style.display = 'none';
    }
}

async function savePrompt(id) {
    const system = document.getElementById(`system-${id}`).value;
    const user = document.getElementById(`user-${id}`).value;
    
    const resp = await fetch(`/api/prompts/${id}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            prompt_id: id,
            enabled: true,
            system_prompt: system,
            user_template: user,
        })
    });
    
    if (resp.ok) {
        showNotification('Prompt saved successfully', 'success');
        loadPromptsList();  // Refresh list to show "Customized" badge
    } else {
        const err = await resp.json();
        showNotification(`Save failed: ${err.detail}`, 'error');
    }
}

async function resetPrompt(id) {
    if (!confirm(`Reset "${id}" to system default? This cannot be undone.`)) return;
    
    const resp = await fetch(`/api/prompts/${id}/reset`, {method: 'POST'});
    if (resp.ok) {
        showNotification('Prompt reset to default', 'success');
        togglePromptCard(id);  // Close card
        loadPromptsList();     // Refresh
    }
}

async function previewPrompt(id) {
    // TODO: Call a /api/prompts/{id}/preview endpoint with sample data
    // For now, just show the raw template
    const system = document.getElementById(`system-${id}`).value;
    const user = document.getElementById(`user-${id}`).value;
    document.getElementById(`preview-${id}`).textContent = 
        `=== System Prompt ===\n${system}\n\n=== User Prompt ===\n${user}`;
}

// Export/Import
document.getElementById('export-prompts-btn').addEventListener('click', async () => {
    const resp = await fetch('/api/prompts/export');
    const data = await resp.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'prompts.json';
    a.click();
});

document.getElementById('import-prompts-btn').addEventListener('click', () => {
    document.getElementById('import-prompts-file').click();
});

document.getElementById('import-prompts-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const text = await file.text();
    const data = JSON.parse(text);
    
    const resp = await fetch('/api/prompts/import', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data),
    });
    
    if (resp.ok) {
        showNotification('Prompts imported successfully', 'success');
        loadPromptsList();
    } else {
        showNotification('Import failed: invalid format', 'error');
    }
});
```

**4.3** 添加 CSS 样式 (`webui/static/style.css`):

```css
.prompt-card {
    border: 1px solid #ddd;
    border-radius: 8px;
    margin-bottom: 16px;
    overflow: hidden;
}

.prompt-header {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    background: #f5f5f5;
    cursor: pointer;
    user-select: none;
}

.prompt-header h4 {
    flex: 1;
    margin: 0;
}

.prompt-header .badge {
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
    margin-right: 12px;
}

.badge.default { background: #e0e0e0; color: #666; }
.badge.customized { background: #4CAF50; color: white; }

.prompt-body {
    padding: 16px;
    background: #fafafa;
}

.prompt-editor textarea {
    width: 100%;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 13px;
    padding: 8px;
    border: 1px solid #ccc;
    border-radius: 4px;
    margin-bottom: 12px;
}

.preview-section {
    margin-top: 16px;
    padding: 12px;
    background: #fff;
    border: 1px solid #ddd;
    border-radius: 4px;
}

.preview-section pre {
    max-height: 300px;
    overflow-y: auto;
    font-size: 12px;
}

.prompt-actions {
    display: flex;
    gap: 8px;
    margin-top: 16px;
}

.prompts-toolbar {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
}
```

---

### Step 5: 实现提示词预览和验证

**5.1** 在 `webui/routes/prompts.py` 中添加 preview 端点：

```python
@router.post("/{prompt_id}/preview")
def preview_prompt(prompt_id: str, custom_prompt: PromptConfig):
    """Preview prompt with sample data (dry-run without calling LLM)."""
    default = DEFAULT_PROMPTS.get(prompt_id)
    if default is None:
        raise HTTPException(404, f"Unknown prompt_id: {prompt_id}")
    
    # Generate sample variables based on prompt type
    if prompt_id == "label_dc":
        variables = {
            "api_name": "vkCmdDrawIndexed",
            "vertex_count": 1024,
            "instance_count": 1,
            "shader_code": "// Sample shader code...",
            "render_targets": "[Color HDR 1920x1080]",
            "category_list": "Scene/Character/UI/PostProcess",
        }
    elif prompt_id == "gles_decompile":
        variables = {
            "ir3_reference": "bary.f / sam / mad.f32 ...",
            "stage": "fragment",
            "disasm": "; sample disasm\n  sam (xyzw)r1.x, r0.x, s#0, t#0\n  ...",
        }
    elif prompt_id == "scene_description":
        variables = {
            "task_description": "Describe what you see...",
            "gpu_summary": "GPU cost: PostProcess 40%, Scene 30%, UI 20%...",
        }
    # ... 其他 prompt 类型
    
    system = custom_prompt.system_prompt or ""
    user = custom_prompt.user_template or ""
    
    try:
        rendered_system = system.format(**variables)
        rendered_user = user.format(**variables)
    except KeyError as e:
        raise HTTPException(400, f"Missing variable in template: {e}")
    
    return {
        "system_prompt": rendered_system,
        "user_prompt": rendered_user,
        "variables": variables,
    }

@router.post("/{prompt_id}/validate")
def validate_prompt(prompt_id: str, custom_prompt: PromptConfig):
    """Validate prompt format and check for dangerous keywords."""
    issues = []
    
    # Check for required variables
    default = DEFAULT_PROMPTS[prompt_id]
    required_vars = set(default.get("variables", []))
    template = custom_prompt.user_template or ""
    
    import re
    used_vars = set(re.findall(r'\{(\w+)\}', template))
    missing = required_vars - used_vars
    if missing:
        issues.append(f"Missing required variables: {', '.join(missing)}")
    
    # Check for dangerous keywords (SQL injection, code execution)
    dangerous = ["DROP TABLE", "DELETE FROM", "rm -rf", "__import__", "eval(", "exec("]
    combined = (custom_prompt.system_prompt or "") + template
    for kw in dangerous:
        if kw in combined:
            issues.append(f"Dangerous keyword detected: {kw}")
    
    # Check output format hints
    if default.get("output_format") == "json":
        if "json" not in combined.lower():
            issues.append("Prompt expects JSON output but template doesn't mention 'JSON'")
    
    return {"valid": len(issues) == 0, "issues": issues}
```

**5.2** 在前端 `app.js` 中调用 preview 端点：

```javascript
async function previewPrompt(id) {
    const system = document.getElementById(`system-${id}`).value;
    const user = document.getElementById(`user-${id}`).value;
    
    const resp = await fetch(`/api/prompts/${id}/preview`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            prompt_id: id,
            enabled: true,
            system_prompt: system,
            user_template: user,
        })
    });
    
    if (resp.ok) {
        const data = await resp.json();
        document.getElementById(`preview-${id}`).textContent = 
            `=== System Prompt (Rendered) ===\n${data.system_prompt}\n\n` +
            `=== User Prompt (Rendered) ===\n${data.user_prompt}\n\n` +
            `=== Variables Used ===\n${JSON.stringify(data.variables, null, 2)}`;
    } else {
        const err = await resp.json();
        alert(`Preview failed: ${err.detail}`);
    }
}
```

---

### Step 6: 处理缓存失效

**6.1** 修改 `analysis/llm_wrapper.py` 的缓存 key 计算：

```python
class _LlmCache:
    @staticmethod
    def hash(prompt: str, include_version: bool = False) -> str:
        """Hash prompt for cache key. If include_version=True, include prompt template version."""
        h = hashlib.sha256(prompt.encode("utf-8")).digest()
        return h[:16].hex()
    
    def get(self, prompt: str, prompt_version: str | None = None) -> str | None:
        """Get cached result. If prompt_version is provided, cache misses on version change."""
        key = self.hash(prompt)
        if prompt_version:
            key = f"{prompt_version}:{key}"
        
        with self._lock:
            pos = self._index.get(key)
            if pos is not None and self._slots[pos] is not None:
                return self._slots[pos]["response"]
        return None
```

**6.2** 在 PromptManager 中添加版本追踪：

```python
class PromptManager:
    def get_prompt_version(self, prompt_id: str) -> str:
        """Return version hash of the current prompt template (for cache invalidation)."""
        cfg = self.get_prompt(prompt_id)
        if cfg is None:
            return "disabled"
        
        combined = cfg.get("system_prompt", "") + cfg.get("user_template", "")
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:8]
```

**6.3** 在各 service 中调用缓存时传递版本：

```python
# In label_service.py
def _label_dc_with_llm(...):
    prompt = _build_llm_prompt_v2(dc, snap, ...)
    prompt_version = _prompt_mgr.get_prompt_version("label_dc")
    
    # Pass version to LLM wrapper (需要修改 llm_wrapper.chat() 签名)
    response = llm.chat(prompt, cache_version=prompt_version)
    ...
```

---

### Step 7: 文档和测试

**7.1** 更新 `README.md` 添加提示词自定义说明：

```markdown
## AI Prompt Customization

pysdp allows you to customize the prompts used by all AI services without modifying code.

### Via WebUI

1. Open Settings → AI Prompts tab
2. Click on a prompt card to expand the editor
3. Modify the system prompt and/or user template
4. Click "Preview" to see the rendered prompt with sample data
5. Click "Save" to apply changes (takes effect immediately)

### Via Configuration File

Edit `prompts.json` in the project root:

\```json
{
  "prompts": {
    "label_dc": {
      "enabled": true,
      "system_prompt": "Your custom system prompt...",
      "user_template": "API:{api_name}\n{shader_code}\n..."
    }
  }
}
\```

Variables (e.g. `{api_name}`) are automatically replaced at runtime.

### Export/Import

- **Export**: Settings → AI Prompts → Export Config → saves `prompts.json`
- **Import**: Settings → AI Prompts → Import Config → upload `prompts.json`

Use this to share prompt configurations across team members.

### Reset to Default

Click "Reset to Default" on any prompt card to restore the hardcoded system default.
```

**7.2** 创建测试脚本 `tests/test_prompt_manager.py`:

```python
"""Test prompt configuration system."""

import json
from pathlib import Path
import pytest

from config.prompt_manager import PromptManager
from analysis.prompt_defaults import DEFAULT_PROMPTS

def test_load_default_prompts(tmp_path):
    """When prompts.json doesn't exist, should fall back to defaults."""
    mgr = PromptManager(config_path=tmp_path / "prompts.json")
    cfg = mgr.get_prompt("label_dc")
    
    assert cfg is not None
    assert "system_prompt" in cfg
    assert cfg["system_prompt"] == DEFAULT_PROMPTS["label_dc"]["system_prompt"]

def test_custom_prompt_overrides_default(tmp_path):
    """Custom prompts.json should override defaults."""
    config_file = tmp_path / "prompts.json"
    config_file.write_text(json.dumps({
        "schema_version": "1.0",
        "prompts": {
            "label_dc": {
                "enabled": True,
                "system_prompt": "CUSTOM SYSTEM PROMPT",
            }
        }
    }))
    
    mgr = PromptManager(config_path=config_file)
    cfg = mgr.get_prompt("label_dc")
    
    assert cfg["system_prompt"] == "CUSTOM SYSTEM PROMPT"
    # user_template should still be default
    assert cfg["user_template"] == DEFAULT_PROMPTS["label_dc"]["user_template"]

def test_render_prompt_with_variables(tmp_path):
    """Prompt template variables should be replaced."""
    mgr = PromptManager(config_path=tmp_path / "prompts.json")
    
    variables = {
        "api_name": "vkCmdDraw",
        "vertex_count": 123,
        "shader_code": "void main() {}",
        "category_list": "Scene/UI",
    }
    
    system, user = mgr.render_prompt("label_dc", variables)
    
    assert "vkCmdDraw" in user
    assert "123" in user
    assert "void main()" in user

def test_disabled_prompt_returns_none(tmp_path):
    """Disabled prompts should return None."""
    config_file = tmp_path / "prompts.json"
    config_file.write_text(json.dumps({
        "schema_version": "1.0",
        "prompts": {
            "label_dc": {"enabled": False}
        }
    }))
    
    mgr = PromptManager(config_path=config_file)
    cfg = mgr.get_prompt("label_dc")
    
    assert cfg is None
```

---

## Validation

### Step 1 验证

运行以下命令确认配置文件和模块正确创建：

```bash
# 检查文件存在性
python -c "from pathlib import Path; print('prompts.json exists:', Path('prompts.json').exists())"
python -c "from analysis.prompt_defaults import DEFAULT_PROMPTS; print('Loaded', len(DEFAULT_PROMPTS), 'default prompts')"
python -c "from config.prompt_manager import PromptManager; mgr = PromptManager(); print('PromptManager initialized:', mgr is not None)"

# 检查默认值完整性
python -c "from analysis.prompt_defaults import DEFAULT_PROMPTS; assert 'label_dc' in DEFAULT_PROMPTS; assert 'gles_decompile' in DEFAULT_PROMPTS; print('All 5+ prompts defined')"
```

**预期输出**:
```
prompts.json exists: True
Loaded 5 default prompts
PromptManager initialized: True
All 5+ prompts defined
```

---

### Step 2 验证

运行以下命令确认各 service 能正确读取提示词：

```bash
# 测试 label_service
python -c "
from pathlib import Path
from analysis.label_service import _prompt_mgr
cfg = _prompt_mgr.get_prompt('label_dc')
print('label_dc enabled:', cfg is not None)
print('Contains category rules:', 'R1' in cfg.get('user_template', ''))
"

# 测试 gles_decompile_service
python -c "
from analysis.gles_decompile_service import _prompt_mgr
cfg = _prompt_mgr.get_prompt('gles_decompile')
print('gles_decompile enabled:', cfg is not None)
print('Contains IR3 reference:', 'bary.f' in cfg.get('system_prompt', ''))
"

# 测试 chat/prompts
python -c "
from chat.prompts import _prompt_mgr
cfg = _prompt_mgr.get_prompt('chat_system')
print('chat_system enabled:', cfg is not None)
print('Contains tools doc:', 'execute_python' in cfg.get('system_prompt', ''))
"
```

**预期输出**:
```
label_dc enabled: True
Contains category rules: True
gles_decompile enabled: True
Contains IR3 reference: True
chat_system enabled: True
Contains tools doc: True
```

---

### Step 3 验证

启动 WebUI 并测试 API 端点：

```bash
# 启动服务器
python -m pysdp --port 8000

# 在另一个终端测试 API
curl http://localhost:8000/api/prompts/list | jq '.prompts | keys'
# 预期输出: ["chat_system", "gles_decompile", "label_dc", "report_generation", "scene_description"]

curl http://localhost:8000/api/prompts/label_dc | jq '.system_prompt' | head -n 3
# 预期输出: "Classify this Vulkan draw call. Reply with JSON only."

curl http://localhost:8000/api/prompts/label_dc/default | jq '.variables | length'
# 预期输出: 6 (或其他预期变量数量)
```

---

### Step 4 验证

在 WebUI 中手动测试编辑功能：

1. 打开 http://localhost:8000 → Settings → AI Prompts
2. 展开 "DrawCall Classification" 卡片
3. 修改 System Prompt 中的任意文本（例如在开头加上 "TEST: "）
4. 点击 "Preview with Sample Data" → 检查预览输出是否包含 "TEST: "
5. 点击 "Save" → 检查是否显示成功提示
6. 刷新页面 → 重新展开卡片 → 检查修改是否保存
7. 点击 "Reset to Default" → 检查是否恢复原始文本
8. 检查 `prompts.json` 文件内容是否正确更新

**预期行为**:
- 编辑和保存后，Badge 从 "Default" 变为 "Customized"
- Reset 后 Badge 变回 "Default"
- `prompts.json` 文件中应包含保存的自定义内容

---

### Step 5 验证

测试提示词在实际 AI 调用中的效果：

```bash
# 创建一个测试 snapshot 目录（使用任意真实 snapshot）
TEST_SNAP="D:/snapdragon/analysis/test_run/snapshot_1"

# 1. 修改 label_dc 提示词（通过 API）
curl -X PUT http://localhost:8000/api/prompts/label_dc \
  -H "Content-Type: application/json" \
  -d '{
    "prompt_id": "label_dc",
    "enabled": true,
    "system_prompt": "TEST MODE: Classify this draw call. Always return category=UI for testing.",
    "user_template": "API:{api_name}\nShader:{shader_code}\nCategories:{category_list}\nOutput JSON:"
  }'

# 2. 运行 label service
python -c "
from pathlib import Path
from analysis.label_service import generate_label_json
result = generate_label_json('$TEST_SNAP')
print('Label file created:', result.exists())
"

# 3. 检查 label.json 输出
python -c "
import json
from pathlib import Path
label_data = json.loads(Path('$TEST_SNAP/label.json').read_text())
categories = [dc['label']['category'] for dc in label_data['draw_calls'][:5]]
print('First 5 categories:', categories)
# 如果提示词修改生效，大部分应该是 'UI' (因为我们强制指定了)
"

# 4. 重置提示词
curl -X POST http://localhost:8000/api/prompts/label_dc/reset

# 5. 重新运行 label service，检查是否恢复正常分类
```

**预期输出**:
- Step 2: `Label file created: True`
- Step 3: `First 5 categories: ['UI', 'UI', 'UI', ...]` (测试模式)
- Step 5 之后重新运行应该看到正常的分类结果（Scene, PostProcess 等混合）

---

### Step 6 验证

测试缓存失效机制：

```bash
# 1. 查看当前 LLM 缓存大小
python -c "
from analysis.llm_wrapper import get_llm
llm = get_llm()
if llm._cache:
    print('Cache size:', llm._cache._count, '/', llm._cache._capacity)
else:
    print('Cache disabled')
"

# 2. 运行一次 label (填充缓存)
python -c "from analysis.label_service import generate_label_json; generate_label_json('$TEST_SNAP')"

# 3. 修改提示词
curl -X PUT http://localhost:8000/api/prompts/label_dc \
  -d '{"prompt_id":"label_dc","enabled":true,"system_prompt":"Modified prompt v2","user_template":"..."}'

# 4. 再次运行 label（应该 cache MISS，因为 prompt_version 变了）
python -c "
from analysis.label_service import generate_label_json
import time
t0 = time.time()
generate_label_json('$TEST_SNAP')
print('Elapsed:', time.time() - t0, 'seconds')
# 如果缓存正确失效，耗时应该显著增加（需要重新调用 LLM）
"
```

**预期行为**:
- Step 2 第一次运行较慢（~10-30秒，取决于 DC 数量）
- 如果立即重复运行 Step 2（不修改提示词），应该很快（<1秒，全部命中缓存）
- Step 4 修改提示词后，再次运行应该变慢（缓存失效，重新调用 LLM）

---

### Step 7 验证

运行单元测试：

```bash
pytest tests/test_prompt_manager.py -v

# 预期输出:
# tests/test_prompt_manager.py::test_load_default_prompts PASSED
# tests/test_prompt_manager.py::test_custom_prompt_overrides_default PASSED
# tests/test_prompt_manager.py::test_render_prompt_with_variables PASSED
# tests/test_prompt_manager.py::test_disabled_prompt_returns_none PASSED
# ===== 4 passed in 0.12s =====
```

---

## Alternatives Considered

### Alternative 1: 提示词存储在 DuckDB 中

**Pros**:
- 统一的数据存储
- 支持多版本历史记录
- 更容易实现细粒度权限控制

**Cons**:
- 无法用 git 追踪提示词变更
- 备份和迁移更复杂（需要导出数据库）
- 手动编辑不方便（需要 SQL）
- 增加系统复杂度

**结论**: JSON 文件更适合配置型数据

---

### Alternative 2: 每个提示词一个独立文件

例如 `prompts/label_dc.txt`, `prompts/gles_decompile.txt`

**Pros**:
- 更易于用文本编辑器批量修改
- 支持 `.txt` 语法高亮（比 JSON 字符串更好）

**Cons**:
- 文件数量多（5-10 个）
- 缺少元数据（variables、output_format 等）
- 需要额外的清单文件记录变量映射

**结论**: 单个 JSON 文件更易于管理和分发

---

### Alternative 3: 在 UI 中提供"高级模式"和"向导模式"

**高级模式**: 直接编辑完整提示词（当前方案）

**向导模式**: 通过表单配置关键参数，自动生成提示词
- 例如：label_dc 可以通过勾选框选择"是否包含 Shadow 分类"、"是否启用 R1a/R1b 规则"等

**Pros**:
- 降低用户门槛
- 减少格式错误

**Cons**:
- 开发工作量大（每个提示词需要自定义表单）
- 灵活性受限（无法支持所有可能的修改）

**结论**: Phase 1 先实现高级模式，后续可根据需求添加向导模式

---

## Risks

### Risk 1: 用户修改提示词导致 AI 输出格式错误

**场景**: label_service 依赖 LLM 输出严格的 JSON 格式 `{"category": "...", "confidence": 0.9}`，如果用户删除了这部分约束，解析会失败。

**Mitigation**:
1. 在 UI 中显著标注 "Required Output Format"
2. 在 `validate_prompt()` 中检测 JSON 格式要求是否存在
3. 提供 "Test with real data" 功能（dry-run 一次实际调用，检查输出是否可解析）
4. 在 service 中添加 fallback：如果解析失败，记录警告并使用 rule-based 结果

---

### Risk 2: 提示词注入攻击

**场景**: 恶意用户在提示词中注入 Python 代码（例如在 chat 提示词中加入 `execute_python("import os; os.system('rm -rf /')")`），试图在 LLM 输出中执行任意代码。

**Mitigation**:
1. **沙盒隔离**: `execute_python` 工具已经使用 RestrictedPython 沙盒，危险操作会被拦截
2. **提示词审计**: 在 `validate_prompt()` 中检测危险关键词（`__import__`, `eval`, `exec`, `os.system` 等）
3. **输出过滤**: 在 LLM 输出中检测并移除 Markdown 代码块中的危险语句
4. **权限管理**: 限制提示词修改权限（未来可添加用户角色系统）

---

### Risk 3: 提示词修改导致性能退化

**场景**: 用户修改 label_service 提示词，导致 LLM 输出变得冗长（例如 LLM 开始解释分类原因），使得批量标注速度降低 10 倍。

**Mitigation**:
1. 在 UI 中显示每个提示词的"调用频率"标签（高频/低频）
2. 高频提示词的修改显示警告："此提示词每帧调用数百次，修改可能影响性能"
3. 提供 "Benchmark" 功能：用测试数据集测量修改前后的吞吐量差异
4. LLM wrapper 中添加超时检测：如果单次调用超过 30 秒，记录警告

---

### Risk 4: 提示词版本冲突

**场景**: 用户 A 修改了 label_dc 提示词并导出 `prompts.json`，用户 B 同时也修改了该提示词，两人尝试合并配置时发生冲突。

**Mitigation**:
1. 在 `prompts.json` 中为每个提示词添加 `last_modified` 和 `modified_by` 元数据
2. Import 时检测冲突：如果导入的提示词时间戳早于当前配置，显示警告
3. 提供 "Diff View" 功能：对比当前配置和导入配置的差异，让用户选择保留哪个
4. 建议用户使用 git 管理 `prompts.json`（文档中明确说明）

---

## Implementation Notes

### Phase 划分

**Phase 1** (本 Plan 范围):
- ✅ 配置文件系统 + PromptManager
- ✅ 各 service 重构
- ✅ 后端 API
- ✅ 前端 UI
- ✅ 基础验证和预览

**Phase 2** (后续扩展):
- 提示词版本历史记录（存储每次修改的快照）
- A/B 测试支持（同时启用两个提示词变体，对比效果）
- 向导模式（表单化配置）
- 团队协作功能（多用户权限管理）
- 更丰富的预览功能（使用真实数据 dry-run）

---

### 扩展性设计

为未来新增 AI 步骤预留接口：

**添加新提示词的步骤**:

1. 在 `analysis/prompt_defaults.py` 中添加默认值：
   ```python
   DEFAULT_PROMPTS["new_feature"] = {
       "description": "New AI feature",
       "system_prompt": "...",
       "user_template": "...",
       "variables": ["var1", "var2"],
       "output_format": "json",
   }
   ```

2. 在新的 service 中使用 PromptManager：
   ```python
   from config.prompt_manager import PromptManager
   _prompt_mgr = PromptManager()
   
   def new_service_function():
       cfg = _prompt_mgr.get_prompt("new_feature")
       system, user = _prompt_mgr.render_prompt("new_feature", variables={...})
       result = llm.chat(system + "\n" + user)
   ```

3. 前端 UI 自动检测并显示新提示词（基于 `/api/prompts/list` 返回结果）

**无需修改**:
- ✅ 前端代码（自动生成卡片）
- ✅ 数据库 schema
- ✅ API 路由（通用化设计）

---

### 依赖关系

```
config/prompt_manager.py
    ↓ (reads defaults from)
analysis/prompt_defaults.py
    ↓ (used by)
analysis/label_service.py
analysis/gles_decompile_service.py
analysis/vlm_screenshot_service.py
analysis/report_service.py
chat/prompts.py
    ↓ (exposes via)
webui/routes/prompts.py
    ↓ (consumed by)
webui/static/app.js (UI)
```

**关键约束**:
- `prompt_defaults.py` 必须不依赖任何外部配置（纯静态常量）
- `prompt_manager.py` 必须不依赖 WebUI（可在 CLI 脚本中使用）
- API 端点必须不依赖 session 状态（RESTful）

---

### 测试策略

**单元测试** (`tests/test_prompt_manager.py`):
- ✅ 配置文件加载和合并逻辑
- ✅ 变量替换和渲染
- ✅ 默认值 fallback

**集成测试** (`tests/integration/test_prompt_api.py`):
- ✅ API 端点响应格式
- ✅ 导出/导入配置文件
- ✅ 验证和预览功能

**端到端测试** (手动 + WebUI 自动化测试):
- ✅ 修改提示词 → 运行分析 → 检查输出
- ✅ 重置提示词 → 验证恢复默认
- ✅ 缓存失效验证

---

### 性能考虑

1. **配置文件加载**: 在 PromptManager 初始化时一次性加载，存储在内存中（单例模式）
2. **提示词渲染**: `str.format()` 性能足够（每次渲染 <1ms）
3. **缓存键计算**: 使用 SHA-256 前 8 字符作为版本号（避免完整哈希计算开销）
4. **API 响应**: `/api/prompts/list` 返回精简的元数据（不包含完整提示词文本），减少传输量

**预期影响**:
- 配置文件读取：+5ms (启动时一次性)
- 每次 LLM 调用：+1ms (提示词渲染)
- 总体性能影响：<1%

---

### 向后兼容性

**Phase 1 完成后**:

如果用户没有创建 `prompts.json`：
- ✅ 系统自动使用硬编码默认值（与现有行为完全一致）
- ✅ WebUI Settings 中显示所有提示词（标记为 "Default"）

如果用户升级旧版本：
- ✅ 无需迁移（新系统向后兼容）
- ✅ 首次打开 Settings → AI Prompts 时会自动生成 `prompts.json`（包含所有默认值）

**Breaking changes**: 无

---

## Execution Status

**状态**: Proposed（等待用户确认后开始实现）

**实现顺序**: 按 Step 1 → Step 7 顺序执行

**预估工作量**:
- Step 1-2: 4-6 小时（配置系统 + service 重构）
- Step 3: 2-3 小时（API 端点）
- Step 4: 4-5 小时（前端 UI）
- Step 5: 2-3 小时（预览和验证）
- Step 6: 2 小时（缓存失效）
- Step 7: 2 小时（文档和测试）

**总计**: 16-21 小时

**依赖**: 无外部依赖，可立即开始实现
