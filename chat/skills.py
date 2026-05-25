"""chat/skills.py — Markdown-based skill loader + executor."""
from __future__ import annotations

import importlib.util
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chat

SKILLS_DIR = Path(__file__).parent / "skills"

_skills: dict[str, "Skill"] = {}
_mtimes: dict[str, float] = {}


@dataclass
class Skill:
    id: str
    name: str
    slash_command: str
    button_label: str
    icon: str
    description: str
    prompt_template: str
    has_py: bool = False
    _py_path: Path | None = field(default=None, repr=False)
    _py_mtime: float = field(default=0, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slash_command": self.slash_command,
            "button_label": self.button_label,
            "icon": self.icon,
            "description": self.description,
        }

    def fill_template(self, snapshot_ids: list[int], params: dict | None = None) -> str:
        text = self.prompt_template
        if snapshot_ids:
            text = text.replace("{snapshot_id}", str(snapshot_ids[0]))
            text = text.replace("{snapshot_ids}", ",".join(str(s) for s in snapshot_ids))
        if params:
            for k, v in params.items():
                text = text.replace(f"{{{k}}}", str(v))
        return text


@dataclass
class SkillContext:
    db: Any
    snapshot_ids: list[int]
    params: dict


def load_skills() -> dict[str, Skill]:
    """Scan skills/ directory and load/reload .md files."""
    global _skills, _mtimes

    if not SKILLS_DIR.exists():
        return _skills

    for md_file in SKILLS_DIR.glob("*.md"):
        skill_id = md_file.stem
        mtime = md_file.stat().st_mtime
        if skill_id in _mtimes and _mtimes[skill_id] == mtime:
            continue

        skill = _parse_skill_md(md_file)
        if skill:
            _skills[skill_id] = skill
            _mtimes[skill_id] = mtime

    return _skills


def get_skills() -> list[Skill]:
    load_skills()
    return list(_skills.values())


def get_skill(slash_command: str) -> Skill | None:
    """Find skill by slash command (e.g. '/bottlenecks')."""
    load_skills()
    for s in _skills.values():
        if s.slash_command == slash_command:
            return s
    return None


def get_skill_by_id(skill_id: str) -> Skill | None:
    load_skills()
    return _skills.get(skill_id)


async def execute_skill(skill: Skill, snapshot_ids: list[int], params: dict | None = None) -> dict | None:
    """Execute skill's .py file if it exists, return structured result."""
    if not skill.has_py or not skill._py_path:
        return None

    import asyncio
    ctx = SkillContext(db=chat.get_db(), snapshot_ids=snapshot_ids, params=params or {})

    try:
        mod = _load_py_module(skill)
        if hasattr(mod, "run"):
            if asyncio.iscoroutinefunction(mod.run):
                return await mod.run(ctx)
            else:
                return await asyncio.to_thread(mod.run, ctx)
    except Exception as e:
        return {"error": str(e)}

    return None


def _parse_skill_md(path: Path) -> Skill | None:
    """Parse a skill .md file with YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
    if not fm_match:
        return None

    fm_text = fm_match.group(1)
    body = fm_match.group(2).strip()

    fm: dict[str, str] = {}
    for line in fm_text.splitlines():
        m = re.match(r'^(\w+)\s*:\s*(.+)$', line)
        if m:
            key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            if val.startswith("\\U") or val.startswith("\\u"):
                try:
                    val = val.encode().decode("unicode_escape")
                except (UnicodeDecodeError, ValueError):
                    pass
            fm[key] = val

    skill_id = path.stem
    py_path = path.with_suffix(".py")
    has_py = py_path.exists()

    return Skill(
        id=skill_id,
        name=fm.get("name", skill_id),
        slash_command=fm.get("slash_command", f"/{skill_id}"),
        button_label=fm.get("button_label", fm.get("name", skill_id)),
        icon=fm.get("icon", ""),
        description=fm.get("description", ""),
        prompt_template=body,
        has_py=has_py,
        _py_path=py_path if has_py else None,
    )


def _load_py_module(skill: Skill):
    """Load or reload skill .py module."""
    path = skill._py_path
    mtime = path.stat().st_mtime
    mod_name = f"chat_skill_{skill.id}"

    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    skill._py_mtime = mtime
    return mod
