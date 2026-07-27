"""Prompt configuration manager — load from prompts.json with fallback to defaults.

Usage:
    from prompt_config.prompt_manager import get_prompt_manager

    pm = get_prompt_manager()
    system, user = pm.render_prompt("label_dc", {
        "api_name": "vkCmdDrawIndexed",
        "vertex_count": 1024,
        ...
    })

    # Check if prompt is enabled
    if pm.is_enabled("label_dc"):
        ...
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from analysis.prompt_defaults import DEFAULT_PROMPTS


class PromptManager:
    """Manages AI prompt templates with file-based customization support."""

    def __init__(self, config_path: Path | None = None):
        """Initialize with optional custom config path.

        Args:
            config_path: Path to prompts.json. Defaults to <project_root>/prompts.json
        """
        if config_path is None:
            # Default to project root
            config_path = Path(__file__).parent.parent / "prompts.json"

        self.config_path = config_path
        self._data = self._load()

    def _load(self) -> dict:
        """Load prompts.json from disk. Returns empty dict if not found."""
        if not self.config_path.exists():
            return {"schema_version": "1.0", "prompts": {}}

        try:
            text = self.config_path.read_text(encoding="utf-8")
            return json.loads(text)
        except Exception:
            # Fallback to empty config on parse error
            return {"schema_version": "1.0", "prompts": {}}

    def reload(self) -> None:
        """Reload configuration from disk."""
        self._data = self._load()

    def get_prompt(self, prompt_id: str) -> dict | None:
        """Get prompt configuration with fallback to default.

        Args:
            prompt_id: Prompt identifier (e.g. "label_dc", "gles_decompile")

        Returns:
            Prompt config dict, or None if disabled
        """
        # Start with default
        default = DEFAULT_PROMPTS.get(prompt_id, {})

        # Override with custom config
        custom = self._data.get("prompts", {}).get(prompt_id, {})
        merged = {**default, **custom}

        # Return None if disabled
        if not merged.get("enabled", True):
            return None

        return merged

    def is_enabled(self, prompt_id: str) -> bool:
        """Check if a prompt is enabled."""
        cfg = self.get_prompt(prompt_id)
        return cfg is not None

    def render_prompt(
        self,
        prompt_id: str,
        variables: dict[str, Any],
    ) -> tuple[str, str]:
        """Render prompt template with variables.

        Args:
            prompt_id: Prompt identifier
            variables: Dict of variable name → value for substitution

        Returns:
            Tuple of (system_prompt, user_prompt)

        Raises:
            ValueError: If prompt is disabled or missing required variables
        """
        cfg = self.get_prompt(prompt_id)
        if cfg is None:
            raise ValueError(f"Prompt '{prompt_id}' is disabled or not found")

        # Check required variables
        required = set(cfg.get("variables", []))
        provided = set(variables.keys())
        missing = required - provided
        if missing:
            raise ValueError(
                f"Prompt '{prompt_id}' missing required variables: {missing}"
            )

        # Render templates
        system_template = cfg.get("system_prompt", "")
        user_template = cfg.get("user_template", "")

        try:
            system = system_template.format(**variables) if system_template else ""
            user = user_template.format(**variables) if user_template else ""
        except KeyError as e:
            raise ValueError(
                f"Prompt '{prompt_id}' template references undefined variable: {e}"
            )

        return system, user

    def list_prompts(self) -> list[dict]:
        """List all available prompts with their metadata.

        Returns:
            List of dicts with keys: id, description, enabled, call_frequency
        """
        result = []
        for prompt_id, default_cfg in DEFAULT_PROMPTS.items():
            cfg = self.get_prompt(prompt_id)
            result.append({
                "id": prompt_id,
                "description": default_cfg.get("description", ""),
                "enabled": cfg is not None,
                "call_frequency": default_cfg.get("call_frequency", "medium"),
                "output_format": default_cfg.get("output_format", "text"),
            })
        return result

    def save_custom_prompt(
        self,
        prompt_id: str,
        system_prompt: str | None = None,
        user_template: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Save custom prompt configuration to prompts.json.

        Args:
            prompt_id: Prompt identifier
            system_prompt: Custom system prompt (None = keep default)
            user_template: Custom user template (None = keep default)
            enabled: Enable/disable flag (None = keep default)
        """
        prompts = self._data.setdefault("prompts", {})
        custom = prompts.setdefault(prompt_id, {})

        if system_prompt is not None:
            custom["system_prompt"] = system_prompt
        if user_template is not None:
            custom["user_template"] = user_template
        if enabled is not None:
            custom["enabled"] = enabled

        # Write to disk
        self.config_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Reload in-memory cache
        self._data = self._load()

    def reset_to_default(self, prompt_id: str) -> None:
        """Remove custom configuration for a prompt (revert to default).

        Args:
            prompt_id: Prompt identifier
        """
        prompts = self._data.get("prompts", {})
        if prompt_id in prompts:
            del prompts[prompt_id]

            # Write to disk
            self.config_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Reload
            self._data = self._load()


# ── Singleton instance ─────────────────────────────────────────────────────────

_instance: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    """Get singleton PromptManager instance."""
    global _instance
    if _instance is None:
        _instance = PromptManager()
    return _instance


def reload_prompts() -> None:
    """Force reload prompts from disk (useful after UI edits)."""
    global _instance
    if _instance is not None:
        _instance.reload()
