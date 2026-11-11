"""Projecting past Microcosm builds' staged graph inputs again.

A past build made after microcosm#1151 whose staged folder holds a graph schema
and an execution overlay becomes an ``orrery-inputs`` submission the collector
makes itself: its files are copied create-only from Hugging Face into intake
storage, the submission is accepted as completion would accept it, and its job
runs through the worker's ``run_job`` and the registered handler, so the
document is validated, published and recorded as a new build's is.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from telemetry_collector import operations
from telemetry_collector.google_cloud import (
    GoogleCloudStorageIntake,
    StoredObjectConflictError,
)
from telemetry_collector.huggingface_read import (
    GENERIC_TOKEN_VARIABLES,
    TOKEN_VARIABLE,
    HuggingFaceReader,
)
from telemetry_collector.intake import UPLOAD_CONTENT_TYPE
from telemetry_collector.past_builds import PastBuild, PastBuildInventory
from telemetry_collector.submissions import intake_object_name
from telemetry_collector.worker import SubmissionRejectedError
from tests.fakes import FakeIntakeObjectStore, FakeSubmissionStore
from tests.huggingface_fakes import FakeHuggingFace
from tests.test_orrery_graphs import (
    FIXTURE_ROOT,
    LINKLESS_CASE,
    OVERLAY,
    SCHEMA,
    UPSTREAM_CONTENT,
    UPSTREAM_NAME,
    Publishing,
    StandInStorageClient,
)

PROJECTIONS_MODULE = "telemetry_collector.past_build_projections"
UK_RELEASES = "policyengine/populace-uk-private"
US_RELEASES = "policyengine/populace-us"
READ_TOKEN = "hf_collector_read_token"
FOLDER_COMMIT = "1" * 40
HOLDER = "reprojection-test"
SCHEMA_BYTES = (FIXTURE_ROOT / LINKLESS_CASE / SCHEMA).read_bytes()
OVERLAY_BYTES = (FIXTURE_ROOT / LINKLESS_CASE / OVERLAY).read_bytes()


def projections_module() -> ModuleType:
    assert importlib.util.find_spec(PROJECTIONS_MODULE) is not None
    return importlib.import_module(PROJECTIONS_MODULE)


def staged_files(run_id: str, *, upstream: bool = True) -> dict[str, bytes]:
    folder = f"staged/{run_id}"
    files = {
        f"{folder}/{SCHEMA}": SCHEMA_BYTES,
        f"{folder}/{OVERLAY}": OVERLAY_BYTES,
        f"{folder}/staged_manifest.json": b"{}",
        f"{folder}/execution.evidence.json": b"{}",
    }
    if upstream:
        files[f"{folder}/{UPSTREAM_NAME}"] = UPSTREAM_CONTENT
    return files


def hugging_face_with(
    dataset: str, files: dict[str, bytes], *, private: bool = True
) -> FakeHuggingFace:
    hugging_face = FakeHuggingFace(token=READ_TOKEN)
    folders = {"/".join(path.split("/")[:2]) for path in files}
    stored = hugging_face.add_dataset(dataset, files, private=private)
    stored.folder_commits = {folder: FOLDER_COMMIT for folder in folders}
    return hugging_face


def past_build(
    run_id: str,
    hugging_face: FakeHuggingFace,
    *,
    dataset: str = UK_RELEASES,
    country_code: str = "uk",
    commit_class: str = "after_1151",
    class_reason: str | None = None,
    rebuild: str = "full",
    skip_reason: str | None = None,
) -> PastBuild:
    return PastBuild(
        run_id=run_id,
        country_code=country_code,
        dataset=dataset,
        revision=hugging_face.datasets[dataset].head_commit,
        staged_folder=f"staged/{run_id}",
        recorded_run=False,
        microcosm_commit="a" * 40,
        commit_source="staged_manifest",
        commit_class=commit_class,
        class_reason=class_reason,
        graph_files=(SCHEMA, OVERLAY),
        rebuild=rebuild,
        skipped=skip_reason is not None,
        skip_reason=skip_reason,
    )


def inventory_of(*builds: PastBuild) -> PastBuildInventory:
    return PastBuildInventory(
        created_at="2026-11-11T12:00:00+00:00",
        dataset_revisions={},
        builds=tuple(builds),
        counts={},
    )


def reader_for(hugging_face: FakeHuggingFace) -> HuggingFaceReader:
    return HuggingFaceReader.from_environment(
        {TOKEN_VARIABLE: READ_TOKEN}, client=hugging_face.client()
    )


def stand_in_handler(results: list[Any]) -> Any:
    def handle(context: Any) -> dict[str, Any]:
        results.append(context.submission)
        return {"processed_by": "stand-in"}

    return handle


def reproject(
    inventory: PastBuildInventory,
    hugging_face: FakeHuggingFace,
    *,
    submissions: FakeSubmissionStore,
    intake: FakeIntakeObjectStore,
    handlers: dict[str, Any],
    dry_run: bool = False,
) -> list[Any]:
    return projections_module().reproject_past_builds(
        inventory,
        reader_for(hugging_face),
        submissions=submissions,
        objects=intake,
        handlers=handlers,
        holder=HOLDER,
        dry_run=dry_run,
    )


def downloads(hugging_face: FakeHuggingFace) -> list[str]:
    return [
        request.url.raw_path.decode()
        for request in hugging_face.requests
        if "/resolve/" in request.url.raw_path.decode()
    ]


def test_a_full_build_after_1151_is_published_through_the_orrery_handler() -> None:
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))
    build = past_build("uk-run-1", hugging_face)
    submissions = FakeSubmissionStore()
    intake = FakeIntakeObjectStore()
    publishing = Publishing()

    (outcome,) = reproject(
        inventory_of(build),
        hugging_face,
        submissions=submissions,
        intake=intake,
        handlers={"orrery-inputs": publishing.handler()},
    )

    assert outcome.run_id == "uk-run-1"
    assert outcome.outcome == "succeeded"
    assert outcome.new_submission is True
    record = submissions.get_submission(outcome.submission_id)
    assert record is not None
    assert record.kind == "orrery-inputs"
    assert record.owner_hf_id == "microcosm-collector"
    assert record.licensed_data is True
    assert record.provenance == {
        "run_id": "uk-run-1",
        "build_id": "uk-run-1",
        "country_code": "GB",
        "release": None,
        "huggingface": {
            "repo": UK_RELEASES,
            "commit": FOLDER_COMMIT,
            "folder": "staged/uk-run-1",
        },
    }
    assert {file["name"]: file["role"] for file in record.files} == {
        SCHEMA: "schema",
        OVERLAY: "execution_overlay",
        UPSTREAM_NAME: "upstream",
    }
    for name, content in (
        (SCHEMA, SCHEMA_BYTES),
        (OVERLAY, OVERLAY_BYTES),
        (UPSTREAM_NAME, UPSTREAM_CONTENT),
    ):
        stored_content, stored_sha256, _ = intake.objects[
            intake_object_name(outcome.submission_id, name)
        ]
        assert stored_content == content
        assert stored_sha256 == hashlib.sha256(content).hexdigest()
    (published,) = publishing.records.records.values()
    assert published.submission_id == outcome.submission_id
    assert published.run_id == "uk-run-1"
    assert published.country_code == "GB"
    assert published.visibility == "private"
    assert outcome.result["document_sha256"] == published.document_sha256


def test_projecting_again_makes_the_same_submission_and_changes_nothing() -> None:
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))
    inventory = inventory_of(past_build("uk-run-1", hugging_face))
    submissions = FakeSubmissionStore()
    intake = FakeIntakeObjectStore()
    publishing = Publishing()
    handlers = {"orrery-inputs": publishing.handler()}

    (first,) = reproject(
        inventory,
        hugging_face,
        submissions=submissions,
        intake=intake,
        handlers=handlers,
    )
    stored_before = dict(intake.objects)
    (second,) = reproject(
        inventory,
        hugging_face,
        submissions=submissions,
        intake=intake,
        handlers=handlers,
    )

    assert second.submission_id == first.submission_id
    assert second.new_submission is False
    assert second.outcome == "succeeded"
    assert intake.objects == stored_before
    assert len(publishing.records.records) == 1
    assert publishing.records.repeated == []


def test_builds_that_cannot_be_projected_are_left_out_with_the_reason() -> None:
    files = {
        **staged_files("uk-run-1"),
        **staged_files("uk-run-2"),
        **staged_files("uk-run-3"),
    }
    hugging_face = hugging_face_with(UK_RELEASES, files)
    inventory = inventory_of(
        past_build(
            "uk-run-1",
            hugging_face,
            commit_class="before_1144",
            skip_reason="made before microcosm#1144",
        ),
        past_build("uk-run-2", hugging_face, rebuild="declaration_only"),
        past_build(
            "uk-run-3",
            hugging_face,
            commit_class="unknown",
            class_reason="GitHub's rate limit for requests without a token was reached",
        ),
    )
    submissions = FakeSubmissionStore()
    handled: list[Any] = []

    outcomes = reproject(
        inventory,
        hugging_face,
        submissions=submissions,
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": stand_in_handler(handled)},
    )

    assert [outcome.outcome for outcome in outcomes] == ["left_out"] * 3
    assert outcomes[0].reason == "made before microcosm#1144"
    assert "without the execution overlay" in outcomes[1].reason
    assert "rate limit" in outcomes[2].reason
    assert all(outcome.submission_id is None for outcome in outcomes)
    assert downloads(hugging_face) == []
    assert handled == []


def test_a_build_staged_in_a_public_dataset_is_not_licensed() -> None:
    hugging_face = hugging_face_with(
        US_RELEASES, staged_files("us-run-1", upstream=False), private=False
    )
    build = past_build("us-run-1", hugging_face, dataset=US_RELEASES, country_code="us")
    submissions = FakeSubmissionStore()
    handled: list[Any] = []

    (outcome,) = reproject(
        inventory_of(build),
        hugging_face,
        submissions=submissions,
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": stand_in_handler(handled)},
    )

    assert outcome.outcome == "succeeded"
    (submission,) = handled
    assert submission.licensed_data is False
    assert submission.provenance["country_code"] == "US"


def test_a_dry_run_lists_what_it_would_project_and_writes_nothing() -> None:
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))
    submissions = FakeSubmissionStore()
    intake = FakeIntakeObjectStore()
    handled: list[Any] = []

    (outcome,) = reproject(
        inventory_of(past_build("uk-run-1", hugging_face)),
        hugging_face,
        submissions=submissions,
        intake=intake,
        handlers={"orrery-inputs": stand_in_handler(handled)},
        dry_run=True,
    )

    assert outcome.outcome == "would_reproject"
    assert outcome.submission_id is None
    assert hugging_face.requests == []
    assert intake.objects == {}
    assert handled == []


def test_a_stored_intake_object_with_other_content_rejects_the_submission() -> None:
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))
    inventory = inventory_of(past_build("uk-run-1", hugging_face))
    (first,) = reproject(
        inventory,
        hugging_face,
        submissions=FakeSubmissionStore(),
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": stand_in_handler([])},
        dry_run=False,
    )
    intake = FakeIntakeObjectStore()
    intake.upload(
        intake_object_name(first.submission_id, SCHEMA),
        b"{}",
        sha256=hashlib.sha256(b"{}").hexdigest(),
    )
    handled: list[Any] = []

    (outcome,) = reproject(
        inventory,
        hugging_face,
        submissions=FakeSubmissionStore(),
        intake=intake,
        handlers={"orrery-inputs": stand_in_handler(handled)},
    )

    assert outcome.submission_id == first.submission_id
    assert outcome.outcome == "rejected"
    assert "stored_object_mismatch" in outcome.reason
    assert handled == []


def test_a_rejection_by_the_handler_is_reported() -> None:
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))

    def rejecting_handler(context: Any) -> dict[str, Any]:
        raise SubmissionRejectedError("orrery_inputs_unusable", "The schema is empty.")

    (outcome,) = reproject(
        inventory_of(past_build("uk-run-1", hugging_face)),
        hugging_face,
        submissions=FakeSubmissionStore(),
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": rejecting_handler},
    )

    assert outcome.outcome == "rejected"
    assert "orrery_inputs_unusable" in outcome.reason
    assert "The schema is empty." in outcome.reason


def test_a_folder_the_submission_rules_refuse_is_reported_without_a_submission() -> (
    None
):
    files = staged_files("uk-run-1", upstream=False)
    for index in range(255):
        content = json.dumps({"upstream": index}).encode()
        files[
            f"staged/uk-run-1/upstream-{hashlib.sha256(content).hexdigest()}.json"
        ] = content
    hugging_face = hugging_face_with(UK_RELEASES, files)
    submissions = FakeSubmissionStore()
    intake = FakeIntakeObjectStore()

    (outcome,) = reproject(
        inventory_of(past_build("uk-run-1", hugging_face)),
        hugging_face,
        submissions=submissions,
        intake=intake,
        handlers={"orrery-inputs": stand_in_handler([])},
    )

    assert outcome.outcome == "refused"
    assert "256" in outcome.reason
    assert outcome.submission_id is None
    assert intake.objects == {}


def test_the_intake_bucket_creates_each_object_once_with_its_sha256(
    tmp_path: Path,
) -> None:
    client = StandInStorageClient()
    intake = GoogleCloudStorageIntake(
        bucket_name="collector-intake", client=client, credentials=object()
    )
    assert callable(getattr(intake, "create_object", None))
    source = tmp_path / SCHEMA
    source.write_bytes(SCHEMA_BYTES)
    sha256 = hashlib.sha256(SCHEMA_BYTES).hexdigest()
    object_name = intake_object_name("sub-1", SCHEMA)

    assert intake.create_object(object_name, source, sha256=sha256) is True
    assert intake.create_object(object_name, source, sha256=sha256) is False
    with pytest.raises(StoredObjectConflictError):
        intake.create_object(object_name, source, sha256="0" * 64)

    stored = client.graph_bucket.stored[object_name]
    assert stored.content == SCHEMA_BYTES
    assert stored.metadata == {"sha256": sha256}
    assert stored.content_type == UPLOAD_CONTENT_TYPE
    assert {
        options["if_generation_match"] for _, options in client.graph_bucket.uploads
    } == {0}


def stand_in_command_services(monkeypatch: pytest.MonkeyPatch) -> FakeHuggingFace:
    projections_module()
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))
    build = past_build("uk-run-1", hugging_face)

    class StandInEngine:
        def dispose(self) -> None:
            pass

    class StandInReader:
        @staticmethod
        def from_environment() -> HuggingFaceReader:
            return HuggingFaceReader.from_environment(client=hugging_face.client())

    monkeypatch.setattr(
        operations, "create_database_engine", lambda database_url: StandInEngine()
    )
    monkeypatch.setattr(operations, "HuggingFaceReader", StandInReader)
    monkeypatch.setattr(
        operations,
        "_past_build_inventory",
        lambda reader, engine: inventory_of(build),
    )
    monkeypatch.setattr(
        operations, "PostgresSubmissionStore", lambda engine: FakeSubmissionStore()
    )
    for name in GENERIC_TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(TOKEN_VARIABLE, READ_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    monkeypatch.setenv("COLLECTOR_INTAKE_BUCKET", "collector-intake-test")
    monkeypatch.setenv("COLLECTOR_ORRERY_BUCKET", "collector-orrery-test")
    return hugging_face


def test_the_dry_run_command_prints_one_document_for_each_build(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hugging_face = stand_in_command_services(monkeypatch)

    assert operations.main(["reproject-past-builds", "--dry-run"]) == 0

    (printed_line,) = capsys.readouterr().out.splitlines()
    printed = json.loads(printed_line)
    assert printed["run_id"] == "uk-run-1"
    assert printed["outcome"] == "would_reproject"
    assert downloads(hugging_face) == []


@pytest.mark.parametrize(
    "missing_name", ["COLLECTOR_INTAKE_BUCKET", "COLLECTOR_ORRERY_BUCKET"]
)
def test_the_command_requires_the_intake_and_orrery_buckets(
    monkeypatch: pytest.MonkeyPatch, missing_name: str
) -> None:
    stand_in_command_services(monkeypatch)
    monkeypatch.delenv(missing_name)

    with pytest.raises(RuntimeError, match=missing_name):
        operations.main(["reproject-past-builds"])


def test_a_left_out_build_keeps_its_place_in_run_id_order() -> None:
    files = {**staged_files("uk-run-1"), **staged_files("uk-run-2")}
    hugging_face = hugging_face_with(UK_RELEASES, files)
    inventory = inventory_of(
        past_build("uk-run-1", hugging_face, rebuild="declaration_only"),
        past_build("uk-run-2", hugging_face),
    )

    outcomes = reproject(
        inventory,
        hugging_face,
        submissions=FakeSubmissionStore(),
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": stand_in_handler([])},
    )

    assert [(outcome.run_id, outcome.outcome) for outcome in outcomes] == [
        ("uk-run-1", "left_out"),
        ("uk-run-2", "succeeded"),
    ]


def test_each_dataset_s_folder_commits_are_read_once_at_its_revision() -> None:
    files = {**staged_files("uk-run-1"), **staged_files("uk-run-2")}
    hugging_face = hugging_face_with(UK_RELEASES, files)
    hugging_face.datasets[UK_RELEASES].folder_commits["staged/uk-run-2"] = "2" * 40
    inventory = inventory_of(
        past_build("uk-run-1", hugging_face), past_build("uk-run-2", hugging_face)
    )
    submissions = FakeSubmissionStore()

    outcomes = reproject(
        inventory,
        hugging_face,
        submissions=submissions,
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": stand_in_handler([])},
    )

    commits = [
        submissions.get_submission(outcome.submission_id).provenance["huggingface"][
            "commit"
        ]
        for outcome in outcomes
    ]
    assert commits == [FOLDER_COMMIT, "2" * 40]
    revision = hugging_face.datasets[UK_RELEASES].head_commit
    folder_listings = [
        request.url.raw_path.decode()
        for request in hugging_face.requests
        if "expand=true" in str(request.url)
    ]
    assert len(folder_listings) == 1
    assert f"/tree/{revision}/staged" in folder_listings[0]
    assert all(f"/resolve/{revision}/" in path for path in downloads(hugging_face))


def test_a_job_released_for_retry_is_reported_as_queued() -> None:
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))

    def failing_handler(context: Any) -> dict[str, Any]:
        raise OSError("Cloud Storage is unavailable.")

    (outcome,) = reproject(
        inventory_of(past_build("uk-run-1", hugging_face)),
        hugging_face,
        submissions=FakeSubmissionStore(),
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": failing_handler},
    )

    assert outcome.outcome == "queued"
    assert "recovery" in outcome.reason


def test_left_out_reasons_cover_a_build_without_a_schema() -> None:
    hugging_face = hugging_face_with(UK_RELEASES, staged_files("uk-run-1"))
    build = replace(
        past_build("uk-run-1", hugging_face), rebuild="none", graph_files=()
    )

    (outcome,) = reproject(
        inventory_of(build),
        hugging_face,
        submissions=FakeSubmissionStore(),
        intake=FakeIntakeObjectStore(),
        handlers={"orrery-inputs": stand_in_handler([])},
    )

    assert outcome.outcome == "left_out"
    assert "nothing on Hugging Face" in outcome.reason
