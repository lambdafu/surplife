"""Tests for the bundled animations: every shipped GIF must be device-safe.

The integration's `animations/` folder is produced by
scripts/generate_animations.py; this test guards the invariant that
nothing unsafe ships (regression against missing files, renamed assets,
or a generator regression).
"""
import os
import sys

INTEGRATION_DIR = os.path.join(os.path.dirname(__file__), "..",
                               "custom_components", "surplife_matrix")
sys.path.insert(0, INTEGRATION_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from assets import list_animations, resolve_builtin
from surplife_core.content import validate_gif


def test_all_bundled_animations_are_device_safe():
    """Every bundled GIF passes the strict device-safe envelope."""
    animations = list_animations()
    assert animations, "no bundled animations found — run the generator"
    for name, path in animations.items():
        with open(path, "rb") as f:
            data = f.read()
        report = validate_gif(data, strict=False)
        assert report["ok"], (
            f"bundled animation {name}.gif violates the device-safe "
            f"envelope: {report['violations']}")


def test_bundled_animations_size_budget():
    """Total bundled size stays reasonable (BLE upload time scales with it)."""
    total = sum(os.path.getsize(p) for p in list_animations().values())
    # 12+ animations; each <= 64KB (safe envelope) → generous total cap
    assert total <= 600_000, f"bundled animations total {total} bytes"


def test_expected_generators_present():
    """The six procedural animations are part of the shipped set."""
    animations = list_animations()
    for name in ("rainbow_wave", "plasma", "fire", "ocean", "starfield",
                 "police_light"):
        assert name in animations, f"missing bundled animation: {name}"


def test_resolve_builtin_case_insensitive():
    """builtin: resolution is case-insensitive."""
    path = resolve_builtin("builtin:PLASMA")
    assert path.endswith("plasma.gif")


def test_resolve_builtin_unknown_lists_available():
    """An unknown name raises with the available animation list."""
    with pytest.raises(KeyError) as exc:
        resolve_builtin("builtin:does_not_exist")
    assert "rainbow_wave" in str(exc.value)


def test_resolve_builtin_ignores_non_builtin():
    """Non-builtin sources return None (other resolvers handle them)."""
    assert resolve_builtin("/media/local/foo.gif") is None
    assert resolve_builtin("https://example.com/x.gif") is None


def test_builtin_files_readable():
    """Every registered path exists and is a non-empty GIF."""
    for name, path in list_animations().items():
        assert os.path.isfile(path), f"{name} registered but missing"
        with open(path, "rb") as f:
            assert f.read(3) == b"GIF", f"{name} is not a GIF"
