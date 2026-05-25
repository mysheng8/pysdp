"""Deterministic correlate skill — clock correlation analysis."""
from chat.tools import ToolExecutor


def run(ctx):
    if not ctx.snapshot_ids:
        return {"error": "No snapshot selected"}
    executor = ToolExecutor()
    return executor._get_clock_correlation(ctx.db, {"snapshot_id": ctx.snapshot_ids[0]})
