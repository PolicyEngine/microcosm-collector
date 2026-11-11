"""The inventory of past Microcosm builds.

Past builds come from two places: the folders staged under ``staged/<run_id>/``
in each country's Hugging Face release dataset, and the collector's recorded
runs. Each build is classed by the Microcosm commit its staged manifest
records, relative to microcosm#1144 and microcosm#1151, using GitHub's compare
API, and says what its Orrery graph document can be rebuilt from.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import httpx
import pytest

from telemetry_collector import operations
from telemetry_collector.calibration_trees import RELEASE_REPOS
from telemetry_collector.database import (
    TelemetryRun,
    create_database_engine,
    create_session_factory,
)
from telemetry_collector.huggingface_read import (
    GENERIC_TOKEN_VARIABLES,
    TOKEN_VARIABLE,
    HuggingFaceReader,
)
from telemetry_collector.migrate import upgrade_database
from tests.huggingface_fakes import FakeHuggingFace

PAST_BUILDS_MODULE = "telemetry_collector.past_builds"
UK_RELEASES = "policyengine/populace-uk-private"
READ_TOKEN = "hf_collector_read_token"
INVENTORY_TIME = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

#: The merge commits of microcosm#1144 and microcosm#1151.
EXECUTION_EVIDENCE_MERGE = "58b4c46175731cac940b897a42d85cd893cb3381"
GRAPH_SCHEMA_MERGE = "852ec44e6bc75db8399ebe3f649b2987c3d77424"
COMMIT_AFTER_1151 = "a" * 40
COMMIT_BETWEEN_1144_AND_1151 = "b" * 40
COMMIT_BEFORE_1144 = "c" * 40
MERGES_CONTAINED = {
    COMMIT_AFTER_1151: {EXECUTION_EVIDENCE_MERGE, GRAPH_SCHEMA_MERGE},
    COMMIT_BETWEEN_1144_AND_1151: {EXECUTION_EVIDENCE_MERGE},
    COMMIT_BEFORE_1144: set(),
}

ALL_GRAPH_FILES = (
    "graph.schema.json",
    "graph.execution.json",
    "graph.orrery.json",
    "execution.evidence.json",
    "orrery.publication.json",
)
SCHEMA_WITHOUT_OVERLAY = ("graph.schema.json", "orrery.publication.json")


def past_builds_module() -> ModuleType:
    assert importlib.util.find_spec(PAST_BUILDS_MODULE) is not None
    return importlib.import_module(PAST_BUILDS_MODULE)


class FakeGitHub:
    """Answers GitHub's compare API for PolicyEngine/microcosm.

    A head contains a base when the base is in ``MERGES_CONTAINED[head]``. After
    ``requests_answered`` requests, every later one gets GitHub's rate-limit
    answer.
    """

    def __init__(self, *, requests_answered: int | None = None) -> None:
        self.requests_answered = requests_answered
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def comparisons(self) -> list[tuple[str, str]]:
        prefix = "/repos/PolicyEngine/microcosm/compare/"
        compared = []
        for request in self.requests:
            assert request.url.host == "api.github.com"
            assert request.url.path.startswith(prefix)
            base, head = request.url.path.removeprefix(prefix).split("...")
            compared.append((base, head))
        return compared

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (
            self.requests_answered is not None
            and len(self.requests) > self.requests_answered
        ):
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"},
                json={"message": "API rate limit exceeded"},
            )
        base, head = self.comparisons()[-1]
        if head not in MERGES_CONTAINED:
            return httpx.Response(404, json={"message": "Not Found"})
        contains_base = base in MERGES_CONTAINED[head]
        return httpx.Response(
            200, json={"status": "ahead" if contains_base else "diverged"}
        )


def staged_folder(
    run_id: str,
    commit: str | None,
    graph_files: Sequence[str] = (),
    *,
    with_manifest: bool = True,
) -> dict[str, bytes]:
    files = {f"staged/{run_id}/{name}": b"{}" for name in graph_files}
    files[f"staged/{run_id}/dataset.h5"] = b"rows"
    if with_manifest:
        manifest = {
            "schema_name": "microcosm.staged-dataset.manifest",
            "run_id": run_id,
            "summary": {"git_commit": commit, "git_dirty": False},
        }
        files[f"staged/{run_id}/staged_manifest.json"] = json.dumps(manifest).encode()
    return files


def list_builds(
    staged_files: dict[str, bytes],
    github: FakeGitHub,
    recorded_runs: Sequence[Any] = (),
) -> tuple[Any, FakeHuggingFace]:
    module = past_builds_module()
    hugging_face = FakeHuggingFace(token=READ_TOKEN)
    hugging_face.add_dataset(UK_RELEASES, staged_files)
    reader = HuggingFaceReader.from_environment(
        {TOKEN_VARIABLE: READ_TOKEN}, client=hugging_face.client()
    )
    with module.GitHubCommitClassifier(client=github.client()) as classifier:
        inventory = module.list_past_builds(
            reader,
            recorded_runs,
            classifier,
            clock=lambda: INVENTORY_TIME,
            release_datasets={"uk": UK_RELEASES},
        )
    return inventory, hugging_face


def builds_by_run_id(inventory: Any) -> dict[str, Any]:
    return {build.run_id: build for build in inventory.builds}


def test_the_graph_files_in_a_staged_folder_decide_what_can_be_rebuilt() -> None:
    staged_files = {
        **staged_folder("uk-run-3-none", COMMIT_AFTER_1151),
        **staged_folder("uk-run-1-full", COMMIT_AFTER_1151, ALL_GRAPH_FILES),
        **staged_folder("uk-run-2-schema", COMMIT_AFTER_1151, SCHEMA_WITHOUT_OVERLAY),
    }

    inventory, _ = list_builds(staged_files, FakeGitHub())

    assert [build.run_id for build in inventory.builds] == [
        "uk-run-1-full",
        "uk-run-2-schema",
        "uk-run-3-none",
    ]
    builds = builds_by_run_id(inventory)
    assert builds["uk-run-1-full"].rebuild == "full"
    assert builds["uk-run-1-full"].graph_files == ALL_GRAPH_FILES
    assert builds["uk-run-2-schema"].rebuild == "declaration_only"
    assert builds["uk-run-2-schema"].graph_files == SCHEMA_WITHOUT_OVERLAY
    assert builds["uk-run-3-none"].rebuild == "none"
    assert builds["uk-run-3-none"].graph_files == ()
    assert inventory.counts == {
        "commit_class": {
            "after_1151": 3,
            "between_1144_and_1151": 0,
            "before_1144": 0,
            "unknown": 0,
        },
        "rebuild": {"full": 1, "declaration_only": 1, "none": 1},
        "skipped": 0,
    }
    assert inventory.created_at == INVENTORY_TIME.isoformat()


def test_a_commit_containing_1151_is_after_1151_after_one_call() -> None:
    github = FakeGitHub()

    inventory, _ = list_builds(
        staged_folder("uk-run-1", COMMIT_AFTER_1151, ALL_GRAPH_FILES), github
    )

    build = builds_by_run_id(inventory)["uk-run-1"]
    assert build.microcosm_commit == COMMIT_AFTER_1151
    assert build.commit_class == "after_1151"
    assert build.class_reason is None
    assert build.skipped is False
    assert github.comparisons() == [(GRAPH_SCHEMA_MERGE, COMMIT_AFTER_1151)]
    (request,) = github.requests
    assert "Authorization" not in request.headers
    assert request.headers["User-Agent"].startswith("microcosm-collector")


def test_a_commit_containing_1144_but_not_1151_is_between_after_two_calls() -> None:
    github = FakeGitHub()

    inventory, _ = list_builds(
        staged_folder("uk-run-1", COMMIT_BETWEEN_1144_AND_1151, ALL_GRAPH_FILES),
        github,
    )

    assert builds_by_run_id(inventory)["uk-run-1"].commit_class == (
        "between_1144_and_1151"
    )
    assert github.comparisons() == [
        (GRAPH_SCHEMA_MERGE, COMMIT_BETWEEN_1144_AND_1151),
        (EXECUTION_EVIDENCE_MERGE, COMMIT_BETWEEN_1144_AND_1151),
    ]


def test_a_commit_containing_neither_is_before_1144_and_skipped() -> None:
    inventory, _ = list_builds(
        staged_folder("uk-run-1", COMMIT_BEFORE_1144, ALL_GRAPH_FILES), FakeGitHub()
    )

    build = builds_by_run_id(inventory)["uk-run-1"]
    assert build.commit_class == "before_1144"
    assert build.rebuild == "full"
    assert build.skipped is True
    assert build.skip_reason == "made before microcosm#1144"


def test_a_build_between_1144_and_1151_is_skipped_only_with_nothing_to_rebuild() -> (
    None
):
    staged_files = {
        **staged_folder("uk-run-1", COMMIT_BETWEEN_1144_AND_1151, ALL_GRAPH_FILES),
        **staged_folder("uk-run-2", COMMIT_BETWEEN_1144_AND_1151),
    }

    inventory, _ = list_builds(staged_files, FakeGitHub())

    builds = builds_by_run_id(inventory)
    assert builds["uk-run-1"].skipped is False
    assert builds["uk-run-1"].skip_reason is None
    assert builds["uk-run-2"].skipped is True
    assert builds["uk-run-2"].skip_reason == (
        "made before microcosm#1151 with nothing on Hugging Face to rebuild from"
    )
    assert inventory.counts["skipped"] == 1


def test_a_malformed_commit_is_unknown_without_a_call() -> None:
    github = FakeGitHub()

    inventory, _ = list_builds(
        staged_folder("uk-run-1", "not-a-commit", ALL_GRAPH_FILES), github
    )

    build = builds_by_run_id(inventory)["uk-run-1"]
    assert build.commit_class == "unknown"
    assert "40" in build.class_reason
    assert build.skipped is False
    assert github.requests == []


def test_builds_sharing_a_commit_make_one_set_of_calls() -> None:
    github = FakeGitHub()
    staged_files = {
        **staged_folder("uk-run-1", COMMIT_BETWEEN_1144_AND_1151, ALL_GRAPH_FILES),
        **staged_folder("uk-run-2", COMMIT_BETWEEN_1144_AND_1151, ALL_GRAPH_FILES),
    }

    inventory, _ = list_builds(staged_files, github)

    assert {build.commit_class for build in inventory.builds} == {
        "between_1144_and_1151"
    }
    assert len(github.requests) == 2


def test_a_rate_limit_answer_makes_that_commit_and_later_ones_unknown() -> None:
    github = FakeGitHub(requests_answered=1)
    staged_files = {
        **staged_folder("uk-run-1", COMMIT_AFTER_1151, ALL_GRAPH_FILES),
        **staged_folder("uk-run-2", COMMIT_BETWEEN_1144_AND_1151, ALL_GRAPH_FILES),
        **staged_folder("uk-run-3", COMMIT_BEFORE_1144, ALL_GRAPH_FILES),
    }

    inventory, _ = list_builds(staged_files, github)

    builds = builds_by_run_id(inventory)
    assert builds["uk-run-1"].commit_class == "after_1151"
    for run_id in ("uk-run-2", "uk-run-3"):
        assert builds[run_id].commit_class == "unknown"
        assert "rate limit" in builds[run_id].class_reason
        assert builds[run_id].skipped is False
    assert len(github.requests) == 2


def test_a_staged_folder_without_a_manifest_has_no_commit() -> None:
    github = FakeGitHub()

    inventory, _ = list_builds(
        staged_folder("uk-run-1", None, ALL_GRAPH_FILES, with_manifest=False), github
    )

    build = builds_by_run_id(inventory)["uk-run-1"]
    assert build.staged_folder == "staged/uk-run-1"
    assert build.microcosm_commit is None
    assert build.commit_class == "unknown"
    assert "staged_manifest.json" in build.class_reason
    assert github.requests == []


def test_recorded_runs_join_staged_folders_by_run_id() -> None:
    module = past_builds_module()
    recorded_runs = [
        module.RecordedRun(run_id="uk-run-staged", country_code="uk"),
        module.RecordedRun(run_id="uk-run-only-recorded", country_code="uk"),
    ]

    inventory, _ = list_builds(
        staged_folder("uk-run-staged", COMMIT_AFTER_1151, ALL_GRAPH_FILES),
        FakeGitHub(),
        recorded_runs,
    )

    assert len(inventory.builds) == 2
    builds = builds_by_run_id(inventory)
    staged = builds["uk-run-staged"]
    assert staged.recorded_run is True
    assert staged.staged_folder == "staged/uk-run-staged"
    assert staged.rebuild == "full"
    only_recorded = builds["uk-run-only-recorded"]
    assert only_recorded.recorded_run is True
    assert only_recorded.country_code == "uk"
    assert only_recorded.staged_folder is None
    assert only_recorded.dataset is None
    assert only_recorded.microcosm_commit is None
    assert only_recorded.commit_class == "unknown"
    assert only_recorded.rebuild == "none"
    assert only_recorded.skipped is False


def test_every_read_is_pinned_to_the_dataset_head_commit() -> None:
    inventory, hugging_face = list_builds(
        staged_folder("uk-run-1", COMMIT_AFTER_1151, ALL_GRAPH_FILES), FakeGitHub()
    )

    head_commit = hugging_face.datasets[UK_RELEASES].head_commit
    assert inventory.dataset_revisions == {UK_RELEASES: head_commit}
    build = builds_by_run_id(inventory)["uk-run-1"]
    assert (build.dataset, build.revision) == (UK_RELEASES, head_commit)
    pinned_reads = [
        request.url.raw_path.decode()
        for request in hugging_face.requests
        if "/tree/" in request.url.raw_path.decode()
        or "/resolve/" in request.url.raw_path.decode()
    ]
    assert pinned_reads
    assert all(f"/{head_commit}" in read_path for read_path in pinned_reads)


def test_recorded_runs_are_read_from_the_telemetry_runs_table(
    postgres_url: str,
) -> None:
    module = past_builds_module()
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    try:
        with create_session_factory(engine)() as session, session.begin():
            session.add(
                TelemetryRun(
                    run_id="uk-run-1",
                    country_code="UK",
                    pipeline="uk-frs-calibration",
                    run_kind="build",
                    owner_hf_id="hf-user-1",
                    owner_hf_username="builder",
                    status="succeeded",
                    current_stage="finished",
                    started_at=INVENTORY_TIME,
                    updated_at=INVENTORY_TIME,
                )
            )

        assert module.recorded_runs(engine) == [
            module.RecordedRun(run_id="uk-run-1", country_code="uk")
        ]
    finally:
        engine.dispose()


def stand_in_past_builds_services(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    module = past_builds_module()
    constructed: dict[str, Any] = {}
    hugging_face = FakeHuggingFace(token=READ_TOKEN)
    for dataset in RELEASE_REPOS.values():
        staged_files = (
            staged_folder("uk-run-1", COMMIT_AFTER_1151, ALL_GRAPH_FILES)
            if dataset == UK_RELEASES
            else {}
        )
        hugging_face.add_dataset(
            dataset, staged_files, private=dataset != "policyengine/populace-us"
        )
    github = FakeGitHub()
    real_classifier = module.GitHubCommitClassifier

    class StandInEngine:
        def dispose(self) -> None:
            constructed["disposed"] = True

    class StandInReader:
        @staticmethod
        def from_environment() -> HuggingFaceReader:
            return HuggingFaceReader.from_environment(client=hugging_face.client())

    monkeypatch.setattr(
        operations, "create_database_engine", lambda database_url: StandInEngine()
    )
    monkeypatch.setattr(operations, "HuggingFaceReader", StandInReader)
    monkeypatch.setattr(
        module,
        "GitHubCommitClassifier",
        lambda: real_classifier(client=github.client()),
    )
    monkeypatch.setattr(
        module,
        "recorded_runs",
        lambda engine: [module.RecordedRun(run_id="uk-run-1", country_code="uk")],
    )
    for name in GENERIC_TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(TOKEN_VARIABLE, READ_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    return constructed


def test_the_past_builds_command_prints_the_inventory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    constructed = stand_in_past_builds_services(monkeypatch)

    assert operations.main(["past-builds"]) == 0

    (printed_line,) = capsys.readouterr().out.splitlines()
    printed = json.loads(printed_line)
    assert set(printed["dataset_revisions"]) == set(RELEASE_REPOS.values())
    (build,) = printed["builds"]
    assert build["run_id"] == "uk-run-1"
    assert build["recorded_run"] is True
    assert build["commit_class"] == "after_1151"
    assert build["rebuild"] == "full"
    assert printed["counts"]["rebuild"]["full"] == 1
    assert constructed["disposed"] is True


@pytest.mark.parametrize("missing_name", ["DATABASE_URL", TOKEN_VARIABLE])
def test_the_past_builds_command_requires_the_database_and_hugging_face_token(
    monkeypatch: pytest.MonkeyPatch, missing_name: str
) -> None:
    stand_in_past_builds_services(monkeypatch)
    monkeypatch.delenv(missing_name)

    with pytest.raises(RuntimeError, match=missing_name):
        operations.main(["past-builds"])
