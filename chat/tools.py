"""chat/tools.py — Tool definitions and executor for chat AI."""
from __future__ import annotations

import asyncio
import json as _json
import math
import statistics
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

    async def _execute_python(self, args: dict) -> Any:
        from chat.sandbox import execute_code
        code = args.get("code", "")
        snapshot_id_override = args.get("snapshot_id")
        snapshot_ids = [snapshot_id_override] if snapshot_id_override else self._last_snapshot_ids or []
        from pathlib import Path
        save_dir = str(Path(self._get_reports_dir(snapshot_ids)) / "img")
        result = await execute_code(code, snapshot_ids, save_dir=save_dir)
        image_paths = result.pop("image_paths", [])
        if result.get("images") and result["result"] is None:
            result["result"] = f"[Chart generated: {len(result['images'])} image(s) displayed inline]"
        if image_paths:
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
        from pathlib import Path
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

        return {"ok": True, "path": str(filepath), "filename": f"{filename}.md"}

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
