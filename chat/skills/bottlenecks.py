"""Deterministic bottlenecks skill — top DCs + detail for top 3."""
from data.model_registry import run_model
from data.query import get_dc_detail


def run(ctx):
    if not ctx.snapshot_ids:
        return {"error": "No snapshot selected"}
    sid = ctx.snapshot_ids[0]
    top = run_model("top_bottleneck_dcs", ctx.db, sid, top_n=10)
    rows = top.get("rows", []) if isinstance(top, dict) else []
    details = []
    for dc in rows[:3]:
        api_id = dc.get("api_id")
        if api_id:
            d = get_dc_detail(ctx.db, sid, api_id)
            if d:
                details.append(d)
    return {"top_dcs": top, "details": details}
