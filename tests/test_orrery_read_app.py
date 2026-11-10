"""The Orrery read service serves Orrery graph files, and the collector doesn't.

The Orrery read service (``telemetry_collector.orrery_read_app``) is a
separate Cloud Run service, run from the collector's image, that lists and
serves the Orrery graph files the worker published
(``telemetry_collector.orrery_graph_api``). Cloud Run checks each caller's
Google identity token before a request reaches it, so it verifies no token
itself, and these tests send none. The collector's main application mounts
none of its routes. These tests read the bucket through the collector's
Google Cloud Storage adapter over a stand-in client, and the records through
in-memory stores, or through PostgreSQL as well where ``TEST_DATABASE_URL``
is set.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, ServiceUnavailable
from google.cloud.storage.exceptions import DataCorruption
from sqlalchemy.exc import OperationalError

from telemetry_collector import google_cloud
from telemetry_collector.app import (
    CollectorSettings,
    create_app,
    create_app_from_environment,
)
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import create_database_engine
from telemetry_collector.intake_api import SUBMISSION_PATHS
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.orrery.projection.provenance import MICROCOSM_COMMIT
from telemetry_collector.orrery_graphs import (
    GRAPH_FILE_NAME,
    OrreryGraphRecord,
    PostgresOrreryGraphStore,
    orrery_bucket,
)
from telemetry_collector.submission_store import PostgresSubmissionStore
from telemetry_collector.submissions import SubmissionRequest
from tests.fakes import FakeOrreryGraphStore, FakeSubmissionStore, FakeTelemetryStore
from tests.test_submission_store import SteppingClock

SERVING_MODULE = "telemetry_collector.orrery_serving"
READ_APP_MODULE = "telemetry_collector.orrery_read_app"
ROUTES_MODULE = "telemetry_collector.orrery_graph_api"
READER = "microcosm-runs-reader@example-project.iam.gserviceaccount.com"
BUCKET = "collector-orrery-test"
JWT_SECRET = "test-secret-that-is-long-enough-32"
PRINCIPAL = HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",))
LISTING = "/v1/orrery-graphs"
FILE_ROUTE = "/v1/orrery-graphs/{submission_id}/{document_sha256}/{file_name}"
CONSUMING_RUN = "uk-run-consumer"
PRODUCING_RUN = "uk-run-producer"
BUILD = "uk-frs-calibration-attempt-1"
# The producing run's build, another than the consuming document's.
PRODUCING_BUILD = "uk-spine-attempt-1"
ORRERY_VERSION = "0.6.2"
COLLECTOR_VERSION = "9.8.7"
DOCUMENT_BYTES = 191_154
PRIVATE_NO_STORE = "private, no-store"
# The most bytes of a file the service may hold at once, both while it hashes
# the file and while it sends it.
SERVED_PART_BYTES = 1024 * 1024
# What google-cloud-storage's BlobReader fetches at once when it is opened
# without a chunk size (fileio.DEFAULT_CHUNK_SIZE).
BLOB_READER_DEFAULT_CHUNK_BYTES = 40 * 1024 * 1024
# Nothing listens on port 1, so connecting to this database is refused at once.
UNREACHABLE_DATABASE_URL = "postgresql://user:password@127.0.0.1:1/none"
# The read service's two settings.
READ_SERVICE_ENVIRONMENT = {
    "COLLECTOR_ORRERY_BUCKET": BUCKET,
    "DATABASE_URL": UNREACHABLE_DATABASE_URL,
}
# The setting the in-code token check read, which nothing reads now.
RETIRED_READER_SETTING = "COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT"
# Text a storage error's message could hold, such as a request's access
# token; the collector's log must never hold it.
CREDENTIAL_IN_STORAGE_ERROR = "ya29.stand-in-access-token"


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# A producing run's graph declaration, which a consuming build copies beside
# its schema as `upstream-<sha256>.json` (Microcosm's save_graph_schema).
UPSTREAM_CONTENT = b'{"producing":"graph declaration"}'
UPSTREAM_SHA256 = sha256_hex(UPSTREAM_CONTENT)
UPSTREAM_NAME = f"upstream-{UPSTREAM_SHA256}.json"


def serving() -> ModuleType:
    assert importlib.util.find_spec(SERVING_MODULE) is not None
    return importlib.import_module(SERVING_MODULE)


def read_app() -> ModuleType:
    assert importlib.util.find_spec(READ_APP_MODULE) is not None
    return importlib.import_module(READ_APP_MODULE)


# The private Orrery bucket, through a stand-in for the Google Cloud client.


@dataclass
class BucketObject:
    content: bytes
    metadata: dict[str, str] | None
    generation: int = 1
    # Every read raises DataCorruption, as the client does when the bytes it
    # downloaded don't match the object's checksum.
    corrupt: bool = False


@dataclass
class ObjectRead:
    """One read of an object's bytes, as the stand-in client served it."""

    name: str
    #: The generation the read was pinned to, or None for the newest one.
    generation: int | None
    #: The most bytes the read held at once: like the library's BlobReader,
    #: each part fetches at least the reader's chunk size.
    largest_held_bytes: int = 0
    #: Whether the read reached the end of the object.
    finished: bool = False


#: The stand-in client's three kinds of call: listing a folder, looking an
#: object up, and reading an object's bytes.
STORAGE_OPERATIONS = ("list", "lookup", "read")


@dataclass
class StorageCall:
    """One call the collector made to the stand-in client, with the options
    it passed, such as ``timeout`` and ``retry``."""

    operation: str
    #: The folder listed, or the object looked up or read.
    name: str
    options: dict[str, Any]


class StandInBlobReader:
    """What the stand-in's ``blob.open("rb", chunk_size=...)`` returns."""

    def __init__(
        self,
        client: StandInStorageClient,
        name: str,
        generation: int | None,
        chunk_size: int | None,
    ) -> None:
        self._client = client
        self._fetch_bytes = chunk_size or BLOB_READER_DEFAULT_CHUNK_BYTES
        self._position = 0
        self._read = ObjectRead(name, generation)
        client.reads.append(self._read)

    def __enter__(self) -> StandInBlobReader:
        return self

    def __exit__(self, *exception: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        stored = self._client.stored_for_read(self._read.name, self._read.generation)
        remaining = len(stored.content) - self._position
        if size < 0:
            held = remaining
        else:
            held = min(remaining, max(size, self._fetch_bytes))
        part = stored.content[
            self._position : self._position + (remaining if size < 0 else size)
        ]
        self._position += len(part)
        self._read.largest_held_bytes = max(self._read.largest_held_bytes, held)
        if not part and not self._read.finished:
            self._read.finished = True
            self._client.read_finished(self._read)
        return part


class StandInBlob:
    """What the stand-in client hands out: a listed or looked-up object, or
    one named for a read pinned to ``generation``."""

    def __init__(
        self,
        client: StandInStorageClient,
        name: str,
        *,
        stored: Any | None = None,
        generation: int | None = None,
    ) -> None:
        self._client = client
        self.name = name
        self.generation = generation if stored is None else stored.generation
        self.size = None if stored is None else len(stored.content)
        self.metadata = (
            None if stored is None or stored.metadata is None else dict(stored.metadata)
        )

    def open(
        self, mode: str = "r", chunk_size: int | None = None, **options: Any
    ) -> StandInBlobReader:
        assert mode == "rb"
        # The library's BlobReader passes these options to each ranged read.
        self._client.calls.append(StorageCall("read", self.name, options))
        return StandInBlobReader(self._client, self.name, self.generation, chunk_size)

    def download_as_bytes(self, **options: Any) -> bytes:
        stored = self._client.stored_for_read(self.name, self.generation)
        read = ObjectRead(
            self.name,
            self.generation,
            largest_held_bytes=len(stored.content),
            finished=True,
        )
        self._client.reads.append(read)
        self._client.read_finished(read)
        return stored.content


class StandInBucket:
    def __init__(self, client: StandInStorageClient) -> None:
        self._client = client

    def blob(self, name: str, generation: int | None = None) -> StandInBlob:
        return StandInBlob(self._client, name, generation=generation)

    def get_blob(self, name: str, **options: Any) -> StandInBlob | None:
        self._client.calls.append(StorageCall("lookup", name, options))
        self._client.check_available("lookup")
        stored = self._client.objects.get(name)
        if stored is None:
            return None
        found = StandInBlob(self._client, name, stored=stored)
        if self._client.after_lookup is not None:
            self._client.after_lookup(name)
        return found


class StandInStorageClient:
    """A bucket in memory. ``objects`` may be the worker's stand-in bucket's
    objects, which have the same ``content``, ``metadata`` and
    ``generation``.

    ``reads`` records each read of an object's bytes, and ``calls`` each
    call with its options. Each operation in ``unavailable`` raises what the
    client raises when Cloud Storage doesn't answer, with
    ``CREDENTIAL_IN_STORAGE_ERROR`` in its message. ``after_lookup`` and
    ``after_read`` let a test change the bucket after an object is looked up
    or after a read reaches its end.
    """

    def __init__(self, objects: dict[str, Any] | None = None) -> None:
        self.objects: dict[str, Any] = {} if objects is None else objects
        self.bucket_names: list[str] = []
        self.reads: list[ObjectRead] = []
        self.calls: list[StorageCall] = []
        self.unavailable: set[str] = set()
        self.after_lookup: Callable[[str], None] | None = None
        self.after_read: Callable[[ObjectRead], None] | None = None
        self._generation = max(
            (stored.generation for stored in self.objects.values()), default=0
        )

    def bucket(self, name: str) -> StandInBucket:
        self.bucket_names.append(name)
        return StandInBucket(self)

    def list_blobs(
        self, bucket_name: str, prefix: str = "", **options: Any
    ) -> list[StandInBlob]:
        self.calls.append(StorageCall("list", prefix, options))
        self.check_available("list")
        return [
            StandInBlob(self, name, stored=stored)
            for name, stored in sorted(self.objects.items())
            if name.startswith(prefix)
        ]

    def check_available(self, operation: str) -> None:
        if operation in self.unavailable:
            raise ServiceUnavailable(
                f"Storage is unavailable to {CREDENTIAL_IN_STORAGE_ERROR}."
            )

    def stored_for_read(self, name: str, generation: int | None) -> Any:
        """The object a read of ``generation`` gets, refusing as Cloud
        Storage does when that generation is no longer stored."""

        self.check_available("read")
        stored = self.objects.get(name)
        if stored is None or (
            generation is not None and stored.generation != generation
        ):
            raise NotFound(f"{name} generation {generation} is not stored.")
        if getattr(stored, "corrupt", False):
            raise DataCorruption(None, f"Checksum mismatch while reading {name}.")
        return stored

    def read_finished(self, read: ObjectRead) -> None:
        if self.after_read is not None:
            self.after_read(read)

    def store(
        self, name: str, content: bytes, *, metadata: dict[str, str] | None = None
    ) -> None:
        """Store an object, as a new generation of any one under ``name``."""

        self._generation += 1
        self.objects[name] = BucketObject(
            content,
            {"sha256": sha256_hex(content)} if metadata is None else metadata,
            generation=self._generation,
        )

    def generation(self, name: str) -> int:
        return self.objects[name].generation


def graph_reader(storage: StandInStorageClient) -> Any:
    return google_cloud.GoogleCloudStorageOrreryGraphReader(
        bucket_name=BUCKET, client=storage
    )


# The records, in memory.


class InMemoryGraphCatalogue:
    """The serving endpoints' reads of ``orrery_graph_files`` and
    ``artifact_submissions``, over the in-memory stores; the catalogue tests
    below hold it to what ``PostgresOrreryGraphCatalogue`` answers."""

    def __init__(
        self, graphs: FakeOrreryGraphStore, submissions: FakeSubmissionStore
    ) -> None:
        self.graphs = graphs
        self.submissions = submissions

    def _recorded(self, key: tuple[str, str], record: Any) -> Any:
        return serving().RecordedGraphFile(
            submission_id=record.submission_id,
            document_sha256=record.document_sha256,
            document_bytes=record.document_bytes,
            run_id=record.run_id,
            build_id=record.build_id,
            country_code=record.country_code,
            object_prefix=record.object_prefix,
            orrery_version=record.orrery_version,
            projection_source=record.projection_source,
            collector_version=record.collector_version,
            reconstructed=record.reconstructed,
            visibility=record.visibility,
            created_at=self.graphs.created_at[key],
        )

    def graph_files(
        self,
        *,
        run_id: str | None = None,
        build_id: str | None = None,
        country_code: str | None = None,
    ) -> list[Any]:
        return [
            self._recorded(key, record)
            for key, record in self.graphs.records.items()
            if (run_id is None or record.run_id == run_id)
            and (build_id is None or record.build_id == build_id)
            and (country_code is None or record.country_code == country_code)
        ]

    def graph_file(self, submission_id: str, document_sha256: str) -> Any:
        key = (submission_id, document_sha256)
        record = self.graphs.records.get(key)
        return None if record is None else self._recorded(key, record)

    def producing_submissions(
        self, file_sha256s: Collection[str]
    ) -> dict[str, frozenset[Any]]:
        wanted = set(file_sha256s)
        producers: dict[str, set[Any]] = {}
        for submission in self.submissions.records():
            for submitted_file in submission.files:
                if (
                    submitted_file["role"] != "upstream"
                    and submitted_file["sha256"] in wanted
                ):
                    producers.setdefault(submitted_file["sha256"], set()).add(
                        serving().SubmissionOrigin(
                            run_id=submission.provenance["run_id"],
                            build_id=submission.provenance["build_id"],
                        )
                    )
        return {sha256: frozenset(origins) for sha256, origins in producers.items()}

    def licensed_runs_and_builds(
        self, *, run_ids: Collection[str], build_ids: Collection[str]
    ) -> Any:
        licensed = [
            submission.provenance
            for submission in self.submissions.records()
            if submission.licensed_data
        ]
        return serving().LicensedRunsAndBuilds(
            run_ids=frozenset(
                provenance["run_id"]
                for provenance in licensed
                if provenance["run_id"] in set(run_ids)
            ),
            build_ids=frozenset(
                provenance["build_id"]
                for provenance in licensed
                if provenance["build_id"] in set(build_ids)
            ),
        )


class UnavailableDatabaseCatalogue:
    """A catalogue whose ``failing_method`` raises what SQLAlchemy raises when
    the database refuses the connection; its other methods answer."""

    def __init__(self, catalogue: Any, failing_method: str) -> None:
        self._catalogue = catalogue
        self._failing_method = failing_method

    def __getattr__(self, name: str) -> Any:
        if name != self._failing_method:
            return getattr(self._catalogue, name)

        def refuse(*arguments: Any, **keyword_arguments: Any) -> Any:
            raise OperationalError(
                "SELECT 1", {}, ConnectionRefusedError("The database refused.")
            )

        return refuse


def submitted_file(name: str, role: str, content: bytes) -> dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "size": len(content),
        "sha256": sha256_hex(content),
    }


def orrery_files(variant: object) -> list[dict[str, Any]]:
    """A schema and an execution overlay, different for each ``variant``."""

    return [
        submitted_file(
            "graph.schema.json", "schema", json.dumps({"schema": variant}).encode()
        ),
        submitted_file(
            "graph.execution.json",
            "execution_overlay",
            json.dumps({"overlay": variant}).encode(),
        ),
    ]


def submission_request(
    *,
    run_id: str,
    build_id: str,
    licensed_data: bool,
    files: list[dict[str, Any]],
    country_code: str = "UK",
) -> SubmissionRequest:
    return SubmissionRequest.model_validate(
        {
            "contract_version": 1,
            "kind": "orrery-inputs",
            "licensed_data": licensed_data,
            "provenance": {
                "run_id": run_id,
                "build_id": build_id,
                "country_code": country_code,
                "release": None,
                "huggingface": None,
            },
            "files": files,
        }
    )


def graph_record(
    submission_id: str,
    document_sha256: str,
    *,
    run_id: str,
    build_id: str,
    visibility: str,
    document_bytes: int = DOCUMENT_BYTES,
    upstream_files: int = 0,
    country_code: str = "UK",
) -> OrreryGraphRecord:
    return OrreryGraphRecord(
        submission_id=submission_id,
        processing_round=1,
        run_id=run_id,
        build_id=build_id,
        country_code=country_code,
        object_prefix=f"graphs/{submission_id}/{document_sha256}/",
        document_sha256=document_sha256,
        document_bytes=document_bytes,
        upstream_files=upstream_files,
        orrery_version=ORRERY_VERSION,
        projection_source=MICROCOSM_COMMIT,
        collector_version=COLLECTOR_VERSION,
        reconstructed=False,
        visibility=visibility,  # type: ignore[arg-type]
    )


# The read service, with the stand-ins above.


@dataclass(frozen=True)
class PublishedDocument:
    submission_id: str
    document_sha256: str
    content: bytes

    @property
    def object_prefix(self) -> str:
        return f"graphs/{self.submission_id}/{self.document_sha256}/"

    def path(self, file_name: str = GRAPH_FILE_NAME) -> str:
        return f"{LISTING}/{self.submission_id}/{self.document_sha256}/{file_name}"


@dataclass
class Serving:
    client: TestClient
    submissions: FakeSubmissionStore
    graphs: FakeOrreryGraphStore
    storage: StandInStorageClient
    clock: SteppingClock
    published: list[PublishedDocument] = field(default_factory=list)

    def get(self, path: str, **request: Any) -> httpx.Response:
        """GET ``path``, as Cloud Run passes on a request it let through: the
        read service reads no credential."""

        return self.client.get(path, **request)

    def listed(self, **filters: str) -> list[dict[str, Any]]:
        response = self.get(LISTING, params=filters or {"run_id": CONSUMING_RUN})
        assert response.status_code == 200, response.text
        assert_private(response)
        return response.json()["documents"]

    def submit(
        self,
        *,
        run_id: str,
        licensed_data: bool,
        files: list[dict[str, Any]],
        build_id: str = BUILD,
        country_code: str = "UK",
    ) -> str:
        record, _ = self.submissions.create_submission(
            submission_request(
                run_id=run_id,
                build_id=build_id,
                licensed_data=licensed_data,
                files=files,
                country_code=country_code,
            ),
            PRINCIPAL,
        )
        return record.submission_id

    def publish(
        self,
        *,
        run_id: str = CONSUMING_RUN,
        build_id: str = BUILD,
        licensed_data: bool = False,
        upstream: dict[str, bytes] | None = None,
        content: bytes | None = None,
        country_code: str = "UK",
    ) -> PublishedDocument:
        """Record and store a document as the worker publishes one, with the
        ``upstream`` files it links to."""

        variant = len(self.published) + 1
        upstream_files = upstream or {}
        if content is None:
            content = json.dumps({"document": variant, "nodes": []}).encode()
        submission_id = self.submit(
            run_id=run_id,
            build_id=build_id,
            licensed_data=licensed_data,
            country_code=country_code,
            files=[
                *orrery_files(f"published-{variant}"),
                *(
                    submitted_file(name, "upstream", upstream_content)
                    for name, upstream_content in upstream_files.items()
                ),
            ],
        )
        published = PublishedDocument(submission_id, sha256_hex(content), content)
        for name, upstream_content in upstream_files.items():
            self.storage.store(published.object_prefix + name, upstream_content)
        self.storage.store(published.object_prefix + GRAPH_FILE_NAME, content)
        self.graphs.record_graph(
            graph_record(
                submission_id,
                published.document_sha256,
                run_id=run_id,
                build_id=build_id,
                visibility="private" if licensed_data else "public",
                document_bytes=len(content),
                upstream_files=len(upstream_files),
                country_code=country_code,
            )
        )
        self.published.append(published)
        return published

    def submit_producer(
        self,
        *,
        licensed_data: bool,
        role: str = "schema",
        run_id: str = PRODUCING_RUN,
        build_id: str = PRODUCING_BUILD,
    ) -> str:
        """A submission of the run and build whose file the consuming build
        copied as its upstream file, holding that file in ``role``."""

        files = orrery_files(f"producer-{run_id}-{role}-{licensed_data}")
        if role == "schema":
            files[0] = submitted_file("graph.schema.json", "schema", UPSTREAM_CONTENT)
        elif role == "evidence":
            files.append(
                submitted_file(
                    "evidence-spine-graph.json", "evidence", UPSTREAM_CONTENT
                )
            )
        else:
            files.append(submitted_file(UPSTREAM_NAME, role, UPSTREAM_CONTENT))
        return self.submit(
            run_id=run_id, build_id=build_id, licensed_data=licensed_data, files=files
        )


def serving_harness(
    *,
    submissions: FakeSubmissionStore | None = None,
    graphs: FakeOrreryGraphStore | None = None,
    storage: StandInStorageClient | None = None,
    catalogue: Callable[[InMemoryGraphCatalogue], Any] | None = None,
    raise_server_exceptions: bool = True,
) -> Serving:
    """The read service over the stand-ins. ``catalogue``, when given, wraps
    the in-memory catalogue the endpoints read."""

    clock = SteppingClock()
    if submissions is None:
        submissions = FakeSubmissionStore(clock=clock)
    if graphs is None:
        graphs = FakeOrreryGraphStore(clock=clock)
    if storage is None:
        storage = StandInStorageClient()
    in_memory_catalogue = InMemoryGraphCatalogue(graphs, submissions)
    services = serving().OrreryServingServices(
        catalogue=in_memory_catalogue
        if catalogue is None
        else catalogue(in_memory_catalogue),
        objects=graph_reader(storage),
    )
    client = TestClient(
        read_app().create_orrery_read_app(services=services),
        raise_server_exceptions=raise_server_exceptions,
    )
    return Serving(
        client=client,
        submissions=submissions,
        graphs=graphs,
        storage=storage,
        clock=clock,
    )


@pytest.fixture
def service() -> Serving:
    return serving_harness()


def assert_private(response: httpx.Response) -> None:
    assert response.headers.get("Cache-Control") == PRIVATE_NO_STORE


def assert_problem(
    response: httpx.Response, status_code: int, code: str
) -> dict[str, Any]:
    """An answer in the collector's error shape, ``detail`` beside ``code``."""

    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["detail"], str) and body["detail"]
    assert_private(response)
    return body


# Listing a run's or a build's documents.


def test_the_documents_of_a_run_come_back_with_their_fields(service: Serving) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.publish(run_id="uk-run-elsewhere", build_id="uk-build-elsewhere")

    assert service.listed(run_id=CONSUMING_RUN) == [
        {
            "submission_id": published.submission_id,
            "document_sha256": published.document_sha256,
            "document_bytes": len(published.content),
            "run_id": CONSUMING_RUN,
            "build_id": BUILD,
            "country_code": "UK",
            "orrery_version": ORRERY_VERSION,
            "projection_source": MICROCOSM_COMMIT,
            "collector_version": COLLECTOR_VERSION,
            "reconstructed": False,
            "created_at": "2026-10-20T09:00:00+00:00",
            "visibility": "public",
            "current": True,
            "document_path": published.path(),
            "upstream_paths": [published.path(UPSTREAM_NAME)],
        }
    ]
    assert service.listed(run_id="uk-run-unknown") == []


def test_a_build_s_documents_are_listed_by_its_build_id(service: Serving) -> None:
    first = service.publish()
    service.clock.advance(60)
    rerun = service.publish(run_id="uk-run-rerun")
    service.publish(build_id="uk-build-elsewhere")

    assert [
        (document["document_sha256"], document["run_id"], document["current"])
        for document in service.listed(build_id=BUILD)
    ] == [
        (rerun.document_sha256, "uk-run-rerun", True),
        (first.document_sha256, CONSUMING_RUN, False),
    ]
    assert [
        document["document_sha256"]
        for document in service.listed(run_id="uk-run-rerun", build_id=BUILD)
    ] == [rerun.document_sha256]


def test_current_marks_the_newest_document_of_each_build(service: Serving) -> None:
    first = service.publish(build_id="uk-build-a")
    service.clock.advance(60)
    second = service.publish(build_id="uk-build-a")
    service.clock.advance(60)
    other_build = service.publish(build_id="uk-build-b")

    assert [
        (document["document_sha256"], document["created_at"], document["current"])
        for document in service.listed()
    ] == [
        (other_build.document_sha256, "2026-10-20T09:02:00+00:00", True),
        (second.document_sha256, "2026-10-20T09:01:00+00:00", True),
        (first.document_sha256, "2026-10-20T09:00:00+00:00", False),
    ]


def test_documents_recorded_at_the_same_time_are_ordered_by_their_sha256(
    service: Serving,
) -> None:
    published = [service.publish(), service.publish()]
    larger_digest, smaller_digest = sorted(
        (document.document_sha256 for document in published), reverse=True
    )

    for _ in range(3):
        assert [
            (document["document_sha256"], document["current"])
            for document in service.listed()
        ] == [(larger_digest, True), (smaller_digest, False)]


def test_a_newer_document_of_the_build_from_another_run_is_its_current_one(
    service: Serving,
) -> None:
    own = service.publish()
    service.clock.advance(60)
    service.publish(run_id="uk-run-rerun")

    assert [
        (document["document_sha256"], document["current"])
        for document in service.listed(run_id=CONSUMING_RUN)
    ] == [(own.document_sha256, False)]


def test_each_country_s_documents_with_one_build_id_have_their_own_current_one(
    service: Serving,
) -> None:
    older_uk = service.publish()
    service.clock.advance(60)
    newer_uk = service.publish(run_id="uk-run-rerun")
    service.clock.advance(60)
    # Another country's build that happens to have the same build ID.
    us_document = service.publish(run_id="us-run-consumer", country_code="US")

    assert [
        (document["country_code"], document["document_sha256"], document["current"])
        for document in service.listed(build_id=BUILD)
    ] == [
        ("US", us_document.document_sha256, True),
        ("UK", newer_uk.document_sha256, True),
        ("UK", older_uk.document_sha256, False),
    ]
    assert [
        (document["document_sha256"], document["current"])
        for document in service.listed(run_id="uk-run-rerun")
    ] == [(newer_uk.document_sha256, True)]
    assert [
        (document["document_sha256"], document["current"])
        for document in service.listed(run_id=CONSUMING_RUN)
    ] == [(older_uk.document_sha256, False)]


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"run_id": ""},
        {"run_id": "a run with spaces"},
        {"build_id": "../uk-build"},
    ],
    ids=["no filter", "an empty run ID", "a malformed run ID", "a malformed build ID"],
)
def test_a_listing_needs_a_well_formed_run_or_build(
    service: Serving, filters: dict[str, str]
) -> None:
    service.publish()

    response = service.get(LISTING, params=filters)

    assert_problem(response, 422, "orrery_graph_query_invalid")


# Effective visibility.


def test_a_licensed_build_s_document_is_private(service: Serving) -> None:
    published = service.publish(licensed_data=True)

    [document] = service.listed()

    assert document["visibility"] == "private"
    assert service.get(published.path()).headers["X-Orrery-Visibility"] == "private"


def test_an_upstream_file_no_recorded_run_submitted_leaves_the_document_public(
    service: Serving,
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})

    [document] = service.listed()

    assert document["visibility"] == "public"
    assert service.get(published.path()).headers["X-Orrery-Visibility"] == "public"


@pytest.mark.parametrize("role", ["schema", "evidence"])
def test_a_licensed_producing_run_makes_a_public_document_private(
    service: Serving, role: str
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.submit_producer(licensed_data=True, role=role)

    [document] = service.listed()

    assert document["visibility"] == "private"
    for file_name in (GRAPH_FILE_NAME, UPSTREAM_NAME):
        response = service.get(published.path(file_name))
        assert response.status_code == 200, response.text
        assert response.headers["X-Orrery-Visibility"] == "private"


def test_an_unlicensed_producing_run_leaves_the_document_public(
    service: Serving,
) -> None:
    service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.submit_producer(licensed_data=False)

    [document] = service.listed()

    assert document["visibility"] == "public"


def test_a_run_that_submitted_the_file_only_as_its_own_upstream_file_is_no_producer(
    service: Serving,
) -> None:
    service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.submit_producer(licensed_data=True, role="upstream")

    [document] = service.listed()

    assert document["visibility"] == "public"


def test_any_licensed_submission_of_the_producing_run_makes_the_document_private(
    service: Serving,
) -> None:
    service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.submit_producer(licensed_data=False)
    service.submit(
        run_id=PRODUCING_RUN,
        build_id="uk-spine-attempt-2",
        licensed_data=True,
        files=orrery_files("producer-later"),
    )

    [document] = service.listed()

    assert document["visibility"] == "private"


def test_any_licensed_submission_of_the_producing_build_makes_the_document_private(
    service: Serving,
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.submit_producer(licensed_data=False)
    # The same build, submitted again from another run.
    service.submit(
        run_id="uk-run-producer-rerun",
        build_id=PRODUCING_BUILD,
        licensed_data=True,
        files=orrery_files("producer-rerun"),
    )

    [document] = service.listed()

    assert document["visibility"] == "private"
    assert service.get(published.path()).headers["X-Orrery-Visibility"] == "private"


def test_a_licensed_submission_of_the_document_s_own_run_makes_it_private(
    service: Serving,
) -> None:
    service.publish()
    service.submit(
        run_id=CONSUMING_RUN,
        build_id="uk-build-elsewhere",
        licensed_data=True,
        files=orrery_files("own-run"),
    )

    [document] = service.listed()

    assert document["visibility"] == "private"


def test_a_licensed_submission_of_the_document_s_own_build_makes_it_private(
    service: Serving,
) -> None:
    published = service.publish()
    # The contract defines licensed_data per build, and a build can be
    # submitted from more than one run.
    service.submit(
        run_id="uk-run-rerun",
        build_id=BUILD,
        licensed_data=True,
        files=orrery_files("own-build"),
    )

    [document] = service.listed()

    assert document["visibility"] == "private"
    assert service.get(published.path()).headers["X-Orrery-Visibility"] == "private"


def test_licensed_submissions_of_other_runs_and_builds_leave_the_document_public(
    service: Serving,
) -> None:
    service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.submit_producer(licensed_data=False)
    service.submit(
        run_id="uk-run-unrelated",
        build_id="uk-build-unrelated",
        licensed_data=True,
        files=orrery_files("unrelated"),
    )

    [document] = service.listed()

    assert document["visibility"] == "public"


def test_a_producing_run_recorded_after_the_document_still_makes_it_private(
    service: Serving,
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    assert [document["visibility"] for document in service.listed()] == ["public"]

    service.submit_producer(licensed_data=True)

    assert [document["visibility"] for document in service.listed()] == ["private"]
    assert service.get(published.path()).headers["X-Orrery-Visibility"] == "private"


# Fetching a document or an upstream file.


def test_a_document_comes_back_with_its_bytes_and_headers(service: Serving) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})

    response = service.get(published.path())

    assert response.status_code == 200, response.text
    assert response.content == published.content
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["ETag"] == f'"{published.document_sha256}"'
    assert response.headers["X-Orrery-Visibility"] == "public"
    assert_private(response)
    assert service.storage.bucket_names == [BUCKET]


def test_an_upstream_file_comes_back_with_its_bytes_and_headers(
    service: Serving,
) -> None:
    published = service.publish(
        licensed_data=True, upstream={UPSTREAM_NAME: UPSTREAM_CONTENT}
    )

    response = service.get(published.path(UPSTREAM_NAME))

    assert response.status_code == 200, response.text
    assert response.content == UPSTREAM_CONTENT
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["ETag"] == f'"{UPSTREAM_SHA256}"'
    assert response.headers["X-Orrery-Visibility"] == "private"
    assert_private(response)


# Paths whose parts the route matches, but which no served file has.
UNSERVED_PATHS: dict[str, Callable[[PublishedDocument], str]] = {
    "an unknown document": lambda published: (
        f"{LISTING}/{published.submission_id}/{'0' * 64}/{GRAPH_FILE_NAME}"
    ),
    "an unknown submission": lambda published: (
        f"{LISTING}/sub-{'0' * 32}/{published.document_sha256}/{GRAPH_FILE_NAME}"
    ),
    "a malformed submission ID": lambda published: (
        f"{LISTING}/submission-1/{published.document_sha256}/{GRAPH_FILE_NAME}"
    ),
    "a digest in capitals": lambda published: (
        f"{LISTING}/{published.submission_id}/{published.document_sha256.upper()}/"
        f"{GRAPH_FILE_NAME}"
    ),
    "the schema's name": lambda published: published.path("graph.schema.json"),
    "a short upstream digest": lambda published: published.path("upstream-abc.json"),
    "an upstream digest in capitals": lambda published: published.path(
        f"upstream-{UPSTREAM_SHA256.upper()}.json"
    ),
    "an upstream file the document does not have": lambda published: published.path(
        f"upstream-{'a' * 64}.json"
    ),
    "the parent folder": lambda published: published.path("%2E%2E"),
    "a parent folder in place of the digest": lambda published: (
        f"{LISTING}/{published.submission_id}/%2E%2E/{GRAPH_FILE_NAME}"
    ),
}


@pytest.mark.parametrize("case", sorted(UNSERVED_PATHS))
def test_a_path_that_names_no_served_file_answers_404(
    service: Serving, case: str
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})

    response = service.get(UNSERVED_PATHS[case](published))

    assert_problem(response, 404, "orrery_graph_not_found")
    assert service.storage.reads == []


@pytest.mark.parametrize(
    "file_part",
    [
        f"..%2F{GRAPH_FILE_NAME}",
        f"%2E%2E%2F%2E%2E%2Fsub-{'0' * 32}%2F{'0' * 64}%2F{GRAPH_FILE_NAME}",
        f"{UPSTREAM_NAME}%2F{GRAPH_FILE_NAME}",
        f"%2F{GRAPH_FILE_NAME}",
    ],
    ids=[
        "a parent folder and an encoded slash",
        "another document through parent folders",
        "a folder below an upstream file",
        "a leading encoded slash",
    ],
)
def test_a_path_with_an_encoded_slash_answers_404(
    service: Serving, file_part: str
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})

    response = service.get(published.path(file_part))

    assert response.status_code == 404, response.text
    assert_private(response)
    assert service.storage.reads == []


@pytest.mark.parametrize("file_name", [GRAPH_FILE_NAME, UPSTREAM_NAME])
def test_a_file_missing_from_the_bucket_answers_404(
    service: Serving, file_name: str
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    del service.storage.objects[published.object_prefix + file_name]

    response = service.get(published.path(file_name))

    assert_problem(response, 404, "orrery_graph_not_found")


CHANGED_CONTENT = b'{"changed":"after publishing"}'


def change_document_bytes(storage: StandInStorageClient, published: Any) -> None:
    storage.store(
        published.object_prefix + GRAPH_FILE_NAME,
        CHANGED_CONTENT,
        metadata={"sha256": published.document_sha256},
    )


def replace_the_recorded_document(
    storage: StandInStorageClient, published: Any
) -> None:
    # Bytes and metadata agree, but the record names other bytes.
    storage.store(published.object_prefix + GRAPH_FILE_NAME, CHANGED_CONTENT)


def change_upstream_bytes(storage: StandInStorageClient, published: Any) -> None:
    storage.store(
        published.object_prefix + UPSTREAM_NAME,
        CHANGED_CONTENT,
        metadata={"sha256": UPSTREAM_SHA256},
    )


def replace_the_named_upstream_file(
    storage: StandInStorageClient, published: Any
) -> None:
    # Bytes and metadata agree, but the name gives another SHA-256.
    storage.store(published.object_prefix + UPSTREAM_NAME, CHANGED_CONTENT)


@pytest.mark.parametrize(
    ("change", "file_name"),
    [
        (change_document_bytes, GRAPH_FILE_NAME),
        (replace_the_recorded_document, GRAPH_FILE_NAME),
        (change_upstream_bytes, UPSTREAM_NAME),
        (replace_the_named_upstream_file, UPSTREAM_NAME),
    ],
    ids=[
        "document bytes changed",
        "document other than the recorded one",
        "upstream bytes changed",
        "upstream file other than the one its name gives",
    ],
)
def test_a_file_whose_sha256_does_not_match_answers_500_and_is_not_served(
    service: Serving,
    caplog: pytest.LogCaptureFixture,
    change: Callable[[StandInStorageClient, Any], None],
    file_name: str,
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    change(service.storage, published)
    caplog.set_level(logging.ERROR)

    response = service.get(published.path(file_name))

    assert_problem(response, 500, "orrery_graph_digest_mismatch")
    assert CHANGED_CONTENT not in response.content
    assert "X-Orrery-Visibility" not in response.headers
    assert_error_logged(caplog, published.object_prefix + file_name)


def assert_error_logged(caplog: pytest.LogCaptureFixture, object_name: str) -> None:
    assert any(
        record.levelno == logging.ERROR and object_name in record.getMessage()
        for record in caplog.records
    )


def test_files_are_checked_without_their_sha256_metadata(service: Serving) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    for file_name, content in (
        (GRAPH_FILE_NAME, published.content),
        (UPSTREAM_NAME, UPSTREAM_CONTENT),
    ):
        service.storage.store(published.object_prefix + file_name, content, metadata={})
    service.submit_producer(licensed_data=True)

    [document] = service.listed()
    served = {
        file_name: service.get(published.path(file_name))
        for file_name in (GRAPH_FILE_NAME, UPSTREAM_NAME)
    }

    # The upstream file's name gives its SHA-256, which links it to the
    # licensed producing run.
    assert document["visibility"] == "private"
    assert served[GRAPH_FILE_NAME].content == published.content
    assert served[UPSTREAM_NAME].content == UPSTREAM_CONTENT
    for response in served.values():
        assert response.status_code == 200, response.text
        assert response.headers["X-Orrery-Visibility"] == "private"


def large_document(variant: str) -> bytes:
    """A document of two and a half parts."""

    padding = "x" * (5 * SERVED_PART_BYTES // 2)
    return json.dumps({"document": variant, "padding": padding}).encode()


@pytest.mark.parametrize("file_name", [GRAPH_FILE_NAME, UPSTREAM_NAME])
def test_a_file_is_hashed_and_then_sent_from_the_same_generation_in_parts(
    service: Serving, file_name: str
) -> None:
    large_upstream_content = large_document("upstream")
    large_upstream_name = f"upstream-{sha256_hex(large_upstream_content)}.json"
    published = service.publish(
        content=large_document("document"),
        upstream={large_upstream_name: large_upstream_content},
    )
    served_name, content = (
        (GRAPH_FILE_NAME, published.content)
        if file_name == GRAPH_FILE_NAME
        else (large_upstream_name, large_upstream_content)
    )
    object_name = published.object_prefix + served_name
    generation = service.storage.generation(object_name)

    response = service.get(published.path(served_name))

    assert response.status_code == 200, response.text
    assert response.content == content
    # One read checks the bytes, and a second sends them; both read the
    # generation that was looked up, to its end, holding at most one part.
    assert [
        (read.name, read.generation, read.finished) for read in service.storage.reads
    ] == [(object_name, generation, True)] * 2
    assert all(
        0 < read.largest_held_bytes <= SERVED_PART_BYTES
        for read in service.storage.reads
    )


@pytest.mark.parametrize("file_name", [GRAPH_FILE_NAME, UPSTREAM_NAME])
def test_a_file_replaced_after_it_was_looked_up_answers_500_and_is_not_served(
    service: Serving, caplog: pytest.LogCaptureFixture, file_name: str
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    object_name = published.object_prefix + file_name
    content = service.storage.objects[object_name].content

    def store_a_new_generation(name: str) -> None:
        # The same bytes, but as another object.
        if name == object_name:
            service.storage.store(name, content)

    service.storage.after_lookup = store_a_new_generation
    caplog.set_level(logging.ERROR)

    response = service.get(published.path(file_name))

    assert_problem(response, 500, "orrery_graph_changed")
    assert content not in response.content
    assert "X-Orrery-Visibility" not in response.headers
    assert_error_logged(caplog, object_name)


def test_a_file_replaced_after_its_check_is_not_sent_whole(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = serving_harness(raise_server_exceptions=False)
    published = harness.publish(content=large_document("document"))
    object_name = published.object_prefix + GRAPH_FILE_NAME

    def store_a_new_generation_after_the_check(read: ObjectRead) -> None:
        if read.name == object_name and len(harness.storage.reads) == 1:
            harness.storage.store(object_name, published.content)

    harness.storage.after_read = store_a_new_generation_after_the_check
    caplog.set_level(logging.ERROR)

    response = harness.get(published.path())

    # The answer has started, so the collector can only stop it unfinished.
    assert len(response.content) < len(published.content)
    assert_error_logged(caplog, object_name)


@pytest.mark.parametrize("file_name", [GRAPH_FILE_NAME, UPSTREAM_NAME])
def test_a_checksum_failure_the_storage_client_reports_answers_500(
    service: Serving, caplog: pytest.LogCaptureFixture, file_name: str
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    object_name = published.object_prefix + file_name
    service.storage.objects[object_name].corrupt = True
    caplog.set_level(logging.ERROR)

    response = service.get(published.path(file_name))

    assert_problem(response, 500, "orrery_graph_digest_mismatch")
    assert "X-Orrery-Visibility" not in response.headers
    assert_error_logged(caplog, object_name)


# Each storage call that can fail: its operation, and the endpoint whose
# request makes it.
STORAGE_FAILURES = {
    "listing a listed document's folder": ("list", "listing"),
    "looking the file up": ("lookup", "file"),
    "reading the file to check it": ("read", "file"),
    "listing the served document's folder": ("list", "file"),
}


@pytest.mark.parametrize("failure", sorted(STORAGE_FAILURES))
def test_a_bucket_that_does_not_answer_gives_503_and_is_logged_without_credentials(
    service: Serving, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    operation, endpoint = STORAGE_FAILURES[failure]
    service.storage.unavailable.add(operation)
    caplog.set_level(logging.DEBUG)
    path = (
        f"{LISTING}?run_id={CONSUMING_RUN}"
        if endpoint == "listing"
        else published.path()
    )

    response = service.get(path)

    assert_problem(response, 503, "orrery_storage_unavailable")
    assert int(response.headers["Retry-After"]) > 0
    [failed_call] = [
        call for call in service.storage.calls if call.operation == operation
    ]
    [logged] = [
        record
        for record in caplog.records
        if record.name == ROUTES_MODULE and record.levelno >= logging.WARNING
    ]
    # The folder or object the call named, and the error's type, but not its
    # message, which can hold what the request carried.
    assert failed_call.name in logged.getMessage()
    assert "ServiceUnavailable" in logged.getMessage()
    assert CREDENTIAL_IN_STORAGE_ERROR not in caplog.text


def test_every_storage_call_has_a_timeout_and_a_retry_deadline_well_inside_the_request_timeout(
    service: Serving,
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})

    service.listed()
    for file_name in (GRAPH_FILE_NAME, UPSTREAM_NAME):
        assert service.get(published.path(file_name)).status_code == 200

    assert {call.operation for call in service.storage.calls} == set(STORAGE_OPERATIONS)
    request_timeout_seconds = read_app().REQUEST_TIMEOUT_SECONDS
    for call in service.storage.calls:
        timeout_seconds = call.options.get("timeout")
        retry = call.options.get("retry")
        assert isinstance(timeout_seconds, int | float), call
        assert timeout_seconds > 0, call
        assert retry is not None and retry.timeout is not None, call
        # A fetch makes several storage calls one after another, so one that
        # is retried until its deadline must leave time for the others.
        assert retry.timeout + timeout_seconds <= request_timeout_seconds / 4, call


@pytest.mark.parametrize(
    ("endpoint", "failing_method"),
    [
        ("listing", "graph_files"),
        ("listing", "producing_submissions"),
        ("listing", "licensed_runs_and_builds"),
        ("file", "graph_file"),
        ("file", "producing_submissions"),
        ("file", "licensed_runs_and_builds"),
    ],
)
def test_a_database_that_does_not_answer_gives_503(
    endpoint: str, failing_method: str
) -> None:
    harness = serving_harness(
        catalogue=lambda in_memory: UnavailableDatabaseCatalogue(
            in_memory, failing_method
        )
    )
    published = harness.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    path = (
        f"{LISTING}?run_id={CONSUMING_RUN}"
        if endpoint == "listing"
        else published.path()
    )

    response = harness.get(path)

    assert_problem(response, 503, "orrery_catalogue_unavailable")
    assert int(response.headers["Retry-After"]) > 0


@pytest.mark.parametrize("endpoint", ["listing", "file"])
def test_the_postgres_catalogue_s_connection_errors_give_503(endpoint: str) -> None:
    engine = create_database_engine(UNREACHABLE_DATABASE_URL)
    try:
        harness = serving_harness(
            catalogue=lambda _: serving().PostgresOrreryGraphCatalogue(engine)
        )
        published = harness.publish()
        path = (
            f"{LISTING}?run_id={CONSUMING_RUN}"
            if endpoint == "listing"
            else published.path()
        )

        response = harness.get(path)
    finally:
        engine.dispose()

    assert_problem(response, 503, "orrery_catalogue_unavailable")
    assert int(response.headers["Retry-After"]) > 0


def test_a_document_the_worker_published_is_listed_and_served() -> None:
    from tests.test_orrery_graphs import UPSTREAM_CONTENT as WORKER_UPSTREAM_CONTENT
    from tests.test_orrery_graphs import UPSTREAM_NAME as WORKER_UPSTREAM_NAME
    from tests.test_orrery_graphs import published_run, upstream_inputs

    submission, publishing, result = published_run(
        upstream_inputs(), licensed_data=False
    )
    harness = serving_harness(
        submissions=submission.submissions,
        graphs=publishing.records,
        storage=StandInStorageClient(objects=publishing.bucket.stored),
    )

    [document] = harness.listed(run_id=submission.record().provenance["run_id"])
    served_document = harness.get(document["document_path"])
    [upstream_path] = document["upstream_paths"]
    served_upstream = harness.get(upstream_path)

    assert document["submission_id"] == submission.submission_id
    assert document["document_sha256"] == result["document_sha256"]
    assert document["visibility"] == "public"
    assert document["current"] is True
    assert upstream_path.endswith("/" + WORKER_UPSTREAM_NAME)
    assert served_document.status_code == 200, served_document.text
    assert served_document.content == publishing.published(result)
    assert served_upstream.status_code == 200, served_upstream.text
    assert served_upstream.content == WORKER_UPSTREAM_CONTENT


# Every answer of the read service is marked private and not to be stored.


def test_every_answer_of_the_read_service_is_marked_not_to_be_stored(
    service: Serving,
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})

    answers = [
        service.get(LISTING, params={"run_id": CONSUMING_RUN}),
        service.get(published.path()),
        service.get(published.path(UPSTREAM_NAME)),
        service.get(LISTING),
        service.get(published.path("graph.schema.json")),
        service.get(f"{LISTING}/unknown"),
        service.client.post(LISTING),
        # Paths outside the Orrery graph routes, which the read service
        # doesn't have.
        service.get("/"),
        service.get("/health"),
        service.get("/openapi.json"),
    ]

    assert [answer.status_code for answer in answers] == [
        200,
        200,
        200,
        422,
        404,
        404,
        405,
        404,
        404,
        404,
    ]
    for answer in answers:
        assert_private(answer)


# The read service and its settings.


def mounted_routes(application: FastAPI) -> dict[str, set[str]]:
    """Each path the application routes, with its methods.

    FastAPI keeps an included router in ``application.routes`` as one entry
    with no path of its own, and lists that router's routes, with its prefix
    applied, through the entry's ``effective_route_contexts()``. This expands
    such entries, so that every route is listed.
    """

    methods_by_path: dict[str, set[str]] = {}
    for entry in application.routes:
        included_routes = getattr(entry, "effective_route_contexts", None)
        routes = included_routes() if included_routes is not None else [entry]
        for route in routes:
            methods_by_path.setdefault(route.path, set()).update(
                getattr(route, "methods", None) or set()
            )
    return methods_by_path


def test_the_read_service_mounts_only_the_orrery_graph_routes(
    service: Serving,
) -> None:
    assert mounted_routes(service.client.app) == {
        LISTING: {"GET"},
        FILE_ROUTE: {"GET"},
    }


def test_the_read_service_verifies_no_token(service: Serving) -> None:
    published = service.publish()

    # Cloud Run checks the caller before a request reaches the service, so
    # whatever the request still carries is not read.
    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        assert service.get(published.path(), headers=headers).status_code == 200
    assert importlib.util.find_spec("telemetry_collector.orrery_access") is None


@dataclass
class ReadServiceEnvironment:
    """The process environment, and the settings of each Cloud Storage reader
    the read service built from it."""

    monkeypatch: pytest.MonkeyPatch
    readers: list[dict[str, Any]]

    def set(self, environment: dict[str, str]) -> None:
        for name, value in environment.items():
            self.monkeypatch.setenv(name, value)


@pytest.fixture
def read_service_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> ReadServiceEnvironment:
    for name in [*READ_SERVICE_ENVIRONMENT, RETIRED_READER_SETTING]:
        monkeypatch.delenv(name, raising=False)
    readers: list[dict[str, Any]] = []

    def stand_in_reader(**settings: Any) -> dict[str, Any]:
        readers.append(settings)
        return settings

    monkeypatch.setattr(
        google_cloud, "GoogleCloudStorageOrreryGraphReader", stand_in_reader
    )
    return ReadServiceEnvironment(monkeypatch, readers)


def test_the_deployed_read_service_starts_with_its_two_settings(
    read_service_environment: ReadServiceEnvironment,
) -> None:
    read_service_environment.set(READ_SERVICE_ENVIRONMENT)

    application = read_app().create_orrery_read_app_from_environment()

    assert set(mounted_routes(application)) == {LISTING, FILE_ROUTE}
    assert read_service_environment.readers == [{"bucket_name": BUCKET}]


@pytest.mark.parametrize("missing_name", sorted(READ_SERVICE_ENVIRONMENT))
def test_the_read_service_refuses_to_start_without_either_setting(
    read_service_environment: ReadServiceEnvironment, missing_name: str
) -> None:
    read_service_environment.set(
        {
            name: value
            for name, value in READ_SERVICE_ENVIRONMENT.items()
            if name != missing_name
        }
    )

    with pytest.raises(RuntimeError, match=missing_name):
        read_app().create_orrery_read_app_from_environment()
    assert read_service_environment.readers == []


@pytest.mark.parametrize(
    "malformed_bucket", ["gs://collector-orrery-test", "Collector-Orrery"]
)
def test_a_malformed_bucket_name_stops_the_read_service(
    read_service_environment: ReadServiceEnvironment, malformed_bucket: str
) -> None:
    read_service_environment.set(
        {**READ_SERVICE_ENVIRONMENT, "COLLECTOR_ORRERY_BUCKET": malformed_bucket}
    )

    with pytest.raises(RuntimeError, match="COLLECTOR_ORRERY_BUCKET"):
        read_app().create_orrery_read_app_from_environment()


@pytest.mark.parametrize("reader_setting", [READER, "not-a-service-account"])
def test_the_read_service_reads_no_reader_account_setting(reader_setting: str) -> None:
    with_reader_setting = serving().OrreryServingSettings.from_environment(
        {**READ_SERVICE_ENVIRONMENT, RETIRED_READER_SETTING: reader_setting}
    )

    assert with_reader_setting == serving().OrreryServingSettings.from_environment(
        READ_SERVICE_ENVIRONMENT
    )
    assert reader_setting not in repr(with_reader_setting)


def test_the_read_service_settings_never_show_the_database_url() -> None:
    settings = serving().OrreryServingSettings.from_environment(
        READ_SERVICE_ENVIRONMENT
    )

    assert settings.bucket == BUCKET
    assert settings.database_url == UNREACHABLE_DATABASE_URL
    assert UNREACHABLE_DATABASE_URL not in repr(settings)


@pytest.mark.parametrize(
    "bucket",
    [BUCKET, "gs://collector-orrery", "Collector-Orrery", "ab", "a" * 63, "a" * 64],
)
def test_the_read_service_checks_the_bucket_name_as_the_worker_does(
    bucket: str,
) -> None:
    try:
        worker_accepts = orrery_bucket({"COLLECTOR_ORRERY_BUCKET": bucket}) == bucket
    except RuntimeError:
        worker_accepts = False
    try:
        settings = serving().OrreryServingSettings.from_environment(
            {**READ_SERVICE_ENVIRONMENT, "COLLECTOR_ORRERY_BUCKET": bucket}
        )
        read_service_accepts = settings.bucket == bucket
    except RuntimeError:
        read_service_accepts = False

    assert read_service_accepts == worker_accepts


# The collector's main application serves no Orrery graph file.


class UnusedAuthenticator:
    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        raise AssertionError("These tests exchange no Hugging Face token.")


def test_the_main_application_mounts_no_orrery_graph_route() -> None:
    application = create_app(
        settings=CollectorSettings(
            jwt_secret=JWT_SECRET, read_token="dashboard-read-token"
        ),
        store=FakeTelemetryStore(),
        huggingface_authenticator=UnusedAuthenticator(),
    )
    document_path = f"{LISTING}/sub-{'0' * 32}/{'0' * 64}/{GRAPH_FILE_NAME}"
    main_routes = mounted_routes(application)

    # The routes of the included intake router are listed too, so the check
    # below covers every route.
    assert set(SUBMISSION_PATHS) <= set(main_routes)
    assert not [path for path in main_routes if path.startswith(LISTING)]
    with TestClient(application) as client:
        for response in (
            client.get(LISTING, params={"run_id": CONSUMING_RUN}),
            client.get(document_path),
        ):
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}
            assert "Cache-Control" not in response.headers


def test_the_deployed_main_application_ignores_the_orrery_settings(
    read_service_environment: ReadServiceEnvironment,
) -> None:
    read_service_environment.set(
        {
            **READ_SERVICE_ENVIRONMENT,
            RETIRED_READER_SETTING: READER,
            "TELEMETRY_JWT_SECRET": "x" * 32,
            "TELEMETRY_READ_TOKEN": "y" * 24,
        }
    )
    for name in ("COLLECTOR_INTAKE_ENABLED", "TELEMETRY_JWT_SIGNING_KEY"):
        read_service_environment.monkeypatch.delenv(name, raising=False)

    application = create_app_from_environment()

    assert not hasattr(CollectorSettings.from_environment(), "orrery_serving")
    main_routes = mounted_routes(application)
    assert set(SUBMISSION_PATHS) <= set(main_routes)
    assert not [path for path in main_routes if path.startswith(LISTING)]
    assert read_service_environment.readers == []


# The catalogue, in memory and in PostgreSQL.


@dataclass
class RecordStores:
    submissions: Any
    graphs: Any
    catalogue: Any
    clock: SteppingClock

    def submit(
        self,
        *,
        run_id: str,
        licensed_data: bool,
        files: list[dict[str, Any]],
        build_id: str = BUILD,
        country_code: str = "UK",
    ) -> str:
        record, _ = self.submissions.create_submission(
            submission_request(
                run_id=run_id,
                build_id=build_id,
                licensed_data=licensed_data,
                files=files,
                country_code=country_code,
            ),
            PRINCIPAL,
        )
        return record.submission_id

    def record(
        self,
        submission_id: str,
        document_sha256: str,
        *,
        run_id: str,
        build_id: str,
        visibility: str = "public",
        country_code: str = "UK",
    ) -> None:
        assert self.graphs.record_graph(
            graph_record(
                submission_id,
                document_sha256,
                run_id=run_id,
                build_id=build_id,
                visibility=visibility,
                country_code=country_code,
            )
        )


@pytest.fixture(params=["memory", "postgres"])
def record_stores(request: pytest.FixtureRequest) -> Iterator[RecordStores]:
    clock = SteppingClock()
    if request.param == "memory":
        submissions = FakeSubmissionStore(clock=clock)
        graphs = FakeOrreryGraphStore(clock=clock)
        yield RecordStores(
            submissions, graphs, InMemoryGraphCatalogue(graphs, submissions), clock
        )
        return
    database_url = request.getfixturevalue("postgres_url")
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    try:
        yield RecordStores(
            PostgresSubmissionStore(engine, clock=clock),
            PostgresOrreryGraphStore(engine, clock=clock),
            serving().PostgresOrreryGraphCatalogue(engine),
            clock,
        )
    finally:
        engine.dispose()


def test_the_catalogue_finds_documents_by_run_by_build_or_both(
    record_stores: RecordStores,
) -> None:
    first = record_stores.submit(
        run_id="run-a", licensed_data=False, files=orrery_files("a")
    )
    second = record_stores.submit(
        run_id="run-b", licensed_data=False, files=orrery_files("b")
    )
    record_stores.record(first, "a" * 64, run_id="run-a", build_id="build-1")
    record_stores.clock.advance(60)
    record_stores.record(second, "b" * 64, run_id="run-b", build_id="build-1")
    record_stores.record(second, "c" * 64, run_id="run-b", build_id="build-2")

    def found(**filters: str) -> list[tuple[str, str]]:
        return sorted(
            (document.submission_id, document.document_sha256)
            for document in record_stores.catalogue.graph_files(**filters)
        )

    assert found(run_id="run-a") == [(first, "a" * 64)]
    assert found(run_id="run-b") == sorted([(second, "b" * 64), (second, "c" * 64)])
    assert found(build_id="build-1") == sorted([(first, "a" * 64), (second, "b" * 64)])
    assert found(run_id="run-b", build_id="build-2") == [(second, "c" * 64)]
    assert found(run_id="run-a", build_id="build-2") == []
    assert found(run_id="run-unknown") == []


def test_the_catalogue_finds_a_build_s_documents_within_one_country(
    record_stores: RecordStores,
) -> None:
    uk_submission = record_stores.submit(
        run_id="run-uk", licensed_data=False, files=orrery_files("uk")
    )
    us_submission = record_stores.submit(
        run_id="run-us",
        licensed_data=False,
        files=orrery_files("us"),
        country_code="US",
    )
    record_stores.record(uk_submission, "a" * 64, run_id="run-uk", build_id="build-1")
    record_stores.record(
        us_submission, "b" * 64, run_id="run-us", build_id="build-1", country_code="US"
    )

    def found(**filters: str) -> list[tuple[str, str]]:
        return sorted(
            (document.submission_id, document.document_sha256)
            for document in record_stores.catalogue.graph_files(**filters)
        )

    assert found(build_id="build-1", country_code="UK") == [(uk_submission, "a" * 64)]
    assert found(build_id="build-1", country_code="US") == [(us_submission, "b" * 64)]
    assert found(build_id="build-1") == sorted(
        [(uk_submission, "a" * 64), (us_submission, "b" * 64)]
    )
    assert found(build_id="build-1", country_code="FR") == []


def test_the_catalogue_reads_one_document_s_record(
    record_stores: RecordStores,
) -> None:
    submission_id = record_stores.submit(
        run_id="run-a", licensed_data=True, files=orrery_files("a")
    )
    record_stores.record(
        submission_id,
        "a" * 64,
        run_id="run-a",
        build_id="build-1",
        visibility="private",
    )

    assert record_stores.catalogue.graph_file(
        submission_id, "a" * 64
    ) == serving().RecordedGraphFile(
        submission_id=submission_id,
        document_sha256="a" * 64,
        document_bytes=DOCUMENT_BYTES,
        run_id="run-a",
        build_id="build-1",
        country_code="UK",
        object_prefix=f"graphs/{submission_id}/{'a' * 64}/",
        orrery_version=ORRERY_VERSION,
        projection_source=MICROCOSM_COMMIT,
        collector_version=COLLECTOR_VERSION,
        reconstructed=False,
        visibility="private",
        created_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
    )
    assert record_stores.catalogue.graph_file(submission_id, "b" * 64) is None
    assert record_stores.catalogue.graph_file(f"sub-{'0' * 32}", "a" * 64) is None


def test_the_catalogue_names_the_runs_and_builds_that_submitted_a_file_not_as_upstream(
    record_stores: RecordStores,
) -> None:
    record_stores.submit(
        run_id="run-producer",
        build_id="build-producer",
        licensed_data=False,
        files=[
            submitted_file("graph.schema.json", "schema", UPSTREAM_CONTENT),
            *orrery_files("producer")[1:],
        ],
    )
    record_stores.submit(
        run_id="run-second-producer",
        build_id="build-second-producer",
        licensed_data=True,
        files=[
            *orrery_files("second-producer"),
            submitted_file("evidence-spine-graph.json", "evidence", UPSTREAM_CONTENT),
        ],
    )
    record_stores.submit(
        run_id="run-consumer",
        build_id="build-consumer",
        licensed_data=True,
        files=[
            *orrery_files("consumer"),
            submitted_file(UPSTREAM_NAME, "upstream", UPSTREAM_CONTENT),
        ],
    )

    assert record_stores.catalogue.producing_submissions(
        [UPSTREAM_SHA256, "f" * 64]
    ) == {
        UPSTREAM_SHA256: frozenset(
            {
                serving().SubmissionOrigin(
                    run_id="run-producer", build_id="build-producer"
                ),
                serving().SubmissionOrigin(
                    run_id="run-second-producer", build_id="build-second-producer"
                ),
            }
        )
    }
    assert record_stores.catalogue.producing_submissions([]) == {}


def test_the_catalogue_names_the_runs_and_builds_recorded_with_licensed_data(
    record_stores: RecordStores,
) -> None:
    for run_id, build_id, licensed_data, variant in [
        ("run-licensed", "build-licensed", True, 1),
        ("run-mixed", "build-mixed", False, 2),
        ("run-mixed", "build-second", True, 3),
        ("run-unlicensed", "build-unlicensed", False, 4),
        # Another run of the mixed build, with licensed data.
        ("run-rebuilt", "build-mixed", True, 5),
    ]:
        record_stores.submit(
            run_id=run_id,
            build_id=build_id,
            licensed_data=licensed_data,
            files=orrery_files(variant),
        )

    licensed = record_stores.catalogue.licensed_runs_and_builds(
        run_ids=["run-licensed", "run-mixed", "run-unlicensed", "run-unknown"],
        build_ids=["build-mixed", "build-unlicensed", "build-unknown"],
    )

    # Only the run IDs and build IDs asked about come back.
    assert licensed == serving().LicensedRunsAndBuilds(
        run_ids=frozenset({"run-licensed", "run-mixed"}),
        build_ids=frozenset({"build-mixed"}),
    )
    assert record_stores.catalogue.licensed_runs_and_builds(
        run_ids=[], build_ids=[]
    ) == serving().LicensedRunsAndBuilds(run_ids=frozenset(), build_ids=frozenset())
    assert record_stores.catalogue.licensed_runs_and_builds(
        run_ids=[], build_ids=["build-licensed"]
    ) == serving().LicensedRunsAndBuilds(
        run_ids=frozenset(), build_ids=frozenset({"build-licensed"})
    )
