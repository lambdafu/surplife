"""Bundled animations for Surplife Matrix.

The integration ships device-safe GIFs in its own `animations/` folder
(same directory mechanism as the bundled font). Users reference them in
`show_media` via the `builtin:` scheme, e.g. `source: "builtin:plasma"`.

Every bundled file is guaranteed to pass `validate_gif()` — they are
produced by `scripts/generate_animations.py`, which pushes each animation
through `fix_gif()` and asserts the safe envelope before writing. A unit
test enforces this for the whole directory.
"""

from __future__ import annotations

import os

ANIMATIONS_DIR = os.path.join(os.path.dirname(__file__), "animations")

# Scheme prefix used in show_media's `source` field.
BUILTIN_SCHEME = "builtin:"


def list_animations() -> dict[str, str]:
    """Registry of bundled animations: name (no extension) -> path."""
    animations: dict[str, str] = {}
    if not os.path.isdir(ANIMATIONS_DIR):
        return animations
    for filename in sorted(os.listdir(ANIMATIONS_DIR)):
        if filename.endswith(".gif"):
            animations[filename.removesuffix(".gif")] = os.path.join(
                ANIMATIONS_DIR, filename)
    return animations


def resolve_builtin(source: str) -> str | None:
    """Resolve a `builtin:<name>` source to a bundled file path.

    Returns None when the source is not a builtin: URI. Raises
    ValueError with the available names on an unknown animation.
    """
    if not source.startswith(BUILTIN_SCHEME):
        return None
    name = source.removeprefix(BUILTIN_SCHEME).strip().lower()
    animations = list_animations()
    if name not in animations:
        available = ", ".join(animations) if animations else "(none)"
        raise KeyError(f"Unknown builtin animation '{name}'. Available: {available}")
    return animations[name]
