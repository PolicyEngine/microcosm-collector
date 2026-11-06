"""Give the read API client the collector's release version.

packages/collector-client is published to npm with the version of the
collector release whose read API it describes. tools/update-version.sh runs
this after bump-version.py, so the release commit carries the same version in
pyproject.toml, the client's package.json and its package-lock.json.

    set-client-version.py [--root DIRECTORY]
"""

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = Path("packages") / "collector-client"


def collector_version(root: Path) -> str:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    return pyproject["project"]["version"]


def _rewrite_json(json_path: Path, update) -> None:
    value = json.loads(json_path.read_text())
    update(value)
    # npm writes these files as two-space JSON with a final newline.
    json_path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def set_client_version(root: Path, version: str) -> None:
    def update_package(package: dict) -> None:
        package["version"] = version

    def update_lock(lock: dict) -> None:
        lock["version"] = version
        lock["packages"][""]["version"] = version

    _rewrite_json(root / CLIENT_DIR / "package.json", update_package)
    _rewrite_json(root / CLIENT_DIR / "package-lock.json", update_lock)


def main(arguments: list[str]) -> None:
    if arguments == []:
        root = ROOT
    elif len(arguments) == 2 and arguments[0] == "--root":
        root = Path(arguments[1])
    else:
        sys.exit("Usage: set-client-version.py [--root DIRECTORY]")
    version = collector_version(root)
    set_client_version(root, version)
    print(f"Client version: {version}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
