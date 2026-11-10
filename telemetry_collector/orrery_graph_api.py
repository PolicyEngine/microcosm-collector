"""HTTP endpoints that serve Orrery graph files to microcosm-runs.

The endpoints are mounted only while serving is on (``OrreryServingSettings``);
while it is off, every path under ``/v1/orrery-graphs`` answers 404 with the
code ``orrery_graphs_not_enabled``. Every request must first pass the caller
check of ``telemetry_collector.orrery_access``: only microcosm-runs' own
service account may list or fetch any document, public or private, and
microcosm-runs decides what to show to whom.

- ``GET /v1/orrery-graphs?run_id=<run>&build_id=<build>``, with either or
  both, lists the recorded documents of the run, the build or both, newest
  first, with each one's current flag and effective visibility
  (``telemetry_collector.orrery_serving``).
- ``GET /v1/orrery-graphs/<submission_id>/<document_sha256>/<file_name>``
  returns ``graph.orrery.json``, or one of its ``upstream-<sha256>.json``
  files, from the private Orrery bucket, once its bytes match the SHA-256 the
  object's metadata records (and, for the document, the SHA-256 its row
  records). A file that doesn't match answers 500 and is never served.

Every answer under ``/v1/orrery-graphs``, refusals and the errors above
included, carries ``Cache-Control: private, no-store``
(``PrivateNoStoreMiddleware``). Only an unexpected exception, which
Starlette's outermost error handler answers with a plain-text 500, lacks it.
The routes are outside read API v2, whose OpenAPI document holds only
``/v2`` paths.
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
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from telemetry_collector.disclosure import OutputVisibility
from telemetry_collector.intake_api import ALL_METHODS, IntakeProblemError
from telemetry_collector.models import SAFE_IDENTIFIER
from telemetry_collector.orrery_access import orrery_reader_check
from telemetry_collector.orrery_serving import (
    DOCUMENT_FILE_NAME,
    UPSTREAM_FILE_PATTERN,
    GraphFolderObject,
    OrreryServingServices,
    OrreryStorageUnavailableError,
    RecordedGraphFile,
    current_documents,
    effective_visibility,
    newest_first,
    upstream_files,
)
from telemetry_collector.submissions import BUILD_IDENTIFIER_PATTERN, SHA256_PATTERN

ORRERY_GRAPHS_PATH = "/v1/orrery-graphs"
PRIVATE_NO_STORE = "private, no-store"
VISIBILITY_HEADER = "X-Orrery-Visibility"
#: A submission ID as ``derive_submission_id`` writes it.
SUBMISSION_ID_PATTERN = r"^sub-[0-9a-f]{32}$"
# The verified bytes are sent in parts, without a Content-Length, so that
# Cloud Run streams a file larger than its 32 MiB limit on a whole response.
RESPONSE_CHUNK_BYTES = 1024 * 1024
STORAGE_RETRY_AFTER_SECONDS = 30
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


def _from_storage[Answer](read: Callable[[str], Answer], name: str) -> Answer:
    try:
        return read(name)
    except OrreryStorageUnavailableError as error:
        raise IntakeProblemError(
            503,
            "orrery_storage_unavailable",
            "The Orrery bucket is unavailable; retry later.",
            headers={"Retry-After": str(STORAGE_RETRY_AFTER_SECONDS)},
        ) from error


def _timestamp(value: datetime) -> str:
    """ISO 8601 in UTC with an explicit ``+00:00`` offset, as intake writes it."""

    return value.astimezone(UTC).isoformat()


def _file_path(document: RecordedGraphFile, file_name: str) -> str:
    return (
        f"{ORRERY_GRAPHS_PATH}/{document.submission_id}/"
        f"{document.document_sha256}/{file_name}"
    )


def _is_served_file_name(file_name: str) -> bool:
    return file_name == DOCUMENT_FILE_NAME or bool(
        re.fullmatch(UPSTREAM_FILE_PATTERN, file_name)
    )


def _chunks(content: bytes) -> Iterator[bytes]:
    for offset in range(0, len(content), RESPONSE_CHUNK_BYTES):
        yield content[offset : offset + RESPONSE_CHUNK_BYTES]


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

    def document_upstream_files(
        document: RecordedGraphFile,
    ) -> list[GraphFolderObject]:
        folder = _from_storage(objects.list_objects, document.object_prefix)
        return upstream_files(folder, document.object_prefix)

    def visibilities(
        documents: list[tuple[RecordedGraphFile, list[GraphFolderObject]]],
    ) -> dict[tuple[str, str], OutputVisibility]:
        """Each document's effective visibility, from two queries in all."""

        upstream_sha256s = {
            document.key: [
                upstream.sha256 for upstream in upstreams if upstream.sha256 is not None
            ]
            for document, upstreams in documents
        }
        all_sha256s = {
            sha256 for sha256s in upstream_sha256s.values() for sha256 in sha256s
        }
        producing_runs = catalogue.producing_run_ids(all_sha256s)
        runs = {document.run_id for document, _ in documents}
        runs.update(run for producers in producing_runs.values() for run in producers)
        licensed_runs = catalogue.licensed_run_ids(runs)
        return {
            document.key: effective_visibility(
                document, upstream_sha256s[document.key], producing_runs, licensed_runs
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
            catalogue.graph_files(run_id=run_id, build_id=build_id),
            key=newest_first,
            reverse=True,
        )
        # A build's current document may belong to another run, so it is
        # chosen among all of the build's documents.
        current = current_documents(
            document
            for build in sorted({document.build_id for document in matching})
            for document in catalogue.graph_files(build_id=build)
        )
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
        if not (
            re.fullmatch(SUBMISSION_ID_PATTERN, submission_id)
            and re.fullmatch(SHA256_PATTERN, document_sha256)
            and _is_served_file_name(file_name)
        ):
            raise _not_found()
        document = catalogue.graph_file(submission_id, document_sha256)
        if document is None:
            raise _not_found()
        object_name = document.object_prefix + file_name
        stored = _from_storage(objects.read_object, object_name)
        if stored is None:
            raise _not_found()
        actual_sha256 = hashlib.sha256(stored.content).hexdigest()
        recorded_sha256 = (
            document.document_sha256
            if file_name == DOCUMENT_FILE_NAME
            else stored.sha256
        )
        if stored.sha256 != actual_sha256 or recorded_sha256 != actual_sha256:
            LOGGER.error(
                "Refused to serve %s: its bytes have SHA-256 %s, its metadata "
                "records %s and its record %s.",
                object_name,
                actual_sha256,
                stored.sha256,
                document.document_sha256
                if file_name == DOCUMENT_FILE_NAME
                else "no SHA-256 for this file",
            )
            raise IntakeProblemError(
                500,
                "orrery_graph_digest_mismatch",
                "The stored file does not match its recorded SHA-256, so it is "
                "not served.",
            )
        visibility = visibilities([(document, document_upstream_files(document))])
        return StreamingResponse(
            _chunks(stored.content),
            media_type="application/json",
            headers={
                "ETag": f'"{actual_sha256}"',
                VISIBILITY_HEADER: visibility[document.key],
            },
        )

    return router
