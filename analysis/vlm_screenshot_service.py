"""vlm_screenshot_service.py — Describe a snapshot's screenshot with a VLM.

Reads: screenshot image (PNG/JPG/BMP) + label.json + metrics.json (optional)
Writes: snapshot_{id}_scene_description.md
Persists: snapshot_descriptions table in DuckDB (if db= passed)
"""
from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

from analysis.llm_wrapper import get_vlm


_SCREENSHOT_NAMES = [
    "snapshot.png", "snapshot_screenshot.png", "snapshot_screenshot.jpg",
    "snapshot.jpg", "snapshot.bmp",
]


def _find_screenshot(snap_dir: Path) -> Path | None:
    for name in _SCREENSHOT_NAMES:
        p = snap_dir / name
        if p.exists():
            return p
    try:
        parts = snap_dir.parts
        analysis_idx = next(i for i in range(len(parts) - 1, -1, -1) if parts[i].lower() == "analysis")
        sdp_parts = parts[:analysis_idx] + parts[analysis_idx + 1:]
        sdp_snap_dir = Path(*sdp_parts)
        for name in _SCREENSHOT_NAMES:
            p = sdp_snap_dir / name
            if p.exists():
                return p
        for p in sorted(sdp_snap_dir.glob("*.bmp")):
            return p
    except (StopIteration, Exception):
        pass
    for p in sorted(snap_dir.glob("*.bmp")):
        return p
    return None


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read width/height without Pillow for PNG, JPG, BMP."""
    try:
        data = path.read_bytes()
        ext = path.suffix.lower()
        if ext == ".png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if ext in (".jpg", ".jpeg") and data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 1:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + length
        if ext == ".bmp" and data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w), abs(h)
    except Exception:
        pass
    return None, None


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _build_gpu_summary(label_data: dict | None, metrics_data: dict | None) -> str:
    if not label_data:
        return ""
    metrics_by_id: dict[str, dict] = {}
    if metrics_data:
        for d in metrics_data.get("draw_calls", []):
            if d.get("metrics"):
                metrics_by_id[str(d["dc_id"])] = d["metrics"]
    cat_clocks: dict[str, int] = {}
    cat_count:  dict[str, int] = {}
    total_clocks = 0
    for d in label_data.get("draw_calls", []):
        cat = d.get("label", {}).get("category", "Other")
        dc_id = str(d.get("dc_id", ""))
        clocks = metrics_by_id.get(dc_id, {}).get("clocks", 0)
        cat_clocks[cat] = cat_clocks.get(cat, 0) + clocks
        cat_count[cat]  = cat_count.get(cat, 0) + 1
        total_clocks += clocks
    if total_clocks == 0:
        return ""
    lines = ["GPU cost breakdown by draw call category:"]
    for cat in sorted(cat_clocks, key=lambda c: cat_clocks[c], reverse=True):
        pct = cat_clocks[cat] * 100.0 / total_clocks
        lines.append(f"  - {cat}: {pct:.1f}% GPU time ({cat_count[cat]} draw calls)")
    return "\n".join(lines)


def _persist(db, snapshot_id: int, screenshot_path: str,
             width: int | None, height: int | None, size: int | None,
             description: str | None, vlm_model: str,
             status: str, error_msg: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.conn().execute(
        """
        INSERT INTO snapshot_descriptions
            (snapshot_id, screenshot_path, screenshot_width, screenshot_height,
             screenshot_size, description, vlm_model, status, error_msg, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_id) DO UPDATE SET
            screenshot_path   = excluded.screenshot_path,
            screenshot_width  = excluded.screenshot_width,
            screenshot_height = excluded.screenshot_height,
            screenshot_size   = excluded.screenshot_size,
            description       = excluded.description,
            vlm_model         = excluded.vlm_model,
            status            = excluded.status,
            error_msg         = excluded.error_msg,
            generated_at      = excluded.generated_at
        """,
        [snapshot_id, screenshot_path, width, height, size,
         description, vlm_model, status, error_msg, now],
    )


def generate_scene_description(snapshot_dir: str | Path, db=None) -> Path:
    """
    Call the VLM to describe the screenshot and write
    snapshot_{id}_scene_description.md.  If db is passed, also persists
    to snapshot_descriptions table.

    Raises FileNotFoundError if no screenshot is found.
    Raises RuntimeError if VLM is not configured or returns an error.
    """
    import sys, time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    from logger import get_logger
    _log = get_logger()

    snap = Path(snapshot_dir)

    screenshot = _find_screenshot(snap)
    if screenshot is None:
        raise FileNotFoundError(f"No screenshot found in {snap}")

    _log.info(f"[VlmScreenshot] start: {screenshot.name}", context={"dir": str(snap.name)})

    label_data   = _load_json(snap / "label.json")
    metrics_data = _load_json(snap / "metrics.json")

    snapshot_id = label_data.get("snapshot_id", 0) if label_data else 0
    sdp_name    = label_data.get("sdp_name", snap.parent.name) if label_data else snap.parent.name

    width, height = _image_dimensions(screenshot)
    size = screenshot.stat().st_size

    gpu_summary = _build_gpu_summary(label_data, metrics_data)

    prompt = (
        "You are a graphics engineer analyzing a mobile game GPU capture.\n"
        "Describe what you see in this screenshot: the scene content, visual style, "
        "and key rendering features (lighting, shadows, transparency, post-effects, UI, etc.).\n"
        "Then, given the GPU profiling data below, briefly note which visible elements "
        "are likely responsible for the highest GPU cost.\n"
        "Be concise (200–400 words). Write in English.\n\n"
    )
    if gpu_summary:
        prompt += gpu_summary + "\n"

    vlm = get_vlm()

    # Persist pending state before the (potentially slow) VLM call
    if db is not None and snapshot_id:
        try:
            _persist(db, snapshot_id, str(screenshot), width, height, size,
                     None, vlm._model, "pending", None)
        except Exception:
            pass

    response = vlm.describe_image(screenshot, prompt)

    if response is None:
        if db is not None and snapshot_id:
            try:
                _persist(db, snapshot_id, str(screenshot), width, height, size,
                         None, vlm._model, "error", vlm.last_error)
            except Exception:
                pass
        raise RuntimeError(f"VLM call failed: {vlm.last_error}")

    # Persist success
    if db is not None and snapshot_id:
        try:
            _persist(db, snapshot_id, str(screenshot), width, height, size,
                     response, vlm._model, "ok", None)
        except Exception:
            pass

    lines = [
        f"# {sdp_name} — Scene Description (snapshot {snapshot_id})",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"Screenshot: `{screenshot.name}`  ",
        f"Model: `{vlm._model}`  ",
        "",
        response.strip(),
        "",
    ]

    fname = f"snapshot_{snapshot_id}_scene_description.md" if snapshot_id else "scene_description.md"
    out = snap / fname
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
