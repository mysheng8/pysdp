"""
dashboard_service.py — Generates snapshot_{id}_dashboard.md.

Python port of DashboardGenerationService.cs.
Reads label.json + metrics.json + status.json from snapshot_dir.
All content is deterministic (no LLM).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _mb(b: float) -> str:
    return f"{b / 1048576:.2f}"


def _fmt_n(v: float) -> str:
    return f"{int(v):,}"


# ── Merge label + metrics into unified dc list ────────────────────────────────

def _merge(label_data: dict, metrics_data: dict) -> list[dict]:
    metrics_by_id = {
        str(d["dc_id"]): d.get("metrics")
        for d in metrics_data.get("draw_calls", [])
        if d.get("metrics")
    }
    merged = []
    for d in label_data.get("draw_calls", []):
        dc_id = str(d["dc_id"])
        merged.append({
            "dc_id":   dc_id,
            "label":   d.get("label", {}),
            "metrics": metrics_by_id.get(dc_id),
        })
    return merged


# ── Per-category aggregate stats ──────────────────────────────────────────────

def _cat_stats(merged: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in merged:
        groups[d["label"].get("category", "Other")].append(d)

    stats = {}
    for cat, dcs in groups.items():
        with_m = [d for d in dcs if d["metrics"]]
        if not with_m:
            stats[cat] = {"count": len(dcs), "total_clocks": 0, "avg_clocks": 0,
                          "avg_read": 0, "avg_write": 0, "avg_frags": 0,
                          "avg_busy": 0, "avg_l1": 0, "avg_stall": 0}
            continue
        n = len(with_m)
        stats[cat] = {
            "count":        len(dcs),
            "total_clocks": int(sum(d["metrics"]["clocks"] for d in with_m)),
            "avg_clocks":   sum(d["metrics"].get("clocks", 0) for d in with_m) / n,
            "avg_read":     sum(d["metrics"].get("read_total_bytes", 0) for d in with_m) / n,
            "avg_write":    sum(d["metrics"].get("write_total_bytes", 0) for d in with_m) / n,
            "avg_frags":    sum(d["metrics"].get("fragments_shaded", 0) for d in with_m) / n,
            "avg_busy":     sum(d["metrics"].get("shaders_busy_pct", 0) for d in with_m) / n,
            "avg_l1":       sum(d["metrics"].get("tex_l1_miss_pct", 0) for d in with_m) / n,
            "avg_stall":    sum(d["metrics"].get("tex_fetch_stall_pct", 0) for d in with_m) / n,
        }
    return stats


# ── Dynamic Top-5 table (outlier columns) ─────────────────────────────────────

def _dynamic_top5_table(top5: list[dict], avg: dict, total_clocks: int) -> str:
    lines = []

    def _ratio(m: dict, key: str, avg_val: float) -> float:
        return m.get(key, 0) / max(avg_val, 1e-6)

    # Candidate columns: (header_tmpl, metric_key, threshold, avg_val, pct_mode)
    candidates = [
        (f"Fragments (avg {_fmt_n(avg['frags'])})",    "fragments_shaded",     1.5, avg["frags"],  False),
        (f"%ShaderBusy (avg {avg['busy']:.1f}%)",      "shaders_busy_pct",     1.3, avg["busy"],   True),
        (f"FragInstr (avg {_fmt_n(avg['finstr'])})",   "fragment_instructions",1.5, avg["finstr"], False),
        (f"%TexStall (avg {avg['stall']:.1f}%)",       "tex_fetch_stall_pct",  1.5, avg["stall"],  True),
        (f"%TexL1Miss (avg {avg['l1']:.1f}%)",         "tex_l1_miss_pct",      1.5, avg["l1"],     True),
        (f"ReadMB (avg {_mb(avg['read'])})",           "read_total_bytes",     2.0, avg["read"],   False),
        (f"WriteMB (avg {_mb(avg['write'])})",         "write_total_bytes",    2.0, avg["write"],  False),
        (f"Vertices (avg {_fmt_n(avg['verts'])})",     "vertices_shaded",      1.5, avg["verts"],  False),
    ]

    # Keep columns where at least one top-5 DC exceeds threshold, sorted by max_ratio desc (mirrors C#)
    active = []
    for hdr, key, thresh, avg_val, pct_mode in candidates:
        max_ratio = max((_ratio(d["metrics"] or {}, key, avg_val) for d in top5 if d["metrics"]), default=0)
        if max_ratio >= thresh:
            active.append((hdr, key, thresh, avg_val, pct_mode, max_ratio))
    active.sort(key=lambda x: x[5], reverse=True)
    active = [(h, k, t, a, p) for h, k, t, a, p, _ in active]

    # Header
    hdrs = ["Rank", "DC", "Category", "Detail", f"Clocks (avg {_fmt_n(avg['clocks'])})"]
    seps = ["----:", "----", "--------", "------", "-----------:"]
    for hdr, *_ in active:
        hdrs.append(hdr); seps.append("-----------:")
    lines.append("| " + " | ".join(hdrs) + " |")
    lines.append("| " + " | ".join(seps) + " |")

    for i, d in enumerate(top5):
        m = d["metrics"] or {}
        clocks = m.get("clocks", 0)
        clock_ratio = clocks / max(avg["clocks"], 1)
        row = [
            str(i + 1),
            d["dc_id"],
            d["label"].get("category", ""),
            d["label"].get("detail", ""),
            f"{_fmt_n(clocks)} ({clock_ratio:.1f}×)",
        ]
        for _, key, thresh, avg_val, pct_mode in active:
            val = m.get(key, 0)
            ratio = val / max(avg_val, 1e-6)
            if pct_mode:
                cell = f"{val:.1f}% (+{max(0, val - avg_val):.1f}pp)"
            else:
                cell = f"{_mb(val) if 'bytes' in key else _fmt_n(val)} ({ratio:.1f}×)"
            if ratio >= thresh:
                cell = f"**{cell}**"
            row.append(cell)
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


# ── Label quality section (from status.json) ──────────────────────────────────

def _label_stats_section(status: dict | None, snapshot_id: int) -> str:
    if not status:
        return ""
    ls = status.get("label_stats")
    if not ls:
        return ""
    lines = ["---\n", "## Label 质量统计\n"]
    conf = ls.get("avg_confidence", 0)
    low  = ls.get("low_confidence_ratio", 0) * 100
    thr  = ls.get("low_confidence_threshold", 0.6)
    lines.append(f"- 均值置信度: **{conf:.2f}**")
    lines.append(f"- 低置信度（< {thr}）占比: **{low:.1f}%**")
    lines.append(f"- LLM 标注: **{ls.get('llm_labeled_count', 0)}**，规则标注: **{ls.get('rule_labeled_count', 0)}**")

    tags = ls.get("reason_tag_distribution", {})
    if tags:
        lines += ["", "**Reason Tag 分布（Top 10）：**\n", "| Tag | Count |", "|-----|------:|"]
        for tag, cnt in list(tags.items())[:10]:
            lines.append(f"| {tag} | {cnt} |")

    low_ids = ls.get("low_confidence_dc_ids", [])
    if low_ids:
        shown = ", ".join(str(x) for x in low_ids[:10])
        suffix = f"... 及另外 {len(low_ids) - 10} 个" if len(low_ids) > 10 else ""
        lines.append("")
        lines.append(f"**低置信度 DC（{len(low_ids)} 个）：** {shown}")
        if suffix:
            lines.append(suffix)
    lines.append("")
    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_dashboard_md(snapshot_dir: str | Path) -> Path:
    snap = Path(snapshot_dir)
    label_path   = snap / "label.json"
    metrics_path = snap / "metrics.json"

    if not label_path.exists():
        raise FileNotFoundError(f"label.json not found in {snap}")
    if not metrics_path.exists():
        raise FileNotFoundError(f"metrics.json not found in {snap}")

    label_data   = _load(label_path)
    metrics_data = _load(metrics_path)
    snapshot_id  = label_data.get("snapshot_id", 0)
    sdp_name     = label_data.get("sdp_name", snap.parent.name)

    # Try to load status.json for label stats section
    status_path = snap / f"snapshot_{snapshot_id}_status.json"
    status_data = _load(status_path) if status_path.exists() else None

    merged   = _merge(label_data, metrics_data)
    with_m   = [d for d in merged if d["metrics"]]
    has_metrics = bool(with_m)

    lines: list[str] = []
    lines.append(f"# {sdp_name} — 帧分析 Dashboard")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"Total DrawCalls: {len(merged)}  ")
    lines.append("")

    # Screenshot — check analysis dir first, then fall back to sibling sdp dir
    _SS_NAMES = ["snapshot.png", "snapshot_screenshot.png", "snapshot_screenshot.jpg",
                 "1_screenshot.bmp"]
    _ss_path = None
    for _name in _SS_NAMES:
        if (snap / _name).exists():
            _ss_path = _name; break
    if _ss_path is None:
        # derive sdp sibling: .../sdp/analysis/<run>/snapshot_N → .../sdp/<run>/snapshot_N
        _parts = snap.parts
        try:
            _ai = next(i for i in range(len(_parts)-1, -1, -1) if _parts[i].lower() == "analysis")
            _sdp_snap = Path(*(_parts[:_ai] + _parts[_ai+1:]))
            for _name in _SS_NAMES:
                if (_sdp_snap / _name).exists():
                    _ss_path = str(_sdp_snap / _name); break
        except StopIteration:
            pass
    if _ss_path:
        lines += ["## Screenshot\n", f"![Frame Snapshot]({_ss_path})\n"]

    # Category distribution table
    cs = _cat_stats(merged)
    cat_order = sorted(cs, key=lambda c: cs[c]["count"], reverse=True)
    lines += ["## DrawCall 分布\n", "| 分类 | DC 数 | 占比 |", "|------|------:|----:|"]
    for cat in cat_order:
        pct = cs[cat]["count"] * 100.0 / max(len(merged), 1)
        lines.append(f"| {cat} | {cs[cat]['count']} | {pct:.1f}% |")
    lines.append("")

    if not has_metrics:
        lines.append("> 未加载 Metrics，以下图表不可用。")
        return _write(lines, snap, snapshot_id)

    total_clocks = sum(d["metrics"].get("clocks", 0) for d in with_m)
    n = len(with_m)

    avg = {
        "clocks": sum(d["metrics"].get("clocks", 0) for d in with_m) / n,
        "read":   sum(d["metrics"].get("read_total_bytes", 0) for d in with_m) / n,
        "write":  sum(d["metrics"].get("write_total_bytes", 0) for d in with_m) / n,
        "frags":  sum(d["metrics"].get("fragments_shaded", 0) for d in with_m) / n,
        "busy":   sum(d["metrics"].get("shaders_busy_pct", 0) for d in with_m) / n,
        "l1":     sum(d["metrics"].get("tex_l1_miss_pct", 0) for d in with_m) / n,
        "stall":  sum(d["metrics"].get("tex_fetch_stall_pct", 0) for d in with_m) / n,
        "finstr": sum(d["metrics"].get("fragment_instructions", 0) for d in with_m) / n,
        "verts":  sum(d["metrics"].get("vertices_shaded", 0) for d in with_m) / n,
    }

    top5 = sorted(with_m, key=lambda d: d["metrics"].get("clocks", 0), reverse=True)[:5]

    # ── Mermaid bar chart ─────────────────────────────────────────────────────
    all_sorted = sorted(with_m, key=lambda d: int(d["dc_id"]) if str(d["dc_id"]).isdigit() else 0)
    chart_max  = max(int(d["metrics"].get("clocks", 0)) for d in all_sorted)
    x_labels   = ", ".join(f'"{d["dc_id"]}"' for d in all_sorted)
    y_vals     = ", ".join(str(int(d["metrics"].get("clocks", 0))) for d in all_sorted)
    lines += [
        "---\n",
        f"## GPU Clock 分布（{n} DC）\n",
        "```mermaid",
        f'%%{{init: {{"xyChart": {{"width": 1600, "height": 420, "xAxis": {{"labelFontSize": 1, "labelPadding": 0}}}}}}}}%%',
        "xychart-beta",
        f'    title "GPU Clocks per DrawCall ({n} DCs)"',
        f"    x-axis [{x_labels}]",
        f"    y-axis \"Clocks\" 0 --> {chart_max}",
        f"    bar [{y_vals}]",
        "```",
        "",
    ]

    # ── Mermaid pie charts ────────────────────────────────────────────────────
    lines += ["## GPU Clock Budget by Category\n"]
    if cs:
        lines += ["```mermaid", 'pie title "GPU Clock Budget by Category"']
        for cat in sorted(cs, key=lambda c: cs[c]["total_clocks"], reverse=True):
            lines.append(f'    "{cat}" : {cs[cat]["total_clocks"]}')
        lines += ["```", "", "```mermaid", 'pie title "DrawCall Count by Category"']
        for cat in sorted(cs, key=lambda c: cs[c]["count"], reverse=True):
            lines.append(f'    "{cat}" : {cs[cat]["count"]}')
        lines += ["```", ""]
        lines += ["| 分类 | DC Count | Total Clocks | % of Frame |",
                  "|------|----------:|-------------:|-----------:|"]
        for cat in sorted(cs, key=lambda c: cs[c]["total_clocks"], reverse=True):
            pct = cs[cat]["total_clocks"] * 100.0 / max(total_clocks, 1)
            lines.append(f"| {cat} | {cs[cat]['count']} | {_fmt_n(cs[cat]['total_clocks'])} | {pct:.1f}% |")
        lines.append("")

    # ── Top-5 global table ────────────────────────────────────────────────────
    lines += [
        "---\n",
        "## Top 5 DrawCalls by GPU Clock Cost\n",
        f"> 全局均值（{n} DC）：Clocks={_fmt_n(avg['clocks'])} | Fragments={_fmt_n(avg['frags'])} | "
        f"ShaderBusy={avg['busy']:.1f}% | FragInstr={_fmt_n(avg['finstr'])} | "
        f"TexStall={avg['stall']:.1f}% | TexL1Miss={avg['l1']:.1f}% | "
        f"Read={_mb(avg['read'])}MB | Write={_mb(avg['write'])}MB\n",
        _dynamic_top5_table(top5, avg, int(total_clocks)),
        "",
    ]

    # ── Per-category Top-5 ────────────────────────────────────────────────────
    lines += ["---\n", "## 各分类 Top 5 DrawCalls\n"]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for d in with_m:
        by_cat[d["label"].get("category", "Other")].append(d)

    for cat in cat_order:
        cat_dcs = sorted(by_cat.get(cat, []), key=lambda d: d["metrics"].get("clocks", 0), reverse=True)[:5]
        if not cat_dcs:
            continue
        cat_avg = sum(d["metrics"].get("clocks", 0) for d in by_cat.get(cat, [])) / max(len(by_cat.get(cat, [])), 1)
        lines += [f"### {cat}\n",
                  "| Rank | DC | Detail | Clocks | %ShaderBusy | Fragments | ReadMB | WriteMB |",
                  "|-----:|-----|--------|-------:|------------:|----------:|-------:|--------:|"]
        for rank, d in enumerate(cat_dcs, 1):
            m = d["metrics"]
            clocks = m.get("clocks", 0)
            lines.append(
                f"| {rank} | {d['dc_id']} | {d['label'].get('detail', '')} | "
                f"{_fmt_n(clocks)} ({clocks / max(cat_avg, 1):.1f}×) | "
                f"{m.get('shaders_busy_pct', 0):.1f}% | "
                f"{_fmt_n(m.get('fragments_shaded', 0))} | "
                f"{_mb(m.get('read_total_bytes', 0))} | "
                f"{_mb(m.get('write_total_bytes', 0))} |"
            )
        lines.append("")

    # ── 3D Mesh Preview ───────────────────────────────────────────────────────
    mesh_dir = snap.parent / "meshes"
    if not mesh_dir.exists():
        mesh_dir = snap / "meshes"
    objs = sorted(mesh_dir.glob("*.obj")) if mesh_dir.exists() else []
    if objs:
        lines += ["---\n", "## 3D Mesh Preview\n",
                  "> 交互式查看器（需要浏览器打开）\n",
                  "**[🔗 Open interactive 3D Viewer](meshes/viewer.html)**\n",
                  "| Rank | DrawCall | OBJ |", "|-----:|----------|-----|"]
        for i, f in enumerate(objs, 1):
            dc_id = f.stem.replace("drawcall_", "")
            lines.append(f"| {i} | DC {dc_id} | [{f.name}](meshes/{f.name}) |")
        lines.append("")

    # ── Category statistics table ─────────────────────────────────────────────
    lines += [
        "---\n",
        "## Category Statistics\n",
        "| Category | Count | TotalClocks | AvgClocks | AvgReadMB | AvgWriteMB | AvgFragments | AvgShaderBusy% |",
        "|----------|------:|------------:|----------:|----------:|-----------:|-------------:|---------------:|",
    ]
    for cat in cat_order:
        s = cs[cat]
        if s["avg_clocks"] == 0:
            lines.append(f"| {cat} | {s['count']} | — | — | — | — | — | — |")
        else:
            lines.append(
                f"| {cat} | {s['count']} | {_fmt_n(s['total_clocks'])} | {_fmt_n(s['avg_clocks'])} | "
                f"{_mb(s['avg_read'])} | {_mb(s['avg_write'])} | "
                f"{_fmt_n(s['avg_frags'])} | {s['avg_busy']:.1f}% |"
            )
    lines.append("")
    lines.append(
        f"> **全局平均:** Clocks={_fmt_n(avg['clocks'])}  Read={_mb(avg['read'])}MB  "
        f"Write={_mb(avg['write'])}MB  Fragments={_fmt_n(avg['frags'])}  ShaderBusy={avg['busy']:.1f}%"
    )
    lines.append("")

    # ── Label quality stats ───────────────────────────────────────────────────
    lines.append(_label_stats_section(status_data, snapshot_id))

    # ── Frame summary ─────────────────────────────────────────────────────────
    top5_clocks = sum(d["metrics"].get("clocks", 0) for d in top5)
    lines += [
        "---\n",
        "## 帧汇总\n",
        f"- 总 Clock 消耗: **{_fmt_n(total_clocks)}**",
        f"- Top 5 占比: **{top5_clocks * 100.0 / max(total_clocks, 1):.1f}%**\n",
        "**各类别 Clock 占比：**\n",
    ]
    for cat in sorted(cs, key=lambda c: cs[c]["total_clocks"], reverse=True):
        pct = cs[cat]["total_clocks"] * 100.0 / max(total_clocks, 1)
        lines.append(f"- {cat}: {_fmt_n(cs[cat]['total_clocks'])} clocks  ({pct:.1f}%)，共 {cs[cat]['count']} 个 DC")
    lines.append("")

    return _write(lines, snap, snapshot_id)


def _write(lines: list[str], snap: Path, snapshot_id: int) -> Path:
    fname = f"snapshot_{snapshot_id}_dashboard.md" if snapshot_id else f"dashboard_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out = snap / fname
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
