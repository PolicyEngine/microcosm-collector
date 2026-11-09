"""Build, check and publish Orrery graph files from ``orrery-inputs`` submissions.

Each attempt downloads the submission's schema and execution overlay into a
new temporary directory. It projects them with the collector's copy of
Microcosm's projection (``orrery_json_from_schema(schema, execution=overlay)``)
and then removes every link the published document could not resolve, which
needs only the upstream files' names, from the submission's file list:

- Evidence files stay in private intake storage, so each evidence reference
  keeps its label and SHA-256 but loses its link, whatever the link's scheme.
  The evidence references are the overlay's phase references
  (``metadata.execution.phases[].references``) and the document's
  ``artifacts``, which the projection builds only from the overlay
  (``orrery_evidence.apply_execution`` copies a phase reference's ``url``
  into its artifact's ``uri``). There every ``url`` and ``uri`` is removed
  unless it names a submitted upstream file before any ``#``.
- Everywhere else, a ``url`` or ``uri`` stays only when it starts with
  ``https://`` or names a submitted upstream file before any ``#``.

Each node or edge that lost a link gets the projection's own ``revision``
digest of the record as published. The document is serialised exactly as the
projection serialises it, so a document with nothing removed keeps the
projection's bytes, and validated with the pinned Orrery validator, which may
resolve links only to the upstream files the document still names. Only those
upstream files are then downloaded; evidence files never are.

The document and those upstream files are written to the environment's
private Orrery bucket under ``graphs/<submission_id>/<document sha256>/``.
The folder is named by the document's content, so a retry, or a reprocessing
with the same code, writes the same objects, and a reprocessing after the
projection changes writes a new folder. Writes only ever create objects; an
existing object with the same SHA-256 is kept. Each document is recorded in
``orrery_graph_files`` with its run and build, the projection's source, the
collector's and Orrery's versions, and its visibility: ``private`` for a
licensed build, served later only to signed-in organization members
(tracker 2b · 5), and ``public`` otherwise.

The worker registers this handler only where ``COLLECTOR_ORRERY_BUCKET`` is
set (``registered_handlers``); elsewhere ``orrery-inputs`` submissions stay in
intake storage and can be reprocessed once it is.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from telemetry_collector.database import OrreryGraphFile, create_session_factory
from telemetry_collector.disclosure import OutputVisibility
from telemetry_collector.google_cloud import GoogleCloudStorageOrreryGraphs
from telemetry_collector.intake import BUCKET_PATTERN
from telemetry_collector.orrery.projection import orrery_json_from_schema
from telemetry_collector.orrery.projection.orrery import (
    _MAX_INPUT_BYTES as MAX_INPUT_BYTES,
)
from telemetry_collector.orrery.projection.orrery import (
    _revision as projection_revision,
)
from telemetry_collector.orrery.projection.provenance import MICROCOSM_COMMIT
from telemetry_collector.orrery.validation import (
    OrreryDocumentRefusedError,
    OrreryValidation,
    validate_orrery_document,
)
from telemetry_collector.worker import (
    JobContext,
    JobHandler,
    SubmissionRejectedError,
)

#: The worker job's setting: the name of this environment's private Orrery bucket.
BUCKET_SETTING = "COLLECTOR_ORRERY_BUCKET"
PROCESSED_BY = "orrery-graph"
GRAPH_FILE_NAME = "graph.orrery.json"
GRAPH_CONTENT_TYPE = "application/json"
OBJECT_FOLDER = "graphs"
COLLECTOR_DISTRIBUTION = "microcosm-collector"
MAX_MESSAGE_CHARACTERS = 500
UNUSABLE_INPUTS = "orrery_inputs_unusable"
DOCUMENT_REFUSED = "orrery_document_refused"

#: The errors from the projection that reject the submission. Microcosm's
#: command (``orrery.main`` at ``MICROCOSM_COMMIT``) treats ``OSError``,
#: ``TypeError``, ``ValueError``, ``RecursionError`` and ``GraphRuntimeError``
#: as input errors; the copied projection reads and writes no file, and no
#: copied module defines or raises ``GraphRuntimeError`` (Microcosm's runtime
#: and content-store errors), so neither applies here. Any other error, such
#: as a ``KeyError``, fails the attempt, so the job runs again.
PROJECTION_INPUT_ERRORS: tuple[type[Exception], ...] = (
    TypeError,
    ValueError,
    RecursionError,
)

# The projection's own serialisation (orrery._bounded_json), so a document
# from which nothing is removed keeps the projection's exact bytes.
_ENCODER = json.JSONEncoder(
    ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
)

Validator = Callable[[Path, Collection[str]], OrreryValidation]
Clock = Callable[[], datetime]


class OrreryGraphObjects(Protocol):
    """The private Orrery bucket, written create-only."""

    def create_object(
        self, object_name: str, source: Path, *, sha256: str, content_type: str
    ) -> bool:
        """Create the object unless an equal one exists; False when one is kept.

        Raises ``StoredObjectConflictError`` when an object with another
        SHA-256 already has the name.
        """


@dataclass(frozen=True)
class OrreryGraphRecord:
    """One published graph file, as ``orrery_graph_files`` records it."""

    submission_id: str
    processing_round: int
    run_id: str
    build_id: str
    country_code: str
    object_prefix: str
    document_sha256: str
    document_bytes: int
    upstream_files: int
    orrery_version: str
    projection_source: str
    collector_version: str
    reconstructed: bool
    visibility: OutputVisibility


class OrreryGraphStore(Protocol):
    """The record of published graph files."""

    def record_graph(self, record: OrreryGraphRecord) -> bool:
        """Record a graph file; False when that submission's document is recorded."""


def _now() -> datetime:
    return datetime.now(UTC)


class PostgresOrreryGraphStore:
    """``orrery_graph_files`` in the collector's database."""

    def __init__(self, engine: sa.Engine, *, clock: Clock = _now) -> None:
        self._sessions: sessionmaker[Session] = create_session_factory(engine)
        self._clock = clock

    def record_graph(self, record: OrreryGraphRecord) -> bool:
        with self._sessions.begin() as session:
            inserted = session.execute(
                insert(OrreryGraphFile)
                .values(**asdict(record), created_at=self._clock())
                .on_conflict_do_nothing(
                    index_elements=[
                        OrreryGraphFile.submission_id,
                        OrreryGraphFile.document_sha256,
                    ]
                )
                .returning(OrreryGraphFile.submission_id)
            ).scalar_one_or_none()
        return inserted is not None


@dataclass(frozen=True)
class PublishedLinks:
    """What removing unpublished links left in a document."""

    #: Submitted upstream files the document still links to.
    upstream_names: frozenset[str]
    #: How many ``url`` and ``uri`` keys were removed.
    removed: int


def _names_upstream_file(link: str, upstream_names: Collection[str]) -> str | None:
    name = link.split("#", 1)[0]
    return name if name in upstream_names else None


def _remove_links(
    value: object,
    upstream_names: Collection[str],
    linked: set[str],
    *,
    keep_https: bool,
) -> int:
    """Remove, in place, each ``url`` and ``uri`` within ``value`` that names
    no submitted upstream file before any ``#``, except, with ``keep_https``,
    one that starts with ``https://``. Add the upstream files still named to
    ``linked``, and return how many links were removed."""

    removed = 0
    pending: list[object] = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key in ("url", "uri"):
                link = current.get(key)
                if not isinstance(link, str):
                    continue
                upstream_name = _names_upstream_file(link, upstream_names)
                if upstream_name is not None:
                    linked.add(upstream_name)
                elif not (keep_https and link.startswith("https://")):
                    del current[key]
                    removed += 1
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return removed


def _evidence_references(document: object) -> list[object]:
    """Where the projection writes the overlay's evidence references.

    ``orrery_evidence.apply_execution`` copies the overlay into
    ``metadata.execution``, whose ``phases[].references`` are the phase
    references, and builds the document's ``artifacts`` only from the
    overlay: one for each content-store object, and one for each phase
    reference, with the reference's ``url`` as its ``uri``.
    """

    if not isinstance(document, dict):
        return []
    references: list[object] = []
    artifacts = document.get("artifacts")
    if isinstance(artifacts, list):
        references.extend(artifacts)
    metadata = document.get("metadata")
    execution = metadata.get("execution") if isinstance(metadata, dict) else None
    phases = execution.get("phases") if isinstance(execution, dict) else None
    for phase in phases if isinstance(phases, list) else []:
        phase_references = phase.get("references") if isinstance(phase, dict) else None
        if isinstance(phase_references, dict):
            references.extend(phase_references.values())
    return references


def _revise(record: dict[str, Any]) -> None:
    """Give a node or edge the projection's digest of its content, as
    ``orrery_document_from_schema`` computes it: over every key but
    ``revision``."""

    record["revision"] = projection_revision(
        {key: value for key, value in record.items() if key != "revision"}
    )


def remove_unpublished_links(
    document: object, upstream_names: Collection[str]
) -> PublishedLinks:
    """Remove, in place, every link the published document cannot resolve.

    In the evidence references (``_evidence_references``), every ``url`` and
    ``uri`` that names no submitted upstream file before any ``#`` is
    removed, whatever its scheme. Everywhere else, a ``url`` or ``uri`` stays
    only when it starts with ``https://`` or names a submitted upstream file
    before any ``#``. The rest of each object, such as its ``label`` and
    ``sha256``, stays, and values that are not strings are not links. Each
    node or edge that lost a link gets the projection's ``revision`` of the
    record as published; nothing else changes, so a document with nothing
    removed is unchanged.
    """

    linked: set[str] = set()
    removed = 0
    for reference in _evidence_references(document):
        removed += _remove_links(reference, upstream_names, linked, keep_https=False)
    if isinstance(document, dict):
        for collection in ("nodes", "edges"):
            records = document.get(collection)
            for record in records if isinstance(records, list) else []:
                record_removed = _remove_links(
                    record, upstream_names, linked, keep_https=True
                )
                if record_removed and isinstance(record, dict):
                    _revise(record)
                removed += record_removed
    # The rest of the document. The evidence references, nodes and edges
    # now hold only links this keeps, so nothing more is removed from them.
    removed += _remove_links(document, upstream_names, linked, keep_https=True)
    return PublishedLinks(upstream_names=frozenset(linked), removed=removed)


def graph_json(document: object) -> str:
    """Serialise a document as the projection does, with a trailing newline."""

    return "".join(_ENCODER.iterencode(document)) + "\n"


def _shortened(text: str) -> str:
    text = text.strip() or "(no message)"
    return (
        text
        if len(text) <= MAX_MESSAGE_CHARACTERS
        else text[: MAX_MESSAGE_CHARACTERS - 1] + "…"
    )


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("an object repeats a key")
        result[key] = value
    return result


def _no_constant(constant: str) -> None:
    raise ValueError("it holds a number that is not finite")


def read_input(path: Path, description: str) -> object:
    """Parse one input as strict JSON, as Microcosm's command reads it.

    These reject the submission with ``orrery_inputs_unusable``, as Microcosm's
    reader (``orrery._read_json`` at ``MICROCOSM_COMMIT``) refuses them: a file
    over 32 MiB (``MAX_INPUT_BYTES``, the reader's limit), one that is not
    UTF-8 or not JSON, one that repeats a key within an object or holds the
    constant ``NaN``, ``Infinity`` or ``-Infinity``, and one nested too deeply
    for Python's JSON decoder, which raises ``RecursionError``. A number too
    large for a float, such as ``1e999``, parses here as an infinity, as in
    Microcosm's command; the projection then refuses it with a ``ValueError``,
    which rejects the submission too. ``path`` is the worker's own download,
    so an ``OSError`` reading it is the worker's fault, not the input's, and
    fails the attempt.
    """

    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise SubmissionRejectedError(
            UNUSABLE_INPUTS,
            f"The {description} is larger than {MAX_INPUT_BYTES // 2**20} MiB.",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SubmissionRejectedError(
            UNUSABLE_INPUTS, f"The {description} is not UTF-8."
        ) from error
    try:
        return json.loads(
            text, object_pairs_hook=_unique_keys, parse_constant=_no_constant
        )
    except RecursionError as error:
        raise SubmissionRejectedError(
            UNUSABLE_INPUTS, f"The {description} nests too deeply."
        ) from error
    except ValueError as error:
        raise SubmissionRejectedError(
            UNUSABLE_INPUTS,
            _shortened(f"The {description} is not usable JSON: {error}"),
        ) from error


def _refusal_message(refusal: OrreryDocumentRefusedError) -> str:
    message = f"The Orrery validator refused the graph file ({refusal.code}): "
    message += refusal.message.strip() or "no message"
    if refusal.issues:
        message += f" First issue at {refusal.issues[0].get('path', '(no path)')}."
    return _shortened(message)


def installed_collector_version() -> str:
    """The installed collector package's version."""

    return importlib.metadata.version(COLLECTOR_DISTRIBUTION)


def object_prefix(submission_id: str, document_sha256: str) -> str:
    """The bucket folder of one submission's document, ending in ``/``."""

    return f"{OBJECT_FOLDER}/{submission_id}/{document_sha256}/"


def orrery_graph_handler(
    *,
    objects: OrreryGraphObjects,
    graphs: OrreryGraphStore,
    validate: Validator = validate_orrery_document,
    collector_version: str | None = None,
) -> JobHandler:
    """The ``orrery-inputs`` handler, publishing to ``objects``."""

    version = collector_version or installed_collector_version()

    def publish_graph(context: JobContext) -> dict[str, Any]:
        submission = context.submission
        visibility = context.output_visibility("orrery_artifact")
        [schema_file] = context.files("schema")
        [overlay_file] = context.files("execution_overlay")
        upstream_files = {file["name"]: file for file in context.files("upstream")}
        if GRAPH_FILE_NAME in upstream_files:
            raise SubmissionRejectedError(
                UNUSABLE_INPUTS,
                f"An upstream file is named {GRAPH_FILE_NAME}, the graph file's name.",
            )
        with tempfile.TemporaryDirectory(prefix="orrery-graph-") as directory:
            attempt_directory = Path(directory)
            inputs = attempt_directory / "inputs"
            inputs.mkdir()
            schema_path = context.download(
                schema_file["name"], inputs / schema_file["name"]
            )
            overlay_path = context.download(
                overlay_file["name"], inputs / overlay_file["name"]
            )
            schema = read_input(schema_path, "schema")
            overlay = read_input(overlay_path, "execution overlay")
            try:
                projected = orrery_json_from_schema(schema, execution=overlay)
            except PROJECTION_INPUT_ERRORS as error:
                raise SubmissionRejectedError(
                    UNUSABLE_INPUTS, _shortened(str(error))
                ) from error
            document = json.loads(projected)
            # Needs only the upstream files' names, from the file list; the
            # projection never reads the upstream files themselves.
            links = remove_unpublished_links(document, upstream_files)
            graph_bytes = graph_json(document).encode("utf-8")
            graph_path = attempt_directory / GRAPH_FILE_NAME
            graph_path.write_bytes(graph_bytes)
            linked_names = sorted(links.upstream_names)
            try:
                validation = validate(graph_path, linked_names)
            except OrreryDocumentRefusedError as refusal:
                raise SubmissionRejectedError(
                    DOCUMENT_REFUSED, _refusal_message(refusal)
                ) from refusal
            document_sha256 = hashlib.sha256(graph_bytes).hexdigest()
            prefix = object_prefix(submission.submission_id, document_sha256)
            # Only the upstream files the document links to are downloaded,
            # and they are written first, so every link resolves once the
            # document exists.
            for name in linked_names:
                objects.create_object(
                    prefix + name,
                    context.download(name, inputs / name),
                    sha256=upstream_files[name]["sha256"],
                    content_type=GRAPH_CONTENT_TYPE,
                )
            objects.create_object(
                prefix + GRAPH_FILE_NAME,
                graph_path,
                sha256=document_sha256,
                content_type=GRAPH_CONTENT_TYPE,
            )
        provenance = submission.provenance
        graphs.record_graph(
            OrreryGraphRecord(
                submission_id=submission.submission_id,
                processing_round=submission.processing_round,
                run_id=provenance["run_id"],
                build_id=provenance["build_id"],
                country_code=provenance["country_code"],
                object_prefix=prefix,
                document_sha256=document_sha256,
                document_bytes=len(graph_bytes),
                upstream_files=len(linked_names),
                orrery_version=validation.orrery_version,
                projection_source=MICROCOSM_COMMIT,
                collector_version=version,
                reconstructed=False,
                visibility=visibility,
            )
        )
        return {
            "processed_by": PROCESSED_BY,
            "object_prefix": prefix,
            "document_sha256": document_sha256,
            "document_bytes": len(graph_bytes),
            "upstream_files": len(linked_names),
            "orrery_version": validation.orrery_version,
            "visibility": visibility,
            "removed_links": links.removed,
        }

    return publish_graph


def orrery_bucket(environ: Mapping[str, str] = os.environ) -> str | None:
    """The private Orrery bucket's name, or ``None`` where it is not set."""

    bucket = environ.get(BUCKET_SETTING, "").strip()
    if not bucket:
        return None
    if not re.fullmatch(BUCKET_PATTERN, bucket):
        raise RuntimeError(f"{BUCKET_SETTING} is not a valid resource name.")
    return bucket


def registered_handlers(
    *, engine: sa.Engine | None, environ: Mapping[str, str] = os.environ
) -> dict[str, JobHandler]:
    """The Orrery graph handler, only where the worker job has its bucket."""

    bucket = orrery_bucket(environ)
    if bucket is None:
        return {}
    if engine is None:
        raise RuntimeError("The Orrery graph handler needs the collector's database.")
    return {
        "orrery-inputs": orrery_graph_handler(
            objects=GoogleCloudStorageOrreryGraphs(bucket_name=bucket),
            graphs=PostgresOrreryGraphStore(engine),
        )
    }
