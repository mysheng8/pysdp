"""label_quality.py — Label confidence distribution model."""
from __future__ import annotations

from datetime import datetime, timezone

from analysis.models.base import AnalysisModel
from data.model_registry import register


@register
class LabelQuality(AnalysisModel):
    name = "label_quality"
    description = "Label confidence distribution and reason-tag histogram"
    params_schema = {}
    viz_type = "bar_chart"

    def run(self, db, snapshot_id: int, **params) -> dict:
        conn = db.conn()

        # Confidence buckets
        buckets_raw = conn.execute(
            """
            SELECT
                CASE
                    WHEN confidence >= 0.9 THEN 'high (>=0.9)'
                    WHEN confidence >= 0.7 THEN 'medium (0.7-0.9)'
                    WHEN confidence >= 0.5 THEN 'low (0.5-0.7)'
                    ELSE 'very low (<0.5)'
                END AS bucket,
                COUNT(*) AS count
            FROM labels
            WHERE snapshot_id = ?
            GROUP BY bucket
            ORDER BY MIN(confidence) DESC
            """,
            [snapshot_id],
        ).fetchall()

        rows = [{"bucket": r[0], "count": r[1]} for r in buckets_raw]

        # Scalar summary
        summary_raw = conn.execute(
            """
            SELECT
                ROUND(AVG(confidence), 4),
                COUNT(*),
                SUM(CASE WHEN confidence < 0.6 THEN 1 ELSE 0 END)
            FROM labels WHERE snapshot_id = ?
            """,
            [snapshot_id],
        ).fetchone()

        avg_conf, total, low_count = (summary_raw or (0.0, 0, 0))
        avg_conf  = avg_conf  or 0.0
        total     = total     or 0
        low_count = low_count or 0

        columns = [
            {"key": "bucket", "label": "Confidence Bucket", "type": "string"},
            {"key": "count",  "label": "DC Count",          "type": "integer"},
        ]

        return self._result(
            snapshot_id,
            columns,
            rows,
            summary={
                "avg_confidence":       avg_conf,
                "total_labeled":        total,
                "low_confidence_count": low_count,
                "low_confidence_ratio": round(low_count / max(total, 1), 4),
            },
            metadata={
                "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
