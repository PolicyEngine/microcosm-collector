"""Backfill the collector's calibration tree store from production's tree manifest.

Before the dashboard reads calibration trees from the collector's own store,
that store must hold every build the dashboard lists today. The
``backfill-calibration-trees`` operator command takes the builds from
production's public tree manifest, ``GET <URL>?country=<cc>``, which it
requests without any credential and never through the Hugging Face reader,
so the read token cannot reach it.

**Checks.** An answer that is not HTTP 200, is larger than
``MAX_PRODUCTION_MANIFEST_BYTES``, is not JSON, has another schema version or
country, or names a latest build that is not one of its release builds, is
an ``error`` for that country, and the other countries continue. Each entry
is checked before any of its values is used in a Hugging Face path
(``production_entry``): its kind, its repo (the country's release repo, or
for a staging build its staging repo or its release repo), a commit of 40
lowercase hex digits, a release ID and release directory that are a
``<release>`` or ``<release>/<build>`` folder, a staging run that is one
path segment and not ``latest``, a publication time with an offset or none,
and a label. An entry that fails is ``invalid``, and the rest of its country
continues.

**Planning.** A release build is read from ``releases/<releaseDirectory or
releaseId>/`` at its commit: ``calibration_diagnostics.json``, and
``build_manifest.json``, ``release_manifest.json`` and ``demographics.json``
when the folder lists them (null otherwise, as the GitHub Actions
publisher's optional reads are). A staging build follows only the rules of
the tree package's staging readers that locate its files:

- in the staging repo, its ``runs/<id>/run_manifest.json`` and
  ``progress.json`` (read to plan, not built from) say where its
  diagnostics are: at the run manifest's declared
  ``calibration_diagnostics`` path, with the declared SHA-256 checked, or,
  for a schema-1 run, ``runs/<id>/calibration_diagnostics.json``; with its
  ``build_manifest.json`` and ``release_manifest.json``;
- a run whose manifest declares a ``staged_dataset`` receipt instead is
  read from the staged bundle the receipt names, in the country's release
  repo at the receipt's revision, with the receipt's SHA-256 checked;
- an entry whose repo is the release repo is a staged bundle at
  ``staged/<id>/``: its ``calibration_diagnostics.json`` or its single
  ``<dataset>.diagnostics.json``, diagnostics only.

Each build's label, publication time, repo and commit come from production's
entry, so its tree records what production's does.

**The latest build.** Only the entry production's
``latestReleaseBuildArtifactId`` names asks to become its country's latest,
and every country's latest is planned before any other build, so a time limit
never leaves one out. The publication command still moves the latest only to
a build at least as new as the store's current one; an entry it did not make
the latest is ``latest_not_moved``. Production's latest when the store
already holds it but it is not the store's latest is built and published
again under the same build ID, with its stored publication time: every part
is kept, and only the latest moves.

**The store.** The store's manifest is read once, with the tree package's
read-only manifest read. An entry whose release ID or staging run the store
already holds, with the same commit and publication time, is
``already_stored``; with another, ``mismatch``. Neither downloads anything.

**Building.** One build at a time, in its own temporary directory, which is
removed afterwards: each file is streamed to disk and checked against its
listing (``HuggingFaceReader.download_file``), and the tree is built and
published by ``calibration_trees.build_and_publish``, as a submission's is: the
worker without the Blob token, the publication with it, under the manifest
lock and nothing else. A file listed as larger than
``MAX_SOURCE_FILE_BYTES`` is ``too_large`` before anything is downloaded. No
build starts after ``BUILD_TIME_LIMIT_SECONDS`` of the execution; the rest
are ``deferred``, so a Cloud Run task retry carries on past what is stored.

**Parity.** Each built or stored entry says whether its build ID matches
production's (``matches_production``). Release builds are expected to match.
Staging builds are expected to differ: the worker hashes 4 of a staging
build's source files, where the GitHub Actions publisher hashes 7.

A dry run reads production's manifest, the Hugging Face listings and the
store's manifest, and reports ``would_build``; it downloads no diagnostics,
runs no tree worker and takes no lock.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

import httpx

from telemetry_collector.calibration_trees import (
    MAX_MESSAGE_CHARACTERS,
    MISSING_REPOSITORY_OR_REVISION,
    RELEASE_REPOS,
    SAFE_IDENTIFIER,
    STAGING_REPOS,
    PublishedTree,
    TreeBuildPlan,
    TreeCommands,
    TreeRefusedError,
    TreeUnbuildableError,
    build_and_publish,
    read_store_manifest,
)
from telemetry_collector.huggingface_read import (
    DatasetFile,
    HuggingFaceReader,
    HuggingFaceReadError,
)

BackfillOutcomeKind = Literal[
    "would_build",
    "built",
    "already_stored",
    "mismatch",
    "invalid",
    "too_large",
    "unbuildable",
    "refused",
    "error",
    "deferred",
    "latest_not_moved",
]
BuildKind = Literal["release", "staging"]

#: The tree package's CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION.
MANIFEST_SCHEMA_VERSION: Final = 6
#: The largest answer read from production's tree manifest, for one country.
MAX_PRODUCTION_MANIFEST_BYTES: Final = 16 * 1024 * 1024
PRODUCTION_MANIFEST_TIMEOUT_SECONDS: Final = 60.0
#: The largest file a tree is built from: the worker job's memory holds the
#: downloaded file, the build (the largest known diagnostics, 128.7 MB, peaked
#: at 3.5 GB) and the written parts.
MAX_SOURCE_FILE_BYTES: Final = 256 * 1024 * 1024
#: No build starts after this long, within the worker job's one-hour task.
BUILD_TIME_LIMIT_SECONDS: Final = 40 * 60
BUILD_KINDS: Final[tuple[BuildKind, ...]] = ("release", "staging")

_BUILD_ARTIFACT_ID: Final = re.compile(r"[0-9a-f]{64}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_COMMIT: Final = re.compile(r"[0-9a-f]{40}")
#: As the tree worker accepts a publication time: seconds and an offset.
_PUBLICATION_TIME: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})"
)
# A release build folder's files, by the worker's role names; only the
# diagnostics are required.
_RELEASE_FILES: Final = (
    ("calibration_diagnostics", "calibration_diagnostics.json"),
    ("build_manifest", "build_manifest.json"),
    ("release_manifest", "release_manifest.json"),
    ("demographics", "demographics.json"),
)
# A staging run's other files, in its run folder.
_STAGING_RUN_FILES: Final = (
    ("build_manifest", "build_manifest.json"),
    ("release_manifest", "release_manifest.json"),
)
_STAGED_BUNDLE_DIAGNOSTICS: Final = "calibration_diagnostics.json"
_STAGED_BUNDLE_STEM_DIAGNOSTICS_SUFFIX: Final = ".diagnostics.json"
_STAGED_BUNDLE_FILE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
_STAGED_BUNDLE_LANDED: Final = frozenset({"uploaded", "already_staged"})


@dataclass(frozen=True)
class BackfillOutcome:
    """What the backfill did with one entry of production's manifest, or a country.

    ``source_id`` is the entry's release ID or staging run, the alias the
    store records it under. An outcome for a whole country has no kind and
    no source.
    """

    country: str
    outcome: BackfillOutcomeKind
    kind: BuildKind | None = None
    source_id: str | None = None
    reason: str | None = None
    hf_repo: str | None = None
    hf_commit: str | None = None
    created_at: str | None = None
    #: Production names this entry as its country's latest.
    make_latest: bool = False
    production_build_artifact_id: str | None = None
    #: The build ID published, or already stored, for this entry.
    build_artifact_id: str | None = None
    matches_production: bool | None = None
    uploaded_parts: int | None = None
    kept_parts: int | None = None
    #: Whether the store's latest is this build now; ``None`` when nothing
    #: was published for it, as in a dry run.
    made_latest: bool | None = None
    #: A later run should try it again: an ``error`` or ``deferred``.
    incomplete: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProductionEntry:
    """One checked entry of production's manifest."""

    country: str
    kind: BuildKind
    build_artifact_id: str
    #: The release ID or staging run.
    source_id: str
    label: str
    #: A release's folder under ``releases/``; ``None`` for a staging build.
    release_directory: str | None
    hf_repo: str
    hf_commit: str
    created_at: str | None
    make_latest: bool


class ProductionManifestError(Exception):
    """Production's answer for a country can't be used."""


class InvalidEntryError(Exception):
    """An entry of production's manifest that fails the checks."""


class SourceUnbuildableError(Exception):
    """A build's files can't be found or don't match what records them."""


def _shortened(text: str) -> str:
    return (
        text
        if len(text) <= MAX_MESSAGE_CHARACTERS
        else text[: MAX_MESSAGE_CHARACTERS - 1] + "…"
    )


def _described(error: BaseException) -> str:
    return _shortened(f"{type(error).__name__}: {error}")


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_release_folder(value: object) -> bool:
    """A ``<release>`` or ``<release>/<build>`` folder, as the tree worker accepts."""

    if not isinstance(value, str) or value == "latest":
        return False
    segments = value.split("/")
    return len(segments) in {1, 2} and all(
        SAFE_IDENTIFIER.fullmatch(segment) for segment in segments
    )


def _is_run_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value != "latest"
        and SAFE_IDENTIFIER.fullmatch(value) is not None
    )


def _is_publication_time(value: object) -> bool:
    if not _matches(_PUBLICATION_TIME, value):
        return False
    assert isinstance(value, str)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _same_instant(stored: object, produced: str | None) -> bool:
    if stored is None or produced is None:
        return stored is None and produced is None
    if not isinstance(stored, str):
        return False
    try:
        return datetime.fromisoformat(stored) == datetime.fromisoformat(produced)
    except ValueError:
        return stored == produced


# Production's manifest ---------------------------------------------------------


def read_production_manifest(
    client: httpx.Client, url: str, country: str
) -> dict[str, Any]:
    """Production's tree manifest for ``country``, checked as a whole.

    Sent with no credential and no redirect followed, and read up to
    ``MAX_PRODUCTION_MANIFEST_BYTES``. Raises ``ProductionManifestError``.
    """

    description = f"Production's tree manifest for {country}"
    content = bytearray()
    try:
        with client.stream(
            "GET", url, params={"country": country}, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise ProductionManifestError(
                    f"{description} answered HTTP {response.status_code}."
                )
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_PRODUCTION_MANIFEST_BYTES:
                    raise ProductionManifestError(
                        f"{description} is larger than "
                        f"{MAX_PRODUCTION_MANIFEST_BYTES} bytes."
                    )
    except httpx.HTTPError as error:
        raise ProductionManifestError(
            f"{description} could not be read: {type(error).__name__}."
        ) from error
    try:
        document = json.loads(bytes(content))
    except ValueError:
        raise ProductionManifestError(f"{description} is not JSON.") from None
    if not isinstance(document, dict):
        raise ProductionManifestError(f"{description} is not a JSON object.")
    schema_version = document.get("schemaVersion")
    if isinstance(schema_version, bool) or schema_version != MANIFEST_SCHEMA_VERSION:
        raise ProductionManifestError(
            f"{description} has schema version {schema_version!r}, not "
            f"{MANIFEST_SCHEMA_VERSION}."
        )
    if document.get("country") != country:
        raise ProductionManifestError(
            f"{description} is for country {document.get('country')!r}."
        )
    builds = document.get("builds")
    if not isinstance(builds, list):
        raise ProductionManifestError(f"{description} has no list of builds.")
    latest = document.get("latestReleaseBuildArtifactId")
    if latest is not None and not any(
        isinstance(build, dict)
        and build.get("buildArtifactId") == latest
        and build.get("kind") == "release"
        for build in builds
    ):
        raise ProductionManifestError(
            f"{description} names a latest build that is not one of its release builds."
        )
    return document


def production_entry(
    raw: object, country: str, latest_build_artifact_id: str | None
) -> ProductionEntry:
    """Check one entry before any of its values is used; raises ``InvalidEntryError``."""

    if not isinstance(raw, dict):
        raise InvalidEntryError("The entry is not a JSON object.")
    build_artifact_id = raw.get("buildArtifactId")
    if not _matches(_BUILD_ARTIFACT_ID, build_artifact_id):
        raise InvalidEntryError("The entry has no valid build ID.")
    kind = raw.get("kind")
    if kind not in BUILD_KINDS:
        raise InvalidEntryError("The entry's kind is neither release nor staging.")
    allowed_repos = {RELEASE_REPOS[country]}
    if kind == "staging" and country in STAGING_REPOS:
        allowed_repos.add(STAGING_REPOS[country])
    hf_repo = raw.get("hfRepo")
    if not isinstance(hf_repo, str) or hf_repo not in allowed_repos:
        raise InvalidEntryError(
            f"The entry's Hugging Face repo is not one {country.upper()} {kind} "
            "builds come from."
        )
    hf_commit = raw.get("hfCommitSha")
    if not _matches(_COMMIT, hf_commit):
        raise InvalidEntryError(
            "The entry's Hugging Face commit is not 40 lowercase hex digits."
        )
    created_at = raw.get("createdAt")
    if created_at is not None and not _is_publication_time(created_at):
        raise InvalidEntryError(
            "The entry's createdAt is neither null nor an ISO 8601 time with an offset."
        )
    label = raw.get("label")
    if not isinstance(label, str) or not label:
        raise InvalidEntryError("The entry has no label.")
    release_id = raw.get("releaseId")
    release_directory = raw.get("releaseDirectory")
    staging_run_id = raw.get("stagingRunId")
    if kind == "release":
        if staging_run_id is not None:
            raise InvalidEntryError("The release entry names a staging run.")
        if not _is_release_folder(release_id):
            raise InvalidEntryError(
                "The entry's release ID is not a <release> or <release>/<build> folder."
            )
        if release_directory is not None and not _is_release_folder(release_directory):
            raise InvalidEntryError(
                "The entry's release directory is not a <release> or "
                "<release>/<build> folder."
            )
        source_id = release_id
        folder = release_directory or release_id
    else:
        if release_id is not None or release_directory is not None:
            raise InvalidEntryError("The staging entry names a release.")
        if not _is_run_id(staging_run_id):
            raise InvalidEntryError(
                "The entry's staging run is not one path segment, or is latest."
            )
        source_id = staging_run_id
        folder = None
    return ProductionEntry(
        country=country,
        kind=kind,
        build_artifact_id=build_artifact_id,
        source_id=source_id,
        label=label,
        release_directory=folder,
        hf_repo=hf_repo,
        hf_commit=hf_commit,
        created_at=created_at,
        make_latest=build_artifact_id == latest_build_artifact_id,
    )


def _invalid_outcome(
    raw: object, country: str, latest_build_artifact_id: str | None, reason: str
) -> BackfillOutcome:
    fields: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    kind = fields.get("kind")
    alias = fields.get("releaseId") if kind == "release" else fields.get("stagingRunId")
    make_latest = (
        latest_build_artifact_id is not None
        and fields.get("buildArtifactId") == latest_build_artifact_id
    )
    return BackfillOutcome(
        country=country,
        outcome="invalid",
        kind=kind if kind in BUILD_KINDS else None,
        source_id=_shortened(alias) if isinstance(alias, str) else None,
        reason=reason,
        make_latest=make_latest,
        made_latest=False if make_latest else None,
    )


# The collector's store -----------------------------------------------------------


@dataclass(frozen=True)
class StoredTrees:
    """The store's manifest, as its read-only manifest read printed it."""

    manifest: Mapping[str, Any]

    def _country(self, country: str) -> Mapping[str, Any]:
        countries = self.manifest.get("countries")
        stored = countries.get(country) if isinstance(countries, dict) else None
        return stored if isinstance(stored, dict) else {}

    def latest(self, country: str) -> str | None:
        return self._country(country).get("latestReleaseBuildArtifactId")

    def entry_for(self, entry: ProductionEntry) -> Mapping[str, Any] | None:
        """The stored build recorded under the entry's release ID or staging run."""

        alias_field = "releaseId" if entry.kind == "release" else "stagingRunId"
        for build in self._country(entry.country).get("builds", []):
            if build.get("kind") == entry.kind and build.get(alias_field) == (
                entry.source_id
            ):
                return build
        return None


# Planning from Hugging Face ------------------------------------------------------


@dataclass(frozen=True)
class SourceFile:
    """One file a tree is built from, as Hugging Face lists it."""

    dataset: str
    revision: str
    listed: DatasetFile
    #: The SHA-256 a run manifest or receipt declares for it, when one does.
    declared_sha256: str | None = None


@dataclass(frozen=True)
class BuildSource:
    plan: TreeBuildPlan
    files: Mapping[str, SourceFile]


def _is_missing_entry(error: HuggingFaceReadError) -> bool:
    """A 404 for the path itself, not for its repo or revision."""

    return (
        error.status_code == 404
        and error.error_code not in MISSING_REPOSITORY_OR_REVISION
    )


class _Listings:
    """Each folder's files by name, listed once per dataset and revision."""

    def __init__(self, reader: HuggingFaceReader) -> None:
        self._reader = reader
        self._folders: dict[tuple[str, str, str], dict[str, DatasetFile]] = {}

    def folder(
        self, dataset: str, revision: str, folder: str
    ) -> dict[str, DatasetFile]:
        key = (dataset, revision, folder)
        if key not in self._folders:
            try:
                listed = self._reader.list_folder(dataset, folder, revision)
            except HuggingFaceReadError as error:
                if not _is_missing_entry(error):
                    raise
                listed = []
            self._folders[key] = {
                PurePosixPath(dataset_file.path).name: dataset_file
                for dataset_file in listed
            }
        return self._folders[key]


def _read_json(
    reader: HuggingFaceReader, dataset: str, path: str, revision: str
) -> tuple[dict[str, Any], bytes] | None:
    """A JSON object read whole, with its bytes, or ``None`` when it is absent."""

    try:
        content = reader.read_file(dataset, path, revision)
    except HuggingFaceReadError as error:
        if _is_missing_entry(error):
            return None
        raise
    try:
        document = json.loads(content)
    except ValueError:
        raise SourceUnbuildableError(f"{path} is not valid JSON.") from None
    if not isinstance(document, dict):
        raise SourceUnbuildableError(f"{path} must be a JSON object.")
    return document, content


def _release_source(entry: ProductionEntry, listings: _Listings) -> BuildSource:
    assert entry.release_directory is not None
    folder = f"releases/{entry.release_directory}"
    listed = listings.folder(entry.hf_repo, entry.hf_commit, folder)
    files = {
        role: SourceFile(entry.hf_repo, entry.hf_commit, listed[name])
        for role, name in _RELEASE_FILES
        if name in listed
    }
    if "calibration_diagnostics" not in files:
        raise SourceUnbuildableError(
            f"{folder}/calibration_diagnostics.json is not in {entry.hf_repo} at "
            f"{entry.hf_commit}."
        )
    return BuildSource(
        plan=TreeBuildPlan(
            kind="release",
            country=entry.country,
            folder=folder,
            hf_repo=entry.hf_repo,
            hf_commit=entry.hf_commit,
            created_at=entry.created_at,
            run_id=None,
            release_id=entry.source_id,
            label=None,
            make_latest=entry.make_latest,
            paths={role: file.listed.path for role, file in files.items()},
            release_directory=entry.release_directory,
        ),
        files=files,
    )


def _schema_version(document: Mapping[str, Any]) -> object:
    version = document.get("schema_version")
    return 1 if version is None else version


def _is_structured(document: Mapping[str, Any] | None) -> bool:
    version = None if document is None else document.get("schema_version")
    return (
        isinstance(version, int | float)
        and not isinstance(version, bool)
        and version >= 2
    )


def _check_run_documents(
    run_id: str,
    run_manifest: Mapping[str, Any] | None,
    progress: Mapping[str, Any] | None,
) -> None:
    """The staging readers' rules for a run's documents: one version, one run."""

    documents = [
        document for document in (run_manifest, progress) if document is not None
    ]
    versions = [_schema_version(document) for document in documents]
    if any(version != versions[0] for version in versions[1:]):
        raise SourceUnbuildableError(f"Run {run_id} mixes document versions.")
    # Version 1 documents name no run to check.
    if not versions or versions[0] == 1:
        return
    for label, document in (("run manifest", run_manifest), ("progress", progress)):
        if document is not None and document.get("run_id") != run_id:
            raise SourceUnbuildableError(f"Run {run_id}'s {label} names another run.")


def _declared_artifacts(
    run_manifest: Mapping[str, Any], run_id: str
) -> dict[str, tuple[str, str]]:
    """Each artifact a structured run manifest declares: its path and SHA-256."""

    artifacts = run_manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise SourceUnbuildableError(
            f"Run {run_id}'s manifest declares no list of artifacts."
        )
    declared: dict[str, tuple[str, str]] = {}
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise SourceUnbuildableError(
                f"Run {run_id}'s artifact {index} is malformed."
            )
        name = artifact.get("logical_name")
        if not _matches(SAFE_IDENTIFIER, name):
            raise SourceUnbuildableError(
                f"Run {run_id}'s artifact {index} has no safe logical name."
            )
        assert isinstance(name, str)
        path = artifact.get("contract_relative_path")
        if path != f"artifacts/{name}.json":
            raise SourceUnbuildableError(
                f"Run {run_id}'s artifact {name} has an unsafe path."
            )
        digest = artifact.get("sha256")
        if not _matches(_SHA256, digest):
            raise SourceUnbuildableError(
                f"Run {run_id}'s artifact {name} has an invalid digest."
            )
        if name in declared:
            raise SourceUnbuildableError(
                f"Run {run_id}'s artifact {name} appears more than once."
            )
        assert isinstance(digest, str)
        declared[name] = (f"runs/{run_id}/{path}", digest)
    return declared


def _staged_bundle_diagnostics_file(files: Mapping[str, Any] | None) -> str | None:
    """A staged bundle's diagnostics file among ``files``, as the staging reader picks it.

    ``calibration_diagnostics.json``, or else the one ``<dataset>.diagnostics.json``
    whose name is one path segment; none, or several, is no file.
    """

    if not files:
        return None
    if _STAGED_BUNDLE_DIAGNOSTICS in files:
        return _STAGED_BUNDLE_DIAGNOSTICS
    stem_files = [
        name
        for name in files
        if isinstance(name, str)
        and name.endswith(_STAGED_BUNDLE_STEM_DIAGNOSTICS_SUFFIX)
    ]
    if len(stem_files) == 1 and _STAGED_BUNDLE_FILE_NAME.fullmatch(stem_files[0]):
        return stem_files[0]
    return None


def _staged_bundle_source(
    entry: ProductionEntry,
    listings: _Listings,
    *,
    dataset: str,
    revision: str,
    file_name: str | None = None,
    declared_sha256: str | None = None,
) -> BuildSource:
    folder = f"staged/{entry.source_id}"
    listed = listings.folder(dataset, revision, folder)
    name = file_name or _staged_bundle_diagnostics_file(listed)
    if name is None or name not in listed:
        raise SourceUnbuildableError(
            f"{folder} holds no calibration diagnostics in {dataset} at {revision}."
        )
    diagnostics = SourceFile(dataset, revision, listed[name], declared_sha256)
    return BuildSource(
        plan=TreeBuildPlan(
            kind="staging",
            country=entry.country,
            folder=folder,
            hf_repo=entry.hf_repo,
            hf_commit=entry.hf_commit,
            created_at=entry.created_at,
            run_id=entry.source_id,
            release_id=None,
            label=entry.label,
            make_latest=False,
            paths={"calibration_diagnostics": diagnostics.listed.path},
        ),
        files={"calibration_diagnostics": diagnostics},
    )


def _receipt_source(
    entry: ProductionEntry,
    reader: HuggingFaceReader,
    listings: _Listings,
    receipt: tuple[str, str],
) -> BuildSource:
    """The staged bundle a run's ``staged_dataset`` receipt names."""

    run_id = entry.source_id
    receipt_path, receipt_sha256 = receipt
    read = _read_json(reader, entry.hf_repo, receipt_path, entry.hf_commit)
    if read is None:
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged dataset receipt is declared but absent."
        )
    delivery, content = read
    if hashlib.sha256(content).hexdigest() != receipt_sha256:
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged dataset receipt does not match the digest its "
            "run manifest declares."
        )
    if delivery.get("status") not in _STAGED_BUNDLE_LANDED:
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged dataset receipt records no staged bundle."
        )
    if delivery.get("run_id") != run_id:
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged dataset receipt names another run."
        )
    # The token is only ever sent to the country's own release repo.
    if delivery.get("repository") != RELEASE_REPOS[entry.country]:
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged dataset receipt names a repo that is not "
            f"{entry.country.upper()}'s release repo."
        )
    if delivery.get("prefix") != f"staged/{run_id}":
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged dataset receipt's prefix is not staged/{run_id}."
        )
    revision = delivery.get("revision")
    if not _matches(_COMMIT, revision):
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged dataset receipt's revision is not a commit."
        )
    assert isinstance(revision, str)
    files = delivery.get("files")
    recorded = files if isinstance(files, dict) else None
    name = _staged_bundle_diagnostics_file(recorded)
    declared = recorded.get(name) if recorded is not None and name else None
    digest = declared.get("sha256") if isinstance(declared, dict) else None
    if name is None or not _matches(_SHA256, digest):
        raise SourceUnbuildableError(
            f"Run {run_id}'s staged bundle records no calibration diagnostics with "
            "a SHA-256."
        )
    assert isinstance(digest, str)
    return _staged_bundle_source(
        entry,
        listings,
        dataset=RELEASE_REPOS[entry.country],
        revision=revision,
        file_name=name,
        declared_sha256=digest,
    )


def _staging_source(
    entry: ProductionEntry, reader: HuggingFaceReader, listings: _Listings
) -> BuildSource:
    run_id = entry.source_id
    if entry.hf_repo == RELEASE_REPOS[entry.country]:
        return _staged_bundle_source(
            entry, listings, dataset=entry.hf_repo, revision=entry.hf_commit
        )
    run_folder = f"runs/{run_id}"
    run_manifest_read = _read_json(
        reader, entry.hf_repo, f"{run_folder}/run_manifest.json", entry.hf_commit
    )
    progress_read = _read_json(
        reader, entry.hf_repo, f"{run_folder}/progress.json", entry.hf_commit
    )
    run_manifest = None if run_manifest_read is None else run_manifest_read[0]
    progress = None if progress_read is None else progress_read[0]
    _check_run_documents(run_id, run_manifest, progress)
    diagnostics_sha256: str | None = None
    if _is_structured(run_manifest):
        assert run_manifest is not None
        artifacts = _declared_artifacts(run_manifest, run_id)
        if "calibration_diagnostics" not in artifacts:
            # A run that staged its dataset bundle carries its diagnostics there.
            if "staged_dataset" not in artifacts:
                raise SourceUnbuildableError(
                    f"Run {run_id}'s manifest declares neither calibration "
                    "diagnostics nor a staged dataset."
                )
            return _receipt_source(entry, reader, listings, artifacts["staged_dataset"])
        diagnostics_path, diagnostics_sha256 = artifacts["calibration_diagnostics"]
    else:
        diagnostics_path = f"{run_folder}/calibration_diagnostics.json"
    diagnostics_folder, _, diagnostics_name = diagnostics_path.rpartition("/")
    listed_diagnostics = listings.folder(
        entry.hf_repo, entry.hf_commit, diagnostics_folder
    ).get(diagnostics_name)
    if listed_diagnostics is None:
        raise SourceUnbuildableError(
            f"{diagnostics_path} is not in {entry.hf_repo} at {entry.hf_commit}."
        )
    files = {
        "calibration_diagnostics": SourceFile(
            entry.hf_repo, entry.hf_commit, listed_diagnostics, diagnostics_sha256
        )
    }
    run_files = listings.folder(entry.hf_repo, entry.hf_commit, run_folder)
    for role, name in _STAGING_RUN_FILES:
        if name in run_files:
            files[role] = SourceFile(entry.hf_repo, entry.hf_commit, run_files[name])
    return BuildSource(
        plan=TreeBuildPlan(
            kind="staging",
            country=entry.country,
            folder=run_folder,
            hf_repo=entry.hf_repo,
            hf_commit=entry.hf_commit,
            created_at=entry.created_at,
            run_id=run_id,
            release_id=None,
            label=entry.label,
            make_latest=False,
            paths={role: file.listed.path for role, file in files.items()},
        ),
        files=files,
    )


# Building --------------------------------------------------------------------------


def _build(
    source: BuildSource, reader: HuggingFaceReader, commands: TreeCommands
) -> PublishedTree:
    """Download the build's files into a new directory, then build and publish it."""

    with tempfile.TemporaryDirectory(prefix="calibration-tree-backfill-") as directory:
        build_directory = Path(directory)
        files_directory = build_directory / "files"
        files_directory.mkdir()
        local_files: dict[str, Path] = {}
        for role, source_file in source.files.items():
            downloaded = reader.download_file(
                source_file.dataset,
                source_file.listed,
                source_file.revision,
                files_directory / PurePosixPath(source_file.listed.path).name,
                max_bytes=MAX_SOURCE_FILE_BYTES,
            )
            if (
                source_file.declared_sha256 is not None
                and downloaded.sha256 != source_file.declared_sha256
            ):
                raise SourceUnbuildableError(
                    f"{source_file.listed.path} does not match the digest its run "
                    "recorded."
                )
            local_files[role] = downloaded.path
        return build_and_publish(
            source.plan, local_files, directory=build_directory, commands=commands
        )


@dataclass(frozen=True)
class _Backfill:
    stored: StoredTrees
    reader: HuggingFaceReader
    listings: _Listings
    commands: TreeCommands
    dry_run: bool
    clock: Callable[[], float]
    started: float
    time_limit_seconds: float

    def entry(self, entry: ProductionEntry) -> BackfillOutcome:
        def outcome(result: BackfillOutcomeKind, **fields: Any) -> BackfillOutcome:
            # An entry production names as the latest that ends any other way
            # than built, or already the store's latest, did not become it.
            if entry.make_latest and "made_latest" not in fields:
                fields["made_latest"] = None if result == "would_build" else False
            return BackfillOutcome(
                country=entry.country,
                outcome=result,
                kind=entry.kind,
                source_id=entry.source_id,
                hf_repo=entry.hf_repo,
                hf_commit=entry.hf_commit,
                created_at=entry.created_at,
                make_latest=entry.make_latest,
                production_build_artifact_id=entry.build_artifact_id,
                **fields,
            )

        created_at = entry.created_at
        republishing = False
        stored_entry = self.stored.entry_for(entry)
        if stored_entry is not None:
            stored_build = stored_entry.get("buildArtifactId")
            stored_fields = {
                "build_artifact_id": stored_build,
                "matches_production": stored_build == entry.build_artifact_id,
            }
            differences = []
            if stored_entry.get("hfCommitSha") != entry.hf_commit:
                differences.append(f"commit {stored_entry.get('hfCommitSha')}")
            if not _same_instant(stored_entry.get("createdAt"), entry.created_at):
                differences.append(f"publication time {stored_entry.get('createdAt')}")
            if differences:
                return outcome(
                    "mismatch",
                    reason=(
                        f"The store already holds {entry.source_id} with "
                        + " and ".join(differences)
                        + "."
                    ),
                    **stored_fields,
                )
            if not entry.make_latest:
                return outcome("already_stored", **stored_fields)
            if self.stored.latest(entry.country) == stored_build:
                return outcome("already_stored", made_latest=True, **stored_fields)
            # Production's latest, stored but not the store's latest: built and
            # published again with its stored time, so only the latest moves.
            created_at = stored_entry.get("createdAt")
            republishing = True
        if not self.dry_run and self.clock() - self.started >= self.time_limit_seconds:
            return outcome(
                "deferred",
                reason="No build starts after the execution's time limit; run again.",
                incomplete=True,
            )
        try:
            source = (
                _release_source(entry, self.listings)
                if entry.kind == "release"
                else _staging_source(entry, self.reader, self.listings)
            )
        except SourceUnbuildableError as error:
            return outcome("unbuildable", reason=_shortened(str(error)))
        except Exception as error:
            return outcome("error", reason=_described(error), incomplete=True)
        source = replace(source, plan=replace(source.plan, created_at=created_at))
        oversized = [
            source_file.listed
            for source_file in source.files.values()
            if source_file.listed.size > MAX_SOURCE_FILE_BYTES
        ]
        if oversized:
            return outcome(
                "too_large",
                reason=(
                    f"{oversized[0].path} is {oversized[0].size} bytes, over the "
                    f"{MAX_SOURCE_FILE_BYTES}-byte limit."
                ),
            )
        republished = (
            "The store holds it, but not as its latest: it is published again to "
            "become the latest."
            if republishing
            else None
        )
        if self.dry_run:
            return outcome("would_build", reason=republished)
        try:
            published = _build(source, self.reader, self.commands)
        except (SourceUnbuildableError, TreeUnbuildableError) as error:
            return outcome("unbuildable", reason=_shortened(str(error)))
        except TreeRefusedError as error:
            return outcome("refused", reason=_shortened(str(error)))
        except Exception as error:
            return outcome("error", reason=_described(error), incomplete=True)
        not_moved = entry.make_latest and not published.made_latest
        return outcome(
            "latest_not_moved" if not_moved else "built",
            reason=(
                "The publication kept the store's latest, which is at least as new."
                if not_moved
                else republished
            ),
            build_artifact_id=published.build_artifact_id,
            matches_production=published.build_artifact_id == entry.build_artifact_id,
            uploaded_parts=published.uploaded_parts,
            kept_parts=published.kept_parts,
            made_latest=published.made_latest,
        )


def backfill_calibration_trees(
    *,
    production_manifest_url: str,
    reader: HuggingFaceReader,
    production_client: httpx.Client,
    commands: TreeCommands,
    countries: Sequence[str] = tuple(RELEASE_REPOS),
    kinds: Sequence[str] = BUILD_KINDS,
    dry_run: bool = False,
    clock: Callable[[], float] = time.monotonic,
    time_limit_seconds: float = BUILD_TIME_LIMIT_SECONDS,
) -> Iterator[BackfillOutcome]:
    """Backfill each country's entries of production's manifest, yielding each outcome.

    The ``invalid`` entries and failed countries come first, then each
    country's latest, then every other entry in production's order.
    """

    started = clock()
    try:
        stored = StoredTrees(read_store_manifest(commands))
    except Exception as error:
        for country in countries:
            yield BackfillOutcome(
                country=country,
                outcome="error",
                reason=f"The store's manifest could not be read: {_described(error)}",
                incomplete=True,
            )
        return
    latest_entries: list[ProductionEntry] = []
    other_entries: list[ProductionEntry] = []
    for country in countries:
        try:
            document = read_production_manifest(
                production_client, production_manifest_url, country
            )
        except ProductionManifestError as error:
            yield BackfillOutcome(
                country=country, outcome="error", reason=str(error), incomplete=True
            )
            continue
        latest = document.get("latestReleaseBuildArtifactId")
        for raw in document["builds"]:
            raw_kind = raw.get("kind") if isinstance(raw, dict) else None
            if raw_kind in BUILD_KINDS and raw_kind not in kinds:
                continue
            try:
                entry = production_entry(raw, country, latest)
            except InvalidEntryError as error:
                yield _invalid_outcome(raw, country, latest, str(error))
                continue
            (latest_entries if entry.make_latest else other_entries).append(entry)
    backfill = _Backfill(
        stored=stored,
        reader=reader,
        listings=_Listings(reader),
        commands=commands,
        dry_run=dry_run,
        clock=clock,
        started=started,
        time_limit_seconds=time_limit_seconds,
    )
    for entry in [*latest_entries, *other_entries]:
        yield backfill.entry(entry)


def summarize(
    outcomes: Iterable[BackfillOutcome], countries: Sequence[str]
) -> dict[str, Any]:
    """The run's summary: counts, whether it is incomplete, parity and each latest.

    It is incomplete when any outcome is (an ``error`` or ``deferred``, a
    failed country among them), or when a country's latest from production
    was not made the store's latest.
    """

    listed = list(outcomes)
    latest: dict[str, dict[str, Any]] = {
        country: {"build": None, "made_latest": None} for country in countries
    }
    for outcome in listed:
        if outcome.make_latest:
            latest[outcome.country] = {
                "build": outcome.production_build_artifact_id,
                "made_latest": outcome.made_latest,
            }
    return {
        "outcomes": dict(Counter(outcome.outcome for outcome in listed)),
        "incomplete": any(outcome.incomplete for outcome in listed)
        or any(country["made_latest"] is False for country in latest.values()),
        "release_builds_differing_from_production": sum(
            1
            for outcome in listed
            if outcome.kind == "release" and outcome.matches_production is False
        ),
        "latest": latest,
    }
