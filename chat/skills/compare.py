"""Deterministic compare skill — side-by-side category breakdown."""
from chat.tools import ToolExecutor


def run(ctx):
    if len(ctx.snapshot_ids) < 2:
        return {"error": "Need at least 2 snapshots to compare"}
    executor = ToolExecutor()
    results = {}
    for sid in ctx.snapshot_ids[:2]:
        results[sid] = executor._get_label_agg(ctx.db, {"snapshot_id": sid, "metric": "clocks", "agg": "sum"})
    return {"snapshots": results}
