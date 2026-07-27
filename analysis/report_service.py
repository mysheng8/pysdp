"""report_service.py — LLM-powered GPU profiling report generator.

Reads:  snapshot_N_status.json + snapshot_N_topdc.json + snapshot_screenshot.png
Writes: snapshot_N_report.md

Flow:
  1. VLM describes the screenshot (what's on screen this frame)
  2. Extract key numbers from status/topdc JSON
  3. One LLM call generates the full MD report
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _fmt_n(v) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _fmt_mb(b) -> str:
    try:
        return f"{float(b) / 1048576:.2f} MB"
    except Exception:
        return "—"


def _find_screenshot(snap: Path) -> Path | None:
    for name in ["snapshot_screenshot.png", "screenshot.png", "screenshot.jpg"]:
        p = snap / name
        if p.exists():
            return p
    return None


def _top_corr_metrics(category_entry: dict, n: int = 3) -> list[str]:
    """Extract top-N most suspicious metrics from topdc attribution data."""
    scores: dict[str, float] = {}
    for dc in (category_entry.get("top_dcs") or [])[:5]:
        for item in (dc.get("attribution") or {}).get("suspicious_metrics") or []:
            m = item.get("metric", "")
            w = item.get("tier_weight") or item.get("value") or 0
            scores[m] = scores.get(m, 0) + float(w)
    return [m for m, _ in sorted(scores.items(), key=lambda x: -x[1])][:n]


def _build_data_summary(status: dict, topdc: dict) -> dict:
    """Compact data dict to inject into LLM prompt."""
    overall = status.get("overall", {})
    cat_stats = status.get("category_stats") or []
    topdc_cats = {c["category"]: c for c in (topdc.get("categories") or [])}

    categories = []
    for cs in sorted(cat_stats, key=lambda c: c.get("clocks_sum", 0), reverse=True):
        cat = cs["category"]
        td  = topdc_cats.get(cat, {})
        top_dcs = []
        for dc in (td.get("top_dcs") or [])[:3]:
            m = dc.get("metrics") or {}
            top_dcs.append({
                "dc_id":      dc.get("dc_id"),
                "clocks":     _fmt_n(m.get("clocks", 0)),
                "read":       _fmt_mb(m.get("read_total_bytes", 0)),
                "write":      _fmt_mb(m.get("write_total_bytes", 0)),
                "shader_busy_pct": round(m.get("shaders_busy_pct", 0), 1),
                "tex_l1_miss_pct": round(m.get("tex_l1_miss_pct", 0), 1),
                "fragments":  _fmt_n(m.get("fragments_shaded", 0)),
                "vertices":   _fmt_n(m.get("vertices_shaded", 0)),
            })
        top_metrics = _top_corr_metrics(td, n=3)
        p50 = cs.get("metrics_p50") or {}
        categories.append({
            "category":      cat,
            "dc_count":      cs.get("dc_count", 0),
            "clocks_sum":    _fmt_n(cs.get("clocks_sum", 0)),
            "clocks_pct":    round(cs.get("clocks_pct", 0), 1),
            "avg_clocks":    _fmt_n(cs.get("clocks_sum", 0) // max(cs.get("dc_count", 1), 1)),
            "top_metrics":   top_metrics,
            "p50_shader_busy": round(p50.get("shaders_busy_pct", 0), 1),
            "p50_tex_miss":    round(p50.get("tex_l1_miss_pct", 0), 1),
            "p50_read":        _fmt_mb(p50.get("read_total_bytes", 0)),
            "top_dcs":       top_dcs,
        })

    # Global top-3 correlated metrics (by frequency in suspicious_metrics across all cats)
    all_scores: dict[str, float] = {}
    for td_cat in topdc_cats.values():
        for m in _top_corr_metrics(td_cat, n=5):
            all_scores[m] = all_scores.get(m, 0) + 1
    global_top_metrics = [m for m, _ in sorted(all_scores.items(), key=lambda x: -x[1])][:3]

    return {
        "overall": {
            "total_dc":       overall.get("total_dc_count", 0),
            "total_clocks":   _fmt_n(overall.get("total_clocks", 0)),
            "total_read":     _fmt_mb(overall.get("total_read_bytes", 0)),
            "total_write":    _fmt_mb(overall.get("total_write_bytes", 0)),
            "fragments":      _fmt_n(overall.get("total_fragments_shaded", 0)),
            "vertices":       _fmt_n(overall.get("total_vertices_shaded", 0)),
            "metrics_coverage": f"{overall.get('metrics_coverage_ratio', 0) * 100:.1f}%",
        },
        "global_top_metrics": global_top_metrics,
        "categories": categories,
    }


def _build_prompt(data: dict, scene_desc: str, sdp_name: str) -> str:
    """Build report generation prompt using PromptManager (customizable via prompts.json)."""
    from prompt_config.prompt_manager import get_prompt_manager

    data_json = json.dumps(data, ensure_ascii=False, indent=2)

    pm = get_prompt_manager()
    variables = {
        "scene_desc": scene_desc or "Not available",
        "sdp_name": sdp_name,
        "data_json": data_json,
    }

    system_prompt, user_prompt = pm.render_prompt("report_generation", variables)

    # Combine system + user (llm.chat expects single prompt string)
    if system_prompt:
        return f"{system_prompt}\n\n{user_prompt}"
    return user_prompt


def _build_prompt_ORIGINAL(data: dict, scene_desc: str, sdp_name: str) -> str:
    """ORIGINAL HARDCODED VERSION - kept for reference only."""
    data_json = json.dumps(data, ensure_ascii=False, indent=2)
    return f"""You are a GPU performance engineer analyzing Snapdragon Adreno profiling data from a mobile game frame.

Scene description from screenshot: {scene_desc or "Not available"}
Capture name: {sdp_name}

Profiling data (JSON):
{data_json}

Generate a detailed GPU performance analysis report in Markdown. The report MUST be written in Chinese and follow this exact structure:

# GPU 性能分析报告 — {{sdp_name}}

## 1. 总览

Describe the frame in 2-3 sentences (using the scene description). Then provide a table:

| 指标 | 数值 |
|------|------|
| Draw Call 总数 | ... |
| 总 Clocks | ... |
| 总内存读取 | ... |
| 总内存写入 | ... |
| 总片元数 | ... |
| 总顶点数 | ... |

Then list the **top 3 most correlated performance metrics** for this frame (from global_top_metrics) and briefly explain what each metric indicates.

## 2. 分类分析

For each category in categories (ordered by clocks_pct descending), write a subsection:

### 2.N. {{category}}（{{dc_count}} DC，占 GPU {{clocks_pct}}%）

- **耗时**: 总 clocks，平均每 DC clocks
- **性能特征**: p50 shader_busy%, tex_l1_miss_pct, read bandwidth
- **关键指标**: top_metrics (explain what they indicate for this category)
- **耗时 Top 3 DC**:

| 排名 | DC ID | Clocks | 片元数 | Read | Write | Shader Busy | Tex L1 Miss |
|------|-------|--------|--------|------|-------|-------------|-------------|
| 1 | ... | ... | ... | ... | ... | ... | ... |

- **小结**: 1-2句话总结该类的主要瓶颈

## 3. 优化建议

Based on the data, provide 4-6 specific, actionable optimization recommendations prioritized by GPU time impact. Each recommendation should reference specific categories and metrics. Format as numbered list.

Be specific and technical. Reference actual numbers from the data.
"""


def generate_report(snapshot_dir: str | Path) -> Path:
    """Generate snapshot_N_report.md using VLM + LLM.

    Returns path to the written report file.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)
    _t0 = time.time()
    _log.info("[Report] start", context={"dir": snap.name})

    # Load data
    label_data = _load(snap / "label.json")
    if not label_data:
        raise FileNotFoundError(f"label.json not found in {snap}")
    snapshot_id = label_data.get("snapshot_id", snap.name.replace("snapshot_", ""))
    sdp_name = label_data.get("sdp_name", snap.parent.name)

    status = _load(snap / f"snapshot_{snapshot_id}_status.json")
    topdc  = _load(snap / f"snapshot_{snapshot_id}_topdc.json")
    if not status:
        raise FileNotFoundError(f"snapshot_{snapshot_id}_status.json not found")
    if not topdc:
        raise FileNotFoundError(f"snapshot_{snapshot_id}_topdc.json not found")

    # VLM: describe screenshot
    from analysis.llm_wrapper import get_vlm
    scene_desc = ""
    screenshot = _find_screenshot(snap)
    if screenshot:
        vlm = get_vlm()
        if vlm.is_enabled:
            _log.info("[Report] calling VLM for screenshot description")
            scene_desc = vlm.describe_image(
                screenshot,
                "Describe what is visible in this mobile game frame in 2-3 sentences. "
                "Focus on: game scene type (e.g. gameplay, menu, cutscene), "
                "what characters/objects/UI elements are visible, "
                "overall visual complexity."
            ) or ""
        else:
            _log.warning("[Report] VLM not configured, skipping screenshot description")
    else:
        _log.warning("[Report] no screenshot found")

    # Build data summary and prompt
    data = _build_data_summary(status, topdc)
    prompt = _build_prompt(data, scene_desc, sdp_name)

    # LLM: generate report
    from analysis.llm_wrapper import get_llm
    llm = get_llm()
    if not llm.is_enabled:
        raise RuntimeError("LLM not configured — set LlmApiEndpoint and LlmApiKey in config")

    _log.info("[Report] calling LLM for report generation")
    result = llm.chat(prompt)
    if not result or not result.strip():
        raise RuntimeError("LLM returned empty response")

    # Write output
    out_path = snap / f"snapshot_{snapshot_id}_report.md"
    out_path.write_text(result.strip(), encoding="utf-8")

    _log.info(f"[Report] done: {time.time()-_t0:.1f}s → {out_path.name}",
              context={"chars": len(result)})
    return out_path
