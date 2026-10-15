"""Each release takes its version and changelog from typed Towncrier fragments."""

import re
import runpy
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VERSIONED_FILE = "pyproject.toml"
FRAGMENT_TYPES = ["breaking", "added", "changed", "fixed", "removed"]


@pytest.fixture
def bump_version() -> dict:
    return runpy.run_path(str(ROOT / "tools" / "bump-version.py"))


def release_tree(release_root: Path, *fragment_names: str) -> Path:
    shutil.copy(ROOT / VERSIONED_FILE, release_root / VERSIONED_FILE)
    (release_root / "changelog.d").mkdir()
    (release_root / "changelog.d" / ".gitkeep").touch()
    for fragment_name in fragment_names:
        (release_root / "changelog.d" / fragment_name).write_text("A change.\n")
    return release_root


def remove_version_line(pyproject_text: str) -> str:
    return re.sub(
        r'^version = "[^"]+"\n', "", pyproject_text, count=1, flags=re.MULTILINE
    )


def add_tool_version_line(pyproject_text: str) -> str:
    return pyproject_text + '\n[tool.example]\nversion = "9.9.9"\n'


def test_towncrier_collects_the_five_fragment_types() -> None:
    towncrier = tomllib.loads((ROOT / VERSIONED_FILE).read_text())["tool"]["towncrier"]

    assert towncrier["directory"] == "changelog.d"
    assert towncrier["filename"] == "CHANGELOG.md"
    assert [fragment["directory"] for fragment in towncrier["type"]] == FRAGMENT_TYPES
    assert all(fragment["showcontent"] for fragment in towncrier["type"])
    assert (ROOT / "changelog.d" / ".gitkeep").is_file()
    assert (ROOT / "CHANGELOG.md").is_file()


@pytest.mark.parametrize(
    ("fragment_types", "expected_version"),
    [
        ({"fixed"}, "1.4.3"),
        ({"fixed", "changed"}, "1.4.3"),
        ({"fixed", "added"}, "1.5.0"),
        ({"removed"}, "1.5.0"),
        ({"added", "breaking"}, "2.0.0"),
    ],
)
def test_fragment_types_choose_the_bump(
    bump_version: dict, fragment_types: set[str], expected_version: str
) -> None:
    assert bump_version["next_version"]("1.4.2", fragment_types) == expected_version


def test_release_rewrites_only_the_version_line(
    tmp_path: Path, bump_version: dict
) -> None:
    release_root = release_tree(tmp_path, "1.added.md", "repair.fixed.md")

    current_version, upcoming_version = bump_version["plan_release"](release_root)
    bump_version["write_version"](release_root, current_version, upcoming_version)

    assert upcoming_version == bump_version["next_version"](
        current_version, {"added", "fixed"}
    )
    assert bump_version["current_version"](release_root) == upcoming_version
    original_lines = (ROOT / VERSIONED_FILE).read_text().splitlines()
    updated_lines = (release_root / VERSIONED_FILE).read_text().splitlines()
    changed_lines = [
        (original_line, updated_line)
        for original_line, updated_line in zip(
            original_lines, updated_lines, strict=True
        )
        if original_line != updated_line
    ]
    assert changed_lines == [
        (f'version = "{current_version}"', f'version = "{upcoming_version}"')
    ]


@pytest.mark.parametrize(
    "fragment_names", [[], ["1.feature.md"], ["1.added.rst"], ["notes.md"]]
)
def test_release_refuses_missing_or_untyped_fragments(
    tmp_path: Path, bump_version: dict, fragment_names: list[str]
) -> None:
    release_root = release_tree(tmp_path, *fragment_names)

    with pytest.raises(ValueError, match="changelog fragment"):
        bump_version["plan_release"](release_root)


@pytest.mark.parametrize(
    "edit_pyproject",
    [remove_version_line, add_tool_version_line],
    ids=["missing", "duplicated"],
)
def test_release_refuses_a_missing_or_duplicated_version_line(
    tmp_path: Path, bump_version: dict, edit_pyproject
) -> None:
    release_root = release_tree(tmp_path, "1.fixed.md")
    pyproject_path = release_root / VERSIONED_FILE
    pyproject_path.write_text(edit_pyproject(pyproject_path.read_text()))

    with pytest.raises(ValueError, match="one version line"):
        bump_version["plan_release"](release_root)


def test_release_updates_the_version_changelog_and_lock_together() -> None:
    update_script = (ROOT / "tools" / "update-version.sh").read_text()

    assert "tools/bump-version.py" in update_script
    assert re.search(r'towncrier==\d+\.\d+\.\d+" towncrier build --yes', update_script)
    assert update_script.rstrip().endswith("uv lock")


def test_pull_requests_require_a_changelog_fragment() -> None:
    pull_request_workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "pr.yaml").read_text(),
        Loader=yaml.BaseLoader,
    )
    jobs = pull_request_workflow["jobs"]

    assert jobs["changelog"]["name"] == "Check changelog fragment"
    assert jobs["changelog"]["steps"][-1]["run"] == "bash tools/check-changelog.sh"
    assert "changelog" in jobs["required"]["needs"]
    assert "--check" in (ROOT / "tools" / "check-changelog.sh").read_text()
