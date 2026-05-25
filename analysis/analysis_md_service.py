"""
analysis_md_service.py — Generates snapshot_{id}_analysis.md.

Python port of AttributionReportService.cs — rule-based path only.
LLM path is preserved as a hook (call generate_analysis_md with llm= kwarg).

Reads: label.json + metrics.json + status.json (+ optional topdc.json)
Writes: snapshot_{id}_analysis.md
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _mb(b: float) -> str:
    return f"{b / 1048576:.2f}"


def _fmt_n(v: float) -> str:
    return f"{int(v):,}"


# ── Section 1: Overall summary ────────────────────────────────────────────────

def _overall_section(status: dict | None, merged: list[dict]) -> list[str]:
    lines = ["## 1. 整体概览\n"]
    if status and (ov := status.get("overall")):
        lines.append(f"- 总 DC 数: **{ov['total_dc_count']}**（Draw: {ov['draw_dc_count']}，Compute: {ov['compute_dc_count']}）")
        lines.append(f"- 总 Clocks: **{_fmt_n(ov['total_clocks'])}**")
        lines.append(f"- 总内存读取: **{_mb(ov['total_read_bytes'])} MB**，总写入: **{_mb(ov['total_write_bytes'])} MB**")
        cov = ov.get("metrics_coverage_ratio", 0) * 100
        lines.append(f"- Metrics 覆盖率: **{cov:.1f}%**")
    else:
        with_m = sum(1 for d in merged if d["metrics"])
        lines.append(f"- 总 DC 数: **{len(merged)}**，有 Metrics: **{with_m}**")

    if status and (ls := status.get("label_stats")):
        avg_conf  = ls.get("avg_confidence", 0)
        low_ratio = ls.get("low_confidence_ratio", 0) * 100
        lines.append(f"- 标签均值置信度: **{avg_conf:.2f}**，低置信度占比: **{low_ratio:.1f}%**")

    lines.append("")
    return lines


# ── Rule-based per-category section ──────────────────────────────────────────

def _rule_category_section(cat: str, cat_dcs: list[dict],
                            status: dict | None, top_n: int = 5) -> list[str]:
    lines = []
    with_m = [d for d in cat_dcs if d["metrics"]]
    top = sorted(with_m, key=lambda d: d["metrics"].get("clocks", 0), reverse=True)[:top_n]

    if not top:
        lines.append("> 该分类无 Metrics 数据。\n")
        return lines

    total_clocks = sum(d["metrics"].get("clocks", 0) for d in with_m)
    avg_clocks   = total_clocks / max(len(with_m), 1)

    lines.append(f"{cat} 类包含 {len(cat_dcs)} 个 DrawCall，以下是高耗时 DrawCall 分析：\n")

    for rank, d in enumerate(top, 1):
        m      = d["metrics"]
        clocks = m.get("clocks", 0)
        detail = d["label"].get("detail", "")
        lines.append(f"##### DC #{rank} — {d['dc_id']}（{detail}）")
        lines.append(f"- **Clocks**: {_fmt_n(clocks)}（{clocks / max(avg_clocks, 1):.1f}× 分类均值）")
        lines.append(f"- **ShaderBusy**: {m.get('shaders_busy_pct', 0):.1f}%  "
                     f"**Fragments**: {_fmt_n(m.get('fragments_shaded', 0))}  "
                     f"**Read**: {_mb(m.get('read_total_bytes', 0))} MB")
        # Flag obvious bottlenecks
        bottlenecks = []
        if m.get("shaders_busy_pct", 0) > 150:
            bottlenecks.append("Shader ALU bound (busy > 150%)")
        if m.get("tex_l1_miss_pct", 0) > 80:
            bottlenecks.append(f"高纹理 L1 缺失率 ({m['tex_l1_miss_pct']:.0f}%)")
        if m.get("tex_fetch_stall_pct", 0) > 30:
            bottlenecks.append(f"纹理 Fetch Stall ({m['tex_fetch_stall_pct']:.0f}%)")
        if m.get("read_total_bytes", 0) > 4 * 1048576:
            bottlenecks.append(f"带宽压力（Read {_mb(m['read_total_bytes'])} MB）")
        if bottlenecks:
            lines.append(f"- **潜在瓶颈**: {'; '.join(bottlenecks)}")
        lines.append("")

    return lines


# ── Rule-based combined recommendations ──────────────────────────────────────

def _rule_recommendations(cat_stats: dict[str, dict],
                           status: dict | None) -> list[str]:
    lines = []
    total_clocks = sum(s["total_clocks"] for s in cat_stats.values())
    # Sort categories by clock cost
    by_cost = sorted(cat_stats, key=lambda c: cat_stats[c]["total_clocks"], reverse=True)

    for i, cat in enumerate(by_cost[:4], 1):
        s   = cat_stats[cat]
        pct = s["total_clocks"] * 100.0 / max(total_clocks, 1)
        tip = {
            "PostProcess": "考虑合并后处理 Pass，降低中间 RT 分辨率，减少全屏纹理采样次数。",
            "Scene":       "合并静态物体 DrawCall，使用实例化减少 DC 数量，检查顶点数据格式。",
            "UI":          "将文本和 2D 元素合并到 Atlas 纹理，减少透明度排序开销。",
            "Shadow":      "降低阴影图分辨率或使用 Cascade Shadow，减少阴影投射物数量。",
            "Character":   "合并相同材质的角色 DrawCall，优化蒙皮 Shader 指令数。",
            "Terrain":     "使用 LOD 减少地形 Patch 数量，优化高度图采样。",
            "VFX":         "减少粒子 DrawCall 数量，使用实例化或合并粒子批次。",
        }.get(cat, "分析 Shader 指令，减少不必要的纹理采样和 ALU 计算。")
        lines.append(f"> *   **优化建议{i}：** {cat} 类占 {pct:.1f}% GPU 时间（{_fmt_n(s['total_clocks'])} clocks，{s['count']} DC）。{tip}")

    return lines


# ── Merge helpers ─────────────────────────────────────────────────────────────

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


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_analysis_md(
    snapshot_dir: str | Path,
    llm_fn: Callable[[str, str, list[dict], dict | None], str] | None = None,
) -> Path:
    """
    Generate snapshot_{id}_analysis.md.

    llm_fn: optional callable(cat, sdp_name, top_dcs, status) -> str
            If provided and returns non-empty, used for per-category content.
    """
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)
    _log.info(f"[AnalysisMd] start", context={"dir": str(snap.name)})
    _t0 = _time.time()

    label_data   = _load(snap / "label.json")
    metrics_data = _load(snap / "metrics.json")

    if not label_data:
        raise FileNotFoundError(f"label.json not found in {snap}")
    if not metrics_data:
        raise FileNotFoundError(f"metrics.json not found in {snap}")

    snapshot_id = label_data.get("snapshot_id", 0)
    sdp_name    = label_data.get("sdp_name", snap.parent.name)

    status = _load(snap / f"snapshot_{snapshot_id}_status.json")
    merged = _merge(label_data, metrics_data)

    # Group by category, sorted by total clocks desc
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for d in merged:
        by_cat[d["label"].get("category", "Other")].append(d)

    cat_stats: dict[str, dict] = {}
    for cat, dcs in by_cat.items():
        with_m = [d for d in dcs if d["metrics"]]
        cat_stats[cat] = {
            "count":        len(dcs),
            "total_clocks": int(sum(d["metrics"].get("clocks", 0) for d in with_m)),
        }
    total_clocks = sum(s["total_clocks"] for s in cat_stats.values())
    cat_order = sorted(by_cat, key=lambda c: cat_stats[c]["total_clocks"], reverse=True)

    lines: list[str] = [
        f"# {sdp_name} DrawCall 归因分析报告",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        "",
    ]

    # Section 1
    lines += _overall_section(status, merged)

    # Section 2: per category
    lines += ["## 2. 分类分析\n"]
    for i, cat in enumerate(cat_order, 1):
        dc_count   = cat_stats[cat]["count"]
        clocks_pct = cat_stats[cat]["total_clocks"] * 100.0 / max(total_clocks, 1)
        lines.append(f"### 2.{i}. {cat} 类（{dc_count} DC，占总 clocks {clocks_pct:.1f}%）\n")

        cat_dcs = by_cat[cat]
        top_dcs = sorted(
            [d for d in cat_dcs if d["metrics"]],
            key=lambda d: d["metrics"].get("clocks", 0), reverse=True
        )[:5]

        if llm_fn and top_dcs:
            try:
                resp = llm_fn(cat, sdp_name, top_dcs, status)
                if resp and resp.strip():
                    lines.append(resp.strip())
                    lines.append("")
                    continue
            except Exception:
                pass  # fall through to rule-based

        lines += _rule_category_section(cat, cat_dcs, status)

    # Section 3: recommendations
    lines += ["---\n", "## 3. 综合建议\n"]
    lines += _rule_recommendations(cat_stats, status)
    lines.append("")

    fname = f"snapshot_{snapshot_id}_analysis.md" if snapshot_id else f"analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out = snap / fname
    out.write_text("\n".join(lines), encoding="utf-8")
    _log.info(f"[AnalysisMd] done: {_time.time()-_t0:.1f}s, {len(cat_order)} categories")
    return out
