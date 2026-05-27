"""gles_decompile_service.py — Decompile GLES IR3 disassembly to GLSL via LLM.

Reads:  sdp.db → VulkanSnapshotShaderData (shaderDisasm column)
Writes: <run_dir>/shaders/pipeline_{id}_{stage}.glsl

Mirrors C# GlesDisasmDecompileService logic:
  - Groups identical (disasm, stage) to avoid redundant LLM calls
  - Skips pipelines that already have .glsl on disk
  - Truncates long disasm to configurable max lines
  - Dedicated decompile cache (SHA-256 of IR3 disasm → GLSL) persists across sessions
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path

from analysis.llm_wrapper import get_llm, _load_config

# ── Decompile cache — persists IR3 disasm → GLSL across sessions ──────────────

class _DecompileCache:
    """Persistent dict cache: SHA-256(disasm+stage) → GLSL result.

    Unlike the ring-pool LLM cache (512 slots, shared with label calls),
    this cache grows unbounded and never evicts. Typical size is ~1000 entries
    (one per unique shader program × stage), each entry ~500 bytes → ~500KB on disk.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._dirty = False
        self._load()

    _PROMPT_VERSION = "v5"

    @staticmethod
    def _key(disasm_text: str, stage_label: str) -> str:
        h = hashlib.sha256((_DecompileCache._PROMPT_VERSION + "\n" + stage_label + "\n" + disasm_text).encode("utf-8")).digest()
        return h[:16].hex()

    def get(self, disasm_text: str, stage_label: str) -> str | None:
        key = self._key(disasm_text, stage_label)
        with self._lock:
            return self._data.get(key)

    def put(self, disasm_text: str, stage_label: str, glsl: str) -> None:
        key = self._key(disasm_text, stage_label)
        with self._lock:
            self._data[key] = glsl
            self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
                if self._path.exists():
                    self._path.unlink()
                tmp.rename(self._path)
            except Exception:
                pass

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            pass


# GL shader type enum values (same as C# GlesDbAdapter)
_GL_FRAGMENT_SHADER = 35632
_GL_VERTEX_SHADER = 35633
_GL_COMPUTE_SHADER = 37305

_STAGE_NAMES = {
    _GL_FRAGMENT_SHADER: "frag",
    _GL_VERTEX_SHADER: "vert",
    _GL_COMPUTE_SHADER: "comp",
}

_SYSTEM_PREAMBLE = (
    "You are an expert in Adreno GPU IR3 assembly (Qualcomm freedreno). "
    "Reconstruct the GLSL ES 3.0 shader source from the IR3 disassembly below.\n\n"
    "IR3 quick reference:\n"
    "  bary.f rD,N,r0.x — interpolate varying slot N (fragment: read varying)\n"
    "  sam/isam (xyzw)rD,rS,s#N,t#N — texture sample\n"
    "  mad.f32 rD,rA,rB,rC — rD=rA*rB+rC (FMA)\n"
    "  mul.f rD,rA,rB — multiply\n"
    "  add.f rD,rA,rB — add\n"
    "  c0..cN / c<a0.x + N> — uniform constants (vec4); indexed = uniform array/bone matrices\n"
    "  r0..rN — 32-bit float regs; hr — 16-bit half regs\n"
    "  mova a0.x, rS — load address register for indirect constant access\n"
    "  shl.b / shr.b — integer shift (often index calculation)\n"
    "  cov.f32s32 / cov.s32f32 — float↔int conversion\n\n"
    "Vertex shader output conventions:\n"
    "  - gl_Position is the final vec4 computed via MVP matrix multiply (4x4 mat × position).\n"
    "    Look for a sequence of mad.f32 instructions combining 4 uniform rows (e.g. c20-c23\n"
    "    or c<a0.x+0..15>) with position components — the result is gl_Position.\n"
    "  - Varyings (out variables) are other computed values passed to the fragment shader.\n"
    "  - Skinning pattern: bone_matrix[index] × position, then MVP × skinned_position.\n"
    "    Indexed constants c<a0.x + 0..15> in groups of 4 rows = one 4x4 bone matrix.\n\n"
    "Fragment shader output conventions:\n"
    "  - end — shader end; fragment color output is in r0.xyzw at the `end` instruction.\n\n"
    "Instructions:\n"
    "  1. Output ONLY valid GLSL ES 3.0 — no explanation, no markdown fences.\n"
    "  2. Use meaningful names: u_mvpMatrix, u_boneMatrices[], a_position, v_color, etc.\n"
    "  3. Declare all uniforms, attributes (in), and varyings (out) at the top.\n"
    "  4. For vertex shaders, ALWAYS assign gl_Position — identify the MVP transform output.\n"
    "     gl_Position MUST be the VERY LAST statement in main(). Nothing may follow it.\n"
    "     If varyings depend on clip-space coords, compute them into a local vec4 first,\n"
    "     assign all varyings using that local, then assign gl_Position = local as the\n"
    "     final line. Example pattern:\n"
    "       vec4 clipPos = u_mvpMatrix * worldPos;\n"
    "       v_depth = clipPos.z / clipPos.w;\n"
    "       gl_Position = clipPos;  // MUST be last\n"
    "  5. Add brief inline comments on non-obvious lines only.\n"
)


def _find_sdp_db(snapshot_dir: Path) -> Path | None:
    """Locate sdp.db from snapshot_dir using dc.json's sdp_name."""
    dc_path = snapshot_dir / "dc.json"
    if not dc_path.exists():
        return None
    try:
        dc = json.loads(dc_path.read_text(encoding="utf-8-sig"))
        sdp_name = dc.get("sdp_name", "")
    except Exception:
        return None
    if not sdp_name:
        return None

    # snapshot_dir = .../analysis/{run}/snapshot_N
    # sdp.db at    = .../sdp/{sdp_name}/sdp.db
    run_dir = snapshot_dir.parent
    analysis_dir = run_dir.parent
    sdp_root = analysis_dir.parent

    for base in [sdp_root / "sdp", sdp_root]:
        candidate = base / sdp_name / "sdp.db"
        if candidate.exists():
            return candidate
    return None


def _read_disasm_rows(db_path: Path, capture_id: int) -> list[tuple[int, int, str]]:
    """Read (pipelineID, shaderStage, shaderDisasm) from VulkanSnapshotShaderData."""
    rows: list[tuple[int, int, str]] = []
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        cur = conn.cursor()
        cur.execute(
            "SELECT pipelineID, shaderStage, shaderDisasm "
            "FROM VulkanSnapshotShaderData "
            "WHERE captureID = ? AND shaderDisasm IS NOT NULL AND shaderDisasm != ''",
            [capture_id],
        )
        for pid, stage, disasm in cur.fetchall():
            if disasm and disasm.strip():
                rows.append((int(pid), int(stage), disasm))
        conn.close()
    except Exception:
        pass
    return rows


def decompile_shaders(snapshot_dir: str | Path) -> dict:
    """Decompile GLES IR3 disasm → GLSL for all shaders in this snapshot.

    Returns: {"written": N, "skipped": N, "deduped": N, "errors": N}
    """
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)
    cfg = _load_config()

    if cfg.get("GlesLlmDecompile", "false").lower() != "true":
        return {"skipped_reason": "GlesLlmDecompile not enabled in config"}

    llm = get_llm()
    if not llm.is_enabled:
        return {"skipped_reason": "LLM not configured"}

    # Find sdp.db
    db_path = _find_sdp_db(snap)
    if db_path is None:
        return {"skipped_reason": "sdp.db not found"}

    # Get captureID from dc.json
    dc_path = snap / "dc.json"
    dc = json.loads(dc_path.read_text(encoding="utf-8-sig"))
    capture_id = dc.get("snapshot_id", 0)

    # Read disasm rows
    rows = _read_disasm_rows(db_path, capture_id)
    if not rows:
        return {"skipped_reason": "no shader disasm rows in VulkanSnapshotShaderData"}

    _log.info(f"[GlesDecompile] start: {len(rows)} shader rows, captureId={capture_id}")
    _t0 = _time.time()

    # Output directory
    from analysis.snapshot_layout import resolve_asset_dir
    shader_dir = resolve_asset_dir(snap, "shaders")
    shader_dir.mkdir(parents=True, exist_ok=True)

    max_lines = int(cfg.get("GlesLlmDecompileMaxLines", "2000"))
    max_concurrent = int(cfg.get("LlmMaxConcurrentRequests", "8"))

    # Group by (disasm_text, stage) — identical shaders share one LLM call
    groups: dict[tuple[str, int], list[int]] = {}
    skipped = 0
    for pid, stage, disasm in rows:
        stage_name = _STAGE_NAMES.get(stage, f"stage{stage:x}")
        out_path = shader_dir / f"pipeline_{pid}_{stage_name}.glsl"
        if out_path.exists():
            skipped += 1
            continue
        key = (disasm, stage)
        groups.setdefault(key, []).append(pid)

    if not groups:
        _log.info(f"[GlesDecompile] all {skipped} shaders already on disk, nothing to do")
        return {"written": 0, "skipped": skipped, "deduped": 0, "errors": 0}

    _log.info(f"[GlesDecompile] {len(groups)} unique shaders to decompile (skipped={skipped} on disk)")

    # Decompile cache — persists across sessions, keyed on IR3 disasm content
    work_dir = Path(cfg.get("WorkingDirectory", "D:/snapdragon"))
    dcache = _DecompileCache(work_dir / "decompile_cache.json")

    # Decompile each unique shader
    written = 0
    deduped = 0
    errors = 0
    empty_responses = 0
    cache_hits = 0
    error_list: list[dict] = []
    lock = threading.Lock()

    def _process(item: tuple[tuple[str, int], list[int]]) -> None:
        nonlocal written, deduped, errors, empty_responses, cache_hits
        (disasm_text, stage), pipeline_ids = item

        stage_label = {_GL_FRAGMENT_SHADER: "fragment", _GL_VERTEX_SHADER: "vertex"}.get(stage, "compute")

        # Check decompile cache first (keyed on raw IR3 disasm, not LLM prompt)
        cached = dcache.get(disasm_text, stage_label)
        if cached is not None:
            stage_name = _STAGE_NAMES.get(stage, f"stage{stage:x}")
            with lock:
                for pid in pipeline_ids:
                    out_path = shader_dir / f"pipeline_{pid}_{stage_name}.glsl"
                    out_path.write_text(cached, encoding="utf-8")
                    written += 1
                if len(pipeline_ids) > 1:
                    deduped += len(pipeline_ids) - 1
                cache_hits += 1
            _log.debug(f"[GlesDecompile] pipeline={pipeline_ids[0]} {stage_label} → cache HIT")
            return

        lines = disasm_text.split("\n")
        body = (
            "\n".join(lines[:max_lines]) + f"\n  ... ({len(lines) - max_lines} more lines truncated)"
            if len(lines) > max_lines
            else disasm_text
        )

        prompt = (
            _SYSTEM_PREAMBLE + "\n"
            f"// {stage_label} shader\n"
            + body + "\n\n"
            f"Reconstruct this as a clean GLSL ES 3.0 {stage_label} shader:"
        )

        result = llm.chat(prompt)
        if result is None:
            with lock:
                errors += 1
                error_list.append({"pipelines": pipeline_ids, "stage": stage_label, "reason": "llm_error"})
            _log.debug(f"[GlesDecompile] pipeline={pipeline_ids[0]} {stage_label} → LLM failed")
            return

        # Strip markdown fences
        result = re.sub(r"^```(?:glsl|c)?\n", "", result.strip())
        result = re.sub(r"\n```$", "", result)

        if not result.strip():
            with lock:
                empty_responses += 1
                error_list.append({"pipelines": pipeline_ids, "stage": stage_label, "reason": "empty_response"})
            _log.warning(f"[GlesDecompile] pipeline={pipeline_ids[0]} {stage_label} → LLM returned empty content, skipping")
            return

        # Store in decompile cache
        dcache.put(disasm_text, stage_label, result)

        stage_name = _STAGE_NAMES.get(stage, f"stage{stage:x}")
        with lock:
            for pid in pipeline_ids:
                out_path = shader_dir / f"pipeline_{pid}_{stage_name}.glsl"
                out_path.write_text(result, encoding="utf-8")
                written += 1
            if len(pipeline_ids) > 1:
                deduped += len(pipeline_ids) - 1
        _log.debug(f"[GlesDecompile] pipeline={pipeline_ids[0]} {stage_label} → OK ({len(pipeline_ids)} files)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        pool.map(_process, list(groups.items()))

    dcache.save()

    if error_list:
        _log.warning(f"[GlesDecompile] {len(error_list)} failures: {error_list}")

    # Update shaders.json with .glsl file paths in shader_stages
    _patch_shaders_json(snap, shader_dir)

    _log.info(f"[GlesDecompile] done: {_time.time()-_t0:.1f}s, written={written} deduped={deduped} cache_hits={cache_hits} errors={errors} empty={empty_responses}")
    return {"written": written, "skipped": skipped, "deduped": deduped, "cache_hits": cache_hits, "errors": errors, "empty_responses": empty_responses, "error_list": error_list}


def _patch_shaders_json(snap: Path, shader_dir: Path) -> None:
    """Patch shaders.json to add .glsl paths into shader_stages (GLES path)."""
    shaders_json = snap / "shaders.json"
    if not shaders_json.exists():
        return
    try:
        data = json.loads(shaders_json.read_text(encoding="utf-8-sig"))
    except Exception:
        return

    changed = False
    for dc in data.get("draw_calls", []):
        if dc.get("shader_stages"):
            continue
        pipeline_id = dc.get("pipeline_id")
        if pipeline_id is None:
            continue

        stages = []
        glsl_files = []
        for suffix, stage_name in [("frag", "fragment"), ("vert", "vertex"), ("comp", "compute")]:
            glsl = shader_dir / f"pipeline_{pipeline_id}_{suffix}.glsl"
            if glsl.exists():
                rel_path = f"../../shaders/{glsl.name}"
                stages.append({"stage": stage_name, "entry_point": "main", "file": rel_path})
                glsl_files.append(rel_path)

        if stages:
            dc["shader_stages"] = stages
            dc["shader_files"] = glsl_files + [f for f in (dc.get("shader_files") or []) if ".disasm" in f]
            changed = True

    if changed:
        shaders_json.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def decompile_single_shader(disasm_path: str | Path) -> dict:
    """Decompile a single .disasm file to .glsl, bypassing cache.

    Args:
        disasm_path: Absolute path to a pipeline_N_stage.disasm file.

    Returns: {"ok": True, "glsl_path": str, "glsl": str} or {"ok": False, "error": str}
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    p = Path(disasm_path)
    if not p.exists():
        return {"ok": False, "error": f"File not found: {p}"}

    # Parse stage from filename: pipeline_N_stage.disasm
    stem = p.stem  # pipeline_N_stage
    parts = stem.split("_")
    if len(parts) < 3:
        return {"ok": False, "error": f"Cannot parse stage from filename: {p.name}"}
    stage_name = parts[2].lower()
    stage_label = {"frag": "fragment", "vert": "vertex", "comp": "compute"}.get(stage_name)
    if not stage_label:
        return {"ok": False, "error": f"Unknown stage: {stage_name}"}

    cfg = _load_config()
    llm = get_llm()
    if not llm.is_enabled:
        return {"ok": False, "error": "LLM not configured"}

    disasm_text = p.read_text(encoding="utf-8-sig").strip()
    if not disasm_text:
        return {"ok": False, "error": "Disasm file is empty"}

    max_lines = int(cfg.get("GlesLlmDecompileMaxLines", "2000"))
    lines = disasm_text.split("\n")
    body = (
        "\n".join(lines[:max_lines]) + f"\n  ... ({len(lines) - max_lines} more lines truncated)"
        if len(lines) > max_lines
        else disasm_text
    )

    prompt = (
        _SYSTEM_PREAMBLE + "\n"
        f"// {stage_label} shader\n"
        + body + "\n\n"
        f"Reconstruct this as a clean GLSL ES 3.0 {stage_label} shader:"
    )

    _log.info(f"[GlesDecompile] single: {p.name} ({stage_label})")
    result = llm.chat(prompt)
    if result is None:
        return {"ok": False, "error": "LLM returned no response"}

    # Strip markdown fences
    result = re.sub(r"^```(?:glsl|c)?\n", "", result.strip())
    result = re.sub(r"\n```$", "", result)

    # Write .glsl file
    glsl_path = p.with_suffix(".glsl")
    glsl_path.write_text(result, encoding="utf-8")

    # Update decompile cache
    work_dir = Path(cfg.get("WorkingDirectory", "D:/snapdragon"))
    dcache = _DecompileCache(work_dir / "decompile_cache.json")
    dcache.put(disasm_text, stage_label, result)
    dcache.save()

    _log.info(f"[GlesDecompile] single done: {glsl_path.name}")
    return {"ok": True, "glsl_path": str(glsl_path), "glsl": result}
