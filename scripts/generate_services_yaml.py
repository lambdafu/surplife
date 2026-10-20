#!/usr/bin/env python3
"""Generate the integration's services.yaml with the animation dropdown.

`services.yaml` is static, but the list of bundled animations is not:
every GIF in `custom_components/surplife_matrix/animations/` should be
selectable in the `show_media` service. This script renders the file
from a template so the two never drift apart.

`tests/test_services_yaml.py` asserts the checked-in file matches what
this script produces, so a forgotten regeneration fails CI.

Usage:
    python3 scripts/generate_services_yaml.py            # write
    python3 scripts/generate_services_yaml.py --check    # verify only
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.join(os.path.dirname(__file__), "..")
ANIM_DIR = os.path.join(REPO, "custom_components", "surplife_matrix",
                        "animations")
TEMPLATE = os.path.join(REPO, "scripts", "services.yaml.template")
TARGET = os.path.join(REPO, "custom_components", "surplife_matrix",
                      "services.yaml")

# Placeholder replaced with the animation options (YAML list items).
ANIMATIONS_PLACEHOLDER = "    # {{ANIMATION_OPTIONS}}"


def animation_names() -> list[str]:
    """Sorted names of the bundled animations."""
    if not os.path.isdir(ANIM_DIR):
        return []
    return sorted(
        name.removesuffix(".gif")
        for name in os.listdir(ANIM_DIR)
        if name.endswith(".gif")
    )


def render() -> str:
    """Render services.yaml from the template."""
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()

    options = "\n".join(
        f"            - {name}" for name in animation_names())
    return template.replace(ANIMATIONS_PLACEHOLDER, options)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Only verify that the file is up to date.")
    args = parser.parse_args()

    rendered = render()

    if args.check:
        current = ""
        if os.path.exists(TARGET):
            with open(TARGET, encoding="utf-8") as f:
                current = f.read()
        if current != rendered:
            print("services.yaml is out of date — run "
                  "python3 scripts/generate_services_yaml.py")
            return 1
        print("services.yaml is up to date")
        return 0

    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"wrote {TARGET} ({len(animation_names())} animations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
