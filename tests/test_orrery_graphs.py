"""Orrery graph files are built, checked and published from orrery-inputs submissions.

The handler projects a submission's schema and execution overlay with the
collector's copy of Microcosm's projection, removes every link the published
document cannot resolve, validates the document with the real Orrery
validator (CI installs it with `npm ci` first), writes it create-only to the
private Orrery bucket in a folder named by its SHA-256, and records it. These
tests run the handler through the worker framework, with in-memory intake
storage, a stand-in for the Google Cloud Storage client and an in-memory
record store, so each outcome ends as the framework records it: succeeded,
rejected, or returned for another attempt.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from google.api_core.exceptions import PreconditionFailed

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import create_database_engine
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.orrery.projection import orrery_json_from_schema
from telemetry_collector.orrery.projection.orrery import (
    _revision as projection_revision,
)
from telemetry_collector.orrery.projection.presentation import presentation_id
from telemetry_collector.orrery.projection.provenance import MICROCOSM_COMMIT
from telemetry_collector.orrery.validation import (
    OrreryValidatorError,
    validate_orrery_document,
)
from telemetry_collector.submission_store import PostgresSubmissionStore
from telemetry_collector.submissions import SubmissionRequest, intake_object_name
from telemetry_collector.worker import DEFAULT_HANDLERS, JobOutcome, run_job
from tests.fakes import FakeIntakeObjectStore, FakeOrreryGraphStore, FakeSubmissionStore
from tests.test_submission_store import SteppingClock
from tests.test_submissions import orrery_submission

GRAPHS_MODULE = "telemetry_collector.orrery_graphs"
HANDLERS_MODULE = "telemetry_collector.handlers"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "orrery_projection"
# The fixture whose overlay records evidence references, and one whose
# projected document has no link at all.
EVIDENCE_CASE = "toy-recorded-execution"
LINKLESS_CASE = "toy-failed-check"
SCHEMA = "graph.schema.json"
OVERLAY = "graph.execution.json"
GRAPH = "graph.orrery.json"
ORRERY_VERSION = "0.6.2"
COLLECTOR_VERSION = "9.8.7"
BUCKET = "collector-orrery-test"
PRINCIPAL = HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",))
# orrery_submission()'s provenance.
RUN_ID = "uk-run-1"
BUILD_ID = "uk-frs-calibration-attempt-1"
# Microcosm's command reads at most this many bytes of an input
# (orrery._MAX_INPUT_BYTES at MICROCOSM_COMMIT).
MICROCOSM_INPUT_BYTE_LIMIT = 32 * 1024 * 1024
# Deeper than Python's JSON decoder can parse: it raises RecursionError.
TOO_DEEPLY_NESTED = b"[" * 100_000 + b"]" * 100_000

UPSTREAM_CONTENT = b'{"spine":true}'
UPSTREAM_SHA256 = hashlib.sha256(UPSTREAM_CONTENT).hexdigest()
UPSTREAM_NAME = f"upstream-{UPSTREAM_SHA256}.json"
UNLINKED_CONTENT = b'{"spine":"unlinked"}'
UNLINKED_NAME = f"upstream-{hashlib.sha256(UNLINKED_CONTENT).hexdigest()}.json"
NOT_SUBMITTED_SHA256 = "f" * 64
NOT_SUBMITTED_NAME = f"upstream-{NOT_SUBMITTED_SHA256}.json"
EVIDENCE_NAME = "evidence-numerical-binding.json"
# A host that only evidence links in these tests name.
EVIDENCE_HOST = "evidence.example.org"
# The evidence fixture's source link, which is not an evidence reference.
SOURCE_LINK = "https://example.org/survey"
UPSTREAM_REFERENCES = [
    {
        "label": "Producing graph declaration",
        "url": UPSTREAM_NAME,
        "sha256": UPSTREAM_SHA256,
    },
    {
        "label": "Producing graph nodes",
        "url": f"{UPSTREAM_NAME}#nodes",
        "sha256": UPSTREAM_SHA256,
    },
    {
        "label": "Producing run manifest",
        "url": NOT_SUBMITTED_NAME,
        "sha256": NOT_SUBMITTED_SHA256,
    },
    {
        "label": "Published method",
        "url": "https://example.org/method",
        "sha256": "a" * 64,
    },
]


def graphs() -> ModuleType:
    assert importlib.util.find_spec(GRAPHS_MODULE) is not None
    return importlib.import_module(GRAPHS_MODULE)


def registry() -> ModuleType:
    assert importlib.util.find_spec(HANDLERS_MODULE) is not None
    return importlib.import_module(HANDLERS_MODULE)


def fixture_bytes(case: str, name: str) -> bytes:
    return (FIXTURE_ROOT / case / name).read_bytes()


def fixture_json(case: str, name: str) -> Any:
    return json.loads(fixture_bytes(case, name))


def json_bytes(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def serialised(document: object) -> bytes:
    """A document as the projection serialises it."""

    text = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


Inputs = dict[str, tuple[str, bytes]]


def fixture_inputs(case: str = EVIDENCE_CASE) -> Inputs:
    """A fixture's schema and overlay, as a build submits them."""

    return {
        SCHEMA: ("schema", fixture_bytes(case, SCHEMA)),
        OVERLAY: ("execution_overlay", fixture_bytes(case, OVERLAY)),
    }


def schema_with_upstream_references(references: list[dict[str, str]]) -> bytes:
    """The evidence fixture's schema with a checkpoint boundary naming upstream
    files, as Microcosm's save_graph_schema writes one (its own test,
    test_save_graph_schema_copies_upstream_bytes_and_relinks, does the same)."""

    schema = fixture_json(EVIDENCE_CASE, SCHEMA)
    contract = schema["extensions"]["microcosm.presentation"]
    contract["scope"]["boundaries"] = [
        {"operation": "survey", "kind": "checkpoint", "upstream": references}
    ]
    return json_bytes(schema)


def upstream_inputs() -> Inputs:
    return {
        **fixture_inputs(),
        SCHEMA: ("schema", schema_with_upstream_references(UPSTREAM_REFERENCES)),
        UPSTREAM_NAME: ("upstream", UPSTREAM_CONTENT),
        UNLINKED_NAME: ("upstream", UNLINKED_CONTENT),
    }


def with_overlay(inputs: Inputs, overlay: object) -> Inputs:
    return {**inputs, OVERLAY: ("execution_overlay", json_bytes(overlay))}


def overlay_with_binding_link(link: str) -> dict[str, Any]:
    """The evidence fixture's overlay, whose first phase's binding reference
    links to ``link``."""

    overlay = fixture_json(EVIDENCE_CASE, OVERLAY)
    overlay["phases"][0]["references"]["binding"]["url"] = link
    return overlay


def run_artifact(document: Any, phase: Mapping[str, Any], kind: str) -> Any:
    """The artifact the projection builds from one of a phase's references."""

    identity = presentation_id(
        "run-artifact", phase["attempt_id"], phase["phase"], kind
    )
    [artifact] = [
        artifact for artifact in document["artifacts"] if artifact["id"] == identity
    ]
    return artifact


def projected_document(inputs: Inputs) -> Any:
    """The projection's own document from a submission's schema and overlay."""

    projected = orrery_json_from_schema(
        json.loads(inputs[SCHEMA][1]), execution=json.loads(inputs[OVERLAY][1])
    )
    return json.loads(projected)


def links_in(document: object) -> set[str]:
    """Every `url` and `uri` string in a document."""

    found: set[str] = set()
    pending = [document]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            found.update(
                current[key]
                for key in ("url", "uri")
                if isinstance(current.get(key), str)
            )
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return found


def remove_links(document: object, keep: Callable[[str, str], bool]) -> int:
    """Remove each `url` or `uri` that ``keep(key, link)`` refuses; count them."""

    removed = 0
    pending = [document]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key in ("url", "uri"):
                link = current.get(key)
                if isinstance(link, str) and not keep(key, link):
                    del current[key]
                    removed += 1
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return removed


class ReadCountingIntakeStore(FakeIntakeObjectStore):
    """Intake storage that counts each object's reads, in 64 KiB chunks."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: dict[str, int] = {}

    def read_chunks(
        self, object_name: str, generation: str, *, chunk_size: int = 1 << 16
    ) -> Iterator[bytes]:
        self.reads[object_name] = self.reads.get(object_name, 0) + 1
        return super().read_chunks(object_name, generation, chunk_size=chunk_size)


@dataclass
class StoredGraphObject:
    content: bytes
    content_type: str | None
    metadata: dict[str, str]
    generation: int


class StandInGraphBlob:
    """What the bucket stand-in hands out for ``bucket.blob(name)``."""

    def __init__(self, bucket: StandInGraphBucket, name: str) -> None:
        self.bucket = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None

    def upload_from_filename(self, filename: str, **options: Any) -> None:
        self.bucket.uploads.append((self.name, options))
        if options.get("if_generation_match") == 0 and self.name in self.bucket.stored:
            raise PreconditionFailed("The object already exists.")
        self.bucket.store(
            self.name,
            Path(filename).read_bytes(),
            metadata=dict(self.metadata or {}),
            content_type=options.get("content_type"),
        )


class StandInGraphBucket:
    """A bucket in memory, refusing a create-only upload of an existing name."""

    def __init__(self) -> None:
        self.stored: dict[str, StoredGraphObject] = {}
        self.uploads: list[tuple[str, dict[str, Any]]] = []
        self._generation = 0

    def store(
        self,
        name: str,
        content: bytes,
        *,
        metadata: dict[str, str],
        content_type: str | None = "application/json",
    ) -> None:
        self._generation += 1
        self.stored[name] = StoredGraphObject(
            content, content_type, metadata, self._generation
        )

    def blob(self, name: str) -> StandInGraphBlob:
        return StandInGraphBlob(self, name)

    def get_blob(self, name: str) -> StandInGraphBlob | None:
        stored = self.stored.get(name)
        if stored is None:
            return None
        blob = StandInGraphBlob(self, name)
        blob.metadata = dict(stored.metadata)
        return blob


class StandInStorageClient:
    def __init__(self) -> None:
        self.graph_bucket = StandInGraphBucket()
        self.bucket_names: list[str] = []

    def bucket(self, name: str) -> StandInGraphBucket:
        self.bucket_names.append(name)
        return self.graph_bucket


@dataclass
class Publishing:
    """The handler's bucket, through the Google Cloud adapter, and its record."""

    client: StandInStorageClient = field(default_factory=StandInStorageClient)
    records: FakeOrreryGraphStore = field(default_factory=FakeOrreryGraphStore)
    validate: Callable[..., Any] = validate_orrery_document

    @property
    def bucket(self) -> StandInGraphBucket:
        return self.client.graph_bucket

    def handler(self) -> Any:
        google_cloud = importlib.import_module("telemetry_collector.google_cloud")
        return graphs().orrery_graph_handler(
            objects=google_cloud.GoogleCloudStorageOrreryGraphs(
                bucket_name=BUCKET, client=self.client
            ),
            graphs=self.records,
            validate=self.validate,
            collector_version=COLLECTOR_VERSION,
        )

    def published(self, result: Mapping[str, Any]) -> bytes:
        return self.bucket.stored[result["object_prefix"] + GRAPH].content

    def published_document(self, result: Mapping[str, Any]) -> Any:
        return json.loads(self.published(result))


@dataclass
class Accepted:
    submissions: FakeSubmissionStore
    objects: ReadCountingIntakeStore
    submission_id: str
    job_id: str

    def run(self, publishing: Publishing) -> JobOutcome:
        return run_job(
            self.job_id,
            submissions=self.submissions,
            objects=self.objects,
            handlers={**DEFAULT_HANDLERS, "orrery-inputs": publishing.handler()},
            holder="exec-1",
        )

    def record(self) -> Any:
        record = self.submissions.get_submission(self.submission_id)
        assert record is not None
        return record

    def reprocess(self) -> None:
        reprocessed = self.submissions.reprocess_submission(self.submission_id)
        assert reprocessed.job_id is not None
        self.job_id = reprocessed.job_id

    def reads(self, name: str) -> int:
        return self.objects.reads.get(intake_object_name(self.submission_id, name), 0)


def accepted(inputs: Inputs, *, licensed_data: bool = True) -> Accepted:
    submission = orrery_submission(licensed_data=licensed_data)
    submission["files"] = [
        {
            "name": name,
            "role": role,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, (role, content) in inputs.items()
    ]
    submissions = FakeSubmissionStore(clock=SteppingClock())
    objects = ReadCountingIntakeStore()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(submission), PRINCIPAL
    )
    generations = {
        file["name"]: objects.upload(
            intake_object_name(record.submission_id, file["name"]),
            inputs[file["name"]][1],
            sha256=file["sha256"],
        )
        for file in record.files
    }
    acceptance = submissions.accept_submission(record.submission_id, generations)
    assert acceptance.job_id is not None
    submissions.record_dispatch(acceptance.job_id, 1)
    return Accepted(submissions, objects, record.submission_id, acceptance.job_id)


def published_run(
    inputs: Inputs, **submission_options: Any
) -> tuple[Accepted, Publishing, dict[str, Any]]:
    submission = accepted(inputs, **submission_options)
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "succeeded", submission.record().rejection
    return submission, publishing, submission.record().result


def test_evidence_references_keep_their_label_and_digest_but_lose_their_link() -> None:
    _, publishing, result = published_run(fixture_inputs(EVIDENCE_CASE))

    # The projection's document, without every link but the https:// ones:
    # the overlay's evidence references are relative names, and the run
    # artifacts carry them as relative `uri`s.
    expected = fixture_json(EVIDENCE_CASE, GRAPH)
    removed = remove_links(
        expected, keep=lambda key, link: key == "url" and link.startswith("https://")
    )
    published = publishing.published_document(result)
    assert removed > 0
    assert published == expected
    assert result["removed_links"] == removed
    assert links_in(published) == {"https://example.org/survey"}
    for phase in published["metadata"]["execution"]["phases"]:
        for reference in phase["references"].values():
            assert set(reference) == {"label", "sha256"}
    run_artifacts = [
        artifact
        for artifact in published["artifacts"]
        if artifact["id"].startswith('["run-artifact"')
    ]
    assert run_artifacts
    assert all(set(artifact) == {"id", "label", "sha256"} for artifact in run_artifacts)


def test_the_document_is_serialised_exactly_as_the_projection_serialises_it() -> None:
    _, publishing, result = published_run(fixture_inputs(EVIDENCE_CASE))
    _, linkless_publishing, linkless_result = published_run(
        fixture_inputs(LINKLESS_CASE)
    )

    expected = fixture_json(EVIDENCE_CASE, GRAPH)
    remove_links(
        expected, keep=lambda key, link: key == "url" and link.startswith("https://")
    )
    assert publishing.published(result) == serialised(expected)
    # With no link to remove, the document is the projection's bytes.
    projected = fixture_bytes(LINKLESS_CASE, GRAPH)
    assert linkless_publishing.published(linkless_result) == projected
    assert linkless_result["removed_links"] == 0
    assert linkless_result["document_sha256"] == hashlib.sha256(projected).hexdigest()
    assert linkless_result["document_bytes"] == len(projected)


def test_links_to_submitted_upstream_files_stay_and_the_files_are_published() -> None:
    _, publishing, result = published_run(upstream_inputs())

    published = publishing.published_document(result)
    # The links to a submitted upstream file stay, with or without a
    # fragment, as do https:// links; the link to an upstream file that was
    # not submitted goes, and its reference keeps its label and digest.
    assert links_in(published) == {
        UPSTREAM_NAME,
        f"{UPSTREAM_NAME}#nodes",
        "https://example.org/method",
        "https://example.org/survey",
    }
    [boundary] = published["metadata"]["presentation_scope"]["boundaries"]
    assert boundary["upstream"] == [
        UPSTREAM_REFERENCES[0],
        UPSTREAM_REFERENCES[1],
        {"label": "Producing run manifest", "sha256": NOT_SUBMITTED_SHA256},
        UPSTREAM_REFERENCES[3],
    ]
    # Only the linked upstream file is published beside the document.
    prefix = result["object_prefix"]
    assert set(publishing.bucket.stored) == {prefix + GRAPH, prefix + UPSTREAM_NAME}
    upstream_object = publishing.bucket.stored[prefix + UPSTREAM_NAME]
    assert upstream_object.content == UPSTREAM_CONTENT
    assert upstream_object.metadata == {"sha256": UPSTREAM_SHA256}
    assert upstream_object.content_type == "application/json"
    assert result["upstream_files"] == 1


def test_evidence_files_are_never_downloaded_published_or_linked() -> None:
    overlay = fixture_json(EVIDENCE_CASE, OVERLAY)
    # An evidence reference naming a file the build submitted as evidence.
    overlay["phases"][0]["references"]["binding"]["url"] = EVIDENCE_NAME
    inputs = {
        **fixture_inputs(EVIDENCE_CASE),
        OVERLAY: ("execution_overlay", json_bytes(overlay)),
        EVIDENCE_NAME: ("evidence", b'{"binding":"native evidence"}'),
    }

    submission, publishing, result = published_run(inputs)

    published = publishing.published_document(result)
    assert EVIDENCE_NAME not in links_in(published)
    assert not any(name.endswith(EVIDENCE_NAME) for name in publishing.bucket.stored)
    # The worker framework reads every file once to check its SHA-256; the
    # handler downloads the inputs again, but never an evidence file.
    assert submission.reads(EVIDENCE_NAME) == 1
    assert submission.reads(SCHEMA) == 2
    assert submission.reads(OVERLAY) == 2


@pytest.mark.parametrize(
    "link",
    [
        f"http://reader:secret@{EVIDENCE_HOST}/execution.evidence.json",
        f"HTTPS://{EVIDENCE_HOST}/execution.evidence.json",
        f"https://{EVIDENCE_HOST}/execution.evidence.json",
    ],
    ids=["http-with-password", "upper-case-https", "https"],
)
def test_an_evidence_reference_loses_its_link_whatever_its_scheme(link: str) -> None:
    overlay = overlay_with_binding_link(link)
    phase = overlay["phases"][0]
    reference = phase["references"]["binding"]

    _, publishing, result = published_run(
        with_overlay(fixture_inputs(EVIDENCE_CASE), overlay)
    )

    published = publishing.published_document(result)
    without_link = {"label": reference["label"], "sha256": reference["sha256"]}
    # The phase reference, as metadata.execution holds it, and the run
    # artifact the projection builds from it both keep only their label and
    # digest.
    published_phase = published["metadata"]["execution"]["phases"][0]
    assert published_phase["references"]["binding"] == without_link
    artifact = run_artifact(published, phase, "binding")
    assert artifact == {"id": artifact["id"], **without_link}
    assert EVIDENCE_HOST not in publishing.published(result).decode("utf-8")
    # A source's https:// link is not an evidence reference, so it stays.
    assert links_in(published) == {SOURCE_LINK}


def test_an_evidence_link_to_a_submitted_upstream_file_stays() -> None:
    link = f"{UPSTREAM_NAME}#binding"
    overlay = overlay_with_binding_link(link)
    phase = overlay["phases"][0]
    inputs = {
        **with_overlay(fixture_inputs(EVIDENCE_CASE), overlay),
        UPSTREAM_NAME: ("upstream", UPSTREAM_CONTENT),
    }

    _, publishing, result = published_run(inputs)

    published = publishing.published_document(result)
    published_phase = published["metadata"]["execution"]["phases"][0]
    assert published_phase["references"]["binding"]["url"] == link
    assert run_artifact(published, phase, "binding")["uri"] == link
    assert links_in(published) == {link, SOURCE_LINK}
    assert result["upstream_files"] == 1
    stored = publishing.bucket.stored[result["object_prefix"] + UPSTREAM_NAME]
    assert stored.content == UPSTREAM_CONTENT


def test_outside_evidence_references_a_uri_follows_the_url_rule() -> None:
    overlay = fixture_json(EVIDENCE_CASE, OVERLAY)
    # An operation's record is not an evidence reference. The projection
    # copies it into the operation's node, its activity and
    # metadata.execution.
    overlay["phases"][0]["operations"]["survey"]["logs"] = [
        {"label": "upper-case", "uri": "HTTPS://logs.example.org/upper"},
        {"label": "password", "uri": "http://reader:secret@logs.example.org/pw"},
        {"label": "plain http", "url": "http://logs.example.org/plain"},
        {"label": "https", "uri": "https://logs.example.org/kept"},
        {"label": "upstream", "uri": UPSTREAM_NAME},
    ]
    inputs = {
        **with_overlay(fixture_inputs(EVIDENCE_CASE), overlay),
        UPSTREAM_NAME: ("upstream", UPSTREAM_CONTENT),
    }

    _, publishing, result = published_run(inputs)

    published = publishing.published_document(result)
    assert links_in(published) == {
        SOURCE_LINK,
        "https://logs.example.org/kept",
        UPSTREAM_NAME,
    }
    [survey] = [
        node
        for node in published["nodes"]
        if node["id"] == presentation_id("operation", "survey")
    ]
    [record] = [
        record
        for record in survey["data"]["execution_history"]
        if record["phase"] == "numerical"
    ]
    assert record["logs"] == [
        {"label": "upper-case"},
        {"label": "password"},
        {"label": "plain http"},
        {"label": "https", "uri": "https://logs.example.org/kept"},
        {"label": "upstream", "uri": UPSTREAM_NAME},
    ]
    assert result["upstream_files"] == 1


def test_node_and_edge_revisions_describe_the_records_as_published() -> None:
    inputs = upstream_inputs()

    _, publishing, result = published_run(inputs)

    published = publishing.published_document(result)
    for record in [*published["nodes"], *published["edges"]]:
        content = {key: value for key, value in record.items() if key != "revision"}
        assert record["revision"] == projection_revision(content), record["id"]
    # Only the survey operation's node lost a link (to an upstream file that
    # was not submitted), so only its revision differs from the projection's.
    projected = projected_document(inputs)
    projected_revisions = {
        record["id"]: record["revision"]
        for record in [*projected["nodes"], *projected["edges"]]
    }
    changed = {
        record["id"]
        for record in [*published["nodes"], *published["edges"]]
        if record["revision"] != projected_revisions[record["id"]]
    }
    assert changed == {presentation_id("operation", "survey")}
    # The digests of the submitted schema and overlay stay.
    assert published["revision"] == projected["revision"]
    assert (
        published["metadata"]["microcosm"]["schema_sha256"]
        == projected["metadata"]["microcosm"]["schema_sha256"]
    )


def test_a_document_with_nothing_to_remove_has_the_projection_s_bytes() -> None:
    overlay = fixture_json(EVIDENCE_CASE, OVERLAY)
    # Every evidence reference names the submitted upstream file, and every
    # other link is an https:// URL or names that file.
    for phase in overlay["phases"]:
        for kind, reference in phase["references"].items():
            reference["url"] = f"{UPSTREAM_NAME}#{kind}"
    references = [
        UPSTREAM_REFERENCES[0],
        UPSTREAM_REFERENCES[1],
        UPSTREAM_REFERENCES[3],
    ]
    inputs = {
        SCHEMA: ("schema", schema_with_upstream_references(references)),
        OVERLAY: ("execution_overlay", json_bytes(overlay)),
        UPSTREAM_NAME: ("upstream", UPSTREAM_CONTENT),
    }

    _, publishing, result = published_run(inputs)

    projected = orrery_json_from_schema(
        json.loads(inputs[SCHEMA][1]), execution=overlay
    ).encode("utf-8")
    assert result["removed_links"] == 0
    assert publishing.published(result) == projected
    assert result["document_sha256"] == hashlib.sha256(projected).hexdigest()
    assert links_in(json.loads(projected)) >= {
        UPSTREAM_NAME,
        f"{UPSTREAM_NAME}#binding",
        "https://example.org/method",
        SOURCE_LINK,
    }


def test_only_the_upstream_files_the_document_links_to_are_downloaded() -> None:
    submission, _, result = published_run(upstream_inputs())

    assert result["upstream_files"] == 1
    # The worker framework reads every file once to check its SHA-256; the
    # handler downloads only the upstream file the document links to.
    assert submission.reads(UPSTREAM_NAME) == 2
    assert submission.reads(UNLINKED_NAME) == 1


def test_the_published_document_passes_the_pinned_orrery_validator(
    tmp_path: Path,
) -> None:
    _, publishing, result = published_run(upstream_inputs())

    document = tmp_path / GRAPH
    document.write_bytes(publishing.published(result))
    validation = validate_orrery_document(document, [UPSTREAM_NAME])
    assert validation.orrery_version == ORRERY_VERSION
    assert result["orrery_version"] == ORRERY_VERSION


def test_a_document_the_validator_refuses_rejects_the_submission() -> None:
    schema = fixture_json(EVIDENCE_CASE, SCHEMA)
    survey = schema["extensions"]["microcosm.presentation"]["sources"]["survey"]
    # The projection keeps an https:// link with a password; the validator
    # refuses it, and says only where it is.
    survey["references"][0]["url"] = "https://reader:hunter2@example.org/survey"
    submission = accepted(
        {**fixture_inputs(EVIDENCE_CASE), SCHEMA: ("schema", json_bytes(schema))}
    )
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "rejected"
    rejection = submission.record().rejection
    assert rejection["code"] == "orrery_document_refused"
    assert "(invalid_source)" in rejection["message"]
    assert "First issue at $." in rejection["message"]
    assert "hunter2" not in rejection["message"]
    assert len(rejection["message"]) <= 500
    assert publishing.bucket.stored == {}
    assert publishing.records.records == {}


def test_a_validator_that_cannot_run_returns_the_job_for_another_attempt(
    tmp_path: Path,
) -> None:
    submission = accepted(fixture_inputs(EVIDENCE_CASE))
    publishing = Publishing(
        validate=functools.partial(
            validate_orrery_document, node=str(tmp_path / "no-node")
        )
    )

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"
    assert publishing.bucket.stored == {}
    assert publishing.records.records == {}


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (SCHEMA, b'{"protocol": '),
        (OVERLAY, b"\xff\xfe not UTF-8"),
        (SCHEMA, b'{"protocol": 1, "protocol": 2}'),
        (OVERLAY, b'{"graph_sha256": NaN}'),
    ],
    ids=["truncated-schema", "overlay-not-utf-8", "repeated-key", "not-finite"],
)
def test_inputs_that_are_not_strict_json_reject_the_submission(
    name: str, content: bytes
) -> None:
    inputs = fixture_inputs(EVIDENCE_CASE)
    inputs[name] = (inputs[name][0], content)
    submission = accepted(inputs)
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "rejected"
    assert submission.record().rejection["code"] == "orrery_inputs_unusable"
    assert publishing.bucket.stored == {}


@pytest.mark.parametrize(
    ("name", "description"),
    [(SCHEMA, "schema"), (OVERLAY, "execution overlay")],
    ids=["schema", "overlay"],
)
def test_inputs_nested_too_deeply_to_parse_reject_the_submission(
    name: str, description: str
) -> None:
    inputs = fixture_inputs(EVIDENCE_CASE)
    inputs[name] = (inputs[name][0], TOO_DEEPLY_NESTED)
    submission = accepted(inputs)
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "rejected"
    assert submission.record().rejection == {
        "code": "orrery_inputs_unusable",
        "message": f"The {description} nests too deeply.",
    }
    assert publishing.bucket.stored == {}


def schema_padded_to(size: int) -> bytes:
    """The linkless fixture's schema, preceded by enough spaces to be ``size``
    bytes long. JSON allows whitespace before a value, so it parses to the
    same schema."""

    schema = fixture_bytes(LINKLESS_CASE, SCHEMA)
    return b" " * (size - len(schema)) + schema


def test_a_schema_over_microcosm_s_input_byte_limit_rejects_the_submission() -> None:
    padded_schema = schema_padded_to(MICROCOSM_INPUT_BYTE_LIMIT + 1)
    submission = accepted(
        {**fixture_inputs(LINKLESS_CASE), SCHEMA: ("schema", padded_schema)}
    )
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "rejected"
    assert submission.record().rejection == {
        "code": "orrery_inputs_unusable",
        "message": "The schema is larger than 32 MiB.",
    }
    assert publishing.bucket.stored == {}


def test_a_schema_at_microcosm_s_input_byte_limit_is_published() -> None:
    padded_schema = schema_padded_to(MICROCOSM_INPUT_BYTE_LIMIT)

    _, publishing, result = published_run(
        {**fixture_inputs(LINKLESS_CASE), SCHEMA: ("schema", padded_schema)}
    )

    assert publishing.published(result) == fixture_bytes(LINKLESS_CASE, GRAPH)


def schema_whose_sources_are_an_object() -> Inputs:
    schema = fixture_json(EVIDENCE_CASE, SCHEMA)
    [source] = schema["graph"]["sources"]
    schema["graph"]["sources"] = {"survey": source}
    return {**fixture_inputs(EVIDENCE_CASE), SCHEMA: ("schema", json_bytes(schema))}


def overlay_whose_summary_is_an_array() -> Inputs:
    overlay = fixture_json(EVIDENCE_CASE, OVERLAY)
    overlay["summaries"] = [[]]
    return with_overlay(fixture_inputs(EVIDENCE_CASE), overlay)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (schema_whose_sources_are_an_object, "graph.sources must be a JSON array"),
        (
            overlay_whose_summary_is_an_array,
            "list indices must be integers or slices, not str",
        ),
    ],
    ids=["schema-sources-object", "overlay-summary-array"],
)
def test_inputs_that_make_the_projection_raise_type_error_reject_the_submission(
    inputs: Callable[[], Inputs], message: str
) -> None:
    submission = accepted(inputs())
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "rejected"
    assert submission.record().rejection == {
        "code": "orrery_inputs_unusable",
        "message": message,
    }
    assert publishing.bucket.stored == {}


def test_a_key_error_from_the_projection_fails_the_attempt() -> None:
    overlay = fixture_json(EVIDENCE_CASE, OVERLAY)
    # A summary without its key. The projection raises KeyError, which can't
    # tell a malformed overlay from a fault in the copied projection, so the
    # job runs again rather than rejecting the submission.
    overlay["summaries"] = [{}]
    submission = accepted(with_overlay(fixture_inputs(EVIDENCE_CASE), overlay))
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"
    assert submission.record().rejection is None
    assert publishing.bucket.stored == {}
    assert publishing.records.records == {}


def test_inputs_the_projection_cannot_use_reject_the_submission() -> None:
    overlay = fixture_json(EVIDENCE_CASE, OVERLAY)
    # An overlay recorded for another graph.
    overlay["graph_sha256"] = "0" * 64
    submission = accepted(
        {
            **fixture_inputs(EVIDENCE_CASE),
            OVERLAY: ("execution_overlay", json_bytes(overlay)),
        }
    )
    publishing = Publishing()

    submission.run(publishing)

    assert submission.record().status == "rejected"
    assert submission.record().rejection == {
        "code": "orrery_inputs_unusable",
        "message": "Graph evidence: overlay graph binding mismatch.",
    }
    assert publishing.bucket.stored == {}


def test_a_nul_in_a_schema_column_name_rejects_with_u_fffd_in_the_message() -> None:
    schema = fixture_json(EVIDENCE_CASE, SCHEMA)
    [resources] = [
        node for node in schema["graph"]["nodes"] if node["id"] == "resources"
    ]
    # json_bytes writes the NUL as the JSON escape \u0000, which intake and
    # read_input accept. The projection's GraphError repeats the column name
    # as it is, and PostgreSQL's jsonb, where the rejection is recorded,
    # cannot hold a NUL.
    resources["inputs"][0]["columns"][0] = "age\x00x"
    submission = accepted(
        {**fixture_inputs(EVIDENCE_CASE), SCHEMA: ("schema", json_bytes(schema))}
    )
    publishing = Publishing()

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "rejected"
    assert submission.record().rejection == {
        "code": "orrery_inputs_unusable",
        "message": (
            "Node 'resources' reads person.age\ufffdx, which no node owns in "
            "version 'survey' or its bases."
        ),
    }
    assert publishing.bucket.stored == {}
    assert publishing.records.records == {}


# The errors Microcosm's command treats as input errors around the projection
# (orrery.main at MICROCOSM_COMMIT), less those the copied projection cannot
# raise.
@pytest.mark.parametrize("error_type", [ValueError, TypeError, RecursionError])
def test_a_long_projection_refusal_is_shortened_to_500_characters(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    def refuse(schema: object, *, execution: object) -> str:
        raise error_type("Orrery adapter: " + "unusable " * 300)

    monkeypatch.setattr(graphs(), "orrery_json_from_schema", refuse)
    submission = accepted(fixture_inputs(EVIDENCE_CASE))

    submission.run(Publishing())

    assert submission.record().status == "rejected"
    rejection = submission.record().rejection
    assert rejection["code"] == "orrery_inputs_unusable"
    assert rejection["message"].startswith("Orrery adapter: unusable")
    assert len(rejection["message"]) <= 500


def test_an_upstream_file_named_like_the_graph_file_rejects_the_submission() -> None:
    submission = accepted(
        {**fixture_inputs(EVIDENCE_CASE), GRAPH: ("upstream", UPSTREAM_CONTENT)}
    )
    publishing = Publishing()

    submission.run(publishing)

    assert submission.record().status == "rejected"
    assert submission.record().rejection["code"] == "orrery_inputs_unusable"
    assert publishing.bucket.stored == {}


@pytest.mark.parametrize(
    "upstream_name",
    [
        "producing-graph-declaration.json",
        f"upstream-{UPSTREAM_SHA256[:32]}.json",
        f"upstream-{UPSTREAM_SHA256.upper()}.json",
        f"upstream-{UPSTREAM_SHA256}.txt",
        f"upstream-{UPSTREAM_SHA256}.json.json",
    ],
    ids=[
        "another name",
        "a short digest",
        "a digest in capitals",
        "another extension",
        "a doubled extension",
    ],
)
def test_an_upstream_file_not_named_upstream_sha256_json_rejects_the_submission(
    upstream_name: str,
) -> None:
    # Microcosm names each upstream file it copies upstream-<its SHA-256>.json
    # (save_graph_schema), and the service reads the digest from the name.
    submission = accepted(
        {
            **fixture_inputs(EVIDENCE_CASE),
            upstream_name: ("upstream", UPSTREAM_CONTENT),
        }
    )
    publishing = Publishing()

    submission.run(publishing)

    assert submission.record().status == "rejected"
    rejection = submission.record().rejection
    assert rejection["code"] == "orrery_inputs_unusable"
    assert upstream_name in rejection["message"]
    assert publishing.bucket.stored == {}
    assert publishing.records.records == {}


def test_an_upstream_file_named_by_another_file_s_sha256_rejects_the_submission() -> (
    None
):
    submission = accepted(
        {
            **fixture_inputs(EVIDENCE_CASE),
            UPSTREAM_NAME: ("upstream", UNLINKED_CONTENT),
        }
    )
    publishing = Publishing()

    submission.run(publishing)

    assert submission.record().status == "rejected"
    rejection = submission.record().rejection
    assert rejection["code"] == "orrery_inputs_unusable"
    assert UPSTREAM_NAME in rejection["message"]
    assert publishing.bucket.stored == {}
    assert publishing.records.records == {}


def test_objects_are_created_once_in_a_folder_named_by_the_document() -> None:
    submission, publishing, result = published_run(fixture_inputs(LINKLESS_CASE))

    document = fixture_bytes(LINKLESS_CASE, GRAPH)
    digest = hashlib.sha256(document).hexdigest()
    prefix = f"graphs/{submission.submission_id}/{digest}/"
    assert result["object_prefix"] == prefix
    assert publishing.client.bucket_names == [BUCKET]
    assert publishing.bucket.uploads == [
        (prefix + GRAPH, {"content_type": "application/json", "if_generation_match": 0})
    ]
    stored = publishing.bucket.stored[prefix + GRAPH]
    assert stored.content == document
    assert stored.metadata == {"sha256": digest}
    assert stored.content_type == "application/json"


def test_reprocessing_with_the_same_code_keeps_the_stored_objects_and_record() -> None:
    submission, publishing, first_result = published_run(upstream_inputs())
    generations = {
        name: stored.generation for name, stored in publishing.bucket.stored.items()
    }

    submission.reprocess()
    outcome = submission.run(publishing)

    assert outcome is JobOutcome.FINISHED
    assert submission.record().status == "succeeded"
    assert submission.record().result == first_result
    # Each object was offered again, create-only, and the stored one kept.
    assert len(publishing.bucket.uploads) == 2 * len(generations)
    assert {
        name: stored.generation for name, stored in publishing.bucket.stored.items()
    } == generations
    # The first record stays; the repeated one is not an error.
    [record] = publishing.records.records.values()
    assert record.processing_round == 1
    assert [repeated.processing_round for repeated in publishing.records.repeated] == [
        2
    ]


@pytest.mark.parametrize(
    "metadata", [{"sha256": "0" * 64}, {}], ids=["other-digest", "no-digest"]
)
def test_an_object_stored_with_other_content_fails_the_attempt(
    metadata: dict[str, str],
) -> None:
    document = fixture_bytes(LINKLESS_CASE, GRAPH)
    submission = accepted(fixture_inputs(LINKLESS_CASE))
    publishing = Publishing()
    name = (
        f"graphs/{submission.submission_id}/"
        f"{hashlib.sha256(document).hexdigest()}/{GRAPH}"
    )
    publishing.bucket.store(name, b"{}\n", metadata=metadata)

    outcome = submission.run(publishing)

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"
    assert publishing.bucket.stored[name].content == b"{}\n"
    assert publishing.records.records == {}


@pytest.mark.parametrize(
    ("licensed_data", "visibility"), [(True, "private"), (False, "public")]
)
def test_licensed_builds_publish_private_graph_files_and_others_public(
    licensed_data: bool, visibility: str
) -> None:
    _, publishing, result = published_run(
        fixture_inputs(LINKLESS_CASE), licensed_data=licensed_data
    )

    [record] = publishing.records.records.values()
    assert result["visibility"] == visibility
    assert record.visibility == visibility


def test_the_record_and_the_result_name_the_build_and_the_versions() -> None:
    submission, publishing, result = published_run(upstream_inputs())

    document = publishing.published(result)
    digest = hashlib.sha256(document).hexdigest()
    prefix = f"graphs/{submission.submission_id}/{digest}/"
    assert result == {
        "processed_by": "orrery-graph",
        "object_prefix": prefix,
        "document_sha256": digest,
        "document_bytes": len(document),
        "upstream_files": 1,
        "orrery_version": ORRERY_VERSION,
        "visibility": "private",
        "removed_links": result["removed_links"],
    }
    assert result["removed_links"] > 0
    [record] = publishing.records.records.values()
    assert record == graphs().OrreryGraphRecord(
        submission_id=submission.submission_id,
        processing_round=1,
        run_id=RUN_ID,
        build_id=BUILD_ID,
        country_code="UK",
        object_prefix=prefix,
        document_sha256=digest,
        document_bytes=len(document),
        upstream_files=1,
        orrery_version=ORRERY_VERSION,
        projection_source=MICROCOSM_COMMIT,
        collector_version=COLLECTOR_VERSION,
        reconstructed=False,
        visibility="private",
    )


def test_the_collector_version_is_the_installed_package_s() -> None:
    assert graphs().installed_collector_version() == importlib.metadata.version(
        "microcosm-collector"
    )


def test_each_attempt_works_in_a_new_directory_that_is_removed_afterwards() -> None:
    documents: list[Path] = []

    def unavailable(document: Path, names: object) -> Any:
        documents.append(document)
        raise OrreryValidatorError("Node is unavailable.")

    submission = accepted(fixture_inputs(EVIDENCE_CASE))
    publishing = Publishing(validate=unavailable)

    submission.run(publishing)
    submission.run(publishing)

    assert len(documents) == 2
    assert documents[0].parent != documents[1].parent
    assert not any(document.parent.exists() for document in documents)


@pytest.fixture
def unconnected_engine() -> Iterator[sa.Engine]:
    # Creating an engine connects to nothing; no test here uses the database.
    engine = create_database_engine("postgresql://user:password@127.0.0.1:1/none")
    try:
        yield engine
    finally:
        engine.dispose()


def stand_in_graph_objects(monkeypatch: pytest.MonkeyPatch, buckets: list[str]) -> None:
    class StandInGraphObjects:
        def __init__(self, *, bucket_name: str) -> None:
            buckets.append(bucket_name)

    monkeypatch.setattr(graphs(), "GoogleCloudStorageOrreryGraphs", StandInGraphObjects)


def test_the_orrery_handler_is_registered_only_where_its_bucket_is_set(
    monkeypatch: pytest.MonkeyPatch, unconnected_engine: sa.Engine
) -> None:
    buckets: list[str] = []
    stand_in_graph_objects(monkeypatch, buckets)

    without_bucket = registry().handlers_from_environment(
        engine=unconnected_engine, environ={}
    )
    with_bucket = registry().handlers_from_environment(
        engine=unconnected_engine, environ={"COLLECTOR_ORRERY_BUCKET": BUCKET}
    )

    assert without_bucket == DEFAULT_HANDLERS
    assert with_bucket["orrery-inputs"] is not DEFAULT_HANDLERS["orrery-inputs"]
    assert (
        with_bucket["calibration-diagnostics"]
        is DEFAULT_HANDLERS["calibration-diagnostics"]
    )
    assert buckets == [BUCKET]


def test_the_registry_holds_both_handlers_where_both_settings_are_set(
    monkeypatch: pytest.MonkeyPatch, unconnected_engine: sa.Engine
) -> None:
    stand_in_graph_objects(monkeypatch, [])

    handlers = registry().handlers_from_environment(
        engine=unconnected_engine,
        environ={
            "COLLECTOR_ORRERY_BUCKET": BUCKET,
            "COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN": "vercel_blob_rw_test_token",
        },
    )

    assert set(handlers) == set(DEFAULT_HANDLERS)
    for kind in DEFAULT_HANDLERS:
        assert handlers[kind] is not DEFAULT_HANDLERS[kind], kind


@pytest.mark.parametrize("bucket", ["gs://collector-orrery", "Collector-Orrery"])
def test_a_malformed_bucket_name_stops_the_worker(
    bucket: str, unconnected_engine: sa.Engine
) -> None:
    with pytest.raises(
        RuntimeError, match="COLLECTOR_ORRERY_BUCKET is not a valid resource name"
    ):
        registry().handlers_from_environment(
            engine=unconnected_engine, environ={"COLLECTOR_ORRERY_BUCKET": bucket}
        )


def test_the_worker_job_publishes_graph_files_where_its_bucket_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telemetry_collector import worker

    given: dict[str, Any] = {}
    buckets: list[str] = []

    class StandInIntake:
        def __init__(self, **settings: Any) -> None:
            pass

    def recorded_run_job(job_id: str, **arguments: Any) -> JobOutcome:
        # run_job uses DEFAULT_HANDLERS when it is given none.
        given["handlers"] = arguments.get("handlers", DEFAULT_HANDLERS)
        return JobOutcome.FINISHED

    stand_in_graph_objects(monkeypatch, buckets)
    monkeypatch.setattr(worker, "GoogleCloudStorageIntake", StandInIntake)
    monkeypatch.setattr(worker, "run_job", recorded_run_job)
    monkeypatch.setenv("COLLECTOR_WORKER_JOB_ID", "job-0123")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    monkeypatch.setenv("COLLECTOR_INTAKE_BUCKET", "collector-intake")
    monkeypatch.delenv("COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN", raising=False)
    monkeypatch.setenv("COLLECTOR_ORRERY_BUCKET", BUCKET)

    assert worker.main() == 0
    assert given["handlers"]["orrery-inputs"] is not DEFAULT_HANDLERS["orrery-inputs"]
    assert buckets == [BUCKET]


def graph_record(submission_id: str, **changes: Any) -> Any:
    record = graphs().OrreryGraphRecord(
        submission_id=submission_id,
        processing_round=1,
        run_id=RUN_ID,
        build_id=BUILD_ID,
        country_code="UK",
        object_prefix=f"graphs/{submission_id}/{'c' * 64}/",
        document_sha256="c" * 64,
        document_bytes=191_154,
        upstream_files=1,
        orrery_version=ORRERY_VERSION,
        projection_source=MICROCOSM_COMMIT,
        collector_version=COLLECTOR_VERSION,
        reconstructed=False,
        visibility="private",
    )
    return replace(record, **changes)


def test_the_postgres_store_records_each_document_once(postgres_url: str) -> None:
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    clock = SteppingClock()
    recorded_at = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    try:
        submission, _ = PostgresSubmissionStore(engine, clock=clock).create_submission(
            SubmissionRequest.model_validate(orrery_submission()), PRINCIPAL
        )
        store = graphs().PostgresOrreryGraphStore(engine, clock=clock)
        record = graph_record(submission.submission_id)

        assert store.record_graph(record) is True
        clock.advance(60)
        assert store.record_graph(replace(record, processing_round=2)) is False

        with engine.connect() as connection:
            rows = (
                connection.execute(sa.text("SELECT * FROM orrery_graph_files"))
                .mappings()
                .all()
            )
        assert len(rows) == 1
        assert {key: rows[0][key] for key in asdict(record)} == asdict(record)
        assert rows[0]["created_at"] == recorded_at
        # Only the two visibilities can be recorded.
        with pytest.raises(sa.exc.IntegrityError):
            store.record_graph(
                replace(record, document_sha256="d" * 64, visibility="cleared")
            )
        # A deleted submission takes its records with it.
        with engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM artifact_submissions WHERE submission_id = :id"),
                {"id": submission.submission_id},
            )
            remaining = connection.scalar(
                sa.text("SELECT count(*) FROM orrery_graph_files")
            )
        assert remaining == 0
    finally:
        engine.dispose()
