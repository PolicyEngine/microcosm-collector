"""A release repo's ``releases.json``, read with every build it records.

``releases.json`` is the one record of which build of each release a
Microcosm dataset repo has published, and which is current:

    releases.json
    releases/<release>/<build>/release_manifest.json
    releases/<release>/<build>/...

``parse_releases_record`` refuses exactly what the tree package's
``parseReleasesRecordBytes`` (``src/release-record.ts``) refuses, and so what
the tree handler's ``calibration_trees.current_default_build`` refuses: a
record larger than 4 MiB, one that is not a readable JSON document, one
without exactly its keys or with a ``schema_version`` other than 1, a release
or build name that is not a safe identifier, a role that is not one, a
malformed build, a ``current`` that is not a listed build or is withdrawn, or
a ``default`` that is not a listed release. It returns every build, withdrawn
ones included, ordered by release and then build, as the package's
``recordedBuilds`` orders them.

The tree handler's ``current_default_build`` returns only the current build
of the default release, and this module returns every build. Both follow
the same rules; once the rule that makes a country's latest tree follow
``releases.json`` is in ``main``, one parser is to replace both.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

RELEASES_RECORD_PATH: Final = "releases.json"
#: The largest releases.json the record's writer accepts.
MAX_RELEASES_RECORD_BYTES: Final = 4 * 1024 * 1024
#: The emitter interface's SAFE_IDENTIFIER and ROLE_IDENTIFIER.
_SAFE_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ROLE_IDENTIFIER: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RECORD_KEYS: Final = frozenset({"default", "releases", "schema_version"})
_RELEASE_KEYS: Final = frozenset({"builds", "current", "role"})
_BUILD_KEYS: Final = frozenset({"published_at", "withdrawn"})


class ReleasesRecordRefusedError(ValueError):
    """A releases.json that breaks the record's rules."""


@dataclass(frozen=True)
class RecordedBuild:
    """One build the record lists, with what it says about the build and its
    release."""

    release: str
    build: str
    #: As the writer recorded it; not reformatted.
    published_at: str
    withdrawn: bool
    #: The build is its release's current build.
    current: bool
    #: The build's release is the record's default release.
    in_default_release: bool

    @property
    def build_id(self) -> str:
        """The build's folder under ``releases/``: ``<release>/<build>``."""

        return f"{self.release}/{self.build}"

    @property
    def current_default(self) -> bool:
        """The build is the repo's current build: its default release's current."""

        return self.current and self.in_default_release


@dataclass(frozen=True)
class ReleasesRecord:
    """A checked releases.json."""

    #: The release a repo's default dataset comes from, or ``None``.
    default: str | None
    #: Every listed build, ordered by release, then build.
    builds: tuple[RecordedBuild, ...]

    @property
    def current_default_build(self) -> RecordedBuild | None:
        """The current build of the default release, or ``None`` while the
        record names no default release or that release has no current build."""

        return next((build for build in self.builds if build.current_default), None)


def _refuse(message: str) -> ReleasesRecordRefusedError:
    return ReleasesRecordRefusedError(f"{RELEASES_RECORD_PATH}: {message}")


def _is_safe_identifier(value: object) -> bool:
    return isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) is not None


def _reject_constant(constant: str) -> None:
    # JSON.parse refuses NaN and Infinity, which Python's json reads.
    raise ValueError(f"{constant} is not JSON.")


def _document(content: bytes) -> Any:
    if len(content) > MAX_RELEASES_RECORD_BYTES:
        raise _refuse(f"larger than {MAX_RELEASES_RECORD_BYTES} bytes.")
    try:
        # As TextDecoder reads it: invalid UTF-8 refused, a leading BOM dropped.
        return json.loads(content.decode("utf-8-sig"), parse_constant=_reject_constant)
    except ValueError:
        raise _refuse("not a readable JSON document.") from None


def parse_releases_record(content: bytes) -> ReleasesRecord:
    """Every build ``content`` records; ``ReleasesRecordRefusedError`` for a
    record the rules refuse."""

    document = _document(content)
    if not isinstance(document, dict) or set(document) != _RECORD_KEYS:
        raise _refuse("must have exactly default, releases, schema_version.")
    schema_version = document["schema_version"]
    if isinstance(schema_version, bool) or schema_version != 1:
        raise _refuse("schema_version must be 1.")
    releases = document["releases"]
    if not isinstance(releases, dict):
        raise _refuse("releases must be an object.")
    for release, entry in releases.items():
        if not _is_safe_identifier(release):
            raise _refuse("a release name is not a safe identifier.")
        if not isinstance(entry, dict) or set(entry) != _RELEASE_KEYS:
            raise _refuse(f"release {release} must have builds, current, role.")
        role = entry["role"]
        if not isinstance(role, str) or _ROLE_IDENTIFIER.fullmatch(role) is None:
            raise _refuse(f"release {release} has an invalid role.")
        if entry["current"] is not None and not isinstance(entry["current"], str):
            raise _refuse(f"release {release} current must be a build or null.")
        builds = entry["builds"]
        if not isinstance(builds, dict):
            raise _refuse(f"release {release} builds must be an object.")
        for build, build_entry in builds.items():
            if not _is_safe_identifier(build):
                raise _refuse("a build name is not a safe identifier.")
            if (
                not isinstance(build_entry, dict)
                or set(build_entry) != _BUILD_KEYS
                or not isinstance(build_entry["published_at"], str)
                or not build_entry["published_at"]
                or not isinstance(build_entry["withdrawn"], bool)
            ):
                raise _refuse(f"build {build} of {release} is malformed.")
    default = document["default"]
    if default is not None and not isinstance(default, str):
        raise _refuse("default must be a release name or null.")
    if default is not None and default not in releases:
        raise _refuse(f"default {default} is not listed.")
    for release, entry in releases.items():
        current = entry["current"]
        if current is None:
            continue
        if current not in entry["builds"]:
            raise _refuse(f"current build {current} of {release} is not listed.")
        if entry["builds"][current]["withdrawn"]:
            raise _refuse(f"current build {current} of {release} is withdrawn.")
    return ReleasesRecord(
        default=default,
        builds=tuple(
            RecordedBuild(
                release=release,
                build=build,
                published_at=releases[release]["builds"][build]["published_at"],
                withdrawn=releases[release]["builds"][build]["withdrawn"],
                current=releases[release]["current"] == build,
                in_default_release=default == release,
            )
            # Safe identifiers are ASCII, so Python's order is JavaScript's.
            for release in sorted(releases)
            for build in sorted(releases[release]["builds"])
        ),
    )
