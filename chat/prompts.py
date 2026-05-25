"""chat/prompts.py — System prompt builder for chat AI."""
from __future__ import annotations

import re

import chat


def _detect_language(text: str) -> str:
    """Detect dominant language from text. Returns 'zh', 'ja', 'ko', or 'en'."""
    if not text:
        return "en"
    cjk = len(re.findall(r'[一-鿿]', text))
    jp = len(re.findall(r'[぀-ゟ゠-ヿ]', text))
    kr = len(re.findall(r'[가-힯]', text))
    total = len(text)
    if jp > 2:
        return "ja"
    if kr > 2:
        return "ko"
    if cjk / max(total, 1) > 0.1:
        return "zh"
    return "en"


_LANG_NAMES = {"zh": "Chinese", "ja": "Japanese", "ko": "Korean", "en": "English"}


def build_system_prompt(snapshot_ids: list[int], user_lang: str = "en") -> str:
    lang_instruction = f"Respond in {_LANG_NAMES.get(user_lang, 'English')}."

    parts = [
        f"GPU profiling assistant. {lang_instruction}",
        "Use execute_python for all computation/charts.",
        "Bindings: db, snapshot_id, data_query (module).",
        "data_query API:",
        "  get_draw_calls(db,sid,category=,tags=) → [dict] with draw_call fields + metrics joined.",
        "  get_metrics(db,sid) → {api_id: {metric:val}} all non-None metric columns",
        "  get_dc_detail(db,sid,api_id) → dict with label,metrics,shader_stages,textures,mesh_file",
        "  query_dcs(db,sid,category=,min_clocks=,tags=) → [dict] ordered by clocks DESC",
        "Metric keys (from GPU counters, may be None): clocks, fragments_shaded, vertices_shaded, read_total_bytes, write_total_bytes, shaders_busy_pct, shaders_stalled_pct, lrz_pixels_killed.",
        "NOTE: vertex_count/index_count are API params (often 0 or small). Use vertices_shaded/fragments_shaded for actual GPU workload.",
        "Label DCs by api_id (e.g. DC#42), not api_name (repeats). Filter None: [d for d in dcs if d.get('clocks')]",
        "If unsure about keys, first run: list(dcs[0].keys()) to discover available columns.",
        "Charts: matplotlib, English labels, no plt.show/savefig/image links. No db.query/pd.read_sql. Use actual data values for axes, not indices. Filter out 0/None before log transforms.",
        "execute_python returns image_paths when charts are generated. In save_report content, reference them as ![desc](path).",
    ]

    if snapshot_ids:
        db = chat.get_db()
        rows = db.cursor().execute(
            "SELECT snapshot_id, sdp_name, run_name FROM snapshots WHERE snapshot_id IN "
            f"({','.join('?' * len(snapshot_ids))})",
            snapshot_ids,
        ).fetchall()
        if rows:
            parts.append("")
            parts.append("Active snapshots: " + ", ".join(f"#{r[0]} {r[1]}" for r in rows))

    return "\n".join(parts)
