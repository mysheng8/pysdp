"""config.py — Central configuration for pySdp.

Resolution order (first wins):
  1. Environment variables (PYSDP_* prefix)
  2. .env file at pySdp root (via python-dotenv)
  3. pySdp/config.ini (committed, safe defaults)
  4. ../SDPCLI/config.ini + ../SDPCLI/secrets.ini (monorepo fallback)
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_PYSDP_ROOT = Path(__file__).resolve().parent

# Mapping: config.ini key → PYSDP_* env var name
_ENV_MAP: dict[str, str] = {
    "LlmApiEndpoint":            "PYSDP_LLM_ENDPOINT",
    "LlmApiKey":                 "PYSDP_LLM_KEY",
    "LlmModel":                  "PYSDP_LLM_MODEL",
    "LlmTimeoutSeconds":         "PYSDP_LLM_TIMEOUT",
    "LlmMaxOutputTokens":        "PYSDP_LLM_MAX_TOKENS",
    "LlmMaxShaderChars":         "PYSDP_LLM_MAX_SHADER_CHARS",
    "LlmMaxConcurrentRequests":  "PYSDP_LLM_MAX_CONCURRENT",
    "LlmCacheEnabled":           "PYSDP_LLM_CACHE_ENABLED",
    "LlmCacheOverride":          "PYSDP_LLM_CACHE_OVERRIDE",
    "LlmCacheSize":              "PYSDP_LLM_CACHE_SIZE",
    "LlmCachePath":              "PYSDP_LLM_CACHE_PATH",
    "VlmApiEndpoint":            "PYSDP_VLM_ENDPOINT",
    "VlmApiKey":                 "PYSDP_VLM_KEY",
    "VlmModel":                  "PYSDP_VLM_MODEL",
    "VlmTimeoutSeconds":         "PYSDP_VLM_TIMEOUT",
    "VlmMaxOutputTokens":        "PYSDP_VLM_MAX_TOKENS",
    "VlmTextureDescriptionEnabled": "PYSDP_VLM_TEXTURE_ENABLED",
    "VlmTextureMinSize":         "PYSDP_VLM_TEXTURE_MIN_SIZE",
    "VlmTextureMaxConcurrent":   "PYSDP_VLM_TEXTURE_MAX_CONCURRENT",
    "ChatApiEndpoint":           "PYSDP_CHAT_ENDPOINT",
    "ChatApiKey":                "PYSDP_CHAT_KEY",
    "ChatModel":                 "PYSDP_CHAT_MODEL",
    "ChatMaxTokens":             "PYSDP_CHAT_MAX_TOKENS",
    "ChatTimeoutSeconds":        "PYSDP_CHAT_TIMEOUT",
    "PyLogLevel":                "PYSDP_LOG_LEVEL",
    "PackageName":               "PYSDP_PACKAGE_NAME",
    "RenderingAPI":              "PYSDP_RENDERING_API",
    "WorkingDirectory":          "PYSDP_WORKING_DIR",
    "ProjectDir":                "PYSDP_PROJECT_DIR",
    "SdpDir":                    "PYSDP_SDP_DIR",
    "AnalysisDir":               "PYSDP_ANALYSIS_DIR",
    "ReportDir":                 "PYSDP_REPORT_DIR",
    "WebSnapshotId":             "PYSDP_WEB_SNAPSHOT_ID",
    "WebTargets":                "PYSDP_WEB_TARGETS",
    "GlesLlmDecompile":          "PYSDP_GLES_DECOMPILE",
    "GlesLlmDecompileMaxLines":  "PYSDP_GLES_DECOMPILE_MAX_LINES",
    "AnalysisCategories":        "PYSDP_ANALYSIS_CATEGORIES",
    "Ir3DisasmPath":             "PYSDP_IR3_DISASM_PATH",
    "Ir3ChipId":                 "PYSDP_IR3_CHIP_ID",
    "VulkanSDKPath":             "PYSDP_VULKAN_SDK_PATH",
    "ShaderOutputFormat":        "PYSDP_SHADER_OUTPUT_FORMAT",
    "AttributionRulesPath":      "PYSDP_RULES_PATH",
}

# Reverse mapping for env→key lookup
_KEY_FROM_ENV = {v: k for k, v in _ENV_MAP.items()}

_settings: dict[str, str] | None = None
_config_path: Path | None = None
_lock = threading.Lock()


def _parse_ini(path: Path) -> dict[str, str]:
    """Parse a flat key=value INI file (no section headers)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out


def _load() -> tuple[dict[str, str], Path]:
    """Build merged config dict. Returns (settings, active_config_path)."""
    # Layer 4: monorepo fallback (lowest priority)
    merged: dict[str, str] = {}
    sdpcli_config = _PYSDP_ROOT.parent / "SDPCLI" / "config.ini"
    active_path = _PYSDP_ROOT / "config.ini"
    if sdpcli_config.exists():
        merged.update(_parse_ini(sdpcli_config))
        secrets = sdpcli_config.parent / "secrets.ini"
        if secrets.exists():
            for k, v in _parse_ini(secrets).items():
                if v:
                    merged[k] = v
        active_path = sdpcli_config

    # Layer 3: pySdp/config.ini (overrides monorepo)
    local_config = _PYSDP_ROOT / "config.ini"
    if local_config.exists():
        for k, v in _parse_ini(local_config).items():
            if v:
                merged[k] = v
        active_path = local_config

    # Layer 2: .env file (overrides config.ini)
    try:
        from dotenv import dotenv_values
        env_file = _PYSDP_ROOT / ".env"
        if env_file.exists():
            for env_key, env_val in dotenv_values(env_file).items():
                if env_val:
                    ini_key = _KEY_FROM_ENV.get(env_key)
                    if ini_key:
                        merged[ini_key] = env_val
                    else:
                        merged[env_key] = env_val
    except ImportError:
        pass

    # Layer 1: environment variables (highest priority)
    for ini_key, env_key in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            merged[ini_key] = val

    return merged, active_path


def get_settings() -> dict[str, str]:
    """Return merged config dict (lazy singleton, thread-safe)."""
    global _settings, _config_path
    if _settings is None:
        with _lock:
            if _settings is None:
                _settings, _config_path = _load()
    return _settings


def get_config_path() -> Path:
    """Return the path of the active config file (for write-back)."""
    get_settings()
    return _config_path  # type: ignore


def reload() -> None:
    """Force re-read of config (e.g. after settings are saved)."""
    global _settings, _config_path
    with _lock:
        _settings, _config_path = _load()
