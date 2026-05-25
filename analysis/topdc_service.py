"""
topdc_service.py — Generates snapshot_{id}_topdc.json.

3-layer attribution engine port of AttributionRuleEngine.cs + TopDcJsonService.cs.
Reads label.json + metrics.json + status.json; loads attribution_rules.json.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_RULES_PATH = Path(__file__).parent / "attribution_rules.json"


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _percentile_rank(value: float, sorted_vals: list[float]) -> int:
    """Percentile rank (0-100) of value within pre-sorted list."""
    if not sorted_vals:
        return 0
    idx = sum(1 for v in sorted_vals if v <= value)
    return int(round(idx * 100.0 / len(sorted_vals)))


class _Engine:
    def __init__(self, rules: dict):
        self.layer1: dict[str, str] = {
            r["metric"]: r["bottleneck_hint"]
            for r in rules.get("layer1_metric_hints", [])
        }
        # tiers sorted p99 → p50 (highest first for early exit)
        self.tiers = sorted(
            rules.get("layer2_percentile_tiers", {}).get("tiers", []),
            key=lambda t: -t["threshold"],
        )
        self.min_samples: int = rules.get("layer2_percentile_tiers", {}).get(
            "min_sample_size_for_category", 5
        )
        self.layer3: dict[str, dict] = {
            b["bottleneck"]: b for b in rules.get("layer3_bottleneck_weights", [])
        }
        self.top_n: int = rules.get("top_n_per_category", 5)
        self.primary_min_score: float = rules.get("primary_bottleneck_min_score", 0.25)

    def _tier_weight(self, value: float, metric: str, cat_pcts: dict) -> float:
        for tier in self.tiers:
            block = cat_pcts.get(f"metrics_{tier['name']}", {})
            threshold = block.get(metric)
            if threshold is not None and value > threshold:
                return tier["weight"]
        return 0.0

    def attribute(self, dc: dict, cat_pcts: dict, has_enough: bool) -> dict:
        m = dc.get("metrics") or {}

        # Layer 1 — identify metrics that exceed p70
        suspicious = []
        p70_block = cat_pcts.get("metrics_p70", {})
        for metric, hint in self.layer1.items():
            val = m.get(metric)
            if val is None:
                continue
            p70 = p70_block.get(metric)
            if p70 is None or not has_enough:
                continue
            if val > p70:
                suspicious.append({
                    "metric": metric,
                    "value": val,
                    "initial_bottleneck_hint": hint,
                })

        # Layer 2 — tier weight per suspicious metric
        metric_weights: dict[str, float] = {}
        percentile_scores = []
        for s in suspicious:
            w = self._tier_weight(s["value"], s["metric"], cat_pcts)
            metric_weights[s["metric"]] = w
            percentile_scores.append({"metric": s["metric"], "tier_weight": w})

        # Layer 3 — bottleneck scores
        bottleneck_scores: dict[str, float] = {}
        for bn, bn_def in self.layer3.items():
            score = sum(
                metric_weights.get(cm["metric"], 0.0) * cm["contribution_weight"]
                for cm in bn_def.get("contributing_metrics", [])
            )
            if score > 0:
                bottleneck_scores[bn] = round(score, 4)

        ranked = sorted(bottleneck_scores.items(), key=lambda kv: -kv[1])
        primary   = ranked[0][0] if ranked and ranked[0][1] >= self.primary_min_score else ""
        secondary = ranked[1][0] if len(ranked) > 1 and ranked[1][1] >= self.primary_min_score else ""
        confidence = ranked[0][1] if ranked else 0.0

        return {
            "suspicious_metrics":   suspicious,
            "percentile_scores":    percentile_scores,
            "bottleneck_scores":    bottleneck_scores,
            "primary_bottleneck":   primary,
            "secondary_bottleneck": secondary,
            "confidence_score":     round(confidence, 4),
        }


def generate_topdc_json(
    snapshot_dir: str | Path,
    rules_path: str | Path | None = None,
) -> Path:
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)
    _log.info(f"[TopDC] start", context={"dir": str(snap.name)})
    _t0 = _time.time()
    rp   = Path(rules_path) if rules_path else _RULES_PATH

    rules = _load(rp)
    if not rules:
        raise FileNotFoundError(f"attribution_rules.json not found: {rp}")

    label_data   = _load(snap / "label.json")
    metrics_data = _load(snap / "metrics.json")
    if not label_data:
        raise FileNotFoundError(f"label.json not found in {snap}")
    if not metrics_data:
        raise FileNotFoundError(f"metrics.json not found in {snap}")

    snapshot_id = label_data.get("snapshot_id", 0)
    sdp_name    = label_data.get("sdp_name", snap.parent.name)

    status = _load(snap / f"snapshot_{snapshot_id}_status.json")

    # Build per-category percentile lookup from status.json
    cat_pcts:   dict[str, dict] = {}
    cat_counts: dict[str, int]  = {}
    if status:
        for cs in status.get("category_stats", []):
            cat = cs["category"]
            cat_pcts[cat]   = {k: v for k, v in cs.items() if k.startswith("metrics_p")}
            cat_counts[cat] = cs.get("dc_count", 0)

    engine = _Engine(rules)

    # Merge label + metrics
    metrics_by_id = {
        str(d["dc_id"]): d["metrics"]
        for d in metrics_data.get("draw_calls", [])
        if d.get("metrics")
    }
    merged: list[dict] = [
        {
            "dc_id":   str(d["dc_id"]),
            "label":   d.get("label", {}),
            "metrics": metrics_by_id.get(str(d["dc_id"])),
        }
        for d in label_data.get("draw_calls", [])
    ]

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for d in merged:
        by_cat[d["label"].get("category", "Other")].append(d)

    categories_out = []
    for cat in sorted(by_cat):
        cat_dcs  = by_cat[cat]
        with_m   = [d for d in cat_dcs if d["metrics"]]
        top_dcs  = sorted(with_m, key=lambda d: d["metrics"].get("clocks", 0), reverse=True)[: engine.top_n]
        pcts     = cat_pcts.get(cat, {})
        has_enough = cat_counts.get(cat, 0) >= engine.min_samples

        all_clocks_sorted = sorted(d["metrics"].get("clocks", 0) for d in with_m)

        top_out = []
        for rank, dc in enumerate(top_dcs, 1):
            m      = dc["metrics"] or {}
            clocks = m.get("clocks", 0)
            top_out.append({
                "dc_id":  dc["dc_id"],
                "rank":   rank,
                "clocks": int(clocks),
                "clocks_rank_in_category": rank,
                "metrics":     m,
                "attribution": engine.attribute(dc, pcts, has_enough),
                "category_comparison": {
                    "clocks_percentile_in_category": _percentile_rank(clocks, all_clocks_sorted),
                },
                "shader_files": [],
                "mesh_file":    "",
                "label":        dc["label"],
            })

        categories_out.append({
            "category": cat,
            "dc_count": len(cat_dcs),
            "top_dcs":  top_out,
        })

    out = {
        "schema_version":      "2.0",
        "snapshot_id":         snapshot_id,
        "sdp_name":            sdp_name,
        "generated_at":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "top_n_per_category":  engine.top_n,
        "categories":          categories_out,
    }

    fname    = f"snapshot_{snapshot_id}_topdc.json" if snapshot_id else f"topdc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path = snap / fname
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _log.info(f"[TopDC] done: {_time.time()-_t0:.1f}s, {len(categories_out)} categories")
    return out_path
