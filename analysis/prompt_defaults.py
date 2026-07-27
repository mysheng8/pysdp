"""Default prompt templates for all AI services.

Extracted from hardcoded strings in:
- analysis/label_service.py
- analysis/gles_decompile_service.py
- analysis/vlm_screenshot_service.py
- analysis/report_service.py
- chat/prompts.py

Each prompt has:
- system_prompt: LLM system message (or empty string for VLM-only)
- user_template: User message template with {variable} placeholders
- variables: List of required variable names for substitution
- output_format: Expected output format (json/glsl/markdown)
- validation_schema: Optional JSON schema for output validation
"""

# ── label_dc: DrawCall classification ─────────────────────────────────────────

LABEL_DC_SYSTEM_PROMPT = "Classify this Vulkan draw call. Reply with JSON only."

LABEL_DC_USER_TEMPLATE = """API:{api_name}
Verts:{vertex_count}  Indices:{index_count}  Instances:{instance_count}  VertsPerInst:{verts_per_inst}  Textures:{texture_count}
Shaders: {shader_stages}
{render_targets_section}
{mesh_section}
{texture_descriptions_section}

Category definitions:
  Scene              — static world geometry rendered to the main HDR buffer (buildings, props).
  Terrain            — heightfield/ground mesh, uses virtual/heightfield textures or terrain lightmaps.
  Character          — dynamic skinned mesh (players, crowd) with per-object SH probe lighting, no lightmap UVs.
  PostProcess        — fullscreen-quad pass that reads from a previously rendered texture and writes to an RT.
  VFX                — particle systems, billboard quads, or other effect geometry (many small instances).
  UI                 — 2D interface elements, no depth RT, typically RGBA8 color RT.
  Other              — compute dispatches.
  Scene(Shadow)      — shadow map pass rendering SCENE geometry into a depth or encoded-depth RT.
  Terrain(Shadow)    — shadow map pass rendering TERRAIN geometry into a depth or encoded-depth RT.
  Character(Shadow)  — shadow map pass rendering CHARACTER geometry into a depth or encoded-depth RT.

Rules (apply in order):
R1 [Render targets first — HIGHEST PRIORITY, overrides everything else]
  ** RULE R1a: Depth-only RT, no Color RT → SHADOW MAP PASS. Determine object type from shader and output
     the matching shadow category: 'Scene(Shadow)', 'Terrain(Shadow)', or 'Character(Shadow)'.
     Default to 'Scene(Shadow)' if indeterminate.
  ** RULE R1b: Color RT with only 2 channels (R8G8/R16G16/R32G16/RG prefix), AND no Depth RT,
     AND real geometry (VertexCount>6 or IndexBuffer) → ENCODED DEPTH SHADOW MAP (VSM/ESM).
     Output matching shadow category. Do NOT output bare 'Scene'.
  ** RULE R1b exception: VertexCount 3-6 AND no IndexBuffer → fullscreen shadow blur → PostProcess.
  Color-only RT, no Depth, R8G8B8A8/B8G8R8A8 → UI
  Color-only RT, no Depth, HDR/float format, screen-size → PostProcess
  Color HDR + Depth, VertsPerInst<=6, many instances → VFX (particles/quads)
  Color HDR + Depth, VertsPerInst>6, normal geometry → Scene/Character/Terrain
R2 [Shader main() for Scene/Character/Terrain]
  Scene:     lightmap textures sampled in main() using TEXCOORD2 UVs (irradiance/sky-visibility/baked).
  Character: per-object SH probe Buffer<float4> loaded via per-instance offset — no lightmap sampling.
             Also: per-instance cosmetic data (tint/recolor) or skinned vertex buffer offset.
             vkCmdDispatch with entry point containing skin/skinning/morph/deform/cloth/hair → Character.
  Terrain:   terrain-specific expression shader or virtual/heightfield texture sampling in main().
R3 [cbuffer vs texture priority — CRITICAL]
  The first/global cbuffer (b0 or b11) is a SHARED per-pass buffer present in EVERY draw call.
  It typically contains terrain, shadow, lighting structs — ALL irrelevant unless READ in main().
  IGNORE global cbuffer struct declarations. Only what is used inside main()/frag_main() matters.
  TRUST ORDER: texture/sampler calls in main() > secondary cbuffers/typed buffers > global cbuffer.
  If a typed Buffer<float4> or ByteAddressBuffer is loaded via a per-instance offset in main(),
  that is a STRONG signal of Character (dynamic SH probe data). Terrain is always static/baked.

Shader (analyze what is actually computed in main()):
{shader_code}

Categories: {category_list}
IMPORTANT RESTRICTIONS:
  - 'Other' is for vkCmdDispatch compute that has NO clear category signal.
    If the compute shader entry point or code mentions skin/skinning/morph/deform/cloth/hair
    → use 'Character' (subcategory 'Compute'), NOT 'Other'.
  - NEVER use 'Other' for vkCmdDraw or vkCmdDrawIndexed.
  - If R1a/R1b apply (shadow map RT), you MUST use a shadow category.
    Do NOT fall back to 'Other' — default to 'Scene(Shadow)'.
  - If the shader mentions 'shadow', 'planar shadow', or 'depth encoding', category MUST end in '(Shadow)'.

Categories: {category_list}
Subcategory examples: Opaque, Transparent, DepthOnly, SkinMesh, GaussianBlur, ToneMapping, SSAO, Bloom, TAA, ShadowDepth, ParticleBillboard, UICanvas.
ReasonTags — pick 1-4: pbr_material, multi_texture_blend, high_uv_sampling, skinned_mesh, morphing, instanced_draw, compute_dispatch, gaussian_blur, tone_mapping, ssao, bloom, taa, shadow_depth_write, shadow_pcf_sample, particle_billboard, trail_ribbon, ui_canvas, font_glyph, depth_only, opaque_geometry, transparent_geometry, large_render_target, mrt_output.
Output JSON only, no markdown, confidence in [0,1]:
{{"category":"<category>","subcategory":"<subcategory>","detail":"<3-8 word description>","reason_tags":["tag1"],"confidence":0.9}}"""

LABEL_DC_VARIABLES = [
    "api_name",
    "vertex_count",
    "index_count",
    "instance_count",
    "verts_per_inst",
    "texture_count",
    "shader_stages",
    "render_targets_section",
    "mesh_section",
    "texture_descriptions_section",
    "shader_code",
    "category_list",
]

LABEL_DC_VALIDATION_SCHEMA = {
    "type": "object",
    "required": ["category", "subcategory", "confidence"],
    "properties": {
        "category": {"type": "string"},
        "subcategory": {"type": "string"},
        "detail": {"type": "string"},
        "reason_tags": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

# ── Combined registry ──────────────────────────────────────────────────────────

DEFAULT_PROMPTS = {
    "label_dc": {
        "enabled": True,
        "description": "DrawCall classification based on shader code and render targets",
        "model_override": None,
        "system_prompt": LABEL_DC_SYSTEM_PROMPT,
        "user_template": LABEL_DC_USER_TEMPLATE,
        "variables": LABEL_DC_VARIABLES,
        "output_format": "json",
        "validation_schema": LABEL_DC_VALIDATION_SCHEMA,
        "cache_key_includes_prompt": True,
        "call_frequency": "high",  # Hundreds per frame
    },
}
