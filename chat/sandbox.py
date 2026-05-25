"""chat/sandbox.py — Sandboxed Python execution for chat AI."""
from __future__ import annotations

import ast
import asyncio
import base64
import io
import sys
import traceback
from typing import Any

_mpl_initialized = False


def _ensure_matplotlib():
    global _mpl_initialized
    if _mpl_initialized:
        return
    _mpl_initialized = True
    try:
        import matplotlib
        matplotlib.use("Agg")
    except ImportError:
        pass

import chat


ALLOWED_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "hasattr", "hash", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "pow", "print", "range", "repr", "reversed", "round", "set", "slice",
    "sorted", "str", "sum", "tuple", "type", "zip",
}

ALLOWED_MODULES = {
    "math", "statistics", "json", "collections", "itertools", "functools",
    "matplotlib", "matplotlib.pyplot", "numpy", "pandas",
}

BLOCKED_NAMES = {
    "os", "subprocess", "open", "__import__", "eval", "exec", "compile",
    "globals", "locals", "getattr", "setattr", "delattr", "breakpoint",
    "socket", "urllib", "requests", "httpx", "shutil", "pathlib",
}


def _safe_import(name, *args, **kwargs):
    top = name.split(".")[0]
    if top not in ALLOWED_MODULES and name not in ALLOWED_MODULES:
        raise ImportError(f"Import of '{name}' is not allowed in sandbox")
    return __builtins__["__import__"](name, *args, **kwargs) if isinstance(__builtins__, dict) else getattr(__builtins__, "__import__")(name, *args, **kwargs)


def _validate_code(code: str) -> str | None:
    """Parse code and check for disallowed constructs. Returns error or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod not in ALLOWED_MODULES:
                    return f"Import of '{alias.name}' is not allowed"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if mod not in ALLOWED_MODULES and mod != "data":
                    return f"Import from '{node.module}' is not allowed"
        elif isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            return f"Access to '{node.id}' is not allowed"

    return None


def _build_globals(db: Any, snapshot_ids: list[int]) -> dict:
    """Build restricted globals dict for exec."""
    import math
    import statistics
    import json
    import collections
    import itertools
    import functools

    if isinstance(__builtins__, dict):
        safe_builtins = {k: __builtins__[k] for k in ALLOWED_BUILTINS if k in __builtins__}
    else:
        safe_builtins = {k: getattr(__builtins__, k) for k in ALLOWED_BUILTINS if hasattr(__builtins__, k)}

    safe_builtins["__import__"] = _safe_import
    safe_builtins["__build_class__"] = __builtins__["__build_class__"] if isinstance(__builtins__, dict) else getattr(__builtins__, "__build_class__")

    from data import query as data_query

    return {
        "__builtins__": safe_builtins,
        "math": math,
        "statistics": statistics,
        "json": json,
        "collections": collections,
        "itertools": itertools,
        "functools": functools,
        "data_query": data_query,
        "db": db,
        "snapshot_ids": snapshot_ids,
        "snapshot_id": snapshot_ids[0] if snapshot_ids else None,
    }


def _capture_figures(save_dir: str | None = None) -> tuple[list[str], list[str]]:
    """Capture all open matplotlib figures as base64 PNGs, then close them.

    Returns: (base64_images, saved_file_paths)
    If save_dir is provided, also saves PNG files to that directory.
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return [], []

    figs = [plt.figure(n) for n in plt.get_fignums()]
    if not figs:
        return [], []

    from pathlib import Path
    import time as _time

    images = []
    paths = []
    for i, fig in enumerate(figs):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        buf.seek(0)
        png_bytes = buf.getvalue()
        b64 = base64.b64encode(png_bytes).decode("ascii")
        images.append(b64)
        buf.close()

        if save_dir:
            d = Path(save_dir)
            d.mkdir(parents=True, exist_ok=True)
            fname = f"chart_{int(_time.time())}_{i}.png"
            fpath = d / fname
            fpath.write_bytes(png_bytes)
            paths.append(str(fpath))

    plt.close("all")
    return images, paths


async def execute_code(code: str, snapshot_ids: list[int], timeout: float = 30.0, save_dir: str | None = None) -> dict:
    """Execute user code in a restricted sandbox.

    Returns: {"output": str, "result": Any, "error": str | None, "images": list[str], "image_paths": list[str]}
    """
    error = _validate_code(code)
    if error:
        return {"output": "", "result": None, "error": error, "images": [], "image_paths": []}

    try:
        db = chat.get_db()
    except (AssertionError, Exception):
        db = None
    globals_dict = _build_globals(db, snapshot_ids)

    uses_matplotlib = "matplotlib" in code or "plt" in code
    if uses_matplotlib:
        _ensure_matplotlib()

    def _run():
        stdout_capture = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = stdout_capture

        try:
            tree = ast.parse(code)
            last_expr_value = None

            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expr = tree.body.pop()
                exec(compile(ast.Module(body=tree.body, type_ignores=[]), "<sandbox>", "exec"), globals_dict)
                last_expr_value = eval(compile(ast.Expression(body=last_expr.value), "<sandbox>", "eval"), globals_dict)
            else:
                exec(compile(tree, "<sandbox>", "exec"), globals_dict)
                last_expr_value = globals_dict.get("result")

            output = stdout_capture.getvalue()
            if uses_matplotlib:
                images, paths = _capture_figures(save_dir)
            else:
                images, paths = [], []

            return {"output": output, "result": last_expr_value, "error": None, "images": images, "image_paths": paths}

        except Exception:
            output = stdout_capture.getvalue()
            tb = traceback.format_exc()
            if uses_matplotlib:
                images, paths = _capture_figures(save_dir)
            else:
                images, paths = [], []
            return {"output": output, "result": None, "error": tb, "images": images, "image_paths": paths}
        finally:
            sys.stdout = old_stdout

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        return {"output": "", "result": None, "error": f"Execution timed out after {timeout}s", "images": [], "image_paths": []}
