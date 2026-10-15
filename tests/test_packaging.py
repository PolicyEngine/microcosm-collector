"""The collector is distributed under its repo's name and the MIT license."""

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION_NAME = "microcosm-collector"
IMPORT_PACKAGE = "telemetry_collector"
# SHA-256 of PolicyEngine/microcosm-emitter's LICENSE: the same MIT text and holder.
EMITTER_LICENSE_SHA256 = (
    "919d42904bc9c6bb0621ab4eb7e4c9828d1467ea5ee64da7ca88eed53af1530b"
)
IGNORED_PARTS = {".git", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "dist"}
TEXT_SUFFIXES = {".env", ".ini", ".lock", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def text_files() -> list[Path]:
    return [
        source_path
        for source_path in ROOT.rglob("*")
        if source_path.is_file()
        and source_path.suffix in TEXT_SUFFIXES
        and not IGNORED_PARTS.intersection(source_path.relative_to(ROOT).parts)
    ]


def test_distribution_is_named_after_the_repo() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    editable_packages = [
        package["name"]
        for package in lock["package"]
        if package.get("source", {}).get("editable") == "."
    ]

    assert pyproject()["project"]["name"] == DISTRIBUTION_NAME
    assert editable_packages == [DISTRIBUTION_NAME]


def test_import_package_keeps_its_name_for_carry_over_merges() -> None:
    wheel_target = pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel_target["packages"] == [IMPORT_PACKAGE]
    assert (ROOT / IMPORT_PACKAGE / "__init__.py").is_file()


def test_retired_distribution_name_is_gone() -> None:
    retired_name = "policyengine-" + "telemetry-collector"
    # The changelog and its fragments record the rename, so they keep the name.
    release_notes = {ROOT / "CHANGELOG.md", *(ROOT / "changelog.d").iterdir()}

    mentions = [
        str(source_path.relative_to(ROOT))
        for source_path in text_files()
        if source_path not in release_notes
        and retired_name in source_path.read_text(encoding="utf-8")
    ]

    assert (ROOT / "uv.lock") in text_files()
    assert mentions == []


def test_collector_is_mit_licensed_like_the_emitter() -> None:
    license_text = (ROOT / "LICENSE").read_bytes()

    assert license_text.startswith(b"MIT License\n\nCopyright (c) 2026 PolicyEngine\n")
    assert hashlib.sha256(license_text).hexdigest() == EMITTER_LICENSE_SHA256
    assert pyproject()["project"]["license"] == "MIT"
