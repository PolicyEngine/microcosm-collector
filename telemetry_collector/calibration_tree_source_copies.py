"""Store the original files missing for builds the calibration tree store holds.

Since the collector stores a gzip copy of each build's original files when it
builds a tree (``calibration-tree-sources/<country>/<build ID>/<name>.gz``),
builds stored before that have none, and a build first built from a
submission has no copy of a file the submission did not carry, such as
``parent_build_manifest.json`` or ``reform_validation.json``. This step of
``backfill-calibration-trees`` adds them.

**Which builds.** Every build in the store's own manifest, read once with the
tree package's read-only manifest read, for the selected countries and kinds.
Production's public manifest is not read here: since the dashboard reads the
collector's production store, it lists that store itself.

**Which files.** Each build's files are found at its stored commit, from its
stored repo, with the backfill's own rules (``release_source`` and
``staging_source`` on the stored entry, which must first pass the checks
production's entries get): a release build's ``releases/<folder>/``, a staging
run's ``runs/<id>/`` or the staged bundle its receipt names, and a staged
bundle's ``staged/<id>/``. The copies the store holds are listed once per
country (``calibration-tree-source-copies.ts list``), and only files without a
copy are read further.

**Which copies are stored.** A copy of one of the four files a tree is built
from (the diagnostics, ``build_manifest.json``, ``release_manifest.json`` and
``demographics.json``) is stored only when the stored tree's index records that
file with the same SHA-256, so a copy is always a file the tree was built from;
a file the index does not record is ``not_in_tree``, and one with another
SHA-256 ``differs``. ``parent_build_manifest.json`` and
``reform_validation.json`` have no record, so they are stored as the build's
folder holds them at its commit (over 64 MiB, the backfill leaves them out),
unless a structured run declares one with another SHA-256, which is
``differs``. A
file over 256 MiB is ``too_large`` and never downloaded. Each file is streamed
to disk and checked against its listing, then the package's command stores
the copies the store still lacks, and checks the index's record again. The
index records a tree file's SHA-256 over its decoded UTF-8 text, so a file
matches when its bytes or that text have the recorded digest; the copy is of
the bytes. A copy already stored stays as it is, whatever it holds: ``kept``
when it holds the same bytes, ``conflict`` when it holds others. Nothing else
is written: no tree part, index or manifest.

**Outcomes.** Each build's ``files`` say what became of each file's copy
(``stored`` when the store already listed it). A build is ``complete`` when
every file it was built from or copies has a copy (stored before, or by
another writer meanwhile), ``copied`` when copies were stored for it,
``would_copy`` in a dry run, and ``not_copied`` when its missing copies can't be
stored (they differ or are too large, or the store holds no tree for the
build). A stored entry that fails the checks is ``invalid``; files that are not
at its commit, or a commit Hugging Face no longer holds, ``not_found``. Those
are final. An ``error`` (a failed read, download or command) and a
``deferred`` build, whose files are not looked for after the time limit, leave
the run incomplete, so it runs again.

A dry run reads the store's manifest, its copy listing, the Hugging Face
listings, the small documents a staging run's files are found by and the
index of each tree missing a copy, and reads a tree file only when Hugging Face gives no SHA-256
for it (one stored without LFS); it stores nothing, takes no lock and has no
time limit. A file it does not read is compared by the SHA-256 Hugging Face
gives for its bytes, so one whose recorded digest is of other text, such as a
file starting with a byte-order mark, shows as ``differs`` where a real run
would copy it. Builds that step 3 of a real run would replace are counted as
they are stored now.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal

from telemetry_collector import calibration_tree_backfill as backfill
from telemetry_collector.calibration_tree_backfill import (
    BUILD_KINDS,
    BUILD_TIME_LIMIT_SECONDS,
    BuildKind,
    InvalidEntryError,
    Listings,
    SourceFile,
    SourceUnbuildableError,
    production_entry,
    release_source,
    staging_source,
)
from telemetry_collector.calibration_trees import (
    MANIFEST_READ_TIMEOUT_SECONDS,
    PACKAGE_DIRECTORY,
    PUBLICATION_TIMEOUT_SECONDS,
    CalibrationTreeCommandError,
    TreeCommands,
    read_store_manifest,
    shortened_message,
)
from telemetry_collector.huggingface_read import (
    HuggingFaceReader,
    HuggingFaceReadError,
)

COMMAND_SCRIPT: Final = PACKAGE_DIRECTORY / "src" / "calibration-tree-source-copies.ts"
#: The command's exit when the store holds no tree for the build.
EXIT_REFUSED: Final = 2

SourceFilesOutcomeKind = Literal[
    "complete",
    "would_copy",
    "copied",
    "not_copied",
    "invalid",
    "not_found",
    "error",
    "deferred",
]
#: What became of one file's copy.
SourceFileStatus = Literal[
    "stored",
    "would_copy",
    "copied",
    "kept",
    "conflict",
    "differs",
    "not_in_tree",
    "too_large",
]

#: The name each original file's copy is stored under, by the worker's role
#: name: the package's ``CALIBRATION_TREE_SOURCE_FILE_NAMES``.
STORED_NAMES: Final = {
    "calibration_diagnostics": "calibration_diagnostics.json",
    "build_manifest": "build_manifest.json",
    "release_manifest": "release_manifest.json",
    "demographics": "demographics.json",
    "parent_build_manifest": "parent_build_manifest.json",
    "reform_validation": "reform_validation.json",
}
#: The package's name for each file, as its command and a tree's index name it.
PACKAGE_NAMES: Final = {
    "calibration_diagnostics": "calibrationDiagnostics",
    "build_manifest": "buildManifest",
    "release_manifest": "releaseManifest",
    "demographics": "demographics",
    "parent_build_manifest": "parentBuildManifest",
    "reform_validation": "reformValidation",
}
#: The files a tree is built from, which its index records with their SHA-256.
TREE_ROLES: Final = frozenset(
    {"calibration_diagnostics", "build_manifest", "release_manifest", "demographics"}
)
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
#: Hugging Face's error code for a commit the repo does not hold.
_REVISION_NOT_FOUND: Final = "RevisionNotFound"
#: A file whose copy this step can't store.
_LEFT_OUT: Final = frozenset({"differs", "too_large"})


class SourceCopiesRefusedError(Exception):
    """The copy command refused the build: the store holds no tree for it."""


@dataclass(frozen=True)
class SourceFilesOutcome:
    """What the step did with one stored build's copies, or with a country.

    An outcome for a whole country has no kind and no source.
    """

    country: str
    outcome: SourceFilesOutcomeKind
    kind: BuildKind | None = None
    #: The release ID or staging run the store records the build under.
    source_id: str | None = None
    build_artifact_id: str | None = None
    hf_repo: str | None = None
    hf_commit: str | None = None
    #: Each original file the build's folder holds, by the name its copy is
    #: stored under, and what became of that copy.
    files: dict[str, SourceFileStatus] | None = None
    reason: str | None = None
    #: A later run should try it again: an ``error`` or ``deferred``.
    incomplete: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _described(error: BaseException) -> str:
    return shortened_message(f"{type(error).__name__}: {error}")


def _answer(
    commands: TreeCommands, arguments: list[str], timeout: float
) -> dict[str, Any]:
    completed = commands.run_command(
        [commands.bun, "run", str(COMMAND_SCRIPT), *arguments],
        commands.publication_environment,
        timeout,
    )
    if completed.returncode == EXIT_REFUSED:
        raise SourceCopiesRefusedError(shortened_message(completed.stderr))
    if completed.returncode != 0:
        raise CalibrationTreeCommandError(
            f"The source copies {arguments[0]} exited with {completed.returncode}: "
            f"{shortened_message(completed.stderr)}"
        )
    lines = completed.stdout.strip().splitlines()
    try:
        answer = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as error:
        raise CalibrationTreeCommandError(
            f"The source copies {arguments[0]} printed no JSON answer."
        ) from error
    if not isinstance(answer, dict):
        raise CalibrationTreeCommandError(
            f"The source copies {arguments[0]} printed no JSON answer."
        )
    return answer


def list_source_copies(
    commands: TreeCommands, country: str
) -> dict[str, frozenset[str]]:
    """The names of the copies stored for each of ``country``'s builds."""

    builds = _answer(commands, ["list", country], MANIFEST_READ_TIMEOUT_SECONDS).get(
        "builds"
    )
    if not isinstance(builds, dict) or not all(
        isinstance(names, list) and all(isinstance(name, str) for name in names)
        for names in builds.values()
    ):
        raise CalibrationTreeCommandError(
            "The source copies list printed no copies by build."
        )
    return {build: frozenset(names) for build, names in builds.items()}


def store_source_copies(commands: TreeCommands, request_path: Path) -> dict[str, Any]:
    """What the copy command did with each file the request names.

    Raises ``SourceCopiesRefusedError`` when the store holds no tree for the
    build, and ``CalibrationTreeCommandError`` for any other failure.
    """

    answer = _answer(
        commands, ["store", str(request_path)], PUBLICATION_TIMEOUT_SECONDS
    )
    for field in ("uploaded", "kept", "conflicts", "refused"):
        if not isinstance(answer.get(field), list):
            raise CalibrationTreeCommandError(
                "The source copies store printed no account of its files."
            )
    return answer


def read_recorded_sources(
    commands: TreeCommands, country: str, build_artifact_id: str
) -> dict[str, Any]:
    """The files a stored tree's index records it was built from, by the
    package's names.

    Raises ``SourceCopiesRefusedError`` when the store holds no tree for the
    build, and ``CalibrationTreeCommandError`` for any other failure.
    """

    sources = _answer(
        commands,
        ["sources", country, build_artifact_id],
        MANIFEST_READ_TIMEOUT_SECONDS,
    ).get("sources")
    if not isinstance(sources, dict):
        raise CalibrationTreeCommandError(
            "The source copies sources printed no record of the tree's files."
        )
    return sources


def _matches_record(content: bytes, recorded_sha256: str) -> bool:
    """Whether ``content`` is the file a tree's index records with
    ``recorded_sha256``. The worker hashes a tree file's UTF-8 text as
    ``TextDecoder`` decodes it, with a leading byte-order mark dropped and
    invalid bytes replaced; for any other file that text is its bytes."""

    if hashlib.sha256(content).hexdigest() == recorded_sha256:
        return True
    text = content.decode("utf-8", errors="replace").removeprefix("\ufeff")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() == recorded_sha256


def _known_sha256(source_file: SourceFile) -> str | None:
    """The SHA-256 the run or Hugging Face's listing gives for a file, if any."""

    return source_file.declared_sha256 or source_file.listed.lfs_sha256


@dataclass
class _SourceCopies:
    reader: HuggingFaceReader
    commands: TreeCommands
    listings: Listings
    dry_run: bool
    clock: Callable[[], float]
    started: float
    time_limit_seconds: float

    def build(
        self, country: str, stored: Mapping[str, Any], copies: frozenset[str]
    ) -> SourceFilesOutcome:
        kind = stored.get("kind")
        alias = (
            stored.get("releaseId") if kind == "release" else stored.get("stagingRunId")
        )
        build_artifact_id = stored.get("buildArtifactId")
        reported: dict[str, Any] = {
            "country": country,
            "kind": kind if kind in BUILD_KINDS else None,
            "source_id": shortened_message(alias) if isinstance(alias, str) else None,
            "build_artifact_id": (
                build_artifact_id if isinstance(build_artifact_id, str) else None
            ),
            "hf_repo": stored.get("hfRepo")
            if isinstance(stored.get("hfRepo"), str)
            else None,
            "hf_commit": (
                stored.get("hfCommitSha")
                if isinstance(stored.get("hfCommitSha"), str)
                else None
            ),
        }
        try:
            # The checks production's entries get, before any of its values
            # is used in a Hugging Face path; the build ID's among them.
            entry = production_entry(stored, country, None)
        except InvalidEntryError as error:
            return SourceFilesOutcome(
                outcome="invalid",
                reason=f"The store's entry can't be read from Hugging Face: {error}",
                **reported,
            )
        if not self.dry_run and self.clock() - self.started >= self.time_limit_seconds:
            return SourceFilesOutcome(
                outcome="deferred",
                reason=(
                    "No build's files are looked for after the execution's time "
                    "limit; run again."
                ),
                incomplete=True,
                **reported,
            )
        try:
            source = (
                release_source(entry, self.listings)
                if entry.kind == "release"
                else staging_source(entry, self.reader, self.listings)
            )
        except SourceUnbuildableError as error:
            return SourceFilesOutcome(
                outcome="not_found", reason=shortened_message(str(error)), **reported
            )
        except HuggingFaceReadError as error:
            if error.error_code == _REVISION_NOT_FOUND:
                return SourceFilesOutcome(
                    outcome="not_found",
                    reason=(
                        "Hugging Face no longer holds a commit this build's files "
                        f"are read at: {_described(error)}"
                    ),
                    **reported,
                )
            return SourceFilesOutcome(
                outcome="error", reason=_described(error), incomplete=True, **reported
            )
        except Exception as error:
            return SourceFilesOutcome(
                outcome="error", reason=_described(error), incomplete=True, **reported
            )
        files: dict[str, SourceFileStatus] = {}
        missing: dict[str, SourceFile] = {}
        for role, source_file in source.files.items():
            if STORED_NAMES[role] in copies:
                files[STORED_NAMES[role]] = "stored"
            else:
                missing[role] = source_file
        if not missing:
            return SourceFilesOutcome(outcome="complete", files=files, **reported)
        try:
            expected = self.recorded_sha256(
                entry.country, entry.build_artifact_id, missing
            )
        except SourceCopiesRefusedError as error:
            return SourceFilesOutcome(
                outcome="not_copied", files=files, reason=str(error), **reported
            )
        except Exception as error:
            return SourceFilesOutcome(
                outcome="error",
                files=files,
                reason=f"The tree's record of its files could not be read: {_described(error)}",
                incomplete=True,
                **reported,
            )
        storable: dict[str, SourceFile] = {}
        for role, source_file in missing.items():
            name = STORED_NAMES[role]
            known = _known_sha256(source_file)
            if role in TREE_ROLES and role not in expected:
                files[name] = "not_in_tree"
            elif source_file.listed.size > backfill.MAX_SOURCE_FILE_BYTES:
                files[name] = "too_large"
            elif (
                self.dry_run
                and role in expected
                and known is not None
                and known != expected[role]
            ):
                # A dry run downloads no file Hugging Face gives a SHA-256 for,
                # so it can compare only that digest of its bytes.
                files[name] = "differs"
            else:
                storable[role] = source_file
        try:
            if self.dry_run:
                self.check_without_downloading(storable, expected, files)
            elif storable:
                self.store(
                    entry.country, entry.build_artifact_id, storable, expected, files
                )
        except SourceCopiesRefusedError as error:
            return SourceFilesOutcome(
                outcome="not_copied", files=files, reason=str(error), **reported
            )
        except Exception as error:
            return SourceFilesOutcome(
                outcome="error",
                files=files,
                reason=_described(error),
                incomplete=True,
                **reported,
            )
        return SourceFilesOutcome(
            outcome=self.outcome_of(files), files=files, **reported
        )

    def outcome_of(
        self, files: Mapping[str, SourceFileStatus]
    ) -> SourceFilesOutcomeKind:
        statuses = set(files.values())
        if "would_copy" in statuses:
            return "would_copy"
        if "copied" in statuses:
            return "copied"
        if statuses & _LEFT_OUT:
            return "not_copied"
        # Every file has a copy: stored before, or by another writer meanwhile.
        return "complete"

    def recorded_sha256(
        self, country: str, build_artifact_id: str, missing: Mapping[str, SourceFile]
    ) -> dict[str, str]:
        """The SHA-256 the stored tree's index records for each missing tree file.

        The index is read whenever a copy is missing, so that a build whose
        tree is not stored is found (``SourceCopiesRefusedError``) before
        anything is downloaded, in a dry run as in a real one.
        """

        recorded = read_recorded_sources(self.commands, country, build_artifact_id)
        expected: dict[str, str] = {}
        for role in TREE_ROLES & set(missing):
            record = recorded.get(PACKAGE_NAMES[role])
            digest = record.get("sha256") if isinstance(record, Mapping) else None
            if isinstance(digest, str) and _SHA256.fullmatch(digest):
                expected[role] = digest
        return expected

    def check_without_downloading(
        self,
        storable: Mapping[str, SourceFile],
        expected: Mapping[str, str],
        files: dict[str, SourceFileStatus],
    ) -> None:
        """What a real run would store, as far as it can be told without
        downloading a file Hugging Face gives a SHA-256 for."""

        for role, source_file in storable.items():
            name = STORED_NAMES[role]
            if role in expected and _known_sha256(source_file) is None:
                content = self.reader.read_file(
                    source_file.dataset, source_file.listed.path, source_file.revision
                )
                if not _matches_record(content, expected[role]):
                    files[name] = "differs"
                    continue
            files[name] = "would_copy"

    def store(
        self,
        country: str,
        build_artifact_id: str,
        storable: Mapping[str, SourceFile],
        expected: Mapping[str, str],
        files: dict[str, SourceFileStatus],
    ) -> None:
        """Download the files, check them, and store the copies the store lacks."""

        with tempfile.TemporaryDirectory(
            prefix="calibration-tree-source-copies-"
        ) as directory:
            root = Path(directory)
            requested: list[dict[str, str]] = []
            by_package_name: dict[str, str] = {}
            for position, (role, source_file) in enumerate(storable.items()):
                name = STORED_NAMES[role]
                downloaded = self.reader.download_file(
                    source_file.dataset,
                    source_file.listed,
                    source_file.revision,
                    root / f"{position}-{name}",
                    max_bytes=backfill.MAX_SOURCE_FILE_BYTES,
                )
                declared = source_file.declared_sha256
                if (declared is not None and downloaded.sha256 != declared) or (
                    role in expected
                    and downloaded.sha256 != expected[role]
                    and not _matches_record(
                        downloaded.path.read_bytes(), expected[role]
                    )
                ):
                    files[name] = "differs"
                    continue
                requested.append(
                    {
                        "file": PACKAGE_NAMES[role],
                        "path": str(downloaded.path),
                        "sha256": downloaded.sha256,
                    }
                )
                by_package_name[PACKAGE_NAMES[role]] = name
            if not requested:
                return
            request_path = root / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "country": country,
                        "buildArtifactId": build_artifact_id,
                        "files": requested,
                    }
                ),
                encoding="utf-8",
            )
            answer = store_source_copies(self.commands, request_path)
        for field, status in (
            ("uploaded", "copied"),
            ("kept", "kept"),
            ("conflicts", "conflict"),
        ):
            for package_name in answer[field]:
                if package_name in by_package_name:
                    files[by_package_name[package_name]] = status
        for refusal in answer["refused"]:
            package_name = refusal.get("file") if isinstance(refusal, dict) else None
            if package_name in by_package_name:
                files[by_package_name[package_name]] = "differs"


def store_missing_source_files(
    *,
    reader: HuggingFaceReader,
    commands: TreeCommands,
    countries: Sequence[str],
    kinds: Sequence[str] = BUILD_KINDS,
    dry_run: bool = False,
    clock: Callable[[], float] = time.monotonic,
    time_limit_seconds: float = BUILD_TIME_LIMIT_SECONDS,
) -> Iterator[SourceFilesOutcome]:
    """Store the copies each build of the store's manifest lacks, yielding each
    build's outcome, country by country in the manifest's order."""

    started = clock()
    try:
        manifest = read_store_manifest(commands)
    except Exception as error:
        for country in countries:
            yield SourceFilesOutcome(
                country=country,
                outcome="error",
                reason=f"The store's manifest could not be read: {_described(error)}",
                incomplete=True,
            )
        return
    step = _SourceCopies(
        reader=reader,
        commands=commands,
        listings=Listings(reader),
        dry_run=dry_run,
        clock=clock,
        started=started,
        time_limit_seconds=time_limit_seconds,
    )
    stored_countries = manifest.get("countries")
    for country in countries:
        stored = (
            stored_countries.get(country)
            if isinstance(stored_countries, dict)
            else None
        )
        builds = stored.get("builds") if isinstance(stored, dict) else None
        selected = [
            build
            for build in builds or []
            if isinstance(build, dict) and build.get("kind") in kinds
        ]
        if not selected:
            continue
        try:
            copies = list_source_copies(commands, country)
        except Exception as error:
            yield SourceFilesOutcome(
                country=country,
                outcome="error",
                reason=f"The stored copies could not be listed: {_described(error)}",
                incomplete=True,
            )
            continue
        for build in selected:
            build_id = build.get("buildArtifactId")
            yield step.build(
                country,
                build,
                copies.get(build_id, frozenset())
                if isinstance(build_id, str)
                else frozenset(),
            )


def summarize(outcomes: Iterable[SourceFilesOutcome]) -> dict[str, Any]:
    """The step's summary: its counts by build and by file, and what is
    incomplete."""

    listed = list(outcomes)
    return {
        "outcomes": dict(Counter(outcome.outcome for outcome in listed)),
        "files": dict(
            Counter(
                status
                for outcome in listed
                for status in (outcome.files or {}).values()
            )
        ),
        "incomplete": any(outcome.incomplete for outcome in listed),
        "incomplete_builds": [
            {
                "country": outcome.country,
                "kind": outcome.kind,
                "source_id": outcome.source_id,
                "build_artifact_id": outcome.build_artifact_id,
            }
            for outcome in listed
            if outcome.incomplete
        ],
    }
