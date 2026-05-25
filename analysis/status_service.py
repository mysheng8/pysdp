"""
status_service.py — Aggregate statistics from label.json + metrics.json → status.json.

Python port of StatusJsonService.cs.  Reads the two raw JSON files already in the
snapshot directory and writes snapshot_{id}_status.json.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Percentile (nearest-rank, same algorithm as C#) ───────────────────────────

def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = int(math.floor(p * len(sorted_v)))
    if idx >= len(sorted_v):
        idx = len(sorted_v) - 1
    return sorted_v[idx]


def _percentile_block(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(_percentile(values, 0.50), 4),
        "p60": round(_percentile(values, 0.60), 4),
        "p70": round(_percentile(values, 0.70), 4),
        "p80": round(_percentile(values, 0.80), 4),
        "p90": round(_percentile(values, 0.90), 4),
        "p95": round(_percentile(values, 0.95), 4),
        "p99": round(_percentile(values, 0.99), 4),
    }


def _percentile_at(values: list[float], p: float) -> dict[str, float]:
    """Single-level percentile for one metric."""
    return round(_percentile(values, p), 4)


# ── Metric helpers ─────────────────────────────────────────────────────────────

def _is_pct_key(key: str) -> bool:
    """Mirror C#: pct/miss/stall keys get round-2 treatment; count/byte keys become ints."""
    k = key.lower()
    return k.endswith("_pct") or k.endswith("_miss") or k.endswith("_stall") or \
           "miss" in k or "stall" in k or k.startswith("pct_")


def _fmt(key: str, value: float) -> float | int:
    if _is_pct_key(key):
        return round(value, 2)
    # byte/clock/count fields: integer (mirrors C# (long)value)
    return int(value) if value == int(value) else value


def _build_percentile_level(dcs_with_metrics: list[dict], p: float) -> dict:
    """Build one metrics_pXX block for a list of DCs."""
    if not dcs_with_metrics:
        return {}
    # Collect all metric keys present
    all_keys: set[str] = set()
    for dc in dcs_with_metrics:
        all_keys.update((dc.get("metrics") or {}).keys())

    result = {}
    for key in sorted(all_keys):
        vals = [dc["metrics"][key] for dc in dcs_with_metrics if key in (dc.get("metrics") or {})]
        if not vals:
            continue
        result[key] = _fmt(key, _percentile(vals, p))
    return result


def _build_global_percentiles(dcs_with_metrics: list[dict]) -> dict:
    if not dcs_with_metrics:
        return {}
    all_keys: set[str] = set()
    for dc in dcs_with_metrics:
        all_keys.update((dc.get("metrics") or {}).keys())

    result = {}
    for key in sorted(all_keys):
        vals = [dc["metrics"][key] for dc in dcs_with_metrics if key in (dc.get("metrics") or {})]
        if not vals:
            continue
        result[key] = _percentile_block(vals)
    return result


# ── DB persistence helper ──────────────────────────────────────────────────────

def _persist_stats_to_db(db, snapshot_id: int, category_stats: list[dict]) -> None:
    """Upsert per-category stats into snapshot_stats table."""
    conn = db.conn()
    computed_at = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            snapshot_id,
            entry["category"],
            entry["dc_count"],
            entry["clocks_sum"],
            entry["clocks_pct"],
            entry.get("_avg_conf"),
            computed_at,
        )
        for entry in category_stats
    ]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO snapshot_stats "
            "(snapshot_id, category, dc_count, clocks_sum, clocks_pct, avg_conf, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_status_json(snapshot_dir: str | Path, db=None) -> Path:
    """
    Read label.json + metrics.json from snapshot_dir.
    Write snapshot_{id}_status.json.  Returns written path.

    If db is provided (a WorkspaceDB-like object with a .conn() method),
    per-category stats are also upserted into the ``snapshot_stats`` table.
    """
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)
    _log.info(f"[Status] start", context={"dir": str(snap.name)})
    _t0 = _time.time()

    label_path   = snap / "label.json"
    metrics_path = snap / "metrics.json"

    if not label_path.exists():
        raise FileNotFoundError(f"label.json not found in {snap}")
    if not metrics_path.exists():
        raise FileNotFoundError(f"metrics.json not found in {snap}")

    label_data   = json.loads(label_path.read_text(encoding="utf-8-sig"))
    metrics_data = json.loads(metrics_path.read_text(encoding="utf-8-sig"))

    snapshot_id = label_data.get("snapshot_id", 0)
    sdp_name    = label_data.get("sdp_name", "")

    # ── Index label and metrics by dc_id ─────────────────────────────────────
    label_by_id: dict[Any, dict] = {
        str(dc["dc_id"]): dc.get("label", {})
        for dc in label_data.get("draw_calls", [])
    }
    metrics_by_id: dict[Any, dict] = {}
    for dc in metrics_data.get("draw_calls", []):
        m = dc.get("metrics")
        if m:
            metrics_by_id[str(dc["dc_id"])] = m

    # ── Merge into unified list ───────────────────────────────────────────────
    all_dc_ids = list(label_by_id.keys())
    merged: list[dict] = []
    for dc_id in all_dc_ids:
        merged.append({
            "dc_id":   dc_id,
            "label":   label_by_id.get(dc_id, {}),
            "metrics": metrics_by_id.get(dc_id),  # None if not available
        })

    dcs_with_m = [d for d in merged if d["metrics"]]

    # ── Overall stats ─────────────────────────────────────────────────────────
    def _sum(key: str) -> int:
        return int(sum(d["metrics"].get(key, 0) for d in dcs_with_m))

    # Need dc_json for api_name (metrics.json doesn't have it; use label.json dc ids)
    # api_name comes from dc.json — try to read it
    dc_path = snap / "dc.json"
    api_names: dict[str, str] = {}
    if dc_path.exists():
        dc_data = json.loads(dc_path.read_text(encoding="utf-8-sig"))
        for dc in dc_data.get("draw_calls", []):
            api_names[str(dc.get("dc_id", ""))] = dc.get("api_name", "")

    draw_count   = sum(1 for dc_id in all_dc_ids if api_names.get(dc_id, "").startswith("vkCmdDraw"))
    compute_count = sum(1 for dc_id in all_dc_ids if api_names.get(dc_id, "").startswith("vkCmdDispatch"))
    total_count  = len(all_dc_ids)
    coverage     = round(len(dcs_with_m) / total_count, 4) if total_count else 0.0

    overall = {
        "total_dc_count":         total_count,
        "draw_dc_count":          draw_count,
        "compute_dc_count":       compute_count,
        "total_clocks":           _sum("clocks"),
        "total_read_bytes":       _sum("read_total_bytes"),
        "total_write_bytes":      _sum("write_total_bytes"),
        "total_fragments_shaded": _sum("fragments_shaded"),
        "total_vertices_shaded":  _sum("vertices_shaded"),
        "metrics_coverage_ratio": coverage,
    }

    total_clocks = overall["total_clocks"]

    # ── Per-category stats ────────────────────────────────────────────────────
    by_category: dict[str, list[dict]] = defaultdict(list)
    for d in merged:
        cat = d["label"].get("category", "Other")
        by_category[cat].append(d)

    LEVELS = [0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.99]
    LEVEL_KEYS = ["metrics_p50", "metrics_p60", "metrics_p70", "metrics_p80",
                  "metrics_p90", "metrics_p95", "metrics_p99"]

    category_stats = []
    for cat in sorted(by_category):
        cat_dcs   = by_category[cat]
        cat_with_m = [d for d in cat_dcs if d["metrics"]]
        cat_clocks = int(sum(d["metrics"].get("clocks", 0) for d in cat_with_m))
        clocks_pct = round(100.0 * cat_clocks / total_clocks, 2) if total_clocks else 0.0
        pct        = round(100.0 * len(cat_dcs) / total_count, 2) if total_count else 0.0
        confs      = [d["label"].get("confidence", 0.0) for d in cat_dcs]
        avg_cat_conf = round(sum(confs) / len(confs), 4) if confs else 0.0

        entry: dict = {
            "category":   cat,
            "dc_count":   len(cat_dcs),
            "percentage": pct,
            "clocks_sum": cat_clocks,
            "clocks_pct": clocks_pct,
            "_avg_conf":  avg_cat_conf,  # internal — used for DB write, stripped before JSON
        }
        if cat_with_m:
            for level, lkey in zip(LEVELS, LEVEL_KEYS):
                entry[lkey] = _build_percentile_level(cat_with_m, level)
        category_stats.append(entry)

    # ── Persist to DB if available ────────────────────────────────────────────
    if db is not None:
        _persist_stats_to_db(db, snapshot_id, category_stats)

    # Strip internal _avg_conf key before serialisation
    for entry in category_stats:
        entry.pop("_avg_conf", None)

    # ── Global percentiles ────────────────────────────────────────────────────
    global_percentiles = _build_global_percentiles(dcs_with_m)

    # ── Label quality stats ───────────────────────────────────────────────────
    LOW_THRESHOLD = 0.60
    confidences = [d["label"].get("confidence", 0.0) for d in merged]
    avg_conf    = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    low_ratio   = round(sum(1 for c in confidences if c < LOW_THRESHOLD) / len(confidences), 4) if confidences else 0.0
    llm_count   = sum(1 for d in merged if d["label"].get("label_source") == "llm")
    rule_count  = len(merged) - llm_count

    tag_dist: dict[str, int] = defaultdict(int)
    for d in merged:
        for tag in d["label"].get("reason_tags", []):
            tag_dist[tag] += 1

    low_conf_ids = sorted(
        (d["dc_id"] for d in merged if d["label"].get("confidence", 1.0) < LOW_THRESHOLD),
        key=lambda dc_id: label_by_id.get(dc_id, {}).get("confidence", 1.0)
    )[:20]

    label_stats = {
        "avg_confidence":           avg_conf,
        "low_confidence_ratio":     low_ratio,
        "low_confidence_threshold": LOW_THRESHOLD,
        "llm_labeled_count":        llm_count,
        "rule_labeled_count":       rule_count,
        "reason_tag_distribution":  dict(sorted(tag_dist.items(), key=lambda kv: -kv[1])),
        "low_confidence_dc_ids":    low_conf_ids,
    }

    # ── Write ─────────────────────────────────────────────────────────────────
    out = {
        "schema_version":    "2.0",
        "snapshot_id":       snapshot_id,
        "sdp_name":          sdp_name,
        "generated_at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall":           overall,
        "category_stats":    category_stats,
        "label_stats":       label_stats,
        "global_percentiles": global_percentiles,
    }

    fname    = f"snapshot_{snapshot_id}_status.json" if snapshot_id else "status.json"
    out_path = snap / fname
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _log.info(f"[Status] done: {_time.time()-_t0:.1f}s, {len(category_stats)} categories")
    return out_path
