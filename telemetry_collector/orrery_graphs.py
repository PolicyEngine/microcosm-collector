"""Build, check and publish Orrery graph files from ``orrery-inputs`` submissions.

Each attempt downloads the submission's schema, execution overlay and
upstream files into a new temporary directory; evidence files are never
downloaded. It projects the two inputs with the collector's copy of
Microcosm's projection (``orrery_json_from_schema(schema, execution=overlay)``)
and then removes every link the published document could not resolve: a
``url`` stays only when it is an ``https://`` URL or names, before any ``#``,
a submitted upstream file, and a relative ``uri`` only when it names a
submitted upstream file. Evidence references so keep their label and SHA-256
but lose their link, since evidence files stay in private intake storage. The
document is serialised exactly as the projection serialises it and validated
with the pinned Orrery validator, which may resolve links only to the
upstream files the document still names.

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
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from telemetry_collector.database import OrreryGraphFile, create_session_factory
from telemetry_collector.disclosure import OutputVisibility
from telemetry_collector.google_cloud import GoogleCloudStorageOrreryGraphs
from telemetry_collector.intake import BUCKET_PATTERN
from telemetry_collector.orrery.projection import orrery_json_from_schema
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


def remove_unpublished_links(
    document: object, upstream_names: Collection[str]
) -> PublishedLinks:
    """Remove, in place, every link the published document cannot resolve.

    A ``url`` stays when it starts with ``https://`` or names a submitted
    upstream file before any ``#``; any other ``url`` is removed. A ``uri``
    stays unless it is a relative reference that names no submitted upstream
    file. The rest of each object, such as its ``label`` and ``sha256``,
    stays. Values that are not strings are not links and stay.
    """

    linked: set[str] = set()
    removed = 0
    pending: list[object] = [document]
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
                    continue
                if key == "url" and link.startswith("https://"):
                    continue
                if key == "uri" and urlsplit(link).scheme:
                    continue
                del current[key]
                removed += 1
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
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

    A file that is not UTF-8, is not JSON, repeats a key within an object or
    holds ``NaN`` or an infinity rejects the submission.
    """

    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise SubmissionRejectedError(
            UNUSABLE_INPUTS, f"The {description} is not UTF-8."
        ) from error
    try:
        return json.loads(
            text, object_pairs_hook=_unique_keys, parse_constant=_no_constant
        )
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
            for name in upstream_files:
                context.download(name, inputs / name)
            schema = read_input(schema_path, "schema")
            overlay = read_input(overlay_path, "execution overlay")
            try:
                projected = orrery_json_from_schema(schema, execution=overlay)
            except ValueError as error:
                raise SubmissionRejectedError(
                    UNUSABLE_INPUTS, _shortened(str(error))
                ) from error
            document = json.loads(projected)
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
            # The upstream files first, so every link resolves once the
            # document exists.
            for name in linked_names:
                objects.create_object(
                    prefix + name,
                    inputs / name,
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
