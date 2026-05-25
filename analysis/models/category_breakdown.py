"""category_breakdown.py — Per-category DC count and clock distribution model."""
from __future__ import annotations

from datetime import datetime, timezone

from analysis.models.base import AnalysisModel
from data.model_registry import register


@register
class CategoryBreakdown(AnalysisModel):
    name = "category_breakdown"
    description = "Per-category DrawCall count, clock budget, and label confidence"
    params_schema = {}
    viz_type = "bar_chart"

    def run(self, db, snapshot_id: int, **params) -> dict:
        conn = db.conn()

        # Primary path: read from snapshot_stats (pre-computed, fast)
        stats_rows = conn.execute(
            """
            SELECT category, dc_count, clocks_sum, clocks_pct, avg_conf
            FROM snapshot_stats
            WHERE snapshot_id = ?
            ORDER BY clocks_sum DESC
            """,
            [snapshot_id],
        ).fetchall()

        columns = [
            {"key": "category",   "label": "Category",        "type": "string"},
            {"key": "dc_count",   "label": "DC Count",        "type": "integer"},
            {"key": "clocks_sum", "label": "Total Clocks",    "type": "integer"},
            {"key": "clocks_pct", "label": "Clock %",         "type": "percent"},
            {"key": "avg_conf",   "label": "Avg Confidence",  "type": "float"},
        ]

        if stats_rows:
            rows = [
                {
                    "category":   r[0],
                    "dc_count":   r[1],
                    "clocks_sum": r[2],
                    "clocks_pct": r[3],
                    "avg_conf":   r[4],
                }
                for r in stats_rows
            ]
            total_dcs    = sum(r[1] for r in stats_rows)
            total_clocks = sum(r[2] for r in stats_rows)
            source = "snapshot_stats"
        else:
            # Fallback: compute from draw_calls + labels + metrics
            raw = self._compute_fallback(conn, snapshot_id)
            total_dcs    = sum(r[1] for r in raw)
            total_clocks = sum(r[2] for r in raw)
            rows = []
            for r in raw:
                clocks_pct = round(100.0 * r[2] / total_clocks, 2) if total_clocks else 0.0
                rows.append({
                    "category":   r[0],
                    "dc_count":   r[1],
                    "clocks_sum": r[2],
                    "clocks_pct": clocks_pct,
                    "avg_conf":   round(float(r[3]), 4),
                })
            source = "computed"

        return self._result(
            snapshot_id,
            columns,
            rows,
            summary={
                "total_categories": len(rows),
                "total_dc_count":   total_dcs,
                "total_clocks":     total_clocks,
            },
            metadata={
                "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source":      source,
            },
        )

    def _compute_fallback(self, conn, snapshot_id: int) -> list[tuple]:
        """Fallback when snapshot_stats is empty: compute from raw tables."""
        return conn.execute(
            """
            SELECT
                COALESCE(lb.category, 'Unknown') AS category,
                COUNT(dc.api_id) AS dc_count,
                COALESCE(SUM(m.clocks), 0) AS clocks_sum,
                ROUND(AVG(COALESCE(lb.confidence, 0.0)), 4) AS avg_conf
            FROM draw_calls dc
            LEFT JOIN labels lb
                ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
            LEFT JOIN metrics m
                ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
            WHERE dc.snapshot_id = ?
            GROUP BY COALESCE(lb.category, 'Unknown')
            ORDER BY clocks_sum DESC
            """,
            [snapshot_id],
        ).fetchall()
