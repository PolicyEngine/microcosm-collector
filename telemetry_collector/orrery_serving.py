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
- **Effective visibility,** computed for each answer, so that a run recorded
  later still counts. A document is private when its row says ``private``
  (its submission had licensed data), or when any recorded submission of a
  run it comes from had licensed data: its own run, or a run that produced
  one of its upstream files. The producing run of an upstream file is every
  run with a recorded submission holding a file with the upstream file's
  SHA-256 in any role but ``upstream``. An upstream file Microcosm copies
  is one of the producing run's own files, byte for byte: its graph
  declaration, run manifest, compiler schema or execution evidence index
  (``checkpoint_references`` at the projection's source commit), named
  ``upstream-<sha256>.json`` by that SHA-256. Upstream references carry no
  run ID, so the file's SHA-256 is the only link to its run. A submission
  that lists the file as its own upstream file only copied it, so it is not
  a producer. An upstream file whose SHA-256 no recorded submission holds,
  or whose stored object has no SHA-256, does not change the visibility.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Iterable, Mapping
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

if TYPE_CHECKING:
    from telemetry_collector.orrery_access import IdentityTokenVerifier

#: The bucket's name; the worker job reads the same setting to publish to it.
BUCKET_SETTING = "COLLECTOR_ORRERY_BUCKET"
#: The address of the service account of microcosm-runs' deployments.
READER_SETTING = "COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT"
#: The document's name in its folder, as the worker writes it.
DOCUMENT_FILE_NAME = "graph.orrery.json"
#: An upstream file's name in the document's folder, as Microcosm writes it.
UPSTREAM_FILE_PATTERN = r"^upstream-[0-9a-f]{64}\.json$"
UPSTREAM_ROLE = "upstream"


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
    producing_runs: Mapping[str, Collection[str]],
    licensed_runs: Collection[str],
) -> OutputVisibility:
    """``private`` when the row says so, or when the document's own run or a
    run that produced one of its upstream files is licensed."""

    if document.visibility == "private":
        return "private"
    runs = {document.run_id}
    for sha256 in upstream_sha256s:
        runs.update(producing_runs.get(sha256, ()))
    return "private" if any(run in licensed_runs for run in runs) else "public"


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

    def producing_run_ids(
        self, file_sha256s: Collection[str]
    ) -> dict[str, frozenset[str]]:
        """For each SHA-256 a recorded submission holds in a role other than
        ``upstream``, the run IDs of those submissions."""

    def licensed_run_ids(self, run_ids: Collection[str]) -> frozenset[str]:
        """Those of ``run_ids`` with a recorded submission with licensed data."""


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
_PRODUCING_RUNS = sa.text(
    "SELECT DISTINCT submitted_files.file_entry ->> 'sha256' AS file_sha256, "
    "artifact_submissions.provenance ->> 'run_id' AS run_id "
    "FROM artifact_submissions, "
    "jsonb_array_elements(artifact_submissions.files) "
    "AS submitted_files(file_entry) "
    "WHERE submitted_files.file_entry ->> 'role' <> :upstream_role "
    "AND submitted_files.file_entry ->> 'sha256' IN :file_sha256s"
).bindparams(sa.bindparam("file_sha256s", expanding=True))


class PostgresOrreryGraphCatalogue:
    """``orrery_graph_files`` and ``artifact_submissions``, read-only."""

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

    def producing_run_ids(
        self, file_sha256s: Collection[str]
    ) -> dict[str, frozenset[str]]:
        if not file_sha256s:
            return {}
        producers: dict[str, set[str]] = {}
        with self._sessions() as session:
            rows = session.execute(
                _PRODUCING_RUNS,
                {
                    "upstream_role": UPSTREAM_ROLE,
                    "file_sha256s": sorted(set(file_sha256s)),
                },
            )
            for file_sha256, run_id in rows:
                producers.setdefault(file_sha256, set()).add(run_id)
        return {sha256: frozenset(runs) for sha256, runs in producers.items()}

    def licensed_run_ids(self, run_ids: Collection[str]) -> frozenset[str]:
        if not run_ids:
            return frozenset()
        run_id = ArtifactSubmission.provenance["run_id"].astext
        query = (
            select(run_id)
            .where(ArtifactSubmission.licensed_data.is_(True))
            .where(run_id.in_(sorted(set(run_ids))))
            .distinct()
        )
        with self._sessions() as session:
            return frozenset(session.scalars(query))


@dataclass(frozen=True)
class GraphFolderObject:
    """An object in a document's folder, and the SHA-256 its metadata records."""

    name: str
    sha256: str | None


@dataclass(frozen=True)
class GraphObjectContent:
    """An object's bytes, and the SHA-256 its metadata records."""

    content: bytes
    sha256: str | None


class OrreryStorageUnavailableError(RuntimeError):
    """The private Orrery bucket did not answer; the request can be repeated."""


class OrreryGraphObjectReader(Protocol):
    """The private Orrery bucket, read-only."""

    def list_objects(self, prefix: str) -> list[GraphFolderObject]:
        """Every object whose name starts with ``prefix``."""

    def read_object(self, object_name: str) -> GraphObjectContent | None:
        """One object, or ``None`` when it does not exist."""


@dataclass(frozen=True)
class OrreryServingServices:
    """What the serving endpoints need while serving is switched on."""

    catalogue: OrreryGraphCatalogue
    objects: OrreryGraphObjectReader
    verifier: IdentityTokenVerifier


def upstream_files(
    folder: Iterable[GraphFolderObject], object_prefix: str
) -> list[GraphFolderObject]:
    """The upstream files in a document's folder, by name within the folder."""

    found = []
    for stored in folder:
        if not stored.name.startswith(object_prefix):
            continue
        name = stored.name[len(object_prefix) :]
        if re.fullmatch(UPSTREAM_FILE_PATTERN, name):
            found.append(GraphFolderObject(name=name, sha256=stored.sha256))
    return sorted(found, key=lambda stored: stored.name)
