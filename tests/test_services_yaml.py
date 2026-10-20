"""Guards the generated parts of the integration.

`services.yaml` is generated from `scripts/services.yaml.template` with
the list of bundled animations, so the dropdown in the UI always offers
every animation that ships with the integration. This test asserts the
checked-in file matches what the generator produces.
"""
import os
import subprocess
import sys

REPO = os.path.join(os.path.dirname(__file__), "..")
INTEGRATION = os.path.join(REPO, "custom_components", "surplife_matrix")
ANIMATIONS = os.path.join(INTEGRATION, "animations")
SERVICES_YAML = os.path.join(INTEGRATION, "services.yaml")
GENERATOR = os.path.join(REPO, "scripts", "generate_services_yaml.py")

sys.path.insert(0, os.path.join(REPO, "scripts"))

import yaml  # noqa: E402


def _animation_names() -> list[str]:
    return sorted(
        name.removesuffix(".gif")
        for name in os.listdir(ANIMATIONS)
        if name.endswith(".gif")
    )


def test_services_yaml_is_up_to_date():
    """The checked-in services.yaml must match the generator output."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, GENERATOR, "--check"],
        capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0, (
        f"services.yaml is out of date:\n{result.stdout}{result.stderr}")


def test_show_media_offers_every_animation():
    """The show_media dropdown lists all bundled animations."""
    with open(SERVICES_YAML, encoding="utf-8") as f:
        services = yaml.safe_load(f)
    options = services["show_media"]["fields"]["source"]["selector"][
        "select"]["options"]
    assert options == _animation_names()


def test_show_animation_offers_every_animation():
    """The show_animation dropdown lists all bundled animations."""
    with open(SERVICES_YAML, encoding="utf-8") as f:
        services = yaml.safe_load(f)
    options = services["show_animation"]["fields"]["animation"]["selector"][
        "select"]["options"]
    assert options == _animation_names()


def test_every_service_has_an_icon():
    """Every service in services.yaml needs an icon in icons.json."""
    import json

    with open(SERVICES_YAML, encoding="utf-8") as f:
        services = yaml.safe_load(f)
    with open(os.path.join(INTEGRATION, "icons.json"), encoding="utf-8") as f:
        icons = json.load(f)
    assert set(services) == set(icons["services"])


def test_every_service_is_translated():
    """Every service needs a name and description in the translations."""
    import json

    with open(SERVICES_YAML, encoding="utf-8") as f:
        services = yaml.safe_load(f)
    with open(os.path.join(INTEGRATION, "translations", "en.json"),
              encoding="utf-8") as f:
        strings = json.load(f)
    assert set(services) == set(strings["services"])
    for name, service in strings["services"].items():
        assert service.get("name"), f"{name} has no name"
        assert service.get("description"), f"{name} has no description"
