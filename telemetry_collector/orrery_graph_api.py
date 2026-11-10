"""HTTP endpoints that serve Orrery graph files to microcosm-runs.

The endpoints are mounted only while serving is on (``OrreryServingSettings``);
while it is off, every path under ``/v1/orrery-graphs`` answers 404 with the
code ``orrery_graphs_not_enabled``. Every request must first pass the caller
check of ``telemetry_collector.orrery_access``: only microcosm-runs' own
service account may list or fetch any document, public or private, and
microcosm-runs decides what to show to whom.

- ``GET /v1/orrery-graphs?run_id=<run>&build_id=<build>``, with either or
  both, lists the recorded documents of the run, the build ID or both,
  newest first, with each one's current flag, set per country code and
  build ID, and its effective visibility
  (``telemetry_collector.orrery_serving``).
- ``GET /v1/orrery-graphs/<submission_id>/<document_sha256>/<file_name>``
  returns ``graph.orrery.json``, or one of its ``upstream-<sha256>.json``
  files, from the private Orrery bucket. The object is read twice at the
  generation it was found at, one part at a time: first to compare its
  SHA-256 with the document's row or the upstream file's name, and only on
  a match to send it. A file that doesn't match, that the storage client
  finds corrupt, or whose generation is no longer stored answers 500, is
  logged, and is never served. Should the second read fail, the status 200
  and the headers have been sent, so it is logged and the body stops
  early; microcosm-runs must compare the body's SHA-256 with the ``ETag``
  before it caches or shows the file.

Every answer under ``/v1/orrery-graphs``, refusals and the errors above
included, carries ``Cache-Control: private, no-store``
(``PrivateNoStoreMiddleware``). The collector's own errors have the
service's ``{"detail", "code"}`` shape: a bucket that doesn't answer gives
503 ``orrery_storage_unavailable`` and a database that doesn't answer 503
``orrery_catalogue_unavailable``. Three answers have only ``{"detail"}``,
without a ``code``: maintenance mode's 503, a 405 for a method a route
doesn't serve, and Starlette's 404 for a path that matches no route. Only
an unexpected exception, which Starlette's outermost error handler answers
with a plain-text 500, lacks both the header and a JSON body. The routes
are outside read API v2, whose OpenAPI document holds only ``/v2`` paths.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from telemetry_collector.disclosure import OutputVisibility
from telemetry_collector.intake_api import ALL_METHODS, IntakeProblemError
from telemetry_collector.models import SAFE_IDENTIFIER
from telemetry_collector.orrery_access import orrery_reader_check
from telemetry_collector.orrery_serving import (
    DOCUMENT_FILE_NAME,
    OrreryGraphObjectReader,
    OrreryObjectChangedError,
    OrreryObjectCorruptError,
    OrreryServingServices,
    OrreryStorageUnavailableError,
    RecordedGraphFile,
    StoredGraphObject,
    UpstreamFile,
    current_documents,
    effective_visibility,
    newest_first,
    upstream_files,
)
from telemetry_collector.submissions import (
    BUILD_IDENTIFIER_PATTERN,
    SHA256_PATTERN,
    upstream_file_sha256,
)

ORRERY_GRAPHS_PATH = "/v1/orrery-graphs"
PRIVATE_NO_STORE = "private, no-store"
VISIBILITY_HEADER = "X-Orrery-Visibility"
#: A submission ID as ``derive_submission_id`` writes it.
SUBMISSION_ID_PATTERN = r"^sub-[0-9a-f]{32}$"
STORAGE_RETRY_AFTER_SECONDS = 30
CATALOGUE_RETRY_AFTER_SECONDS = 30
NOT_ENABLED_DETAIL = "Serving Orrery graph files is not enabled on this collector."

LOGGER = logging.getLogger(__name__)


def is_orrery_graph_path(path: str) -> bool:
    return path == ORRERY_GRAPHS_PATH or path.startswith(ORRERY_GRAPHS_PATH + "/")


class PrivateNoStoreMiddleware:
    """Mark every answer under ``ORRERY_GRAPHS_PATH`` private and not to be
    stored, whichever route, refusal or handled error produced it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_orrery_graph_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        async def send_private(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message)["Cache-Control"] = PRIVATE_NO_STORE
            await send(message)

        await self.app(scope, receive, send_private)


def orrery_graphs_not_enabled_router() -> APIRouter:
    """Answer every Orrery graph path while serving is off, outside the schema."""

    router = APIRouter()

    def refuse(graph_path: str = "") -> None:
        raise IntakeProblemError(404, "orrery_graphs_not_enabled", NOT_ENABLED_DETAIL)

    for path in (ORRERY_GRAPHS_PATH, ORRERY_GRAPHS_PATH + "/{graph_path:path}"):
        router.add_api_route(path, refuse, methods=ALL_METHODS, include_in_schema=False)
    return router


def _query_invalid(detail: str) -> IntakeProblemError:
    return IntakeProblemError(422, "orrery_graph_query_invalid", detail)


def _not_found() -> IntakeProblemError:
    return IntakeProblemError(
        404, "orrery_graph_not_found", "Orrery graph file was not found."
    )


def _storage_unavailable() -> IntakeProblemError:
    return IntakeProblemError(
        503,
        "orrery_storage_unavailable",
        "The Orrery bucket is unavailable; retry later.",
        headers={"Retry-After": str(STORAGE_RETRY_AFTER_SECONDS)},
    )


def _digest_mismatch() -> IntakeProblemError:
    return IntakeProblemError(
        500,
        "orrery_graph_digest_mismatch",
        "The stored file does not match its recorded SHA-256, so it is not served.",
    )


def _from_storage[**Arguments, Answer](
    read: Callable[Arguments, Answer],
    *arguments: Arguments.args,
    **keyword_arguments: Arguments.kwargs,
) -> Answer:
    try:
        return read(*arguments, **keyword_arguments)
    except OrreryStorageUnavailableError as error:
        raise _storage_unavailable() from error


def _from_catalogue[**Arguments, Answer](
    read: Callable[Arguments, Answer],
    *arguments: Arguments.args,
    **keyword_arguments: Arguments.kwargs,
) -> Answer:
    """Read the catalogue, answering 503 when the database doesn't answer."""

    try:
        return read(*arguments, **keyword_arguments)
    except SQLAlchemyError as error:
        # The class name only: SQLAlchemy's messages hold the SQL statement.
        LOGGER.warning(
            "The collector's database did not answer a read for Orrery graph "
            "files (%s).",
            type(error).__name__,
        )
        raise IntakeProblemError(
            503,
            "orrery_catalogue_unavailable",
            "The collector's database is unavailable; retry later.",
            headers={"Retry-After": str(CATALOGUE_RETRY_AFTER_SECONDS)},
        ) from error


def _timestamp(value: datetime) -> str:
    """ISO 8601 in UTC with an explicit ``+00:00`` offset, as intake writes it."""

    return value.astimezone(UTC).isoformat()


def _file_path(document: RecordedGraphFile, file_name: str) -> str:
    return (
        f"{ORRERY_GRAPHS_PATH}/{document.submission_id}/"
        f"{document.document_sha256}/{file_name}"
    )


def _expected_sha256(file_name: str, document_sha256: str) -> str | None:
    """The SHA-256 a served file must have: the document's, which its row
    records, or the one an upstream file's name gives, which the worker
    checked is its own. ``None`` for any other file name."""

    if file_name == DOCUMENT_FILE_NAME:
        return document_sha256
    return upstream_file_sha256(file_name)


def check_stored_bytes(
    objects: OrreryGraphObjectReader,
    stored: StoredGraphObject,
    expected_sha256: str,
) -> None:
    """Read the object once, at its generation, hashing each part and keeping
    none, and refuse it unless its SHA-256 is ``expected_sha256``."""

    digest = hashlib.sha256()
    try:
        for part in objects.read_parts(stored):
            digest.update(part)
    except OrreryStorageUnavailableError as error:
        raise _storage_unavailable() from error
    except OrreryObjectChangedError:
        LOGGER.error(
            "Refused to serve %s: generation %d is no longer stored, so the "
            "object changed after it was found.",
            stored.name,
            stored.generation,
        )
        raise IntakeProblemError(
            500,
            "orrery_graph_changed",
            "The stored file changed while it was being checked, so it is not served.",
        ) from None
    except OrreryObjectCorruptError:
        LOGGER.error(
            "Refused to serve %s generation %d: the storage client found its "
            "bytes don't match their checksum.",
            stored.name,
            stored.generation,
        )
        raise _digest_mismatch() from None
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        LOGGER.error(
            "Refused to serve %s generation %d: its bytes have SHA-256 %s, "
            "where its record or name gives %s.",
            stored.name,
            stored.generation,
            actual_sha256,
            expected_sha256,
        )
        raise _digest_mismatch()


def sent_parts(
    objects: OrreryGraphObjectReader, stored: StoredGraphObject
) -> Iterator[bytes]:
    """The checked object's bytes, read again at the same generation.

    The answer has started by the time these are read, so a failure can only
    stop it: it is logged and raised, and the server ends the answer
    unfinished.
    """

    try:
        yield from objects.read_parts(stored)
    except (
        OrreryStorageUnavailableError,
        OrreryObjectChangedError,
        OrreryObjectCorruptError,
    ) as error:
        LOGGER.error(
            "Stopped sending %s generation %d before its end: %s",
            stored.name,
            stored.generation,
            error,
        )
        raise


def orrery_graph_router(
    *, services: OrreryServingServices, reader_service_account: str
) -> APIRouter:
    """Create the endpoints over explicit services, for one reader account."""

    require_orrery_reader = orrery_reader_check(
        services.verifier, reader_service_account
    )
    router = APIRouter(
        prefix=ORRERY_GRAPHS_PATH, dependencies=[Depends(require_orrery_reader)]
    )
    catalogue = services.catalogue
    objects = services.objects

    def document_upstream_files(document: RecordedGraphFile) -> list[UpstreamFile]:
        names = _from_storage(objects.list_object_names, document.object_prefix)
        return upstream_files(names, document.object_prefix)

    def visibilities(
        documents: list[tuple[RecordedGraphFile, list[UpstreamFile]]],
    ) -> dict[tuple[str, str], OutputVisibility]:
        """Each document's effective visibility, from two queries in all."""

        upstream_sha256s = {
            document.key: [upstream.sha256 for upstream in upstreams]
            for document, upstreams in documents
        }
        all_sha256s = {
            sha256 for sha256s in upstream_sha256s.values() for sha256 in sha256s
        }
        producers = _from_catalogue(catalogue.producing_submissions, all_sha256s)
        origins = {document.origin for document, _ in documents}
        origins.update(origin for found in producers.values() for origin in found)
        licensed = _from_catalogue(
            catalogue.licensed_runs_and_builds,
            run_ids={origin.run_id for origin in origins},
            build_ids={origin.build_id for origin in origins},
        )
        return {
            document.key: effective_visibility(
                document, upstream_sha256s[document.key], producers, licensed
            )
            for document, _ in documents
        }

    @router.get("")
    def list_graph_files(
        run_id: str | None = None, build_id: str | None = None
    ) -> dict[str, Any]:
        if run_id is None and build_id is None:
            raise _query_invalid("Give run_id, build_id or both.")
        if run_id is not None and not re.fullmatch(SAFE_IDENTIFIER, run_id):
            raise _query_invalid("run_id is not a run ID.")
        if build_id is not None and not re.fullmatch(
            BUILD_IDENTIFIER_PATTERN, build_id
        ):
            raise _query_invalid("build_id is not a build ID.")
        matching = sorted(
            _from_catalogue(catalogue.graph_files, run_id=run_id, build_id=build_id),
            key=newest_first,
            reverse=True,
        )
        # A build's current document may belong to another run, so it is
        # chosen among all of the build's documents: those with its country
        # code and build ID.
        builds_documents: list[RecordedGraphFile] = []
        for country_code, build_id in sorted({document.build for document in matching}):
            builds_documents.extend(
                _from_catalogue(
                    catalogue.graph_files,
                    build_id=build_id,
                    country_code=country_code,
                )
            )
        current = current_documents(builds_documents)
        with_upstreams = [
            (document, document_upstream_files(document)) for document in matching
        ]
        visibility = visibilities(with_upstreams)
        return {
            "documents": [
                {
                    "submission_id": document.submission_id,
                    "document_sha256": document.document_sha256,
                    "document_bytes": document.document_bytes,
                    "run_id": document.run_id,
                    "build_id": document.build_id,
                    "country_code": document.country_code,
                    "orrery_version": document.orrery_version,
                    "projection_source": document.projection_source,
                    "collector_version": document.collector_version,
                    "reconstructed": document.reconstructed,
                    "created_at": _timestamp(document.created_at),
                    "visibility": visibility[document.key],
                    "current": document.key in current,
                    "document_path": _file_path(document, DOCUMENT_FILE_NAME),
                    "upstream_paths": [
                        _file_path(document, upstream.name) for upstream in upstreams
                    ],
                }
                for document, upstreams in with_upstreams
            ]
        }

    @router.get("/{submission_id}/{document_sha256}/{file_name}")
    def read_graph_file(
        submission_id: str, document_sha256: str, file_name: str
    ) -> StreamingResponse:
        # Every part is checked before it names an object, so no path can
        # reach outside the document's folder.
        expected_sha256 = _expected_sha256(file_name, document_sha256)
        if not (
            re.fullmatch(SUBMISSION_ID_PATTERN, submission_id)
            and re.fullmatch(SHA256_PATTERN, document_sha256)
            and expected_sha256 is not None
        ):
            raise _not_found()
        document = _from_catalogue(catalogue.graph_file, submission_id, document_sha256)
        if document is None:
            raise _not_found()
        stored = _from_storage(objects.find_object, document.object_prefix + file_name)
        if stored is None:
            raise _not_found()
        # Nothing is sent until the whole object has been read and matched.
        check_stored_bytes(objects, stored, expected_sha256)
        visibility = visibilities([(document, document_upstream_files(document))])
        # Sent in parts without a Content-Length, so that Cloud Run streams a
        # file larger than its 32 MiB limit on a whole response.
        return StreamingResponse(
            sent_parts(objects, stored),
            media_type="application/json",
            headers={
                "ETag": f'"{expected_sha256}"',
                VISIBILITY_HEADER: visibility[document.key],
            },
        )

    return router
