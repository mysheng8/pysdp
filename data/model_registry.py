"""model_registry.py — Pure dispatch registry for AnalysisModel subclasses.

Models register themselves via @register at import time.
server.py triggers registration by importing analysis.models.
"""
from __future__ import annotations

from typing import Any

_REGISTRY: dict[str, Any] = {}


def register(model_cls: Any) -> Any:
    """Decorator or callable: register an AnalysisModel subclass by its .name."""
    _REGISTRY[model_cls.name] = model_cls
    return model_cls


def list_models() -> list[dict]:
    """Return metadata dicts for all registered models."""
    return [
        {
            "name":          cls.name,
            "description":   cls.description,
            "params_schema": cls.params_schema,
            "viz_type":      cls.viz_type,
        }
        for cls in _REGISTRY.values()
    ]


def run_model(name: str, db: Any, snapshot_id: int, **params: Any) -> dict:
    """Run a registered model by name. Raises KeyError if not found."""
    if name not in _REGISTRY:
        raise KeyError(f"Model '{name}' not registered. Available: {list(_REGISTRY)}")
    return _REGISTRY[name]().run(db, snapshot_id, **params)
