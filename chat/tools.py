"""chat/tools.py — Tool definitions and executor for chat AI."""
from __future__ import annotations

import asyncio
import json as _json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import chat

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": "Run Python code. Bindings: db, snapshot_id, data_query. Last expression is returned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_snapshots",
            "description": "List available snapshots.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_report",
            "description": "Save markdown report to disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "Markdown content"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_aspect",
            "description": "Fetch a specific aspect of a snapshot's GPU data. Use when the user asks about a snapshot dimension not yet in the conversation. Do NOT call if the (snapshot_id, aspect) pair is in the Already-Fetched list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "integer", "description": "Integer snapshot id"},
                    "aspect": {
                        "type": "string",
                        "enum": ["gpu_timing", "bandwidth", "draw_call_breakdown",
                                 "shader_complexity", "texture_usage", "triangle_count",
                                 "bottleneck_summary"],
                    },
                },
                "required": ["snapshot_id", "aspect"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_snapshots",
            "description": "Compare performance between two snapshots. Returns category-level deltas, top regressions/improvements, and bottleneck shift analysis. Use when the user asks to compare, diff, or find what changed between snapshots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "baseline_id": {"type": "integer", "description": "Baseline snapshot id (the 'before')"},
                    "target_id": {"type": "integer", "description": "Target snapshot id (the 'after')"},
                    "focus_category": {
                        "type": "string",
                        "description": "Optional: limit comparison to one category (e.g. 'Character', 'Scene')",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top regressed/improved DCs to return (default 10)",
                    },
                },
                "required": ["baseline_id", "target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_history",
            "description": "Browse earlier conversation rounds that were trimmed from context. Use when you need detail from a previous exchange that is no longer visible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search in past rounds (directory mode)",
                    },
                    "round_id": {
                        "type": "string",
                        "description": "Expand a specific round by its user message id (e.g. msg_003)",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["parent", "next"],
                        "description": "Page to adjacent round from round_id",
                    },
                },
            },
        },
    },
]

# Extended tools — available via executor but not sent to LLM (reduce payload)
EXTENDED_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Save code as reusable skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "slash_command": {"type": "string"},
                    "button_label": {"type": "string"},
                    "icon": {"type": "string"},
                    "description": {"type": "string"},
                    "prompt_template": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["id", "name", "slash_command", "button_label", "icon", "description", "prompt_template", "code"],
            },
        },
    },
]


def _truncate(data: list, limit: int = 50) -> list | dict:
    if len(data) <= limit:
        return data
    return {"rows": data[:limit], "_truncated": True, "_total": len(data)}


def _indent_code(code: str, spaces: int) -> str:
    """Indent each line of code by N spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line if line.strip() else line for line in code.splitlines())


class ToolExecutor:
    def __init__(self):
        self._last_snapshot_ids: list[int] = []
        self._session = None
        self._excluded_ids: set[str] = set()

    def set_session_context(self, session, excluded_ids: set[str]):
        self._session = session
        self._excluded_ids = excluded_ids

    def set_snapshot_ids(self, ids: list[int]):
        self._last_snapshot_ids = ids

    def _get_reports_dir(self, snapshot_ids: list[int] | None = None) -> str:
        from pathlib import Path
        from analysis.llm_wrapper import _load_config
        cfg = _load_config()
        report_dir = cfg.get("ReportDir", "reports")
        project = cfg.get("ProjectDir", "")
        if not project:
            working = cfg.get("WorkingDirectory", "")
            if working:
                project = str(Path(working) / "project")
        if project and not Path(report_dir).is_absolute():
            report_dir = str(Path(project) / report_dir)
        if not Path(report_dir).is_absolute():
            report_dir = str(Path(chat.__file__).resolve().parent.parent / "reports")
        return report_dir

    async def execute(self, name: str, args: dict) -> Any:
        if name == "execute_python":
            return await self._execute_python(args)
        elif name == "create_skill":
            return await asyncio.to_thread(self._create_skill, args)
        elif name == "save_report":
            return await asyncio.to_thread(self._save_report, args)
        elif name == "fetch_aspect":
            return await asyncio.to_thread(self._fetch_aspect, args)
        elif name == "compare_snapshots":
            return await asyncio.to_thread(self._compare_snapshots, args)
        elif name == "recall_history":
            return self._recall_history(args)
        return await asyncio.to_thread(self._execute_sync, name, args)

    def _execute_sync(self, name: str, args: dict) -> Any:
        db = chat.get_db()

        if name == "get_snapshots":
            return self._get_snapshots(db)
        elif name == "get_draw_calls":
            return self._get_draw_calls(db, args)
        elif name == "get_dc_detail":
            return self._get_dc_detail(db, args)
        elif name == "get_clock_correlation":
            return self._get_clock_correlation(db, args)
        elif name == "get_label_agg":
            return self._get_label_agg(db, args)
        else:
            return {"error": f"Unknown tool: {name}"}

    def _get_project_dir(self) -> Path | None:
        from analysis.llm_wrapper import _load_config
        cfg = _load_config()
        project = cfg.get("ProjectDir", "")
        if not project:
            working = cfg.get("WorkingDirectory", "")
            if working:
                project = str(Path(working) / "project")
        return Path(project) if project else None

    async def _execute_python(self, args: dict) -> Any:
        from chat.sandbox import execute_code
        code = args.get("code", "")
        snapshot_id_override = args.get("snapshot_id")
        snapshot_ids = [snapshot_id_override] if snapshot_id_override else self._last_snapshot_ids or []
        save_dir = str(Path(self._get_reports_dir(snapshot_ids)) / "img")
        result = await execute_code(code, snapshot_ids, save_dir=save_dir)
        image_paths = result.pop("image_paths", [])
        if result.get("images") and result["result"] is None:
            result["result"] = f"[Chart generated: {len(result['images'])} image(s) displayed inline]"
        if image_paths:
            project_dir = self._get_project_dir()
            if project_dir:
                rel_paths = []
                for p in image_paths:
                    try:
                        rel_paths.append(str(Path(p).relative_to(project_dir)).replace("\\", "/"))
                    except ValueError:
                        rel_paths.append(p)
                result["image_paths"] = rel_paths
            else:
                result["image_paths"] = image_paths
        if result["result"] is not None:
            try:
                serialized = _json.dumps(result["result"], default=str)
                if len(serialized) > 8000:
                    if isinstance(result["result"], list) and len(result["result"]) > 50:
                        result["result"] = {"rows": result["result"][:50], "_truncated": True, "_total": len(result["result"])}
                    else:
                        result["result"] = serialized[:8000] + "... (truncated)"
            except (TypeError, ValueError):
                result["result"] = repr(result["result"])[:4000]
        if result["output"] and len(result["output"]) > 4000:
            result["output"] = result["output"][:4000] + "\n... (truncated)"
        return result

    def _create_skill(self, args: dict) -> Any:
        import ast as _ast
        from chat.skills import SKILLS_DIR, load_skills

        skill_id = args["id"]
        code = args["code"]

        if not skill_id.isidentifier():
            return {"error": f"Invalid skill ID: '{skill_id}' — must be a valid Python identifier"}

        try:
            _ast.parse(code)
        except SyntaxError as e:
            return {"error": f"Code syntax error: {e}"}

        SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        md_content = f"""---
name: {args['name']}
slash_command: {args['slash_command']}
button_label: {args['button_label']}
icon: "{args['icon']}"
description: {args['description']}
---

{args['prompt_template']}
"""
        (SKILLS_DIR / f"{skill_id}.md").write_text(md_content, encoding="utf-8")

        py_content = f'''"""Auto-generated skill: {args['name']}"""
from chat.skills import SkillContext


def run(ctx: SkillContext):
    db = ctx.db
    snapshot_ids = ctx.snapshot_ids
    snapshot_id = snapshot_ids[0] if snapshot_ids else None
    from data import query as data_query

{_indent_code(code, 4)}
'''
        (SKILLS_DIR / f"{skill_id}.py").write_text(py_content, encoding="utf-8")

        load_skills()
        return {"ok": True, "message": f"Skill /{args['slash_command'].lstrip('/')} created successfully"}

    def _save_report(self, args: dict) -> Any:
        from datetime import datetime

        title = args["title"]
        content = args["content"]
        snapshot_ids = args.get("snapshot_ids") or self._last_snapshot_ids or []
        filename = args.get("filename") or title.lower().replace(" ", "_")[:40]

        reports_dir = Path(self._get_reports_dir(snapshot_ids))
        reports_dir.mkdir(parents=True, exist_ok=True)
        filepath = reports_dir / f"{filename}.md"

        header = f"# {title}\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Snapshots: {snapshot_ids}\n\n---\n\n"
        filepath.write_text(header + content, encoding="utf-8")

        project_dir = self._get_project_dir()
        if project_dir:
            try:
                rel = str(filepath.relative_to(project_dir)).replace("\\", "/")
            except ValueError:
                rel = str(filepath)
        else:
            rel = str(filepath)
        return {"ok": True, "path": rel, "filename": f"{filename}.md"}

    def _get_snapshots(self, db) -> Any:
        rows = db.cursor().execute(
            "SELECT snapshot_id, sdp_name, run_name, snapshot_dir FROM snapshots ORDER BY snapshot_id"
        ).fetchall()
        return [{"snapshot_id": r[0], "sdp_name": r[1], "run_name": r[2], "dir": r[3]} for r in rows]

    def _get_draw_calls(self, db, args: dict) -> Any:
        from data.query import get_draw_calls
        snapshot_id = args["snapshot_id"]
        category = args.get("category")
        result = get_draw_calls(db, snapshot_id, category=category)
        return _truncate(result)

    def _get_dc_detail(self, db, args: dict) -> Any:
        from data.query import get_dc_detail
        snapshot_id = args["snapshot_id"]
        api_id = args["api_id"]
        result = get_dc_detail(db, snapshot_id, api_id)
        if result is None:
            return {"error": f"DC not found: snapshot={snapshot_id}, api_id={api_id}"}
        return result

    def _get_clock_correlation(self, db, args: dict) -> Any:
        from data.query import _snap_where
        snapshot_id = args["snapshot_id"]
        category = args.get("category")

        snap_clause, snap_params = _snap_where(snapshot_id, "dc")
        if category:
            cat_filter = " AND COALESCE(lb.category, 'Unlabeled') = ?"
            cat_params = [category]
            min_n = 3
        else:
            cat_filter = ""
            cat_params = []
            min_n = 10

        where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"

        col_result = db.cursor().execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'metrics' ORDER BY ordinal_position"
        )
        all_cols = [r[0] for r in col_result.fetchall() if r[0] not in ("snapshot_id", "api_id", "clocks")]
        if not all_cols:
            return []

        cols_sql = ", ".join(f"m.{c}" for c in all_cols)
        sql = f"""
            SELECT m.clocks, {cols_sql}
            FROM draw_calls dc
            LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
            LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
            {where}{cat_filter}
        """
        raw = db.cursor().execute(sql, snap_params + cat_params).fetchall()
        if not raw:
            return []

        clocks_all = [row[0] for row in raw]
        results = []
        for ci, col in enumerate(all_cols):
            vals = [(clocks_all[i], row[ci + 1]) for i, row in enumerate(raw)
                    if clocks_all[i] is not None and row[ci + 1] is not None]
            n = len(vals)
            if n < min_n:
                continue
            xs = [v[0] for v in vals]
            ys = [v[1] for v in vals]
            xm = sum(xs) / n
            ym = sum(ys) / n
            num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
            denom = math.sqrt(sum((x - xm) ** 2 for x in xs) * sum((y - ym) ** 2 for y in ys))
            if denom == 0:
                continue
            r = num / denom
            results.append({"metric": col, "r2": round(r * r, 4), "r": round(r, 4), "n": n})

        results.sort(key=lambda x: x["r2"], reverse=True)
        return results

    def _get_label_agg(self, db, args: dict) -> Any:
        from data.query import _snap_where
        snapshot_id = args["snapshot_id"]
        metric = args.get("metric", "clocks")
        agg = args.get("agg", "sum")

        snap_clause, snap_params = _snap_where(snapshot_id, "dc")
        where = f"WHERE {snap_clause}" if snap_clause else ""

        sql = f"""
            SELECT COALESCE(lb.category, 'Unlabeled') AS category, m.{metric} AS val
            FROM draw_calls dc
            LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
            LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
            {where}
        """
        raw = db.cursor().execute(sql, snap_params).fetchall()

        groups: dict[str, list] = {}
        for cat, val in raw:
            if val is not None:
                groups.setdefault(cat, []).append(val)

        results = []
        for cat, vals in groups.items():
            if agg == "sum":
                v = sum(vals)
            elif agg == "avg":
                v = sum(vals) / len(vals)
            elif agg == "median":
                v = statistics.median(vals)
            elif agg == "max":
                v = max(vals)
            elif agg == "min":
                v = min(vals)
            else:
                v = sum(vals)
            results.append({"category": cat, "value": round(v, 2), "dc_count": len(vals)})

        results.sort(key=lambda x: x["value"], reverse=True)
        return results

    # ── recall_history ─────────────────────────────────────────────────────────

    def _recall_history(self, args: dict) -> dict:
        from chat.sessions import get_path, extract_answer_recap
        from chat.context import _extract_rounds, _round_to_openai, estimate_tokens

        session = self._session
        if not session:
            return {"error": "No session context available"}

        query = args.get("query")
        round_id = args.get("round_id")
        direction = args.get("direction")

        rounds = _extract_rounds(session)
        round_map = {r["user_id"]: (i, r) for i, r in enumerate(rounds)}

        # Mode 1: Directory — keyword search over answer/recap
        if query and not round_id:
            query_lower = query.lower()
            results = []
            for rnd in rounds:
                user_id = rnd["user_id"]
                # Find assistant text in this round
                answer = ""
                recap = ""
                user_text = ""
                for msg in rnd["messages"]:
                    if msg.get("role") == "user" and msg.get("type") == "text":
                        user_text = msg.get("content", "")[:80]
                    elif msg.get("role") == "assistant" and msg.get("type") == "text":
                        a, r = extract_answer_recap(msg.get("content", ""))
                        answer = a
                        recap = r

                searchable = f"{user_text} {answer} {recap}".lower()
                if query_lower in searchable:
                    results.append({
                        "round_id": user_id,
                        "user_preview": user_text[:60],
                        "answer": answer[:120],
                        "recap": recap[:120],
                        "in_context": user_id not in self._excluded_ids,
                    })
            return {"mode": "directory", "query": query, "matches": results[:15]}

        # Mode 2: Expand — full round content, capped at ~1500 tokens
        if round_id and not direction:
            if round_id not in round_map:
                return {"error": f"Round {round_id} not found"}
            _, rnd = round_map[round_id]
            msgs = _round_to_openai(rnd)
            # Cap total tokens
            total = 0
            capped = []
            for m in msgs:
                t = estimate_tokens(m.get("content") or "")
                if total + t > 1500:
                    capped.append({**m, "content": (m.get("content") or "")[:4500] + "...[truncated]"})
                    break
                capped.append(m)
                total += t
            return {"mode": "expand", "round_id": round_id, "messages": capped}

        # Mode 3: Page — adjacent round
        if round_id and direction:
            if round_id not in round_map:
                return {"error": f"Round {round_id} not found"}
            idx, _ = round_map[round_id]
            if direction == "parent":
                target_idx = idx - 1
            else:
                target_idx = idx + 1

            if target_idx < 0 or target_idx >= len(rounds):
                return {"error": f"No {direction} round from {round_id}"}

            target_rnd = rounds[target_idx]
            user_text = ""
            answer = ""
            recap = ""
            for msg in target_rnd["messages"]:
                if msg.get("role") == "user" and msg.get("type") == "text":
                    user_text = msg.get("content", "")[:80]
                elif msg.get("role") == "assistant" and msg.get("type") == "text":
                    a, r = extract_answer_recap(msg.get("content", ""))
                    answer = a
                    recap = r
            return {
                "mode": "page",
                "direction": direction,
                "round_id": target_rnd["user_id"],
                "user_preview": user_text[:60],
                "answer": answer[:120],
                "recap": recap[:120],
            }

        return {"error": "Provide either query or round_id"}

    # ── fetch_aspect ────────────────────────────────────────────────────────────

    def _fetch_aspect(self, args: dict) -> Any:
        db = chat.get_db()
        snapshot_id = args["snapshot_id"]
        aspect = args["aspect"]
        handler = _ASPECT_HANDLERS.get(aspect)
        if not handler:
            return {"error": f"Unknown aspect: {aspect}"}
        return handler(db, snapshot_id)

    # ── compare_snapshots ───────────────────────────────────────────────────────

    def _compare_snapshots(self, args: dict) -> Any:
        db = chat.get_db()
        return _compare_snapshots_impl(db, args)


def _aspect_gpu_timing(db, snapshot_id: int) -> dict:
    """GPU timing distribution — clocks by category + top DCs."""
    from data.query import _snap_where
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    sql = f"""
        SELECT dc.api_id, COALESCE(lb.category, 'Unlabeled') AS category,
               lb.subcategory, m.clocks
        FROM draw_calls dc
        LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where} AND m.clocks IS NOT NULL
        ORDER BY m.clocks DESC
    """
    rows = db.cursor().execute(sql, snap_params).fetchall()
    if not rows:
        return {"snapshot_id": snapshot_id, "aspect": "gpu_timing", "data": None, "note": "No clocks data"}

    total_clocks = sum(r[3] for r in rows)
    by_cat: dict[str, int] = defaultdict(int)
    for _, cat, _, clocks in rows:
        by_cat[cat] += clocks

    categories = sorted(
        [{"category": c, "clocks": v, "pct": round(v / total_clocks * 100, 1)} for c, v in by_cat.items()],
        key=lambda x: x["clocks"], reverse=True,
    )
    top_dcs = [
        {"api_id": r[0], "category": r[1], "subcategory": r[2], "clocks": r[3],
         "pct": round(r[3] / total_clocks * 100, 1)}
        for r in rows[:10]
    ]
    return {
        "snapshot_id": snapshot_id, "aspect": "gpu_timing",
        "total_clocks": total_clocks, "dc_count": len(rows),
        "by_category": categories, "top_10_dcs": top_dcs,
    }


def _aspect_bandwidth(db, snapshot_id: int) -> dict:
    """Memory bandwidth — read/write by category."""
    from data.query import _snap_where
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    sql = f"""
        SELECT COALESCE(lb.category, 'Unlabeled') AS category,
               m.read_total_bytes, m.write_total_bytes
        FROM draw_calls dc
        LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where} AND (m.read_total_bytes IS NOT NULL OR m.write_total_bytes IS NOT NULL)
    """
    rows = db.cursor().execute(sql, snap_params).fetchall()
    if not rows:
        return {"snapshot_id": snapshot_id, "aspect": "bandwidth", "data": None}

    by_cat: dict[str, dict] = defaultdict(lambda: {"read": 0, "write": 0, "dc_count": 0})
    for cat, rb, wb in rows:
        by_cat[cat]["read"] += rb or 0
        by_cat[cat]["write"] += wb or 0
        by_cat[cat]["dc_count"] += 1

    total_read = sum(v["read"] for v in by_cat.values())
    total_write = sum(v["write"] for v in by_cat.values())
    categories = sorted(
        [{"category": c, "read_bytes": v["read"], "write_bytes": v["write"],
          "total_bytes": v["read"] + v["write"], "dc_count": v["dc_count"]}
         for c, v in by_cat.items()],
        key=lambda x: x["total_bytes"], reverse=True,
    )
    return {
        "snapshot_id": snapshot_id, "aspect": "bandwidth",
        "total_read_bytes": total_read, "total_write_bytes": total_write,
        "by_category": categories,
    }


def _aspect_draw_call_breakdown(db, snapshot_id: int) -> dict:
    """Draw call count by category."""
    from data.query import _snap_where
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    sql = f"""
        SELECT COALESCE(lb.category, 'Unlabeled') AS category,
               COUNT(*) AS dc_count, SUM(m.clocks) AS total_clocks
        FROM draw_calls dc
        LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where}
        GROUP BY category
        ORDER BY total_clocks DESC NULLS LAST
    """
    rows = db.cursor().execute(sql, snap_params).fetchall()
    total_dc = sum(r[1] for r in rows)
    categories = [
        {"category": r[0], "dc_count": r[1], "clocks_sum": r[2],
         "pct_of_total": round(r[1] / total_dc * 100, 1) if total_dc else 0}
        for r in rows
    ]
    return {
        "snapshot_id": snapshot_id, "aspect": "draw_call_breakdown",
        "total_dc_count": total_dc, "by_category": categories,
    }


def _aspect_shader_complexity(db, snapshot_id: int) -> dict:
    """Shader/ALU pressure — busy%, stalled%, instruction counts."""
    from data.query import _snap_where
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    sql = f"""
        SELECT dc.api_id, COALESCE(lb.category, 'Unlabeled') AS category,
               m.shaders_busy_pct, m.shaders_stalled_pct,
               m.time_alus_working_pct, m.fragment_instructions, m.vertex_instructions,
               m.clocks
        FROM draw_calls dc
        LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where} AND m.shaders_busy_pct IS NOT NULL
        ORDER BY m.shaders_busy_pct DESC
    """
    rows = db.cursor().execute(sql, snap_params).fetchall()
    if not rows:
        return {"snapshot_id": snapshot_id, "aspect": "shader_complexity", "data": None}

    busy_vals = [r[2] for r in rows if r[2] is not None]
    stalled_vals = [r[3] for r in rows if r[3] is not None]
    avg_busy = round(sum(busy_vals) / len(busy_vals), 1) if busy_vals else 0
    avg_stalled = round(sum(stalled_vals) / len(stalled_vals), 1) if stalled_vals else 0

    top_alu = [
        {"api_id": r[0], "category": r[1], "shaders_busy_pct": r[2],
         "shaders_stalled_pct": r[3], "time_alus_working_pct": r[4],
         "fragment_instructions": r[5], "vertex_instructions": r[6], "clocks": r[7]}
        for r in rows[:10]
    ]
    return {
        "snapshot_id": snapshot_id, "aspect": "shader_complexity",
        "dc_count": len(rows),
        "avg_shaders_busy_pct": avg_busy, "avg_shaders_stalled_pct": avg_stalled,
        "top_10_shader_heavy": top_alu,
    }


def _aspect_texture_usage(db, snapshot_id: int) -> dict:
    """Texture usage — count, formats, estimated VRAM."""
    rows = db.cursor().execute(
        "SELECT texture_id, width, height, depth, format, layers, levels "
        "FROM textures WHERE snapshot_id = ? ORDER BY width*height DESC",
        [snapshot_id],
    ).fetchall()
    if not rows:
        return {"snapshot_id": snapshot_id, "aspect": "texture_usage", "data": None}

    formats: dict[str, int] = defaultdict(int)
    total_pixels = 0
    for _, w, h, d, fmt, layers, levels in rows:
        formats[fmt or "unknown"] += 1
        total_pixels += (w or 0) * (h or 0) * max(d or 1, 1)

    top_textures = [
        {"texture_id": r[0], "width": r[1], "height": r[2], "format": r[4],
         "layers": r[5], "levels": r[6]}
        for r in rows[:10]
    ]
    return {
        "snapshot_id": snapshot_id, "aspect": "texture_usage",
        "texture_count": len(rows), "total_pixels": total_pixels,
        "format_distribution": dict(formats),
        "top_10_largest": top_textures,
    }


def _aspect_triangle_count(db, snapshot_id: int) -> dict:
    """Triangle/vertex count by category (from vertices_shaded)."""
    from data.query import _snap_where
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    sql = f"""
        SELECT COALESCE(lb.category, 'Unlabeled') AS category,
               SUM(m.vertices_shaded) AS total_verts,
               SUM(m.fragments_shaded) AS total_frags,
               COUNT(*) AS dc_count
        FROM draw_calls dc
        LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where} AND m.vertices_shaded IS NOT NULL
        GROUP BY category
        ORDER BY total_verts DESC
    """
    rows = db.cursor().execute(sql, snap_params).fetchall()
    if not rows:
        return {"snapshot_id": snapshot_id, "aspect": "triangle_count", "data": None}

    total_verts = sum(r[1] or 0 for r in rows)
    categories = [
        {"category": r[0], "vertices_shaded": r[1], "fragments_shaded": r[2],
         "dc_count": r[3], "vert_pct": round((r[1] or 0) / max(total_verts, 1) * 100, 1)}
        for r in rows
    ]
    return {
        "snapshot_id": snapshot_id, "aspect": "triangle_count",
        "total_vertices_shaded": total_verts, "by_category": categories,
    }


def _aspect_bottleneck_summary(db, snapshot_id: int) -> dict:
    """Bottleneck attribution summary using the 3-layer rule engine."""
    import json as json_mod
    from data.query import get_draw_calls

    rules_path = Path(__file__).resolve().parent.parent / "analysis" / "attribution_rules.json"
    if not rules_path.exists():
        return {"snapshot_id": snapshot_id, "aspect": "bottleneck_summary",
                "error": "attribution_rules.json not found"}

    from analysis.topdc_service import _Engine
    rules = json_mod.loads(rules_path.read_text(encoding="utf-8-sig"))
    engine = _Engine(rules)

    dcs = get_draw_calls(db, snapshot_id)
    if not dcs:
        return {"snapshot_id": snapshot_id, "aspect": "bottleneck_summary", "data": None}

    by_cat: dict[str, list] = defaultdict(list)
    for dc in dcs:
        cat = dc.get("category") or "Unlabeled"
        by_cat[cat].append(dc)

    # Build percentile thresholds per category
    METRIC_KEYS = list(engine.layer1.keys())
    cat_pcts: dict[str, dict] = {}
    for cat, cat_dcs in by_cat.items():
        pcts: dict[str, dict] = {}
        for tier in engine.tiers:
            tier_vals: dict[str, float] = {}
            for metric in METRIC_KEYS:
                vals = sorted(v for dc in cat_dcs if (v := dc.get(metric)) is not None)
                if len(vals) >= 5:
                    idx = int(len(vals) * tier["threshold"])
                    tier_vals[metric] = vals[min(idx, len(vals) - 1)]
            pcts[f"metrics_{tier['name']}"] = tier_vals
        cat_pcts[cat] = pcts

    # Run attribution on top DCs per category
    bottleneck_counts: dict[str, int] = defaultdict(int)
    top_bottlenecks = []
    for cat, cat_dcs in by_cat.items():
        with_clocks = [d for d in cat_dcs if d.get("clocks")]
        top = sorted(with_clocks, key=lambda d: d["clocks"], reverse=True)[:5]
        has_enough = len(with_clocks) >= 5
        for dc in top:
            metrics_dict = {k: dc.get(k) for k in METRIC_KEYS if dc.get(k) is not None}
            attr = engine.attribute({"metrics": metrics_dict}, cat_pcts.get(cat, {}), has_enough)
            primary = attr["primary_bottleneck"]
            if primary:
                bottleneck_counts[primary] += 1
                if len(top_bottlenecks) < 10:
                    top_bottlenecks.append({
                        "api_id": dc["api_id"], "category": cat,
                        "clocks": dc["clocks"],
                        "primary_bottleneck": primary,
                        "confidence": attr["confidence_score"],
                    })

    return {
        "snapshot_id": snapshot_id, "aspect": "bottleneck_summary",
        "dc_analyzed": sum(min(len([d for d in dcs if d.get("clocks")]), 5) for dcs in by_cat.values()),
        "bottleneck_distribution": dict(bottleneck_counts),
        "top_bottleneck_dcs": top_bottlenecks,
    }


_ASPECT_HANDLERS = {
    "gpu_timing": _aspect_gpu_timing,
    "bandwidth": _aspect_bandwidth,
    "draw_call_breakdown": _aspect_draw_call_breakdown,
    "shader_complexity": _aspect_shader_complexity,
    "texture_usage": _aspect_texture_usage,
    "triangle_count": _aspect_triangle_count,
    "bottleneck_summary": _aspect_bottleneck_summary,
}


# ── compare_snapshots ───────────────────────────────────────────────────────────


def _compare_snapshots_impl(db, args: dict) -> dict:
    """Compare performance between two snapshots."""
    import json as json_mod
    from data.query import get_draw_calls

    baseline_id = args["baseline_id"]
    target_id = args["target_id"]
    focus_category = args.get("focus_category")
    top_n = args.get("top_n", 10)

    # Snapshot metadata
    snap_rows = db.cursor().execute(
        "SELECT snapshot_id, sdp_name FROM snapshots WHERE snapshot_id IN (?, ?)",
        [baseline_id, target_id],
    ).fetchall()
    snap_meta = {r[0]: r[1] for r in snap_rows}

    # Category-level aggregation
    base_agg = _cat_agg(db, baseline_id, focus_category)
    tgt_agg = _cat_agg(db, target_id, focus_category)

    base_total = sum(v["clocks"] for v in base_agg.values())
    tgt_total = sum(v["clocks"] for v in tgt_agg.values())
    delta_clocks = tgt_total - base_total
    delta_pct = round(delta_clocks / max(base_total, 1) * 100, 1)

    if abs(delta_pct) <= 5:
        verdict = "neutral"
    elif delta_pct > 0:
        verdict = "regression"
    else:
        verdict = "improvement"

    # Category comparison
    all_cats = sorted(set(list(base_agg.keys()) + list(tgt_agg.keys())))
    category_comparison = []
    for cat in all_cats:
        b = base_agg.get(cat, {"clocks": 0, "dc_count": 0})
        t = tgt_agg.get(cat, {"clocks": 0, "dc_count": 0})
        d = t["clocks"] - b["clocks"]
        category_comparison.append({
            "category": cat,
            "baseline_clocks": b["clocks"],
            "target_clocks": t["clocks"],
            "delta": d,
            "delta_pct": round(d / max(b["clocks"], 1) * 100, 1),
            "baseline_dc_count": b["dc_count"],
            "target_dc_count": t["dc_count"],
        })
    category_comparison.sort(key=lambda x: abs(x["delta"]), reverse=True)

    # DC-level pairing
    base_dcs = get_draw_calls(db, baseline_id, category=focus_category)
    tgt_dcs = get_draw_calls(db, target_id, category=focus_category)
    paired, new_dcs, removed_dcs = _pair_draw_calls(base_dcs, tgt_dcs)

    # Add new/removed counts to category comparison
    new_by_cat: dict[str, int] = defaultdict(int)
    removed_by_cat: dict[str, int] = defaultdict(int)
    for dc in new_dcs:
        new_by_cat[dc.get("category") or "Unlabeled"] += 1
    for dc in removed_dcs:
        removed_by_cat[dc.get("category") or "Unlabeled"] += 1
    for entry in category_comparison:
        entry["new_dcs"] = new_by_cat.get(entry["category"], 0)
        entry["removed_dcs"] = removed_by_cat.get(entry["category"], 0)

    # Attribution + ranking
    regressions, improvements, bottleneck_dist = _rank_and_attribute(
        paired, db, baseline_id, target_id
    )

    return {
        "summary": {
            "baseline": {"snapshot_id": baseline_id, "sdp_name": snap_meta.get(baseline_id, ""),
                         "total_clocks": base_total, "dc_count": sum(v["dc_count"] for v in base_agg.values())},
            "target": {"snapshot_id": target_id, "sdp_name": snap_meta.get(target_id, ""),
                       "total_clocks": tgt_total, "dc_count": sum(v["dc_count"] for v in tgt_agg.values())},
            "delta_clocks": delta_clocks,
            "delta_pct": delta_pct,
            "verdict": verdict,
        },
        "category_comparison": category_comparison[:15],
        "top_regressions": regressions[:top_n],
        "top_improvements": improvements[:top_n],
        "bottleneck_distribution": bottleneck_dist,
    }


def _cat_agg(db, snapshot_id: int, focus_category: str | None = None) -> dict[str, dict]:
    """Per-category clocks sum + dc_count."""
    from data.query import _snap_where
    snap_clause, snap_params = _snap_where(snapshot_id, "dc")
    where = f"WHERE {snap_clause}" if snap_clause else "WHERE 1=1"
    if focus_category:
        where += " AND COALESCE(lb.category, 'Unlabeled') = ?"
        snap_params = snap_params + [focus_category]
    sql = f"""
        SELECT COALESCE(lb.category, 'Unlabeled') AS category,
               SUM(COALESCE(m.clocks, 0)) AS total_clocks, COUNT(*) AS dc_count
        FROM draw_calls dc
        LEFT JOIN labels lb ON lb.snapshot_id = dc.snapshot_id AND lb.api_id = dc.api_id
        LEFT JOIN metrics m ON m.snapshot_id = dc.snapshot_id AND m.api_id = dc.api_id
        {where}
        GROUP BY category
    """
    rows = db.cursor().execute(sql, snap_params).fetchall()
    return {r[0]: {"clocks": r[1] or 0, "dc_count": r[2]} for r in rows}


def _pair_draw_calls(
    base_dcs: list[dict], tgt_dcs: list[dict]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pair DCs across snapshots using label matching."""
    paired: list[tuple[dict, dict]] = []
    new_dcs: list[dict] = []
    removed_dcs: list[dict] = []

    def _label_key(dc: dict) -> tuple:
        return (dc.get("category") or "", dc.get("subcategory") or "", dc.get("detail") or "")

    def _cat_key(dc: dict) -> tuple:
        return (dc.get("category") or "", dc.get("subcategory") or "")

    # Phase 1: exact (category, subcategory, detail) match
    base_by_label: dict[tuple, list[dict]] = defaultdict(list)
    tgt_by_label: dict[tuple, list[dict]] = defaultdict(list)
    for dc in base_dcs:
        base_by_label[_label_key(dc)].append(dc)
    for dc in tgt_dcs:
        tgt_by_label[_label_key(dc)].append(dc)

    base_matched: set[int] = set()
    tgt_matched: set[int] = set()

    for key in set(base_by_label) & set(tgt_by_label):
        b_list = sorted(base_by_label[key], key=lambda d: d.get("clocks") or 0, reverse=True)
        t_list = sorted(tgt_by_label[key], key=lambda d: d.get("clocks") or 0, reverse=True)
        for b, t in zip(b_list, t_list):
            paired.append((b, t))
            base_matched.add(b["api_id"])
            tgt_matched.add(t["api_id"])
        if len(t_list) > len(b_list):
            for t in t_list[len(b_list):]:
                tgt_matched.add(t["api_id"])
                new_dcs.append(t)
        elif len(b_list) > len(t_list):
            for b in b_list[len(t_list):]:
                base_matched.add(b["api_id"])
                removed_dcs.append(b)

    # Phase 2: fallback (category, subcategory) for remaining
    base_remain = [dc for dc in base_dcs if dc["api_id"] not in base_matched]
    tgt_remain = [dc for dc in tgt_dcs if dc["api_id"] not in tgt_matched]

    base_by_cat: dict[tuple, list[dict]] = defaultdict(list)
    tgt_by_cat: dict[tuple, list[dict]] = defaultdict(list)
    for dc in base_remain:
        base_by_cat[_cat_key(dc)].append(dc)
    for dc in tgt_remain:
        tgt_by_cat[_cat_key(dc)].append(dc)

    for key in set(base_by_cat) & set(tgt_by_cat):
        b_list = sorted(base_by_cat[key], key=lambda d: d.get("clocks") or 0, reverse=True)
        t_list = sorted(tgt_by_cat[key], key=lambda d: d.get("clocks") or 0, reverse=True)
        for b, t in zip(b_list, t_list):
            paired.append((b, t))
            base_matched.add(b["api_id"])
            tgt_matched.add(t["api_id"])
        if len(t_list) > len(b_list):
            new_dcs.extend(t_list[len(b_list):])
        elif len(b_list) > len(t_list):
            removed_dcs.extend(b_list[len(t_list):])

    # Remaining unmatched
    new_dcs.extend(dc for dc in tgt_remain if dc["api_id"] not in tgt_matched)
    removed_dcs.extend(dc for dc in base_remain if dc["api_id"] not in base_matched)

    return paired, new_dcs, removed_dcs


def _rank_and_attribute(
    paired: list[tuple[dict, dict]], db, baseline_id: int, target_id: int
) -> tuple[list[dict], list[dict], dict]:
    """Rank paired DCs by delta and compute bottleneck shifts."""
    import json as json_mod
    from analysis.topdc_service import _Engine

    rules_path = Path(__file__).resolve().parent.parent / "analysis" / "attribution_rules.json"
    engine = None
    if rules_path.exists():
        rules = json_mod.loads(rules_path.read_text(encoding="utf-8-sig"))
        engine = _Engine(rules)

    # Build percentile lookups
    base_pcts = _build_percentiles_for_compare(db, baseline_id, engine)
    tgt_pcts = _build_percentiles_for_compare(db, target_id, engine)

    deltas = []
    base_bn_counts: dict[str, int] = defaultdict(int)
    tgt_bn_counts: dict[str, int] = defaultdict(int)
    shifts: dict[tuple[str, str], int] = defaultdict(int)

    for base_dc, tgt_dc in paired:
        base_clocks = base_dc.get("clocks") or 0
        tgt_clocks = tgt_dc.get("clocks") or 0
        if not base_clocks and not tgt_clocks:
            continue
        delta = tgt_clocks - base_clocks
        cat = tgt_dc.get("category") or "Unlabeled"

        base_bn = ""
        tgt_bn = ""
        if engine:
            metric_keys = list(engine.layer1.keys())
            base_metrics = {k: base_dc.get(k) for k in metric_keys if base_dc.get(k) is not None}
            tgt_metrics = {k: tgt_dc.get(k) for k in metric_keys if tgt_dc.get(k) is not None}
            has_enough = True
            if base_metrics:
                base_attr = engine.attribute({"metrics": base_metrics}, base_pcts.get(cat, {}), has_enough)
                base_bn = base_attr["primary_bottleneck"]
            if tgt_metrics:
                tgt_attr = engine.attribute({"metrics": tgt_metrics}, tgt_pcts.get(cat, {}), has_enough)
                tgt_bn = tgt_attr["primary_bottleneck"]

        if base_bn:
            base_bn_counts[base_bn] += 1
        if tgt_bn:
            tgt_bn_counts[tgt_bn] += 1
        if base_bn or tgt_bn:
            shifts[(base_bn or "none", tgt_bn or "none")] += 1

        # Top metric changes
        key_changes = _top_metric_changes(base_dc, tgt_dc)

        deltas.append({
            "category": cat,
            "subcategory": tgt_dc.get("subcategory") or "",
            "api_id_baseline": base_dc["api_id"],
            "api_id_target": tgt_dc["api_id"],
            "match_method": "label",
            "baseline_clocks": base_clocks,
            "target_clocks": tgt_clocks,
            "delta": delta,
            "delta_pct": round(delta / max(base_clocks, 1) * 100, 1),
            "bottleneck_shift": {"from": base_bn, "to": tgt_bn},
            "key_metric_changes": key_changes,
        })

    deltas.sort(key=lambda d: d["delta"], reverse=True)
    regressions = [d for d in deltas if d["delta"] > 0]
    improvements = sorted([d for d in deltas if d["delta"] < 0], key=lambda d: d["delta"])

    # Bottleneck distribution
    shift_list = [
        {"from": k[0], "to": k[1], "count": v}
        for k, v in sorted(shifts.items(), key=lambda kv: -kv[1])
        if k[0] != k[1]
    ][:10]

    bottleneck_dist = {
        "baseline": dict(base_bn_counts),
        "target": dict(tgt_bn_counts),
        "shifts": shift_list,
    }

    return regressions, improvements, bottleneck_dist


def _build_percentiles_for_compare(db, snapshot_id: int, engine) -> dict[str, dict]:
    """Build per-category percentile thresholds from DB for attribution."""
    if not engine:
        return {}
    from data.query import get_draw_calls

    dcs = get_draw_calls(db, snapshot_id)
    by_cat: dict[str, list] = defaultdict(list)
    for dc in dcs:
        by_cat[dc.get("category") or "Unlabeled"].append(dc)

    metric_keys = list(engine.layer1.keys())
    result: dict[str, dict] = {}
    for cat, cat_dcs in by_cat.items():
        pcts: dict[str, dict] = {}
        for tier in engine.tiers:
            tier_vals: dict[str, float] = {}
            for metric in metric_keys:
                vals = sorted(v for dc in cat_dcs if (v := dc.get(metric)) is not None)
                if len(vals) >= 5:
                    idx = int(len(vals) * tier["threshold"])
                    tier_vals[metric] = vals[min(idx, len(vals) - 1)]
            pcts[f"metrics_{tier['name']}"] = tier_vals
        result[cat] = pcts
    return result


def _top_metric_changes(base_dc: dict, tgt_dc: dict, n: int = 3) -> dict:
    """Find top N metrics with largest absolute change."""
    _COMPARE_METRICS = [
        "clocks", "fragments_shaded", "vertices_shaded",
        "read_total_bytes", "write_total_bytes",
        "shaders_busy_pct", "shaders_stalled_pct",
        "tex_fetch_stall_pct", "tex_l1_miss_pct",
        "time_alus_working_pct",
    ]
    changes = []
    for m in _COMPARE_METRICS:
        bv = base_dc.get(m)
        tv = tgt_dc.get(m)
        if bv is not None and tv is not None:
            d = tv - bv
            changes.append((m, bv, tv, abs(d)))
    changes.sort(key=lambda x: x[3], reverse=True)
    return {
        c[0]: {"baseline": c[1], "target": c[2], "delta": c[2] - c[1]}
        for c in changes[:n]
    }
