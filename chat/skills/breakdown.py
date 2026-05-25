"""Deterministic breakdown skill — runs category_breakdown model."""
from data.model_registry import run_model


def run(ctx):
    if not ctx.snapshot_ids:
        return {"error": "No snapshot selected"}
    return run_model("category_breakdown", ctx.db, ctx.snapshot_ids[0])
