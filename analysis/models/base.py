"""base.py — Abstract base class for all analysis models."""
from __future__ import annotations

from typing import ClassVar, Any


class AnalysisModel:
    """Abstract base class for all analysis models.

    Subclasses must declare ClassVar attributes and implement run().
    """
    name: ClassVar[str]           # unique snake_case identifier
    description: ClassVar[str]    # one-line human description
    params_schema: ClassVar[dict]  # JSON Schema dict — {} means no params
    viz_type: ClassVar[str]       # default: "table" | "bar_chart" | "pie_chart" | "number"

    def run(self, db, snapshot_id: int, **params) -> dict:
        raise NotImplementedError

    # ── Convenience: build standard result envelope ───────────────────────────
    def _result(
        self,
        snapshot_id: int,
        columns: list,
        rows: list,
        summary: dict | None = None,
        viz_type: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return {
            "model_name":  self.name,
            "snapshot_id": snapshot_id,
            "viz_type":    viz_type or self.viz_type,
            "columns":     columns,
            "rows":        rows,
            "summary":     summary or {},
            "metadata":    metadata or {},
        }
