"""Settings, records and storage the collector serves Orrery graph files from.

The worker publishes each Orrery graph file to the environment's private
Orrery bucket and records it in ``orrery_graph_files``
(``telemetry_collector.orrery_graphs``). The service reads both to serve the
files to microcosm-runs (``telemetry_collector.orrery_graph_api``):

- **Settings.** Serving is on when the service has both
  ``COLLECTOR_ORRERY_BUCKET``, the bucket's name, checked as the worker
  checks it, and ``COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT``, the address of
  the Google service account microcosm-runs acts as. With neither it is off,
  and with only one the service refuses to start.
- **Current documents.** Among a build's documents, the newest ``created_at``
  is its current one; documents recorded at the same moment are ordered by
  ``document_sha256``, then by ``submission_id``, the larger first.
- **Effective visibility,** computed for each answer, so that a submission
  recorded later still counts. A document is private when its row says
  ``private`` (its submission had licensed data), or when any recorded
  submission with licensed data shares the run ID or the build ID of the
  document or of a producing submission of one of its upstream files. The
  contract defines ``licensed_data`` per build, and a build can be
  submitted from more than one run, so both IDs count; that can only make a
  document more private. The producing submissions of an upstream file are
  the recorded submissions holding a file with the upstream file's SHA-256
  in any role but ``upstream``. An upstream file Microcosm copies is one of
  the producing run's own files, byte for byte: its graph declaration, run
  manifest, compiler schema or execution evidence index
  (``checkpoint_references`` at the projection's source commit). Upstream
  references carry no run ID, so the file's SHA-256 is the only link to its
  run. The SHA-256 comes from the file's name, ``upstream-<sha256>.json``,
  which the worker requires to be the file's own (``orrery_graphs``). A
  submission that lists the file as its own upstream file only copied it, so
  it is not a producer. An upstream file whose SHA-256 no recorded
  submission holds in another role does not change the visibility.
- **Reading objects.** Objects are never replaced, so a file is read at the
  generation it was found at, twice, holding at most ``SERVED_PART_BYTES``
  of it at once: first to hash it and compare the SHA-256 with its row's
  (the document) or its name's (an upstream file), and only then to send
  it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from telemetry_collector.database import (
    ArtifactSubmission,
    OrreryGraphFile,
    create_session_factory,
)
from telemetry_collector.disclosure import OutputVisibility
from telemetry_collector.intake import BUCKET_PATTERN, SERVICE_ACCOUNT_PATTERN
from telemetry_collector.submissions import upstream_file_sha256

if TYPE_CHECKING:
    from telemetry_collector.orrery_access import IdentityTokenVerifier

#: The bucket's name; the worker job reads the same setting to publish to it.
BUCKET_SETTING = "COLLECTOR_ORRERY_BUCKET"
#: The address of the service account of microcosm-runs' deployments.
READER_SETTING = "COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT"
#: The document's name in its folder, as the worker writes it.
DOCUMENT_FILE_NAME = "graph.orrery.json"
UPSTREAM_ROLE = "upstream"
#: The most bytes of an object the service holds at once: it reads, hashes
#: and sends each file in parts of at most this size.
SERVED_PART_BYTES = 1024 * 1024


@dataclass(frozen=True)
class OrreryServingSettings:
    """The private Orrery bucket and the one account its files are served to."""

    bucket: str
    reader_service_account: str

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] = os.environ
    ) -> OrreryServingSettings | None:
        """Both settings, or ``None`` with neither; refuses only one of them."""

        bucket = environ.get(BUCKET_SETTING, "").strip()
        reader_service_account = environ.get(READER_SETTING, "").strip()
        if not bucket and not reader_service_account:
            return None
        if not bucket or not reader_service_account:
            missing, present = (
                (BUCKET_SETTING, READER_SETTING)
                if not bucket
                else (READER_SETTING, BUCKET_SETTING)
            )
            raise RuntimeError(
                f"{missing} is required when {present} is set: the collector "
                "serves Orrery graph files only with both."
            )
        if not re.fullmatch(BUCKET_PATTERN, bucket):
            raise RuntimeError(f"{BUCKET_SETTING} is not a valid resource name.")
        if not re.fullmatch(SERVICE_ACCOUNT_PATTERN, reader_service_account):
            raise RuntimeError(
                f"{READER_SETTING} is not a user-managed service account address."
            )
        return cls(bucket=bucket, reader_service_account=reader_service_account)


@dataclass(frozen=True)
class RecordedGraphFile:
    """One row of ``orrery_graph_files``, as the serving endpoints read it."""

    submission_id: str
    document_sha256: str
    document_bytes: int
    run_id: str
    build_id: str
    country_code: str
    object_prefix: str
    orrery_version: str
    projection_source: str
    collector_version: str
    reconstructed: bool
    #: As recorded: ``private`` for a submission with licensed data.
    visibility: OutputVisibility
    created_at: datetime

    @property
    def key(self) -> tuple[str, str]:
        return (self.submission_id, self.document_sha256)

    @property
    def origin(self) -> SubmissionOrigin:
        return SubmissionOrigin(run_id=self.run_id, build_id=self.build_id)


@dataclass(frozen=True)
class SubmissionOrigin:
    """The run ID and build ID a recorded submission gives in its provenance."""

    run_id: str
    build_id: str


@dataclass(frozen=True)
class LicensedRunsAndBuilds:
    """Of some run IDs and build IDs, those that a recorded submission with
    licensed data gives."""

    run_ids: frozenset[str]
    build_ids: frozenset[str]

    def include(self, origin: SubmissionOrigin) -> bool:
        return origin.run_id in self.run_ids or origin.build_id in self.build_ids


def newest_first(document: RecordedGraphFile) -> tuple[datetime, str, str]:
    """The sort key that, in reverse, lists a build's current document first."""

    return (document.created_at, document.document_sha256, document.submission_id)


def current_documents(documents: Iterable[RecordedGraphFile]) -> set[tuple[str, str]]:
    """The key of each build's current document among ``documents``."""

    current: dict[str, RecordedGraphFile] = {}
    for document in documents:
        known = current.get(document.build_id)
        if known is None or newest_first(document) > newest_first(known):
            current[document.build_id] = document
    return {document.key for document in current.values()}


def effective_visibility(
    document: RecordedGraphFile,
    upstream_sha256s: Iterable[str],
    producers: Mapping[str, Collection[SubmissionOrigin]],
    licensed: LicensedRunsAndBuilds,
) -> OutputVisibility:
    """``private`` when the row says so, or when a licensed submission shares
    the run ID or build ID of the document or of a producing submission of
    one of its upstream files."""

    if document.visibility == "private":
        return "private"
    origins = {document.origin}
    for sha256 in upstream_sha256s:
        origins.update(producers.get(sha256, ()))
    return (
        "private" if any(licensed.include(origin) for origin in origins) else "public"
    )


class OrreryGraphCatalogue(Protocol):
    """What the serving endpoints read from the collector's database."""

    def graph_files(
        self, *, run_id: str | None = None, build_id: str | None = None
    ) -> list[RecordedGraphFile]:
        """Every recorded document of the run and the build given, in no order."""

    def graph_file(
        self, submission_id: str, document_sha256: str
    ) -> RecordedGraphFile | None:
        """One recorded document, or ``None``."""

    def producing_submissions(
        self, file_sha256s: Collection[str]
    ) -> dict[str, frozenset[SubmissionOrigin]]:
        """For each SHA-256 a recorded submission holds in a role other than
        ``upstream``, the run ID and build ID of each such submission."""

    def licensed_runs_and_builds(
        self, *, run_ids: Collection[str], build_ids: Collection[str]
    ) -> LicensedRunsAndBuilds:
        """Those of ``run_ids`` and ``build_ids`` that a recorded submission
        with licensed data gives."""


def _recorded(row: OrreryGraphFile) -> RecordedGraphFile:
    return RecordedGraphFile(
        submission_id=row.submission_id,
        document_sha256=row.document_sha256,
        document_bytes=row.document_bytes,
        run_id=row.run_id,
        build_id=row.build_id,
        country_code=row.country_code,
        object_prefix=row.object_prefix,
        orrery_version=row.orrery_version,
        projection_source=row.projection_source,
        collector_version=row.collector_version,
        reconstructed=row.reconstructed,
        visibility=cast(OutputVisibility, row.visibility),
        created_at=row.created_at,
    )


# Every submission of every status counts: a recorded run can only make
# documents private, never public.
_PRODUCING_SUBMISSIONS = sa.text(
    "SELECT DISTINCT submitted_files.file_entry ->> 'sha256' AS file_sha256, "
    "artifact_submissions.provenance ->> 'run_id' AS run_id, "
    "artifact_submissions.provenance ->> 'build_id' AS build_id "
    "FROM artifact_submissions, "
    "jsonb_array_elements(artifact_submissions.files) "
    "AS submitted_files(file_entry) "
    "WHERE submitted_files.file_entry ->> 'role' <> :upstream_role "
    "AND submitted_files.file_entry ->> 'sha256' IN :file_sha256s"
).bindparams(sa.bindparam("file_sha256s", expanding=True))


class PostgresOrreryGraphCatalogue:
    """``orrery_graph_files`` and ``artifact_submissions``, read-only.

    A database that doesn't answer raises SQLAlchemy's errors, which the
    endpoints answer with 503 (``orrery_catalogue_unavailable``).
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._sessions: sessionmaker[Session] = create_session_factory(engine)

    def graph_files(
        self, *, run_id: str | None = None, build_id: str | None = None
    ) -> list[RecordedGraphFile]:
        query = select(OrreryGraphFile)
        if run_id is not None:
            query = query.where(OrreryGraphFile.run_id == run_id)
        if build_id is not None:
            query = query.where(OrreryGraphFile.build_id == build_id)
        with self._sessions() as session:
            return [_recorded(row) for row in session.scalars(query)]

    def graph_file(
        self, submission_id: str, document_sha256: str
    ) -> RecordedGraphFile | None:
        with self._sessions() as session:
            row = session.get(
                OrreryGraphFile,
                {"submission_id": submission_id, "document_sha256": document_sha256},
            )
            return None if row is None else _recorded(row)

    def producing_submissions(
        self, file_sha256s: Collection[str]
    ) -> dict[str, frozenset[SubmissionOrigin]]:
        if not file_sha256s:
            return {}
        producers: dict[str, set[SubmissionOrigin]] = {}
        with self._sessions() as session:
            rows = session.execute(
                _PRODUCING_SUBMISSIONS,
                {
                    "upstream_role": UPSTREAM_ROLE,
                    "file_sha256s": sorted(set(file_sha256s)),
                },
            )
            for file_sha256, run_id, build_id in rows:
                producers.setdefault(file_sha256, set()).add(
                    SubmissionOrigin(run_id=run_id, build_id=build_id)
                )
        return {sha256: frozenset(origins) for sha256, origins in producers.items()}

    def licensed_runs_and_builds(
        self, *, run_ids: Collection[str], build_ids: Collection[str]
    ) -> LicensedRunsAndBuilds:
        wanted_runs = set(run_ids)
        wanted_builds = set(build_ids)
        if not wanted_runs and not wanted_builds:
            return LicensedRunsAndBuilds(run_ids=frozenset(), build_ids=frozenset())
        run_id = ArtifactSubmission.provenance["run_id"].astext
        build_id = ArtifactSubmission.provenance["build_id"].astext
        query = (
            select(run_id, build_id)
            .where(ArtifactSubmission.licensed_data.is_(True))
            .where(
                sa.or_(
                    run_id.in_(sorted(wanted_runs)),
                    build_id.in_(sorted(wanted_builds)),
                )
            )
            .distinct()
        )
        with self._sessions() as session:
            rows = session.execute(query).all()
        return LicensedRunsAndBuilds(
            run_ids=frozenset(
                licensed_run for licensed_run, _ in rows if licensed_run in wanted_runs
            ),
            build_ids=frozenset(
                licensed_build
                for _, licensed_build in rows
                if licensed_build in wanted_builds
            ),
        )


@dataclass(frozen=True)
class StoredGraphObject:
    """An object in the private Orrery bucket, at the generation it was found
    at."""

    name: str
    generation: int


@dataclass(frozen=True)
class UpstreamFile:
    """An upstream file in a document's folder, by its name in the folder,
    and the SHA-256 that name gives."""

    name: str
    sha256: str


class OrreryStorageUnavailableError(RuntimeError):
    """The private Orrery bucket did not answer; the request can be repeated."""


class OrreryObjectChangedError(RuntimeError):
    """The generation an object was found at is no longer stored: the object
    was replaced or deleted since."""


class OrreryObjectCorruptError(RuntimeError):
    """The storage client found that bytes it read don't match the object's
    checksum."""


class OrreryGraphObjectReader(Protocol):
    """The private Orrery bucket, read-only."""

    def list_object_names(self, prefix: str) -> list[str]:
        """The name of every object whose name starts with ``prefix``."""

    def find_object(self, object_name: str) -> StoredGraphObject | None:
        """One object's current generation, or ``None`` when it does not
        exist."""

    def read_parts(self, stored: StoredGraphObject) -> Iterator[bytes]:
        """The object's bytes at ``stored.generation``, in parts of at most
        ``SERVED_PART_BYTES``, holding no more than one part at once.

        Raises, while iterating, ``OrreryObjectChangedError``,
        ``OrreryObjectCorruptError`` or ``OrreryStorageUnavailableError``.
        """


@dataclass(frozen=True)
class OrreryServingServices:
    """What the serving endpoints need while serving is switched on."""

    catalogue: OrreryGraphCatalogue
    objects: OrreryGraphObjectReader
    verifier: IdentityTokenVerifier


def upstream_files(
    object_names: Iterable[str], object_prefix: str
) -> list[UpstreamFile]:
    """The upstream files among the objects in a document's folder."""

    found = []
    for object_name in object_names:
        if not object_name.startswith(object_prefix):
            continue
        name = object_name[len(object_prefix) :]
        sha256 = upstream_file_sha256(name)
        if sha256 is not None:
            found.append(UpstreamFile(name=name, sha256=sha256))
    return sorted(found, key=lambda upstream: upstream.name)
