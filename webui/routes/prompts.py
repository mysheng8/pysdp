"""API routes for AI prompt customization."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from prompt_config.prompt_manager import get_prompt_manager, reload_prompts

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptUpdateRequest(BaseModel):
    """Request model for updating a prompt."""

    system_prompt: str | None = None
    user_template: str | None = None
    enabled: bool | None = None


class PromptPreviewRequest(BaseModel):
    """Request model for previewing a rendered prompt."""

    prompt_id: str
    system_prompt: str | None = None
    user_template: str | None = None
    sample_variables: dict[str, Any]


@router.get("")
async def list_prompts():
    """List all available AI prompts with metadata.

    Returns:
        List of prompt metadata dicts
    """
    pm = get_prompt_manager()
    prompts = pm.list_prompts()
    return {"prompts": prompts}


@router.get("/{prompt_id}")
async def get_prompt(prompt_id: str):
    """Get detailed configuration for a specific prompt.

    Args:
        prompt_id: Prompt identifier (e.g. "label_dc")

    Returns:
        Prompt configuration dict with system_prompt, user_template, variables, etc.
    """
    pm = get_prompt_manager()
    cfg = pm.get_prompt(prompt_id)

    if cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt '{prompt_id}' is disabled or not found",
        )

    # Include default values for comparison
    from analysis.prompt_defaults import DEFAULT_PROMPTS

    default = DEFAULT_PROMPTS.get(prompt_id, {})

    return {
        "id": prompt_id,
        "current": {
            "enabled": cfg.get("enabled", True),
            "description": cfg.get("description", ""),
            "system_prompt": cfg.get("system_prompt", ""),
            "user_template": cfg.get("user_template", ""),
            "variables": cfg.get("variables", []),
            "output_format": cfg.get("output_format", "text"),
            "validation_schema": cfg.get("validation_schema"),
            "call_frequency": cfg.get("call_frequency", "medium"),
        },
        "default": {
            "system_prompt": default.get("system_prompt", ""),
            "user_template": default.get("user_template", ""),
        },
    }


@router.put("/{prompt_id}")
async def update_prompt(prompt_id: str, request: PromptUpdateRequest):
    """Update a prompt's configuration.

    Args:
        prompt_id: Prompt identifier
        request: Update request with system_prompt, user_template, enabled

    Returns:
        Success message
    """
    pm = get_prompt_manager()

    # Check if prompt exists in defaults
    from analysis.prompt_defaults import DEFAULT_PROMPTS

    if prompt_id not in DEFAULT_PROMPTS:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt '{prompt_id}' not found in defaults",
        )

    try:
        pm.save_custom_prompt(
            prompt_id,
            system_prompt=request.system_prompt,
            user_template=request.user_template,
            enabled=request.enabled,
        )

        # Reload to pick up changes
        reload_prompts()

        return {"success": True, "message": f"Prompt '{prompt_id}' updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{prompt_id}/reset")
async def reset_prompt(prompt_id: str):
    """Reset a prompt to its default configuration.

    Args:
        prompt_id: Prompt identifier

    Returns:
        Success message
    """
    pm = get_prompt_manager()

    try:
        pm.reset_to_default(prompt_id)
        reload_prompts()
        return {"success": True, "message": f"Prompt '{prompt_id}' reset to default"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/preview")
async def preview_prompt(request: PromptPreviewRequest):
    """Preview a rendered prompt with sample variables.

    This allows users to see what the final prompt looks like before saving.

    Args:
        request: Preview request with prompt_id, optional custom templates, and sample variables

    Returns:
        Rendered system_prompt and user_prompt
    """
    pm = get_prompt_manager()

    # Get base config
    cfg = pm.get_prompt(request.prompt_id)
    if cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt '{request.prompt_id}' is disabled or not found",
        )

    # Override with custom templates if provided
    system_template = request.system_prompt if request.system_prompt is not None else cfg.get("system_prompt", "")
    user_template = request.user_template if request.user_template is not None else cfg.get("user_template", "")

    # Check required variables
    required = set(cfg.get("variables", []))
    provided = set(request.sample_variables.keys())
    missing = required - provided

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required variables: {list(missing)}",
        )

    # Render with provided variables
    try:
        system = system_template.format(**request.sample_variables) if system_template else ""
        user = user_template.format(**request.sample_variables) if user_template else ""

        return {
            "system_prompt": system,
            "user_prompt": user,
            "char_count": {
                "system": len(system),
                "user": len(user),
                "total": len(system) + len(user),
            },
        }
    except KeyError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Template references undefined variable: {e}",
        )


@router.post("/reload")
async def reload_config():
    """Force reload prompts.json from disk.

    Useful after manually editing the file.

    Returns:
        Success message with reload timestamp
    """
    from datetime import datetime

    reload_prompts()
    return {
        "success": True,
        "message": "Prompts configuration reloaded",
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/export")
async def export_config():
    """Export current prompts.json configuration.

    Returns:
        Full prompts.json content as JSON
    """
    pm = get_prompt_manager()

    # Read raw file content
    if pm.config_path.exists():
        import json

        content = json.loads(pm.config_path.read_text(encoding="utf-8"))
        return content
    else:
        return {"schema_version": "1.0", "prompts": {}}


@router.post("/import")
async def import_config(data: dict):
    """Import prompts.json configuration.

    Args:
        data: Full prompts.json structure

    Returns:
        Success message
    """
    pm = get_prompt_manager()

    try:
        # Validate schema version
        if data.get("schema_version") != "1.0":
            raise HTTPException(
                status_code=400,
                detail="Invalid schema_version — expected '1.0'",
            )

        # Write to disk
        pm.config_path.write_text(
            __import__("json").dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        reload_prompts()
        return {"success": True, "message": "Configuration imported successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Preset Management ──────────────────────────────────────────────────────────


class PresetSaveRequest(BaseModel):
    """Request model for saving a preset."""

    name: str
    description: str | None = None
    system_prompt: str | None = None
    user_template: str | None = None


@router.get("/{prompt_id}/presets")
async def list_presets(prompt_id: str):
    """List all saved presets for a prompt.

    Args:
        prompt_id: Prompt identifier

    Returns:
        List of preset metadata
    """
    from pathlib import Path
    import json

    preset_dir = Path("presets") / prompt_id
    if not preset_dir.exists():
        return {"presets": []}

    presets = []
    for preset_file in preset_dir.glob("*.json"):
        try:
            data = json.loads(preset_file.read_text(encoding="utf-8"))
            presets.append({
                "name": preset_file.stem,
                "description": data.get("description", ""),
                "created_at": data.get("created_at"),
            })
        except Exception:
            continue

    return {"presets": sorted(presets, key=lambda x: x.get("created_at", ""), reverse=True)}


@router.post("/{prompt_id}/presets")
async def save_preset(prompt_id: str, request: PresetSaveRequest):
    """Save current prompt as a preset.

    Args:
        prompt_id: Prompt identifier
        request: Preset save request with name, description, and prompt content

    Returns:
        Success message
    """
    from pathlib import Path
    from datetime import datetime
    import json
    import re

    # Validate preset name
    if not request.name or not re.match(r'^[a-zA-Z0-9_-]+$', request.name):
        raise HTTPException(
            status_code=400,
            detail="Preset name must contain only letters, numbers, hyphens, and underscores",
        )

    preset_dir = Path("presets") / prompt_id
    preset_dir.mkdir(parents=True, exist_ok=True)

    preset_file = preset_dir / f"{request.name}.json"

    preset_data = {
        "name": request.name,
        "description": request.description or "",
        "system_prompt": request.system_prompt or "",
        "user_template": request.user_template or "",
        "created_at": datetime.now().isoformat(),
    }

    try:
        preset_file.write_text(
            json.dumps(preset_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"success": True, "message": f"Preset '{request.name}' saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{prompt_id}/presets/{preset_name}")
async def load_preset(prompt_id: str, preset_name: str):
    """Load a saved preset.

    Args:
        prompt_id: Prompt identifier
        preset_name: Preset name

    Returns:
        Preset content
    """
    from pathlib import Path
    import json

    preset_file = Path("presets") / prompt_id / f"{preset_name}.json"

    if not preset_file.exists():
        raise HTTPException(status_code=404, detail=f"Preset '{preset_name}' not found")

    try:
        data = json.loads(preset_file.read_text(encoding="utf-8"))
        return {
            "name": data.get("name"),
            "description": data.get("description"),
            "system_prompt": data.get("system_prompt"),
            "user_template": data.get("user_template"),
            "created_at": data.get("created_at"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{prompt_id}/presets/{preset_name}")
async def delete_preset(prompt_id: str, preset_name: str):
    """Delete a saved preset.

    Args:
        prompt_id: Prompt identifier
        preset_name: Preset name

    Returns:
        Success message
    """
    from pathlib import Path

    preset_file = Path("presets") / prompt_id / f"{preset_name}.json"

    if not preset_file.exists():
        raise HTTPException(status_code=404, detail=f"Preset '{preset_name}' not found")

    try:
        preset_file.unlink()
        return {"success": True, "message": f"Preset '{preset_name}' deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
