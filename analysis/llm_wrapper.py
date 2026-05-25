"""llm_wrapper.py — OpenAI-compatible LLM client with SHA-256 ring-pool cache.

Mirrors C# LlmApiWrapper + LlmResponseCache:
  - Reads config from SDPCLI/config.ini (LlmApiEndpoint, LlmApiKey, LlmModel, ...)
  - Cache key = SHA-256(prompt) first 16 bytes (32 hex chars) — same as C#
  - Cache file format identical to C# llm_cache.json — shared across both runtimes
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

import requests as _requests
from requests.adapters import HTTPAdapter as _HTTPAdapter

# ── Config location ────────────────────────────────────────────────────────────

def _find_config_ini() -> Path | None:
    """Legacy helper — returns active config path via config.py."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import get_config_path
    return get_config_path()


def _load_config() -> dict[str, str]:
    """Return merged config dict via config.py (single source of truth)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import get_settings
    return get_settings()


# ── Ring-pool cache ────────────────────────────────────────────────────────────

class _LlmCache:
    """Thread-safe ring-pool cache — file format identical to C# llm_cache.json."""

    def __init__(self, capacity: int, path: Path) -> None:
        self._capacity = max(8, capacity)
        self._path = path
        self._lock = threading.Lock()
        self._slots: list[dict | None] = [None] * self._capacity
        self._index: dict[str, int] = {}
        self._write_head = 0
        self._count = 0
        self._load()

    @staticmethod
    def hash(prompt: str) -> str:
        h = hashlib.sha256(prompt.encode("utf-8")).digest()
        return h[:16].hex()

    def get(self, prompt: str) -> str | None:
        key = self.hash(prompt)
        with self._lock:
            pos = self._index.get(key)
            if pos is not None and self._slots[pos] is not None:
                return self._slots[pos]["response"]  # type: ignore[index]
        return None

    def put(self, prompt: str, response: str) -> None:
        key = self.hash(prompt)
        with self._lock:
            if key in self._index:
                pos = self._index[key]
                self._slots[pos]["response"] = response  # type: ignore[index]
                self._slots[pos]["ts"] = _utc_now()  # type: ignore[index]
            else:
                victim = self._slots[self._write_head]
                if victim is not None:
                    self._index.pop(victim["key"], None)
                    self._count -= 1
                self._slots[self._write_head] = {"key": key, "response": response, "ts": _utc_now()}
                self._index[key] = self._write_head
                self._count += 1
                self._write_head = (self._write_head + 1) % self._capacity
            self._save_nolock()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            slots = data.get("slots") or []
            load_n = min(len(slots), self._capacity)
            for i in range(load_n):
                s = slots[i]
                if not s or not s.get("key"):
                    continue
                self._slots[i] = s
                self._index[s["key"]] = i
                self._count += 1
            saved_head = data.get("write_head", self._count % self._capacity)
            self._write_head = max(0, min(saved_head, self._capacity - 1))
        except Exception:
            pass

    def _save_nolock(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "capacity":   self._capacity,
                "write_head": self._write_head,
                "count":      self._count,
                "slots":      self._slots,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            if self._path.exists():
                self._path.unlink()
            tmp.rename(self._path)
        except Exception:
            pass


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Public wrapper ─────────────────────────────────────────────────────────────

class LlmWrapper:
    """OpenAI-compatible single-turn chat client. Thread-safe."""

    def __init__(self) -> None:
        cfg = _load_config()
        self._endpoint        = cfg.get("LlmApiEndpoint", "").strip()
        self._api_key         = cfg.get("LlmApiKey",      "").strip()
        self._model           = cfg.get("LlmModel",       "gpt-4o").strip()
        self._timeout         = int(cfg.get("LlmTimeoutSeconds",  "60"))
        self._max_tokens      = int(cfg.get("LlmMaxOutputTokens", "800"))
        self.is_enabled       = bool(self._endpoint and self._api_key)
        self.last_error: str | None = None

        # Connection-pooled HTTP session (reuses TCP/TLS across concurrent threads)
        max_pool = int(cfg.get("LlmMaxConcurrentRequests", "16"))
        self._session = _requests.Session()
        adapter = _HTTPAdapter(pool_connections=max_pool, pool_maxsize=max_pool)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._cache: _LlmCache | None = None
        # LlmCacheOverride=true → skip cache reads, always call LLM fresh (still writes)
        self._cache_override = cfg.get("LlmCacheOverride", "false").lower() == "true"
        if self.is_enabled and cfg.get("LlmCacheEnabled", "true").lower() != "false":
            capacity  = int(cfg.get("LlmCacheSize", "512"))
            cfg_path  = _find_config_ini()
            default   = str(cfg_path.parent.parent / "llm_cache.json") if cfg_path else "llm_cache.json"
            cache_path = Path(cfg.get("LlmCachePath", default).lstrip("#").strip())
            self._cache = _LlmCache(capacity, cache_path)

    def chat(self, prompt: str) -> str | None:
        """Send prompt; return response text or None on error."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
        from logger import get_logger
        _log = get_logger()

        self.last_error = None
        if not self.is_enabled:
            self.last_error = "LLM not configured"
            return None

        if self._cache and not self._cache_override:
            hit = self._cache.get(prompt)
            if hit is not None:
                _log.debug(f"[LLM] cache HIT model={self._model} prompt_len={len(prompt)}")
                return hit

        _log.debug(f"[LLM] cache MISS model={self._model} prompt_len={len(prompt)}")
        t0 = time.time()
        try:
            response = self._call(prompt)
        except Exception as exc:
            self.last_error = str(exc)
            _log.error(f"[LLM] call FAILED model={self._model} elapsed={time.time()-t0:.1f}s", exc=exc)
            return None

        elapsed = time.time() - t0
        _log.info(f"[LLM] call OK model={self._model} elapsed={elapsed:.1f}s resp_len={len(response) if response else 0}")

        if response is not None and self._cache:
            self._cache.put(prompt, response)
        return response

    def _call(self, prompt: str) -> str | None:
        body = {
            "model":       self._model,
            "max_tokens":  self._max_tokens,
            "temperature": 0.0,
            "messages":    [{"role": "user", "content": prompt}],
        }

        resp = self._session.post(
            self._endpoint,
            json=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
            },
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]


# Module-level singleton — lazy-initialised on first import
_instance: LlmWrapper | None = None
_init_lock = threading.Lock()


def get_llm() -> LlmWrapper:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = LlmWrapper()
    return _instance


# ── VLM wrapper (vision) ───────────────────────────────────────────────────────

import base64
from pathlib import Path as _Path


class VlmWrapper:
    """OpenAI-compatible vision client. Sends image as base64 data URL.

    Reads VlmApiEndpoint / VlmApiKey / VlmModel / VlmTimeoutSeconds /
    VlmMaxOutputTokens from SDPCLI/config.ini.
    """

    def __init__(self) -> None:
        cfg = _load_config()
        self._endpoint   = cfg.get("VlmApiEndpoint", "").strip()
        self._api_key    = cfg.get("VlmApiKey",      "").strip()
        self._model      = cfg.get("VlmModel",       "").strip()
        self._timeout    = int(cfg.get("VlmTimeoutSeconds",   "60"))
        self._max_tokens = int(cfg.get("VlmMaxOutputTokens",  "2000"))
        self.is_enabled  = bool(self._endpoint and self._api_key and self._model)
        self.last_error: str | None = None

        max_pool = int(cfg.get("VlmTextureMaxConcurrent", "4"))
        self._session = _requests.Session()
        adapter = _HTTPAdapter(pool_connections=max_pool, pool_maxsize=max_pool)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def describe_image(self, image_path: str | _Path, prompt: str) -> str | None:
        """Send image + text prompt; return response text or None on error."""
        import sys
        sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "webui"))
        from logger import get_logger
        _log = get_logger()

        self.last_error = None
        if not self.is_enabled:
            self.last_error = "VLM not configured"
            return None

        p = _Path(image_path)
        if not p.exists():
            self.last_error = f"Image not found: {image_path}"
            return None

        ext = p.suffix.lstrip(".").lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "bmp": "image/bmp", "webp": "image/webp"}.get(ext, "image/png")
        img_size = p.stat().st_size
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"

        _log.debug(f"[VLM] calling model={self._model} image={p.name} size={img_size}")
        t0 = time.time()

        body = {
            "model":      self._model,
            "max_tokens": self._max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text",      "text": prompt},
                ],
            }],
        }

        try:
            resp = self._session.post(
                self._endpoint,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                _log.error(f"[VLM] call FAILED model={self._model} image={p.name} elapsed={time.time()-t0:.1f}s: {self.last_error}")
                return None
            data = resp.json()
            result = data["choices"][0]["message"]["content"]
            elapsed = time.time() - t0
            _log.info(f"[VLM] call OK model={self._model} image={p.name} elapsed={elapsed:.1f}s resp_len={len(result)}")
            return result
        except Exception as exc:
            self.last_error = str(exc)
            _log.error(f"[VLM] call FAILED model={self._model} image={p.name} elapsed={time.time()-t0:.1f}s", exc=exc)
            return None


_vlm_instance: VlmWrapper | None = None
_vlm_lock = threading.Lock()


def get_vlm() -> VlmWrapper:
    global _vlm_instance
    if _vlm_instance is None:
        with _vlm_lock:
            if _vlm_instance is None:
                _vlm_instance = VlmWrapper()
    return _vlm_instance
