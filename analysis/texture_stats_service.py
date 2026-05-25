"""texture_stats_service.py — Scan extracted PNG textures, read dimensions,
optionally call VLM for description, write <run>/textures/textures.json.

Output format:
  { "textures": [
      { "texture_id": 1109, "file": "texture_1109.png",
        "width": 512, "height": 512, "size": 12345,
        "description": "...",   # null if VLM disabled or skipped
        "vlm_model": "..."      # null if not called
      }, ...
  ]}

Intended to run BEFORE ingest so ingest picks up width/height/description.
"""
from __future__ import annotations

import concurrent.futures
import json
import struct
import threading
from pathlib import Path


# ── Image dimension reader (no Pillow) ────────────────────────────────────────

def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    except Exception:
        return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            try:
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            except Exception:
                return None
        try:
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
        except Exception:
            break
        i += 2 + length
    return None


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
        ext = path.suffix.lower()
        if ext == ".png":
            return _png_dimensions(data)
        if ext in (".jpg", ".jpeg"):
            return _jpeg_dimensions(data)
        if ext == ".bmp" and data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w), abs(h)
    except Exception:
        pass
    return None


# ── VLM description ────────────────────────────────────────────────────────────

_VLM_PROMPT = """\
You are a graphics engineer analyzing a mobile game GPU texture atlas.
Examine this texture image carefully and provide a structured description:

1. **Content**: What does this texture depict? (e.g. character skin, terrain ground, \
foliage leaves, UI element, normal map, roughness/metalness PBR map, shadow map, \
render target, noise pattern, etc.)

2. **Type**: Classify the texture type:
   - Diffuse/Albedo (base color)
   - Normal map (blue-purple tint, encodes surface normals)
   - PBR map (roughness/metalness/AO — typically grayscale or packed channels)
   - Emissive
   - Shadow/Depth map
   - Render target / framebuffer
   - UI / HUD element
   - Sprite sheet / atlas
   - Procedural / noise
   - Other (describe)

3. **Visual characteristics**: Color palette, dominant patterns, level of detail, \
any compression artifacts, alpha channel usage (if transparent areas visible), \
repeating tile patterns.

4. **Rendering role**: Based on content and type, how is this texture likely used \
in the rendering pipeline? (e.g. applied to character mesh, used as environment \
diffuse, sampled in post-process pass, etc.)

Be concise — 3–5 sentences total. Focus on information useful for GPU optimization."""


def _describe_texture(path: Path, vlm) -> str | None:
    """Call VLM on a single texture file. Returns description text or None."""
    return vlm.describe_image(path, _VLM_PROMPT)


# ── Main service ──────────────────────────────────────────────────────────────

def generate_texture_stats(run_dir: str | Path) -> Path:
    """
    Scan <run_dir>/textures/*.png, read dimensions, optionally call VLM,
    write <run_dir>/textures/textures.json.

    VLM is only called when VlmTextureDescriptionEnabled=true in config.ini
    and texture dimensions exceed VlmTextureMinSize on both axes.

    Returns path to written textures.json.
    Raises FileNotFoundError if textures directory doesn't exist.
    """
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    run = Path(run_dir)
    tex_dir = run / "textures"
    if not tex_dir.exists():
        raise FileNotFoundError(f"Textures directory not found: {tex_dir}")

    img_files = sorted(
        p for p in tex_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp")
        and p.stem.startswith("texture_")
    )
    if not img_files:
        raise FileNotFoundError(f"No texture_*.png files found in {tex_dir}")

    _log.info(f"[TextureStats] start: {len(img_files)} texture files")
    _t0 = _time.time()

    # ── Load config ────────────────────────────────────────────────────────────
    from analysis.llm_wrapper import _load_config, get_vlm
    cfg = _load_config()
    vlm_enabled  = cfg.get("VlmTextureDescriptionEnabled", "false").lower() == "true"
    min_size     = int(cfg.get("VlmTextureMinSize", "64"))
    max_workers  = int(cfg.get("VlmTextureMaxConcurrent", "4"))

    vlm = get_vlm() if vlm_enabled else None

    # ── Collect stats ──────────────────────────────────────────────────────────
    entries: list[dict] = []
    for img in img_files:
        stem = img.stem  # texture_1109
        try:
            tex_id = int(stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        dims = _image_dimensions(img)
        entries.append({
            "texture_id":  tex_id,
            "file":        img.name,
            "width":       dims[0] if dims else None,
            "height":      dims[1] if dims else None,
            "size":        img.stat().st_size,
            "description": None,
            "vlm_model":   None,
        })
        _log.debug(f"[TextureStats] {img.name}: {dims[0]}x{dims[1]} size={img.stat().st_size}" if dims else f"[TextureStats] {img.name}: dims unknown")

    # ── VLM descriptions (concurrent) ─────────────────────────────────────────
    if vlm_enabled and vlm and vlm.is_enabled:
        to_describe = [
            e for e in entries
            if e["width"] and e["height"]
            and e["width"] > min_size and e["height"] > min_size
        ]
        _log.info(f"[TextureStats] VLM describing {len(to_describe)}/{len(entries)} textures")

        lock = threading.Lock()
        _vlm_ok = [0]

        def _describe(entry: dict) -> None:
            img_path = tex_dir / entry["file"]
            desc = _describe_texture(img_path, vlm)
            with lock:
                entry["description"] = desc
                entry["vlm_model"]   = vlm._model if desc else None
                if desc:
                    _vlm_ok[0] += 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_describe, to_describe))

        _log.info(f"[TextureStats] VLM done: {_vlm_ok[0]}/{len(to_describe)} described")

    # ── Write output ───────────────────────────────────────────────────────────
    out = tex_dir / "textures.json"
    out.write_text(
        json.dumps({"textures": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _log.info(f"[TextureStats] done: {_time.time()-_t0:.1f}s, {len(entries)} textures")
    return out
