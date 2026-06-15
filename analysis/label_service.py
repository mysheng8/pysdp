"""
label_service.py — DrawCall labeling: LLM (per pipeline) with rule-based fallback.

Mirrors C# DrawCallLabelService.Label():
  1. If LLM is configured and shader files exist for the pipeline → call LLM
     (result cached in-process by pipeline_id and on disk via llm_wrapper cache)
  2. Fall back to keyword + geometry heuristic rules when LLM is unavailable,
     returns empty/error, or shader files are missing.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Keyword rules (same order/priority as C# DrawCallLabelService.Rules) ──────
_RULES: list[tuple[list[str], str, str]] = [
    (["shadow", "planar", "shadowmap", "shadowdepth", "shadowpass", "shadowcaster"],
     "Shadow", "Shadow pass"),
    (["ui", "hud", "canvas", "glyph", "font", "widget", "overlay", "icon", "button", "menu"],
     "UI", "UI rendering"),
    (["particle", "vfx", "emitter", "ribbon", "trail", "spark", "smoke", "fire", "explosion", "distort"],
     "VFX", "Particle / VFX"),
    (["character", "skin", "skinning", "skinnedmesh", "morph", "morphtarget", "deform",
      "hair", "cloth", "body", "player", "hero", "humanoid", "avatar", "face", "eye"],
     "Character", "Character rendering"),
    (["terrain", "landscape", "heightfield", "heightmap"],
     "Terrain", "Terrain rendering"),
    (["blur", "bloom", "tonemap", "tonemapping", "ssao", "ao", "dof", "composite",
      "resolve", "postprocess", "taa", "fxaa", "msaa", "temporal", "upscale", "blit",
      "lut", "vignette", "chromatic", "grain", "sharpen", "motion", "velocity",
      "denoise", "irradiance", "specular", "prefilter", "brdf", "ssr", "reflection"],
     "PostProcess", "Post-process pass"),
    (["sky", "skybox", "water", "ocean", "grass", "foliage",
      "ground", "scene", "mesh", "world", "indoor", "outdoor", "building", "road"],
     "Scene", "Scene rendering"),
]


def _make_label(category: str, subcategory: str, detail: str,
                reason_tags: list[str], confidence: float,
                label_source: str = "rule") -> dict:
    return {
        "category":     category,
        "subcategory":  subcategory,
        "detail":       detail,
        "reason_tags":  reason_tags,
        "confidence":   confidence,
        "label_source": label_source,
    }


def _label_dc(dc: dict) -> dict:
    """Mirror C# DrawCallLabelService.LabelByRules()."""
    api_name   = dc.get("api_name", "")
    vc         = dc.get("vertex_count", 0) or 0
    ic         = dc.get("index_count", 0) or 0
    inst       = dc.get("instance_count", 0) or 0
    rts        = dc.get("render_targets") or []

    is_compute   = api_name == "vkCmdDispatch"
    # fullscreen quad: 3-6 verts, no index buffer, <=1 vertex buffer — same as C#
    is_fullscreen = (not is_compute
                     and vc in (3, 4, 6)
                     and ic == 0)

    # ── R1: RG-encoded shadow map (VSM/ESM) ───────────────────────────────────
    # Color-only 2-channel RT (R8G8/R16G16/R32G32), no depth, real geometry
    if not is_fullscreen and not is_compute and rts:
        has_depth    = any(r.get("attachment_type") in ("Depth", "Stencil", "DepthStencil") for r in rts)
        has_color    = any(r.get("attachment_type") == "Color" for r in rts)
        all_color_rg = all(
            r.get("attachment_type") != "Color" or _is_rg_format(r.get("format") or r.get("format_name") or "")
            for r in rts
        )
        if has_color and not has_depth and all_color_rg:
            return _make_label("Scene(Shadow)", "DepthOnly",
                               "Encoded shadow map (RG depth)",
                               ["shadow_depth_write"], 0.75)

    # ── R1a: Depth-only RT ────────────────────────────────────────────────────
    if rts:
        has_depth  = any(r.get("attachment_type") in ("Depth", "Stencil", "DepthStencil") for r in rts)
        has_color  = any(r.get("attachment_type") == "Color" for r in rts)
        if has_depth and not has_color:
            return _make_label("Scene(Shadow)", "DepthOnly",
                               "Depth-only / shadow map",
                               ["shadow_depth_write"], 0.75)

    # ── Keyword matching on shader entry points ───────────────────────────────
    stages = dc.get("shader_stages") or []
    entry_points = [
        (s.get("entry_point") or "").lower()
        for s in stages
        if (s.get("entry_point") or "") not in ("", "main")
    ]
    if entry_points:
        combined = " ".join(entry_points)
        for keywords, category, detail_hint in _RULES:
            for kw in keywords:
                if kw in combined:
                    return _make_label(category, "", detail_hint,
                                       [re.sub(r"[^a-z0-9]+", "_", kw).strip("_")], 0.65)

    # ── R2: Compute ───────────────────────────────────────────────────────────
    if is_compute:
        # Character compute: skinning / morph-target deformation shaders
        _CHARACTER_COMPUTE_KW = (
            "skin", "skinning", "skinnedmesh", "morph", "morphtarget", "deform",
            "cloth", "hair", "character", "player", "humanoid",
        )
        combined_cs = " ".join(entry_points)
        if any(kw in combined_cs for kw in _CHARACTER_COMPUTE_KW):
            return _make_label("Character", "Compute",
                               "Character skinning / morph compute",
                               ["skinned_mesh", "compute_dispatch"], 0.80)
        return _make_label("PostProcess", "Compute", "Compute dispatch",
                            ["compute_dispatch"], 0.70)

    # ── R3: Fullscreen quad → PostProcess ─────────────────────────────────────
    if is_fullscreen:
        return _make_label("PostProcess", "Fullscreen", "Fullscreen post-process quad",
                            ["tone_mapping"], 0.65)

    # ── R4: Geometry-scale heuristics ─────────────────────────────────────────
    if rts:
        has_depth = any(r.get("attachment_type") in ("Depth", "Stencil", "DepthStencil") for r in rts)
        has_color = any(r.get("attachment_type") == "Color" for r in rts)

        # UI: color-only, RGBA8, no depth
        if has_color and not has_depth:
            color_rts = [r for r in rts if r.get("attachment_type") == "Color"]
            if all(_is_rgba8_format(r.get("format") or r.get("format_name") or "") for r in color_rts):
                return _make_label("UI", "UICanvas", "UI / 2D overlay",
                                    ["ui_canvas"], 0.65)

        # VFX: color+depth, low verts-per-instance (billboard quads), many instances
        if has_color and has_depth and inst > 1:
            verts_per_inst = vc // inst if inst > 0 else vc
            if verts_per_inst <= 6:
                return _make_label("VFX", "Particle", "Particle / billboard",
                                    ["particle_billboard"], 0.65)

    # ── Default ───────────────────────────────────────────────────────────────
    return _make_label("Scene", "Opaque", "Scene rendering",
                        ["opaque_geometry"], 0.60)


def _is_rg_format(fmt: str) -> bool:
    """2-channel color format used for VSM/ESM encoded shadow maps."""
    f = fmt.upper()
    return (f.startswith("R8G8") or f.startswith("R16G16") or f.startswith("R32G32"))


def _is_rgba8_format(fmt: str) -> bool:
    f = fmt.upper()
    return "R8G8B8A8" in f or "B8G8R8A8" in f


# ── LLM labeling ──────────────────────────────────────────────────────────────

_ALLOWED_CATEGORIES = [
    "Scene", "Terrain", "Character", "Shadow", "PostProcess", "VFX", "UI", "Other",
    "Scene(Shadow)", "Terrain(Shadow)", "Character(Shadow)",
]

# In-process per-pipeline cache: pipeline_id → label dict  (mirrors C# _llmCache)
_pipeline_llm_cache: dict[int, dict] = {}


def _extract_shader_sections(src: str, resource_limit: int = 2500) -> str:
    """Mirror C# DrawCallLabelService.ExtractRelevantShaderSections().

    Section 1 — resource declarations: Texture/Buffer/Sampler globals and small
      per-draw cbuffers (b0/b1/b2). The large shared per-pass cbuffer is skipped
      entirely — it is noise for classification (R3 rule in prompt).
    Section 2 — main() / frag_main() / vert_main() / comp_main() body only.
    """
    lines = src.split("\n")
    resources: list[str] = []
    main_body: list[str] = []

    # ── Section 1 ─────────────────────────────────────────────────────────────
    in_skipped  = False
    brace_depth = 0
    res_chars   = 0
    res_done    = False

    for raw in lines:
        line = raw.rstrip()

        if in_skipped:
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                in_skipped = False
            continue

        # Large global cbuffer: anything NOT on register b0/b1/b2 → skip
        if line.startswith("cbuffer ") and \
                ": register(b0)" not in line and \
                ": register(b1)" not in line and \
                ": register(b2)" not in line:
            brace_depth = 1 if "{" in line else 0
            in_skipped  = True
            resources.append("// [large shared cbuffer skipped — see R3]")
            continue

        # Skip static local variable definitions (noise)
        if line.startswith("static "):
            continue

        if not res_done:
            resources.append(line)
            res_chars += len(line) + 1
            if res_chars >= resource_limit:
                res_done = True
                resources.append("// ... (resource section truncated)")

    # ── Section 2 ─────────────────────────────────────────────────────────────
    main_start = -1
    for i, raw in enumerate(lines):
        t = raw.lstrip()
        if (t.startswith("void main(") or t.startswith("void frag_main(") or
                t.startswith("void vert_main(") or t.startswith("void comp_main(")):
            main_start = i
            break

    if main_start >= 0:
        chars = 0
        for i in range(main_start, len(lines)):
            main_body.append(lines[i])
            chars += len(lines[i]) + 1
            if chars >= 10000:
                main_body.append("// ... (main body truncated)")
                break

    # Detect empty/trivial shader (only braces + forwarder calls like `frag_main();`)
    main_str = "\n".join(main_body)
    stripped = re.sub(
        r"^\s*(\{|\}|[a-z]+_main\s*\(\s*\)\s*;|void\s+\w+\s*\(.*?\))\s*$",
        "", main_str, flags=re.MULTILINE,
    ).strip()
    if len(stripped) < 5:
        return "(empty shader — no executable statements in main)"

    return (
        "// -- Resource declarations (textures, buffers, small cbuffers) --\n"
        + "\n".join(resources)
        + "\n\n// -- main() / frag_main() body --\n"
        + main_str
    )


def _load_shader_code(snap: Path, pipeline_id: int) -> str:
    """Load pipeline shader files and extract relevant sections."""
    from analysis.snapshot_layout import resolve_asset_dir
    shader_dir = resolve_asset_dir(snap, "shaders")
    if not shader_dir.is_dir():
        return "(shader directory not found — shaders may not have been extracted yet)"

    files = sorted(
        list(shader_dir.glob(f"pipeline_{pipeline_id}_*.hlsl")) +
        list(shader_dir.glob(f"pipeline_{pipeline_id}_*.glsl"))
    )
    if not files:
        return "(no decompiled shader files — only raw SPIR-V available)"

    # If all files are tiny stubs (<200 bytes each) skip LLM entirely
    total_size = sum(f.stat().st_size for f in files)
    if total_size < 200 * len(files):
        return "(empty shader — trivial stub, no rendering operations)"

    parts: list[str] = []
    for f in files:
        parts.append(f"// === {f.name} ===")
        src = f.read_text(encoding="utf-8", errors="replace")
        parts.append(_extract_shader_sections(src))
    return "\n\n".join(parts)


def _build_llm_prompt(dc: dict, shader_code: str) -> str:
    """Mirror C# DrawCallLabelService.BuildPrompt() exactly."""
    cat_list  = "/".join(_ALLOWED_CATEGORIES)
    api_name  = dc.get("api_name", "")
    vc        = dc.get("vertex_count",  0) or 0
    ic        = dc.get("index_count",   0) or 0
    inst      = dc.get("instance_count",0) or 0
    textures  = dc.get("texture_ids")  or dc.get("textures") or []
    stages    = dc.get("shader_stages") or []
    stage_str = ", ".join(
        f"{s.get('stage','')}:{s.get('entry_point','')}" for s in stages
    ) or "none"
    rts = dc.get("render_targets") or []

    verts_per_inst = (vc // inst) if inst > 0 else vc

    out: list[str] = [
        "Classify this Vulkan draw call. Reply with JSON only.",
        "",
        f"API:{api_name}",
        f"Verts:{vc}  Indices:{ic}  Instances:{inst}"
        f"  VertsPerInst:{verts_per_inst}  Textures:{len(textures)}",
        f"Shaders: {stage_str}",
    ]

    if rts:
        out.append("Render targets:")
        for rt in rts:
            sz  = (f" {rt.get('width')}x{rt.get('height')}"
                   if rt.get("width") and rt.get("height") else "")
            fmt = f" {rt.get('format_name','')}" if rt.get("format_name") else ""
            out.append(f"  [{rt.get('attachment_index','')}]"
                       f"{rt.get('attachment_type','')}{sz}{fmt}")

    # Mesh geometry summary (from meshes.json stats)
    mesh = dc.get("mesh_stats") or {}
    if mesh:
        bbox_min = mesh.get("bbox_min")
        bbox_max = mesh.get("bbox_max")
        bbox_str = ""
        if bbox_min and bbox_max:
            bbox_str = (f"  bbox:[{bbox_min[0]:.3f},{bbox_min[1]:.3f},{bbox_min[2]:.3f}]"
                        f"→[{bbox_max[0]:.3f},{bbox_max[1]:.3f},{bbox_max[2]:.3f}]")
        out += [
            "Mesh:",
            f"  vertices:{mesh.get('vertex_count',0)}  faces:{mesh.get('face_count',0)}"
            f"  normals:{mesh.get('normal_count',0)}  uvs:{mesh.get('uv_count',0)}"
            + bbox_str,
        ]

    # Texture descriptions (from textures.json VLM analysis)
    tex_descs = dc.get("texture_descriptions") or []
    if tex_descs:
        out.append("Textures:")
        for td in tex_descs:
            sz  = f" {td['width']}x{td['height']}" if td.get("width") and td.get("height") else ""
            desc = td.get("description") or ""
            desc_short = desc[:120].replace("\n", " ") if desc else "(no description)"
            out.append(f"  [{td.get('texture_id','')}]{sz} {desc_short}")

    out += [
        "",
        "Category definitions:",
        "  Scene              — static world geometry rendered to the main HDR buffer (buildings, props).",
        "  Terrain            — heightfield/ground mesh, uses virtual/heightfield textures or terrain lightmaps.",
        "  Character          — dynamic skinned mesh (players, crowd) with per-object SH probe lighting, no lightmap UVs.",
        "  PostProcess        — fullscreen-quad pass that reads from a previously rendered texture and writes to an RT.",
        "  VFX                — particle systems, billboard quads, or other effect geometry (many small instances).",
        "  UI                 — 2D interface elements, no depth RT, typically RGBA8 color RT.",
        "  Other              — compute dispatches.",
        "  Scene(Shadow)      — shadow map pass rendering SCENE geometry into a depth or encoded-depth RT.",
        "  Terrain(Shadow)    — shadow map pass rendering TERRAIN geometry into a depth or encoded-depth RT.",
        "  Character(Shadow)  — shadow map pass rendering CHARACTER geometry into a depth or encoded-depth RT.",
        "",
        "Rules (apply in order):",
        "R1 [Render targets first — HIGHEST PRIORITY, overrides everything else]",
        "  ** RULE R1a: Depth-only RT, no Color RT → SHADOW MAP PASS. Determine object type from shader and output",
        "     the matching shadow category: 'Scene(Shadow)', 'Terrain(Shadow)', or 'Character(Shadow)'.",
        "     Default to 'Scene(Shadow)' if indeterminate.",
        "  ** RULE R1b: Color RT with only 2 channels (R8G8/R16G16/R32G32/RG prefix), AND no Depth RT,",
        "     AND real geometry (VertexCount>6 or IndexBuffer) → ENCODED DEPTH SHADOW MAP (VSM/ESM).",
        "     Output matching shadow category. Do NOT output bare 'Scene'.",
        "  ** RULE R1b exception: VertexCount 3-6 AND no IndexBuffer → fullscreen shadow blur → PostProcess.",
        "  Color-only RT, no Depth, R8G8B8A8/B8G8R8A8 → UI",
        "  Color-only RT, no Depth, HDR/float format, screen-size → PostProcess",
        "  Color HDR + Depth, VertsPerInst<=6, many instances → VFX (particles/quads)",
        "  Color HDR + Depth, VertsPerInst>6, normal geometry → Scene/Character/Terrain",
        "R2 [Shader main() for Scene/Character/Terrain]",
        "  Scene:     lightmap textures sampled in main() using TEXCOORD2 UVs (irradiance/sky-visibility/baked).",
        "  Character: per-object SH probe Buffer<float4> loaded via per-instance offset — no lightmap sampling.",
        "             Also: per-instance cosmetic data (tint/recolor) or skinned vertex buffer offset.",
        "             vkCmdDispatch with entry point containing skin/skinning/morph/deform/cloth/hair → Character.",
        "  Terrain:   terrain-specific expression shader or virtual/heightfield texture sampling in main().",
        "R3 [cbuffer vs texture priority — CRITICAL]",
        "  The first/global cbuffer (b0 or b11) is a SHARED per-pass buffer present in EVERY draw call.",
        "  It typically contains terrain, shadow, lighting structs — ALL irrelevant unless READ in main().",
        "  IGNORE global cbuffer struct declarations. Only what is used inside main()/frag_main() matters.",
        "  TRUST ORDER: texture/sampler calls in main() > secondary cbuffers/typed buffers > global cbuffer.",
        "  If a typed Buffer<float4> or ByteAddressBuffer is loaded via a per-instance offset in main(),",
        "  that is a STRONG signal of Character (dynamic SH probe data). Terrain is always static/baked.",
        "",
        "Shader (analyze what is actually computed in main()):",
        shader_code,
        "",
        f"Categories: {cat_list}",
        "IMPORTANT RESTRICTIONS:",
        "  - 'Other' is for vkCmdDispatch compute that has NO clear category signal.",
        "    If the compute shader entry point or code mentions skin/skinning/morph/deform/cloth/hair",
        "    → use 'Character' (subcategory 'Compute'), NOT 'Other'.",
        "  - NEVER use 'Other' for vkCmdDraw or vkCmdDrawIndexed.",
        "  - If R1a/R1b apply (shadow map RT), you MUST use a shadow category.",
        "    Do NOT fall back to 'Other' — default to 'Scene(Shadow)'.",
        "  - If the shader mentions 'shadow', 'planar shadow', or 'depth encoding', category MUST end in '(Shadow)'.",
        "",
        f"Categories: {cat_list}",
        "Subcategory examples: Opaque, Transparent, DepthOnly, SkinMesh, GaussianBlur, ToneMapping, SSAO,"
        " Bloom, TAA, ShadowDepth, ParticleBillboard, UICanvas.",
        "ReasonTags — pick 1-4: pbr_material, multi_texture_blend, high_uv_sampling, skinned_mesh, morphing,"
        " instanced_draw, compute_dispatch, gaussian_blur, tone_mapping, ssao, bloom, taa, shadow_depth_write,"
        " shadow_pcf_sample, particle_billboard, trail_ribbon, ui_canvas, font_glyph, depth_only,"
        " opaque_geometry, transparent_geometry, large_render_target, mrt_output.",
        "Output JSON only, no markdown, confidence in [0,1]:",
        '{"category":"<category>","subcategory":"<subcategory>","detail":"<3-8 word description>",'
        '"reason_tags":["tag1"],"confidence":0.9}',
    ]
    return "\n".join(out)


def _parse_llm_response(text: str) -> dict | None:
    """Parse LLM JSON response, return label dict or None on failure."""
    if not text:
        return None
    # Strip markdown fences
    t = text.strip()
    if t.startswith("```"):
        start = t.find("\n") + 1
        end   = t.rfind("```")
        if end > start:
            t = t[start:end].strip()
    # Find JSON object
    start = t.find("{")
    end   = t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None

    raw_cat = (obj.get("category") or "").strip()
    # Match category (exact then partial then shadow compound)
    matched = next((c for c in _ALLOWED_CATEGORIES if c.lower() == raw_cat.lower()), None)
    if not matched:
        matched = next((c for c in _ALLOWED_CATEGORIES if c.lower() in raw_cat.lower()), None)
    if not matched:
        m = re.match(r"^(\w+)\(Shadow\)$", raw_cat, re.IGNORECASE)
        if m:
            base = m.group(1)
            if any(c.lower() == base.lower() for c in _ALLOWED_CATEGORIES):
                matched = base.capitalize() + "(Shadow)"
    if not matched:
        return None

    tags = obj.get("reason_tags") or []
    if isinstance(tags, str):
        tags = [tags]

    return _make_label(
        category    = matched,
        subcategory = (obj.get("subcategory") or "").strip(),
        detail      = (obj.get("detail") or "").strip(),
        reason_tags = [str(t) for t in tags],
        confidence  = float(min(1.0, max(0.0, obj.get("confidence") or 0.85))),
        label_source = "llm",
    )


def _label_dc_with_llm(dc: dict, snap: Path,
                        mesh_stats: dict | None = None,
                        tex_descs: list | None = None) -> dict | None:
    """Try LLM labeling for a DC. Returns label dict or None if LLM unavailable/fails."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    from analysis.llm_wrapper import get_llm
    llm = get_llm()
    if not llm.is_enabled:
        return None

    pipeline_id: int | None = dc.get("pipeline_id")
    if not pipeline_id:
        return None

    # In-process pipeline cache (same pipeline → same shaders → same label)
    if pipeline_id in _pipeline_llm_cache:
        cached = dict(_pipeline_llm_cache[pipeline_id])
        cached["label_source"] = "cache"
        _log.debug(f"[Label] dc={dc.get('api_id')} pipeline={pipeline_id} → pipeline_cache HIT: {cached.get('category')}")
        return cached

    shader_code = _load_shader_code(snap, pipeline_id)
    if shader_code.startswith("("):
        _log.debug(f"[Label] dc={dc.get('api_id')} pipeline={pipeline_id} → no shader: {shader_code[:60]}")
        return None

    # Enrich dc with mesh/texture context for prompt building
    enriched = dict(dc)
    if mesh_stats:
        enriched["mesh_stats"] = mesh_stats
    if tex_descs:
        enriched["texture_descriptions"] = tex_descs

    prompt   = _build_llm_prompt(enriched, shader_code)
    response = llm.chat(prompt)
    if not response:
        _log.debug(f"[Label] dc={dc.get('api_id')} pipeline={pipeline_id} → LLM returned None")
        return None

    label = _parse_llm_response(response)
    if label:
        _pipeline_llm_cache[pipeline_id] = label
        _log.debug(f"[Label] dc={dc.get('api_id')} pipeline={pipeline_id} → LLM: {label.get('category')}/{label.get('subcategory')}")
    else:
        _log.debug(f"[Label] dc={dc.get('api_id')} pipeline={pipeline_id} → LLM parse failed")
    return label


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_label_json(snapshot_dir: str | Path, db=None) -> Path:
    """
    Read dc.json from snapshot_dir, classify every draw call, write label.json.
    Returns the path to the written file.
    """
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)
    dc_path = snap / "dc.json"
    if not dc_path.exists():
        raise FileNotFoundError(f"dc.json not found in {snap}")

    dc_data = json.loads(dc_path.read_text(encoding="utf-8-sig"))
    draw_calls: list[dict[str, Any]] = dc_data.get("draw_calls", [])
    _log.info(f"[Label] start: {len(draw_calls)} draw calls", context={"dir": str(snap.name)})
    _t0 = _time.time()

    # Clear per-snapshot pipeline cache so different snapshots don't share entries
    _pipeline_llm_cache.clear()

    from analysis.snapshot_layout import resolve_asset_dir

    # ── Load mesh stats index: api_id → stats dict ─────────────────────────────
    _mesh_index: dict[int, dict] = {}
    meshes_json = resolve_asset_dir(snap, "meshes") / "meshes.json"
    if meshes_json.exists():
        try:
            mdata = json.loads(meshes_json.read_text(encoding="utf-8-sig"))
            for m in (mdata.get("meshes") or []):
                aid = m.get("api_id")
                if aid is not None:
                    _mesh_index[aid] = m
        except Exception:
            pass

    # ── Load texture description index: texture_id → entry dict ───────────────
    _tex_index: dict[int, dict] = {}
    tex_json = resolve_asset_dir(snap, "textures") / "textures.json"
    if tex_json.exists():
        try:
            tdata = json.loads(tex_json.read_text(encoding="utf-8-sig"))
            for t in (tdata.get("textures") or []):
                tid = t.get("texture_id")
                if tid is not None:
                    _tex_index[tid] = t
        except Exception:
            pass

    from analysis.llm_wrapper import _load_config
    cfg = _load_config()
    max_workers = int(cfg.get("LlmMaxConcurrentRequests", "8"))

    labeled: list[dict | None] = [None] * len(draw_calls)

    def _process(idx: int, dc: dict) -> None:
        api_id = dc.get("api_id")
        mesh_stats = _mesh_index.get(api_id) if api_id else None
        tex_ids    = dc.get("texture_ids") or []
        tex_descs  = [_tex_index[tid] for tid in tex_ids if tid in _tex_index] or None
        label = _label_dc_with_llm(dc, snap, mesh_stats, tex_descs) or _label_dc(dc)
        labeled[idx] = {
            "dc_id":  dc.get("dc_id"),
            "api_id": api_id,
            "label":  label,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process, i, dc) for i, dc in enumerate(draw_calls)]
        concurrent.futures.wait(futures)

    labeled = [e for e in labeled if e is not None]

    llm_count = sum(1 for e in labeled if e and e.get("label", {}).get("label_source") == "llm")
    cache_count = sum(1 for e in labeled if e and e.get("label", {}).get("label_source") == "cache")
    rule_count = sum(1 for e in labeled if e and e.get("label", {}).get("label_source") == "rule")
    _log.info(f"[Label] done: {_time.time()-_t0:.1f}s, llm={llm_count} cache={cache_count} rule={rule_count}")

    snapshot_id: int = dc_data.get("snapshot_id", 0)
    sdp_name: str    = dc_data.get("sdp_name", "")

    out = {
        "schema_version": "3.0",
        "snapshot_id":    snapshot_id,
        "sdp_name":       sdp_name,
        "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_dc_count": len(labeled),
        "draw_calls":     labeled,
    }

    out_path = snap / "label.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    if db is not None:
        _persist_labels_to_db(db, snapshot_id, sdp_name, snap, labeled)

    return out_path


def _persist_labels_to_db(db, snapshot_id: int, sdp_name: str, snap: Path,
                           labeled: list[dict]) -> None:
    conn = db.conn()
    run_name = snap.parent.name
    snapshot_dir_str = str(snap.resolve())
    snap_index = snapshot_id  # preserve C# original index before any reassignment

    # Resolve ID conflicts: same dir → reuse ID; same ID different dir → allocate new
    existing_by_dir = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE snapshot_dir = ?", [snapshot_dir_str]
    ).fetchone()
    if existing_by_dir:
        snapshot_id = existing_by_dir[0]
    else:
        existing_by_id = conn.execute(
            "SELECT snapshot_dir FROM snapshots WHERE snapshot_id = ?", [snapshot_id]
        ).fetchone()
        if existing_by_id:
            max_id = conn.execute("SELECT MAX(snapshot_id) FROM snapshots").fetchone()[0] or 0
            snapshot_id = max_id + 1
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, sdp_name, run_name, snapshot_dir, ingested_at, snap_index) "
            "VALUES (?,?,?,?,?,?)",
            [snapshot_id, sdp_name, run_name, snapshot_dir_str,
             datetime.now(timezone.utc).isoformat(), snap_index],
        )

    dc_path = snap / "dc.json"
    if dc_path.exists():
        dc_data = json.loads(dc_path.read_text(encoding="utf-8-sig"))
        dc_rows = [
            (
                snapshot_id,
                dc.get("api_id", 0),
                dc.get("dc_id", 0),
                dc.get("api_name", ""),
                dc.get("pipeline_id"),
                dc.get("vertex_count", 0),
                dc.get("index_count", 0),
                dc.get("instance_count", 0),
                dc.get("first_vertex", 0),
                dc.get("first_index", 0),
                dc.get("vertex_offset", 0),
                dc.get("first_instance", 0),
                dc.get("draw_count", 0),
                dc.get("group_count_x", 0),
                dc.get("group_count_y", 0),
                dc.get("group_count_z", 0),
            )
            for dc in dc_data.get("draw_calls", [])
        ]
        if dc_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO draw_calls "
                "(snapshot_id, api_id, dc_id, api_name, pipeline_id, "
                " vertex_count, index_count, instance_count, first_vertex, first_index, "
                " vertex_offset, first_instance, draw_count, "
                " group_count_x, group_count_y, group_count_z) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                dc_rows,
            )

    labeled_at = datetime.now(timezone.utc).isoformat()
    label_rows = []
    for item in labeled:
        api_id = item.get("api_id")
        if api_id is None:
            continue
        lb = item.get("label") or {}
        label_rows.append((
            snapshot_id,
            api_id,
            lb.get("category", ""),
            lb.get("subcategory", ""),
            lb.get("detail", ""),
            json.dumps(lb.get("reason_tags") or []),
            float(lb.get("confidence", 0.0)),
            lb.get("label_source", "rule"),
            None,
            None,
            labeled_at,
        ))

    if label_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO labels "
            "(snapshot_id, api_id, category, subcategory, detail, reason_tags, "
            "confidence, label_source, bottleneck_text, embedding, labeled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            label_rows,
        )


def relabel_single_dc(snapshot_dir: str | Path, api_id: int, db=None) -> dict:
    """Re-label a single draw call by api_id using LLM (bypasses pipeline cache).

    Returns: {"ok": True, "label": {...}} or {"ok": False, "error": str}
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)
    dc_path = snap / "dc.json"
    if not dc_path.exists():
        return {"ok": False, "error": f"dc.json not found in {snap}"}

    dc_data = json.loads(dc_path.read_text(encoding="utf-8-sig"))
    draw_calls = dc_data.get("draw_calls", [])

    # Find the target DC
    target_dc = None
    for dc in draw_calls:
        if dc.get("api_id") == api_id:
            target_dc = dc
            break
    if target_dc is None:
        return {"ok": False, "error": f"api_id {api_id} not found in dc.json"}

    # Clear pipeline cache for this pipeline so LLM is forced
    pipeline_id = target_dc.get("pipeline_id")
    if pipeline_id in _pipeline_llm_cache:
        del _pipeline_llm_cache[pipeline_id]

    from analysis.snapshot_layout import resolve_asset_dir

    # Load mesh/texture context
    mesh_stats = None
    meshes_json = resolve_asset_dir(snap, "meshes") / "meshes.json"
    if meshes_json.exists():
        try:
            mdata = json.loads(meshes_json.read_text(encoding="utf-8-sig"))
            for m in (mdata.get("meshes") or []):
                if m.get("api_id") == api_id:
                    mesh_stats = m
                    break
        except Exception:
            pass

    tex_descs = None
    tex_json = resolve_asset_dir(snap, "textures") / "textures.json"
    if tex_json.exists():
        try:
            tdata = json.loads(tex_json.read_text(encoding="utf-8-sig"))
            _tex_idx = {t["texture_id"]: t for t in (tdata.get("textures") or []) if "texture_id" in t}
            tex_ids = target_dc.get("texture_ids") or []
            tex_descs = [_tex_idx[tid] for tid in tex_ids if tid in _tex_idx] or None
        except Exception:
            pass

    _log.info(f"[Label] relabel single: api_id={api_id} pipeline={pipeline_id}")
    label = _label_dc_with_llm(target_dc, snap, mesh_stats, tex_descs)
    if label is None:
        label = _label_dc(target_dc)

    # Update label.json on disk
    label_path = snap / "label.json"
    if label_path.exists():
        try:
            ldata = json.loads(label_path.read_text(encoding="utf-8-sig"))
            for entry in ldata.get("draw_calls", []):
                if entry.get("api_id") == api_id:
                    entry["label"] = label
                    break
            label_path.write_text(json.dumps(ldata, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # Update DuckDB
    if db is not None:
        try:
            conn = db.conn()
            row = conn.execute(
                "SELECT snapshot_id FROM snapshots WHERE snapshot_dir = ?", [str(snap)]
            ).fetchone()
            if not row:
                _log.debug(f"[Label] snapshot not in DB, skipping DB update: {snap}")
                return {"ok": True, "label": label}
            snapshot_id = row[0]
            conn.execute("DELETE FROM labels WHERE snapshot_id = ? AND api_id = ?", [snapshot_id, api_id])
            conn.execute(
                "INSERT INTO labels (snapshot_id, api_id, category, subcategory, detail, "
                "reason_tags, confidence, label_source, bottleneck_text, embedding, labeled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [snapshot_id, api_id,
                 label.get("category", ""),
                 label.get("subcategory", ""),
                 label.get("detail", ""),
                 json.dumps(label.get("reason_tags", [])),
                 label.get("confidence", 0.0),
                 label.get("label_source", ""),
                 label.get("bottleneck_text", ""),
                 None,
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")],
            )
        except Exception as exc:
            _log.error("relabel_single DB update failed", exc=exc)

    _log.info(f"[Label] relabel done: api_id={api_id} → {label.get('category')}/{label.get('subcategory')}")
    return {"ok": True, "label": label}
