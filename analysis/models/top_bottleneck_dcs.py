"""top_bottleneck_dcs.py — Top N draw calls by GPU clock cost model."""
from __future__ import annotations

from datetime import datetime, timezone

from analysis.models.base import AnalysisModel
from data.model_registry import register


@register
class TopBottleneckDcs(AnalysisModel):
    name = "top_bottleneck_dcs"
    description = "Top N DrawCalls by GPU clock cost with label and metric details"
    params_schema = {
        "type": "object",
        "properties": {
            "top_n":    {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            "category": {"type": "string"},
        },
    }
    viz_type = "table"

    def run(self, db, snapshot_id: int, **params) -> dict:
        top_n    = int(params.get("top_n", 10))
        category = params.get("category")
        conn     = db.conn()

        sql = """
            SELECT
                dc.api_id,
                dc.dc_id,
                dc.api_name,
                COALESCE(lb.category, 'Unknown')        AS category,
                COALESCE(lb.subcategory, '')            AS subcategory,
                COALESCE(lb.detail, '')                 AS detail,
                ROUND(COALESCE(lb.confidence, 0.0), 4) AS confidence,
                m.clocks,
                m.read_total_bytes,
                m.write_total_bytes,
                m.fragments_shaded,
                m.shaders_busy_pct
            FROM draw_calls dc
            LEFT JOIN labels lb
                ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
            LEFT JOIN metrics m
                ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
            WHERE dc.snapshot_id = ?
        """
        params_list: list = [snapshot_id]

        if category:
            sql += " AND lb.category = ?"
            params_list.append(category)

        sql += " ORDER BY m.clocks DESC NULLS LAST LIMIT ?"
        params_list.append(top_n)

        raw = conn.execute(sql, params_list).fetchall()

        col_names = [
            "api_id", "dc_id", "api_name", "category", "subcategory",
            "detail", "confidence", "clocks", "read_total_bytes",
            "write_total_bytes", "fragments_shaded", "shaders_busy_pct",
        ]
        rows = [dict(zip(col_names, r)) for r in raw]

        columns = [
            {"key": "api_id",           "label": "API ID",        "type": "integer"},
            {"key": "api_name",         "label": "API Call",      "type": "string"},
            {"key": "category",         "label": "Category",      "type": "string"},
            {"key": "detail",           "label": "Detail",        "type": "string"},
            {"key": "clocks",           "label": "Clocks",        "type": "integer"},
            {"key": "shaders_busy_pct", "label": "Shader Busy %", "type": "percent"},
            {"key": "fragments_shaded", "label": "Fragments",     "type": "integer"},
            {"key": "read_total_bytes", "label": "Read Bytes",    "type": "integer"},
        ]

        return self._result(
            snapshot_id,
            columns,
            rows,
            summary={"returned": len(rows), "top_n": top_n},
            metadata={
                "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "params":      {"top_n": top_n, "category": category},
            },
        )
