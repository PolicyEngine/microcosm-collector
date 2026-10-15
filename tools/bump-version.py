"""Choose the next release version from pending changelog fragments.

A breaking fragment bumps the major version, an added or removed fragment the
minor version, and any other fragment the patch version. The version is read
from and written to the single version line in pyproject.toml.

Prints the next version. With --check, writes nothing.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT_TYPES = ("breaking", "added", "changed", "fixed", "removed")
FRAGMENT_HELP = (
    "Name each fragment changelog.d/<name>.<type>.md, where <type> is one of: "
    + ", ".join(FRAGMENT_TYPES)
)
VERSIONED_FILE = "pyproject.toml"
PYPROJECT_VERSION = re.compile(r'^version = "(\d+\.\d+\.\d+)"$', re.MULTILINE)


def fragment_types(changelog_dir: Path) -> set[str]:
    types = set()
    for fragment in changelog_dir.iterdir():
        if fragment.name == ".gitkeep":
            continue
        parts = fragment.name.split(".")
        if (
            not fragment.is_file()
            or len(parts) < 3
            or parts[-1] != "md"
            or parts[-2] not in FRAGMENT_TYPES
        ):
            raise ValueError(
                f"Unrecognised changelog fragment {fragment.name}. {FRAGMENT_HELP}."
            )
        types.add(parts[-2])
    if not types:
        raise ValueError(f"No changelog fragment to release. {FRAGMENT_HELP}.")
    return types


def next_version(version: str, types: set[str]) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if "breaking" in types:
        return f"{major + 1}.0.0"
    if types & {"added", "removed"}:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def current_version(root: Path) -> str:
    versions = PYPROJECT_VERSION.findall((root / VERSIONED_FILE).read_text())
    if len(versions) != 1:
        raise ValueError(
            f"Expected one version line in {VERSIONED_FILE}, found {versions}."
        )
    return versions[0]


def plan_release(root: Path) -> tuple[str, str]:
    current = current_version(root)
    return current, next_version(current, fragment_types(root / "changelog.d"))


def write_version(root: Path, current: str, upcoming: str) -> None:
    path = root / VERSIONED_FILE
    path.write_text(
        PYPROJECT_VERSION.sub(
            lambda match: match.group(0).replace(current, upcoming),
            path.read_text(),
            count=1,
        )
    )


def main(arguments: list[str]) -> None:
    if arguments not in ([], ["--check"]):
        sys.exit("Usage: bump-version.py [--check]")
    try:
        current, upcoming = plan_release(ROOT)
    except ValueError as error:
        sys.exit(str(error))
    if not arguments:
        write_version(ROOT, current, upcoming)
    print(f"Version: {current} -> {upcoming}", file=sys.stderr)
    print(upcoming)


if __name__ == "__main__":
    main(sys.argv[1:])
