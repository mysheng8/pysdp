#!/usr/bin/env python3
"""Test script for prompt customization system.

Usage:
    python test_prompt_customization.py

This script tests:
1. PromptManager loading defaults
2. Rendering prompts with variables
3. Custom prompts from prompts.json (if exists)
"""

from config.prompt_manager import get_prompt_manager


def test_default_prompts():
    """Test loading default prompts."""
    print("=" * 60)
    print("Test 1: Loading default prompts")
    print("=" * 60)

    pm = get_prompt_manager()

    # List all available prompts
    prompts = pm.list_prompts()
    print(f"\nAvailable prompts: {len(prompts)}")
    for p in prompts:
        print(f"  - {p['id']:20s} | {p['description'][:50]}")
        print(f"    Enabled: {p['enabled']:5}  Frequency: {p['call_frequency']:8s}  Format: {p['output_format']}")

    print("\n[OK] Default prompts loaded successfully\n")


def test_render_label_dc():
    """Test rendering label_dc prompt with sample variables."""
    print("=" * 60)
    print("Test 2: Rendering label_dc prompt")
    print("=" * 60)

    pm = get_prompt_manager()

    # Sample variables
    variables = {
        "api_name": "vkCmdDrawIndexed",
        "vertex_count": 1024,
        "index_count": 3072,
        "instance_count": 1,
        "verts_per_inst": 1024,
        "texture_count": 3,
        "shader_stages": "vert:vs_main, frag:ps_main",
        "render_targets_section": "Render targets:\n  [0] Color 1920x1080 R8G8B8A8_UNORM\n  [1] Depth 1920x1080 D32_SFLOAT",
        "mesh_section": "Mesh:\n  vertices:1024  faces:512  normals:1024  uvs:1024",
        "texture_descriptions_section": "Textures:\n  [0] 2048x2048 PBR material: albedo map\n  [1] 2048x2048 PBR material: normal map",
        "shader_code": "// Sample shader code\nvoid main() {\n  vec4 albedo = texture(texAlbedo, uv);\n  vec4 normal = texture(texNormal, uv);\n  gl_FragColor = vec4(lighting(albedo, normal), 1.0);\n}",
        "category_list": "Scene/Terrain/Character/PostProcess/VFX/UI/Other/Scene(Shadow)/Terrain(Shadow)/Character(Shadow)",
    }

    try:
        system, user = pm.render_prompt("label_dc", variables)

        print(f"\nSystem prompt length: {len(system)} chars")
        print(f"User prompt length:   {len(user)} chars")
        print(f"\nSystem prompt:\n{system[:200]}...")
        print(f"\nUser prompt (first 500 chars):\n{user[:500]}...")
        print("\n[OK] Prompt rendered successfully\n")
    except Exception as e:
        print(f"\n[ERROR] Error rendering prompt: {e}\n")
        raise


def test_custom_prompts():
    """Test loading custom prompts from prompts.json."""
    print("=" * 60)
    print("Test 3: Custom prompts (if prompts.json exists)")
    print("=" * 60)

    from pathlib import Path

    prompts_json = Path("prompts.json")

    if not prompts_json.exists():
        print("\n[INFO] No prompts.json found — using defaults only")
        print("  To test customization, copy prompts.json.example to prompts.json and edit it\n")
        return

    pm = get_prompt_manager()
    pm.reload()  # Force reload from disk

    cfg = pm.get_prompt("label_dc")
    if cfg:
        print(f"\nlabel_dc configuration:")
        print(f"  Enabled: {cfg.get('enabled')}")
        print(f"  Description: {cfg.get('description')}")
        print(f"  System prompt length: {len(cfg.get('system_prompt', ''))} chars")
        print(f"  User template length: {len(cfg.get('user_template', ''))} chars")
        print("\n[OK] Custom prompts loaded successfully\n")
    else:
        print("\n[ERROR] label_dc is disabled in prompts.json\n")


def main():
    """Run all tests."""
    try:
        test_default_prompts()
        test_render_label_dc()
        test_custom_prompts()

        print("=" * 60)
        print("[OK] All tests passed")
        print("=" * 60)
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"[ERROR] Test failed: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
