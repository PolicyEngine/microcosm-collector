"""Orrery graph files are served only to microcosm-runs' own service account.

The collector lists and serves the Orrery graph files its worker published
(``telemetry_collector.orrery_graph_api``) only to requests that carry a
Google-signed identity token of the configured reader account, which
``telemetry_collector.orrery_access`` checks. These tests sign tokens with a
key generated here and give the check that key's certificate, so they never
reach Google. They read the bucket through the collector's Google Cloud
Storage adapter over a stand-in client, and the records through in-memory
stores, or through PostgreSQL as well where ``TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import threading
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, ServiceUnavailable
from google.auth.exceptions import TransportError
from google.cloud.storage.exceptions import DataCorruption
from sqlalchemy.exc import OperationalError

from telemetry_collector import google_cloud
from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import create_database_engine
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.orrery.projection.provenance import MICROCOSM_COMMIT
from telemetry_collector.orrery_graphs import (
    GRAPH_FILE_NAME,
    OrreryGraphRecord,
    PostgresOrreryGraphStore,
    orrery_bucket,
)
from telemetry_collector.read_api_v2 import openapi_document
from telemetry_collector.submission_store import PostgresSubmissionStore
from telemetry_collector.submissions import SubmissionRequest
from tests.fakes import FakeOrreryGraphStore, FakeSubmissionStore, FakeTelemetryStore
from tests.test_submission_store import SteppingClock

ACCESS_MODULE = "telemetry_collector.orrery_access"
SERVING_MODULE = "telemetry_collector.orrery_serving"
# The audience microcosm-runs requests its identity tokens for.
AUDIENCE = "microcosm-collector/orrery-graphs"
GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_CERTIFICATES_URL = "https://www.googleapis.com/oauth2/v1/certs"
READER = "microcosm-runs-reader@example-project.iam.gserviceaccount.com"
ANOTHER_ACCOUNT = "someone-else@example-project.iam.gserviceaccount.com"
# The reader account's unique ID, which Google puts in `sub` and `azp`.
READER_ACCOUNT_ID = "112233445566778899000"
BUCKET = "collector-orrery-test"
KEY_ID = "google-test-key-1"
# The key ID of a key Google starts signing with while the collector still
# holds the certificates it fetched before.
ROTATED_KEY_ID = "google-test-key-2"
# A token whose key the cached certificates don't hold, or a fetch that
# failed while the cached certificates may still be used, fetches them again
# at most this often.
REFETCH_INTERVAL_SECONDS = 60
# How long past their expiry the cached certificates are still used while
# Google's endpoint can't be reached.
CERTIFICATE_GRACE_SECONDS = 3600
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
SERVING_ENVIRONMENT = {
    "COLLECTOR_ORRERY_BUCKET": BUCKET,
    "COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT": READER,
}


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# A producing run's graph declaration, which a consuming build copies beside
# its schema as `upstream-<sha256>.json` (Microcosm's save_graph_schema).
UPSTREAM_CONTENT = b'{"producing":"graph declaration"}'
UPSTREAM_SHA256 = sha256_hex(UPSTREAM_CONTENT)
UPSTREAM_NAME = f"upstream-{UPSTREAM_SHA256}.json"


def access() -> ModuleType:
    assert importlib.util.find_spec(ACCESS_MODULE) is not None
    return importlib.import_module(ACCESS_MODULE)


def serving() -> ModuleType:
    assert importlib.util.find_spec(SERVING_MODULE) is not None
    return importlib.import_module(SERVING_MODULE)


# Keys and identity tokens.


@functools.cache
def google_signing_key() -> rsa.RSAPrivateKey:
    """The key the tests' stand-in for Google signs identity tokens with."""

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@functools.cache
def impostor_signing_key() -> rsa.RSAPrivateKey:
    """A key Google's certificates don't hold."""

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@functools.cache
def rotated_signing_key() -> rsa.RSAPrivateKey:
    """The key the stand-in for Google signs with after it rotates its keys."""

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@functools.cache
def google_certificate() -> str:
    """The signing key's certificate, in PEM, as Google publishes its own."""

    return self_signed_certificate(google_signing_key(), "google-test-signing-key")


@functools.cache
def rotated_certificate() -> str:
    """The rotated signing key's certificate, in PEM."""

    return self_signed_certificate(rotated_signing_key(), "google-test-rotated-key")


def self_signed_certificate(signing_key: rsa.RSAPrivateKey, common_name: str) -> str:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(signing_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2036, 1, 1, tzinfo=UTC))
        .sign(signing_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


# Marks a claim that identity_claims leaves out.
LEFT_OUT = object()


def identity_claims(**claim_changes: Any) -> dict[str, Any]:
    """The claims of an identity token that IAM Credentials' generateIdToken
    issues for the reader account with ``includeEmail``, with
    ``claim_changes`` applied; ``LEFT_OUT`` removes a claim."""

    issued_at = int(time.time())
    claims: dict[str, Any] = {
        "iss": GOOGLE_ISSUER,
        "aud": AUDIENCE,
        "azp": READER_ACCOUNT_ID,
        "sub": READER_ACCOUNT_ID,
        "email": READER,
        "email_verified": True,
        "iat": issued_at,
        "exp": issued_at + 3600,
    }
    claims.update(claim_changes)
    return {name: value for name, value in claims.items() if value is not LEFT_OUT}


def identity_token(
    *,
    signing_key: rsa.RSAPrivateKey | None = None,
    key_id: str = KEY_ID,
    **claim_changes: Any,
) -> str:
    return jwt.encode(
        identity_claims(**claim_changes),
        signing_key or google_signing_key(),
        algorithm="RS256",
        headers={"kid": key_id},
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class CertificateEndpoint:
    """Stands in for Google's certificate endpoint and counts its fetches.

    ``published`` is what it answers. After ``hold_fetches``, each fetch
    waits until ``release_fetches``, as a slow endpoint would, and
    ``fetch_started`` is set once one is waiting.
    """

    def __init__(self) -> None:
        self.fetches = 0
        self.available = True
        self.lifetime_seconds = 3600.0
        self.published = {KEY_ID: google_certificate()}
        self.fetch_started = threading.Event()
        self._released = threading.Event()
        self._released.set()

    def hold_fetches(self) -> None:
        self.fetch_started.clear()
        self._released.clear()

    def release_fetches(self) -> None:
        self._released.set()

    def __call__(self) -> Any:
        self.fetches += 1
        self.fetch_started.set()
        if not self._released.wait(timeout=10):
            raise AssertionError("A held certificate fetch was never released.")
        if not self.available:
            raise access().GoogleCertificatesUnavailableError(
                "Google's certificates could not be fetched."
            )
        return access().FetchedCertificates(
            certificates=dict(self.published),
            lifetime_seconds=self.lifetime_seconds,
        )


class MonotonicClock:
    """A monotonic clock, in seconds, that the tests move forward."""

    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


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

    def get_blob(self, name: str) -> StandInBlob | None:
        self._client.check_available()
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

    ``reads`` records each read of an object's bytes. ``after_lookup`` and
    ``after_read`` let a test change the bucket after an object is looked up
    or after a read reaches its end.
    """

    def __init__(self, objects: dict[str, Any] | None = None) -> None:
        self.objects: dict[str, Any] = {} if objects is None else objects
        self.bucket_names: list[str] = []
        self.reads: list[ObjectRead] = []
        self.available = True
        self.after_lookup: Callable[[str], None] | None = None
        self.after_read: Callable[[ObjectRead], None] | None = None
        self._generation = max(
            (stored.generation for stored in self.objects.values()), default=0
        )

    def bucket(self, name: str) -> StandInBucket:
        self.bucket_names.append(name)
        return StandInBucket(self)

    def list_blobs(self, bucket_name: str, prefix: str = "") -> list[StandInBlob]:
        self.check_available()
        return [
            StandInBlob(self, name, stored=stored)
            for name, stored in sorted(self.objects.items())
            if name.startswith(prefix)
        ]

    def check_available(self) -> None:
        if not self.available:
            raise ServiceUnavailable("Storage is unavailable.")

    def stored_for_read(self, name: str, generation: int | None) -> Any:
        """The object a read of ``generation`` gets, refusing as Cloud
        Storage does when that generation is no longer stored."""

        self.check_available()
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
        self, *, run_id: str | None = None, build_id: str | None = None
    ) -> list[Any]:
        return [
            self._recorded(key, record)
            for key, record in self.graphs.records.items()
            if (run_id is None or record.run_id == run_id)
            and (build_id is None or record.build_id == build_id)
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
    *, run_id: str, build_id: str, licensed_data: bool, files: list[dict[str, Any]]
) -> SubmissionRequest:
    return SubmissionRequest.model_validate(
        {
            "contract_version": 1,
            "kind": "orrery-inputs",
            "licensed_data": licensed_data,
            "provenance": {
                "run_id": run_id,
                "build_id": build_id,
                "country_code": "UK",
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
) -> OrreryGraphRecord:
    return OrreryGraphRecord(
        submission_id=submission_id,
        processing_round=1,
        run_id=run_id,
        build_id=build_id,
        country_code="UK",
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


# The service, with the stand-ins above.


class UnusedAuthenticator:
    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        raise AssertionError("These tests exchange no Hugging Face token.")


def collector_settings(**changes: Any) -> CollectorSettings:
    return CollectorSettings(
        jwt_secret=JWT_SECRET, read_token="dashboard-read-token", **changes
    )


def serving_settings() -> Any:
    return serving().OrreryServingSettings(bucket=BUCKET, reader_service_account=READER)


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
    certificate_endpoint: CertificateEndpoint
    certificate_clock: MonotonicClock
    clock: SteppingClock
    published: list[PublishedDocument] = field(default_factory=list)

    def get(
        self, path: str, *, headers: dict[str, str] | None = None, **request: Any
    ) -> httpx.Response:
        """GET ``path``, by default as the reader account."""

        if headers is None:
            headers = bearer(identity_token())
        return self.client.get(path, headers=headers, **request)

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
    ) -> str:
        record, _ = self.submissions.create_submission(
            submission_request(
                run_id=run_id,
                build_id=build_id,
                licensed_data=licensed_data,
                files=files,
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
    maintenance_mode: bool = False,
    raise_server_exceptions: bool = True,
) -> Serving:
    """The service over the stand-ins. ``catalogue``, when given, wraps the
    in-memory catalogue the endpoints read."""

    clock = SteppingClock()
    if submissions is None:
        submissions = FakeSubmissionStore(clock=clock)
    if graphs is None:
        graphs = FakeOrreryGraphStore(clock=clock)
    if storage is None:
        storage = StandInStorageClient()
    in_memory_catalogue = InMemoryGraphCatalogue(graphs, submissions)
    certificate_endpoint = CertificateEndpoint()
    certificate_clock = MonotonicClock()
    services = serving().OrreryServingServices(
        catalogue=in_memory_catalogue
        if catalogue is None
        else catalogue(in_memory_catalogue),
        objects=graph_reader(storage),
        verifier=access().GoogleIdentityTokenVerifier(
            certificates=access().GoogleCertificates(
                fetch=certificate_endpoint, clock=certificate_clock
            )
        ),
    )
    client = TestClient(
        create_app(
            settings=collector_settings(
                orrery_serving=serving_settings(), maintenance_mode=maintenance_mode
            ),
            store=FakeTelemetryStore(),
            huggingface_authenticator=UnusedAuthenticator(),
            orrery_services=services,
        ),
        raise_server_exceptions=raise_server_exceptions,
    )
    return Serving(
        client=client,
        submissions=submissions,
        graphs=graphs,
        storage=storage,
        certificate_endpoint=certificate_endpoint,
        certificate_clock=certificate_clock,
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


# The caller check.


def test_microcosm_runs_requests_tokens_for_the_documented_audience() -> None:
    assert access().ORRERY_GRAPHS_AUDIENCE == AUDIENCE


def test_an_identity_token_of_the_reader_account_passes(service: Serving) -> None:
    published = service.publish()

    listing = service.get(LISTING, params={"run_id": CONSUMING_RUN})
    document = service.get(published.path())

    assert listing.status_code == 200, listing.text
    assert [entry["submission_id"] for entry in listing.json()["documents"]] == [
        published.submission_id
    ]
    assert document.status_code == 200, document.text
    assert_private(listing)
    assert_private(document)


# Each request is refused with 401, and the code the refusal carries.
UNAUTHENTICATED_REQUESTS: dict[str, tuple[Callable[[], dict[str, str]], str]] = {
    "no token": (lambda: {}, "identity_token_required"),
    "another scheme": (
        lambda: {"Authorization": f"Basic {identity_token()}"},
        "identity_token_required",
    ),
    "an empty bearer token": (
        lambda: {"Authorization": "Bearer "},
        "identity_token_required",
    ),
    "a malformed token": (lambda: bearer("not-a-token"), "identity_token_invalid"),
    "a token whose payload is not JSON": (
        # The header `{}` and the payload `not-json`, unsigned.
        lambda: bearer("e30.bm90LWpzb24.c2ln"),
        "identity_token_invalid",
    ),
    "a bad signature": (
        lambda: bearer(identity_token(signing_key=impostor_signing_key())),
        "identity_token_invalid",
    ),
    "a key Google does not publish": (
        lambda: bearer(identity_token(key_id="google-test-key-2")),
        "identity_token_invalid",
    ),
    "another algorithm": (
        lambda: bearer(
            jwt.encode(
                identity_claims(),
                "a-shared-secret-that-is-long-enough-for-hs256-" + "x" * 32,
                algorithm="HS256",
                headers={"kid": KEY_ID},
            )
        ),
        "identity_token_invalid",
    ),
    "an expired token": (
        lambda: bearer(
            identity_token(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)
        ),
        "identity_token_invalid",
    ),
    "a token issued for later": (
        lambda: bearer(
            identity_token(iat=int(time.time()) + 3600, exp=int(time.time()) + 7200)
        ),
        "identity_token_invalid",
    ),
    "a token without an expiry": (
        lambda: bearer(identity_token(exp=LEFT_OUT)),
        "identity_token_invalid",
    ),
    "a wrong audience": (
        lambda: bearer(identity_token(aud="https://collector.example.test")),
        "identity_token_invalid",
    ),
    "a wrong issuer": (
        lambda: bearer(identity_token(iss="https://issuer.example.test")),
        "identity_token_invalid",
    ),
}


@pytest.mark.parametrize("case", sorted(UNAUTHENTICATED_REQUESTS))
@pytest.mark.parametrize("endpoint", ["listing", "file"])
def test_requests_without_a_valid_google_identity_token_are_refused_with_401(
    service: Serving, case: str, endpoint: str
) -> None:
    published = service.publish()
    headers, code = UNAUTHENTICATED_REQUESTS[case]
    path = (
        f"{LISTING}?run_id={CONSUMING_RUN}"
        if endpoint == "listing"
        else published.path()
    )

    response = service.get(path, headers=headers())

    assert_problem(response, 401, code)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert service.storage.reads == []


@pytest.mark.parametrize(
    "claim_changes",
    [
        {"email": ANOTHER_ACCOUNT},
        {"email": READER.upper()},
        {"email_verified": False},
        {"email_verified": "true"},
        # A token generateIdToken issued without includeEmail.
        {"email": LEFT_OUT, "email_verified": LEFT_OUT},
    ],
    ids=[
        "another account",
        "the reader account in capitals",
        "an unverified address",
        "a verification flag that is not a boolean",
        "no address",
    ],
)
def test_a_valid_google_token_of_any_other_account_is_refused_with_403(
    service: Serving, claim_changes: dict[str, Any]
) -> None:
    published = service.publish()

    for path in (f"{LISTING}?run_id={CONSUMING_RUN}", published.path()):
        response = service.get(path, headers=bearer(identity_token(**claim_changes)))

        assert_problem(response, 403, "identity_not_allowed")
    assert service.storage.reads == []


def test_without_google_s_certificates_no_token_is_accepted_and_the_answer_is_503(
    service: Serving,
) -> None:
    service.publish()
    service.certificate_endpoint.available = False

    response = service.get(LISTING, params={"run_id": CONSUMING_RUN})

    assert_problem(response, 503, "identity_certificates_unavailable")
    assert int(response.headers["Retry-After"]) > 0
    assert "WWW-Authenticate" not in response.headers


def test_google_s_certificates_are_fetched_once_for_as_long_as_they_last(
    service: Serving,
) -> None:
    service.publish()

    for _ in range(3):
        service.listed()
    assert service.certificate_endpoint.fetches == 1

    service.certificate_clock.advance(service.certificate_endpoint.lifetime_seconds - 1)
    service.listed()
    assert service.certificate_endpoint.fetches == 1

    service.certificate_clock.advance(1)
    service.listed()
    assert service.certificate_endpoint.fetches == 2


def rotated_key_token() -> dict[str, str]:
    """The reader's token, signed with a key Google published after the
    collector fetched its certificates."""

    return bearer(
        identity_token(signing_key=rotated_signing_key(), key_id=ROTATED_KEY_ID)
    )


def test_a_token_signed_with_a_new_key_fetches_the_certificates_again(
    service: Serving,
) -> None:
    service.publish()
    service.listed()
    service.certificate_endpoint.published = {
        KEY_ID: google_certificate(),
        ROTATED_KEY_ID: rotated_certificate(),
    }
    service.certificate_clock.advance(REFETCH_INTERVAL_SECONDS)

    response = service.get(
        LISTING, params={"run_id": CONSUMING_RUN}, headers=rotated_key_token()
    )

    assert response.status_code == 200, response.text
    assert service.certificate_endpoint.fetches == 2
    # The certificates fetched again serve the old key too, without a fetch.
    service.listed()
    assert service.certificate_endpoint.fetches == 2


def test_an_unknown_key_fetches_the_certificates_again_at_most_once_a_minute(
    service: Serving,
) -> None:
    service.publish()
    service.listed()

    def refused_with_fetches() -> int:
        response = service.get(
            LISTING, params={"run_id": CONSUMING_RUN}, headers=rotated_key_token()
        )
        assert_problem(response, 401, "identity_token_invalid")
        return service.certificate_endpoint.fetches

    # Within a minute of the fetch, the token is refused without another.
    assert [refused_with_fetches() for _ in range(3)] == [1, 1, 1]
    service.certificate_clock.advance(REFETCH_INTERVAL_SECONDS)
    # Google still doesn't publish the key: one fetch, then none for a minute.
    assert [refused_with_fetches() for _ in range(3)] == [2, 2, 2]
    service.certificate_clock.advance(REFETCH_INTERVAL_SECONDS - 1)
    assert refused_with_fetches() == 2
    service.certificate_clock.advance(1)
    assert refused_with_fetches() == 3


def test_a_failed_fetch_keeps_the_certificates_for_an_hour_past_their_expiry(
    service: Serving, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    service.publish()
    service.listed()
    endpoint = service.certificate_endpoint
    endpoint.available = False

    service.certificate_clock.advance(endpoint.lifetime_seconds)
    service.listed()
    assert endpoint.fetches == 2
    assert [
        record.levelno for record in caplog.records if record.name == ACCESS_MODULE
    ] == [logging.WARNING]

    # The failed fetch is tried again at most once a minute.
    service.certificate_clock.advance(REFETCH_INTERVAL_SECONDS - 1)
    service.listed()
    assert endpoint.fetches == 2
    service.certificate_clock.advance(1)
    service.listed()
    assert endpoint.fetches == 3

    # Until an hour past the expiry, and no longer.
    service.certificate_clock.advance(
        CERTIFICATE_GRACE_SECONDS - REFETCH_INTERVAL_SECONDS - 1
    )
    service.listed()
    service.certificate_clock.advance(1)
    response = service.get(LISTING, params={"run_id": CONSUMING_RUN})
    assert_problem(response, 503, "identity_certificates_unavailable")

    # Once Google answers again, its new certificates are used.
    endpoint.available = True
    service.listed()


@dataclass
class VerifierUnderTest:
    """A verifier over the stand-in certificate endpoint, and what the
    requests verified in other threads returned."""

    endpoint: CertificateEndpoint = field(default_factory=CertificateEndpoint)
    clock: MonotonicClock = field(default_factory=MonotonicClock)
    outcomes: list[object] = field(default_factory=list)
    verifier: Any = None

    def __post_init__(self) -> None:
        self.verifier = access().GoogleIdentityTokenVerifier(
            certificates=access().GoogleCertificates(
                fetch=self.endpoint, clock=self.clock
            )
        )

    def verify_in_thread(self, token: str) -> threading.Thread:
        def verify() -> None:
            try:
                self.outcomes.append(self.verifier.verify(token)["email"])
            except Exception as error:
                self.outcomes.append(error)

        thread = threading.Thread(target=verify, daemon=True)
        thread.start()
        return thread


def test_requests_the_cached_certificates_can_check_do_not_wait_for_a_fetch() -> None:
    under_test = VerifierUnderTest()
    token = identity_token()
    under_test.verifier.verify(token)
    under_test.clock.advance(under_test.endpoint.lifetime_seconds)
    under_test.endpoint.hold_fetches()

    fetching = under_test.verify_in_thread(token)
    try:
        assert under_test.endpoint.fetch_started.wait(timeout=5)
        # The certificates have expired but are within their grace period,
        # so this request uses them instead of waiting for the slow fetch.
        not_waiting = under_test.verify_in_thread(token)
        not_waiting.join(timeout=2)
        assert not not_waiting.is_alive()
        assert under_test.outcomes == [READER]
    finally:
        under_test.endpoint.release_fetches()
        fetching.join(timeout=5)

    assert under_test.outcomes == [READER, READER]
    assert under_test.endpoint.fetches == 2


def test_requests_without_usable_certificates_wait_for_one_fetch() -> None:
    under_test = VerifierUnderTest()
    token = identity_token()
    under_test.endpoint.hold_fetches()

    fetching = under_test.verify_in_thread(token)
    assert under_test.endpoint.fetch_started.wait(timeout=5)
    waiting = under_test.verify_in_thread(token)
    try:
        # No certificates are cached yet, so it waits for the fetch running.
        waiting.join(timeout=0.5)
        assert waiting.is_alive()
    finally:
        under_test.endpoint.release_fetches()
        fetching.join(timeout=5)
        waiting.join(timeout=5)

    assert under_test.outcomes == [READER, READER]
    assert under_test.endpoint.fetches == 1


def test_the_token_never_appears_in_answers_or_logs(
    service: Serving, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    published = service.publish()
    tokens = [
        identity_token(),
        identity_token(email=ANOTHER_ACCOUNT),
        identity_token(aud="https://collector.example.test"),
        identity_token(signing_key=impostor_signing_key()),
        "not-a-token-" + "q" * 40,
    ]
    answers = []

    for token in tokens:
        for path in (f"{LISTING}?run_id={CONSUMING_RUN}", published.path()):
            answers.append((token, service.get(path, headers=bearer(token))))
    # Past the certificates' lifetime, with Google's endpoint down, the
    # collector warns and keeps using them; an hour later it answers 503.
    service.certificate_endpoint.available = False
    for advance_seconds in (
        service.certificate_endpoint.lifetime_seconds,
        CERTIFICATE_GRACE_SECONDS,
    ):
        service.certificate_clock.advance(advance_seconds)
        answers.append(
            (tokens[0], service.get(published.path(), headers=bearer(tokens[0])))
        )

    assert {answer.status_code for _, answer in answers} == {200, 401, 403, 503}
    assert [answer.status_code for _, answer in answers[-2:]] == [200, 503]
    for token, answer in answers:
        assert token not in answer.text
        assert all(token not in value for value in answer.headers.values())
    # The collector's own log records were captured, so their text was checked.
    assert any(
        record.name == ACCESS_MODULE and record.levelno == logging.WARNING
        for record in caplog.records
    )
    for token in tokens:
        assert token not in caplog.text


@pytest.mark.parametrize(
    ("headers", "lifetime_seconds"),
    [
        ({"Cache-Control": "public, max-age=21600, must-revalidate"}, 21600),
        ({"Cache-Control": "public, max-age=21600", "Age": "600"}, 21000),
        ({"cache-control": "max-age=60"}, 60),
        ({"Cache-Control": "max-age=60", "Age": "90"}, 0),
        ({"Cache-Control": "no-store"}, 0),
        ({"Cache-Control": "no-cache, max-age=60"}, 0),
        ({"Cache-Control": "max-age=soon"}, 0),
        ({}, 0),
    ],
)
def test_certificates_are_kept_only_as_long_as_google_s_cache_control_allows(
    headers: dict[str, str], lifetime_seconds: float
) -> None:
    assert access().certificate_lifetime_seconds(headers) == lifetime_seconds


@dataclass
class CertificateResponse:
    """What google.auth.transport.requests.Request returns."""

    status: int
    data: bytes
    headers: dict[str, str] = field(default_factory=dict)


def test_google_s_certificates_are_fetched_from_its_oauth2_certificate_endpoint() -> (
    None
):
    requests_made: list[dict[str, Any]] = []

    def request(**arguments: Any) -> CertificateResponse:
        requests_made.append(arguments)
        return CertificateResponse(
            200,
            json.dumps({KEY_ID: google_certificate()}).encode(),
            {"Cache-Control": "public, max-age=21600, must-revalidate"},
        )

    fetched = access().fetch_google_certificates(request)

    assert dict(fetched.certificates) == {KEY_ID: google_certificate()}
    assert fetched.lifetime_seconds == 21600
    assert [(made["url"], made["method"]) for made in requests_made] == [
        (GOOGLE_CERTIFICATES_URL, "GET")
    ]
    assert requests_made[0]["timeout"] > 0


def unreachable(**arguments: Any) -> CertificateResponse:
    raise TransportError("The connection was refused.")


@pytest.mark.parametrize(
    "request_certificates",
    [
        unreachable,
        lambda **arguments: CertificateResponse(503, b"{}"),
        lambda **arguments: CertificateResponse(200, b"not json"),
        lambda **arguments: CertificateResponse(200, b"[]"),
        lambda **arguments: CertificateResponse(200, b"{}"),
        lambda **arguments: CertificateResponse(200, json.dumps({KEY_ID: 5}).encode()),
    ],
    ids=[
        "unreachable",
        "an error status",
        "not JSON",
        "not an object",
        "no certificates",
        "a certificate that is not text",
    ],
)
def test_a_certificate_fetch_that_fails_makes_the_certificates_unavailable(
    request_certificates: Callable[..., CertificateResponse],
) -> None:
    with pytest.raises(access().GoogleCertificatesUnavailableError):
        access().fetch_google_certificates(request_certificates)


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


@pytest.mark.parametrize("endpoint", ["listing", "file"])
def test_an_unavailable_bucket_answers_503(service: Serving, endpoint: str) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})
    service.storage.available = False
    path = (
        f"{LISTING}?run_id={CONSUMING_RUN}"
        if endpoint == "listing"
        else published.path()
    )

    response = service.get(path)

    assert_problem(response, 503, "orrery_storage_unavailable")
    assert int(response.headers["Retry-After"]) > 0


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


# Every answer of these endpoints is marked private and not to be stored.


def test_every_answer_under_the_orrery_graph_path_is_marked_not_to_be_stored(
    service: Serving,
) -> None:
    published = service.publish(upstream={UPSTREAM_NAME: UPSTREAM_CONTENT})

    answers = [
        service.get(LISTING, params={"run_id": CONSUMING_RUN}),
        service.get(published.path()),
        service.get(published.path(UPSTREAM_NAME)),
        service.get(published.path(), headers={}),
        service.get(published.path(), headers=bearer(identity_token(email=READER[1:]))),
        service.get(LISTING),
        service.get(published.path("graph.schema.json")),
        service.get(f"{LISTING}/unknown"),
        service.client.post(LISTING, headers=bearer(identity_token())),
    ]

    assert [answer.status_code for answer in answers] == [
        200,
        200,
        200,
        401,
        403,
        422,
        404,
        404,
        405,
    ]
    for answer in answers:
        assert_private(answer)
    assert "Cache-Control" not in service.client.get("/health").headers


def test_maintenance_mode_s_answers_under_the_orrery_graph_path_are_not_to_be_stored() -> (
    None
):
    harness = serving_harness(maintenance_mode=True)
    published = harness.publish()

    answers = [
        harness.get(LISTING, params={"run_id": CONSUMING_RUN}),
        harness.get(published.path()),
        harness.get(published.path(), headers={}),
    ]

    assert [answer.status_code for answer in answers] == [503, 503, 503]
    for answer in answers:
        assert_private(answer)
    assert harness.storage.reads == []
    assert "Cache-Control" not in harness.client.get("/health").headers


# Settings.


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in [
        *SERVING_ENVIRONMENT,
        "COLLECTOR_INTAKE_ENABLED",
        "TELEMETRY_JWT_SIGNING_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEMETRY_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("TELEMETRY_READ_TOKEN", "y" * 24)
    return monkeypatch


def test_without_either_setting_the_endpoints_are_not_enabled(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    assert CollectorSettings.from_environment().orrery_serving is None
    application = create_app(
        settings=collector_settings(),
        store=FakeTelemetryStore(),
        huggingface_authenticator=UnusedAuthenticator(),
    )
    client = TestClient(application)
    document_path = f"{LISTING}/sub-{'0' * 32}/{'0' * 64}/{GRAPH_FILE_NAME}"

    for response in (
        client.get(LISTING, params={"run_id": CONSUMING_RUN}),
        client.get(document_path, headers=bearer(identity_token())),
    ):
        assert_problem(response, 404, "orrery_graphs_not_enabled")
    assert not any(path.startswith(LISTING) for path in application.openapi()["paths"])


@pytest.mark.parametrize(
    ("present_name", "missing_name"),
    [
        ("COLLECTOR_ORRERY_BUCKET", "COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT"),
        ("COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT", "COLLECTOR_ORRERY_BUCKET"),
    ],
)
def test_the_service_refuses_to_start_with_only_one_setting(
    clean_environment: pytest.MonkeyPatch, present_name: str, missing_name: str
) -> None:
    clean_environment.setenv(present_name, SERVING_ENVIRONMENT[present_name])

    with pytest.raises(RuntimeError, match=missing_name):
        CollectorSettings.from_environment()


def test_both_settings_switch_serving_on(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    for name, value in SERVING_ENVIRONMENT.items():
        clean_environment.setenv(name, value)

    assert CollectorSettings.from_environment().orrery_serving == serving_settings()


@pytest.mark.parametrize(
    ("name", "malformed_value"),
    [
        ("COLLECTOR_ORRERY_BUCKET", "gs://collector-orrery-test"),
        ("COLLECTOR_ORRERY_BUCKET", "Collector-Orrery"),
        ("COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT", "microcosm-runs-reader"),
        (
            "COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT",
            "123456789-compute@developer.gserviceaccount.com",
        ),
        ("COLLECTOR_ORRERY_READER_SERVICE_ACCOUNT", "reader@example.invalid"),
    ],
)
def test_a_malformed_setting_stops_the_service(
    clean_environment: pytest.MonkeyPatch, name: str, malformed_value: str
) -> None:
    for setting_name, value in SERVING_ENVIRONMENT.items():
        clean_environment.setenv(setting_name, value)
    clean_environment.setenv(name, malformed_value)

    with pytest.raises(RuntimeError, match=name):
        CollectorSettings.from_environment()


@pytest.mark.parametrize(
    "bucket",
    [BUCKET, "gs://collector-orrery", "Collector-Orrery", "ab", "a" * 63, "a" * 64],
)
def test_the_service_checks_the_bucket_name_as_the_worker_does(bucket: str) -> None:
    try:
        worker_accepts = orrery_bucket({"COLLECTOR_ORRERY_BUCKET": bucket}) == bucket
    except RuntimeError:
        worker_accepts = False
    try:
        settings = serving().OrreryServingSettings.from_environment(
            {**SERVING_ENVIRONMENT, "COLLECTOR_ORRERY_BUCKET": bucket}
        )
        service_accepts = settings is not None and settings.bucket == bucket
    except RuntimeError:
        service_accepts = False

    assert service_accepts == worker_accepts


class StandInGraphReader:
    def __init__(self, **settings: Any) -> None:
        self.settings = settings


@pytest.mark.parametrize("enabled", [True, False])
def test_the_deployed_app_serves_orrery_graph_files_only_with_both_settings(
    clean_environment: pytest.MonkeyPatch, enabled: bool
) -> None:
    from telemetry_collector.app import create_app_from_environment

    clean_environment.setattr(
        google_cloud, "GoogleCloudStorageOrreryGraphReader", StandInGraphReader
    )
    clean_environment.setenv(
        "DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none"
    )
    if enabled:
        for name, value in SERVING_ENVIRONMENT.items():
            clean_environment.setenv(name, value)

    paths = create_app_from_environment().openapi()["paths"]

    assert (LISTING in paths) is enabled
    assert (FILE_ROUTE in paths) is enabled


def test_switching_serving_on_without_its_services_is_refused() -> None:
    with pytest.raises(ValueError, match="Orrery"):
        create_app(
            settings=collector_settings(orrery_serving=serving_settings()),
            store=FakeTelemetryStore(),
            huggingface_authenticator=UnusedAuthenticator(),
        )


def test_the_read_api_document_leaves_out_the_orrery_graph_routes(
    service: Serving,
) -> None:
    without_serving = create_app(
        settings=collector_settings(),
        store=FakeTelemetryStore(),
        huggingface_authenticator=UnusedAuthenticator(),
    )

    assert openapi_document(service.client.app) == openapi_document(without_serving)


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
    ) -> str:
        record, _ = self.submissions.create_submission(
            submission_request(
                run_id=run_id,
                build_id=build_id,
                licensed_data=licensed_data,
                files=files,
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
    ) -> None:
        assert self.graphs.record_graph(
            graph_record(
                submission_id,
                document_sha256,
                run_id=run_id,
                build_id=build_id,
                visibility=visibility,
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
