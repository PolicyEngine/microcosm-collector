"""Reconciling a country's calibration trees with its Hugging Face repos.

The ``reconcile-calibration-trees`` command lists a country's builds at each
repo's head commit, with the GitHub Actions publisher's listing rules ported
to Python: every build ``releases.json`` lists, and every finalized staging
run. It skips what the store already holds and what a submission is still
processing, and builds the rest with the backfill's steps. A release run
then makes the country's latest follow ``releases.json``
(``calibration_trees.follow_releases_record``). These tests stand in for
Hugging Face, for the tree package's Bun processes and for the database, so
nothing reaches the network, runs Bun or needs Postgres, except the tests
marked as Postgres tests.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.calibration_trees import (
    MANIFEST_LOCK_KEY,
    FollowedLatest,
    follow_releases_record,
    tree_commands,
)
from telemetry_collector.database import create_database_engine
from telemetry_collector.huggingface_read import (
    HuggingFaceReader,
    HuggingFaceReadSettings,
)
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.submission_store import PostgresSubmissionStore
from telemetry_collector.submissions import SubmissionRequest
from tests.huggingface_fakes import DEFAULT_COMMIT_DATE, FakeHuggingFace
from tests.test_calibration_tree_backfill import (
    BLOB_TOKEN,
    BUILD_MANIFEST,
    DIAGNOSTICS,
    MINUTE,
    READ_TOKEN,
    RECORD_REPO_KEY,
    ManualClock,
    StandInBun,
    build_id,
    hugging_face_with,
    schema_1_run,
    sha256,
    store_manifest,
    structured_run,
)
from tests.test_submissions import calibration_submission

RECONCILIATION_MODULE = "telemetry_collector.calibration_tree_reconciliation"
BACKFILL_MODULE = "telemetry_collector.calibration_tree_backfill"
US_RELEASES = "policyengine/populace-us"
US_STAGING = "policyengine/populace-us-staging"
UK_RELEASES = "policyengine/populace-uk-private"
UK_STAGING = "policyengine/populace-uk-staging"
BE_RELEASES = "policyengine/populace-be-private"
US_RELEASE = "microcosm-us-2025"
OLDER_BUILD = "us-calibration-attempt-20261019T083323Z-6cba5d71"
CURRENT_BUILD = "us-calibration-attempt-20261101T120000Z-0a1b2c3d"
OLDER_ID = f"{US_RELEASE}/{OLDER_BUILD}"
CURRENT_ID = f"{US_RELEASE}/{CURRENT_BUILD}"
RUN_ID = "us-calibration-attempt-20261102T080000Z-1a2b3c4d"
OTHER_RUN_ID = "us-calibration-attempt-20261103T080000Z-5e6f7a8b"
FOLDER_COMMIT = "4" * 40
NEWER_FOLDER_COMMIT = "6" * 40


def reconciliation_module() -> ModuleType:
    if importlib.util.find_spec(RECONCILIATION_MODULE) is None:
        pytest.fail(f"{RECONCILIATION_MODULE} does not exist yet.")
    return importlib.import_module(RECONCILIATION_MODULE)


def reconciliation_attribute(name: str) -> Any:
    attribute = getattr(reconciliation_module(), name, None)
    if attribute is None:
        pytest.fail(f"{RECONCILIATION_MODULE} has no {name} yet.")
    return attribute


def latest_step_supported() -> bool:
    """Whether the command takes the release run's latest step yet."""

    parameters = inspect.signature(
        reconciliation_module().reconcile_calibration_trees
    ).parameters
    return "follow_latest" in parameters


def require_latest_step() -> None:
    """Fails, rather than raising TypeError, while the command has no latest
    step."""

    if not latest_step_supported():
        pytest.fail("reconcile_calibration_trees takes no follow_latest yet.")


# Hugging Face -------------------------------------------------------------------


def releases_json(
    builds: Mapping[str, tuple[str, bool]],
    *,
    current: str | None = CURRENT_BUILD,
    release: str = US_RELEASE,
    default: str | None = US_RELEASE,
) -> bytes:
    """A record of one release whose builds map to (published_at, withdrawn)."""

    return json.dumps(
        {
            "schema_version": 1,
            "default": default,
            "releases": {
                release: {
                    "role": "national",
                    "current": current,
                    "builds": {
                        build: {"published_at": published_at, "withdrawn": withdrawn}
                        for build, (published_at, withdrawn) in builds.items()
                    },
                }
            },
        }
    ).encode()


TWO_BUILDS = {
    OLDER_BUILD: ("2026-10-19T08:33:23Z", True),
    CURRENT_BUILD: ("2026-11-01T12:00:00+00:00", False),
}


def build_folder(
    release: str,
    build: str,
    *,
    names: tuple[str, str] | None = None,
    manifest: bytes | None = None,
    diagnostics: bool = True,
) -> dict[str, bytes]:
    """A build folder whose release manifest names ``names`` (its own by default)."""

    folder = f"releases/{release}/{build}"
    named_release, named_build = names or (release, build)
    files = {
        f"{folder}/build_manifest.json": BUILD_MANIFEST,
        f"{folder}/release_manifest.json": (
            manifest
            if manifest is not None
            else json.dumps({"release": named_release, "build": named_build}).encode()
        ),
    }
    if diagnostics:
        files[f"{folder}/calibration_diagnostics.json"] = DIAGNOSTICS
    return files


def structured_staging_run(
    run_id: str,
    *,
    status: str = "completed",
    candidate: str | None = US_RELEASE,
    progress_updated_at: str | None = "2026-11-02T09:15:00Z",
    manifest_updated_at: str | None = "2026-11-02T09:00:00Z",
    manifest_run_id: str | None = None,
    artifacts: Mapping[str, bytes] | None = None,
    declared: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    """A schema-2 run with its status, candidate release and times, whose
    manifest declares ``declared`` (``artifacts`` unless given) and whose
    folder holds ``artifacts`` (its calibration diagnostics unless given)."""

    folder = f"runs/{run_id}"
    files = structured_run(
        run_id,
        {"calibration_diagnostics": DIAGNOSTICS} if artifacts is None else artifacts,
        declared=declared,
    )
    manifest = json.loads(files[f"{folder}/run_manifest.json"])
    progress = json.loads(files[f"{folder}/progress.json"])
    for document, updated_at in (
        (manifest, manifest_updated_at),
        (progress, progress_updated_at),
    ):
        document["status"] = status
        if candidate is not None:
            document["candidate_id"] = candidate
        if updated_at is not None:
            document["updated_at"] = updated_at
    if manifest_run_id is not None:
        manifest["run_id"] = manifest_run_id
    files[f"{folder}/run_manifest.json"] = json.dumps(manifest).encode()
    files[f"{folder}/progress.json"] = json.dumps(progress).encode()
    return files


def at_head(hugging_face: FakeHuggingFace, dataset: str, files: Mapping[str, bytes]):
    hugging_face.datasets[dataset].files.update(files)


def head_commit(hugging_face: FakeHuggingFace, dataset: str) -> str:
    return hugging_face.datasets[dataset].head_commit


def requested_paths(hugging_face: FakeHuggingFace) -> list[str]:
    return [request.url.path for request in hugging_face.requests]


# The run ------------------------------------------------------------------------


@dataclass
class InProgress:
    """Stands in for the submissions table: (country, folder) pairs a
    ``queued`` or ``processing`` calibration-diagnostics submission holds."""

    folders: set[tuple[str, str]] = field(default_factory=set)
    asked: list[tuple[str, str]] = field(default_factory=list)

    def in_progress(self, country: str, folder: str) -> bool:
        self.asked.append((country, folder))
        return (country, folder) in self.folders


def recorded_release_build(
    bun: StandInBun, country: str, release_id: str
) -> str | None:
    """The tree recorded for a release ID: one this run published, or one the
    store's manifest held."""

    for call in bun.steps("publication"):
        if call["source"] == release_id and not bun.publication_exits.get(release_id):
            return call["publication"]["worker"]["buildArtifactId"]
    stored = bun.store["countries"].get(country, {}).get("builds", [])
    return next(
        (
            build["buildArtifactId"]
            for build in stored
            if build.get("kind") == "release" and build.get("releaseId") == release_id
        ),
        None,
    )


@dataclass
class StandInLatestCommand:
    """Stands in for the tree package's latest command, which points a
    country's latest at the tree recorded for a release ID, as
    ``follow_releases_record`` runs it.

    It answers exit 2 when no tree is recorded for the build, and ``moved``
    otherwise; ``exit_code`` forces another exit.
    """

    exit_code: int | None = None
    moved: bool = True

    def run(
        self,
        bun: StandInBun,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert Path(argv[2]).name == "calibration-tree-latest.ts"
        request = json.loads(Path(argv[3]).read_text())
        bun.events.append(f"latest {request['releaseId']}")
        bun.calls.append(
            {"step": "latest", "argv": list(argv), "env": dict(env), "request": request}
        )
        if self.exit_code is not None:
            return subprocess.CompletedProcess(
                argv, self.exit_code, "", "The latest command stopped."
            )
        recorded = recorded_release_build(bun, request["country"], request["releaseId"])
        if recorded is None:
            return subprocess.CompletedProcess(
                argv,
                2,
                "",
                f"The manifest records no {request['country']} release build "
                f"{request['releaseId']}.",
            )
        answer = {
            "country": request["country"],
            "releaseId": request["releaseId"],
            "buildArtifactId": recorded,
            "moved": self.moved,
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")


@dataclass
class Reconciliation:
    hugging_face: FakeHuggingFace = field(default_factory=lambda: hugging_face_with({}))
    bun: StandInBun = field(default_factory=StandInBun)
    submissions: InProgress = field(default_factory=InProgress)
    clock: ManualClock = field(default_factory=ManualClock)
    #: (country, kind) pairs whose lock another execution holds past the wait.
    held_locks: set[tuple[str, str]] = field(default_factory=set)
    latest: StandInLatestCommand = field(default_factory=StandInLatestCommand)
    #: What the last run's release kind reported of its latest step.
    latest_steps: list[Any] = field(default_factory=list)

    def lock(self, country: str, kind: str) -> Any:
        @contextmanager
        def held() -> Iterator[None]:
            if (country, kind) in self.held_locks:
                raise reconciliation_attribute("ReconciliationLockTimeoutError")(
                    f"{country} {kind} is locked."
                )
            self.bun.events.append(f"{kind} lock taken")
            yield
            self.bun.events.append(f"{kind} lock released")

        return held()

    def run(
        self, country: str = "us", kinds: Sequence[str] = ("release",), **options: Any
    ) -> list[Any]:
        module = reconciliation_module()

        @contextmanager
        def manifest_lock() -> Iterator[None]:
            self.bun.events.append("manifest lock taken")
            yield
            self.bun.events.append("manifest lock released")

        commands = tree_commands(
            blob_token=BLOB_TOKEN,
            manifest_lock=manifest_lock,
            bun="/usr/local/bin/bun",
            run_command=self.bun,
        )
        reader = HuggingFaceReader(
            HuggingFaceReadSettings(token=READ_TOKEN),
            client=self.hugging_face.client(),
        )
        if latest_step_supported() and "follow_latest" not in options:

            def run_latest_command(
                argv: Sequence[str], env: Mapping[str, str], timeout: float
            ) -> subprocess.CompletedProcess[str]:
                return self.latest.run(self.bun, argv, env, timeout)

            # main's own function, with the run's reader and manifest lock.
            options["follow_latest"] = functools.partial(
                follow_releases_record,
                blob_token=BLOB_TOKEN,
                manifest_lock=manifest_lock,
                reader=reader,
                bun="/usr/local/bin/bun",
                run_command=run_latest_command,
            )
        produced = list(
            module.reconcile_calibration_trees(
                country=country,
                kinds=kinds,
                reader=reader,
                commands=commands,
                submissions=self.submissions,
                lock=self.lock,
                clock=self.clock,
                **options,
            )
        )
        self.latest_steps = [
            item for item in produced if not isinstance(item, module.ReconcileOutcome)
        ]
        return [item for item in produced if isinstance(item, module.ReconcileOutcome)]


def outcomes_by_source(outcomes: Sequence[Any]) -> list[tuple[str | None, str]]:
    return [(outcome.source_id, outcome.outcome) for outcome in outcomes]


def outcome_of(outcomes: Sequence[Any], source_id: str) -> Any:
    [outcome] = [outcome for outcome in outcomes if outcome.source_id == source_id]
    return outcome


def us_release_reconciliation(
    record: bytes | None = None, folders: Mapping[str, bytes] | None = None
) -> Reconciliation:
    reconciliation = Reconciliation()
    files = dict(
        folders
        if folders is not None
        else {
            **build_folder(US_RELEASE, OLDER_BUILD),
            **build_folder(US_RELEASE, CURRENT_BUILD),
        }
    )
    files["releases.json"] = record if record is not None else releases_json(TWO_BUILDS)
    at_head(reconciliation.hugging_face, US_RELEASES, files)
    return reconciliation


# Release runs ------------------------------------------------------------------


def test_every_build_releases_json_lists_is_built_the_current_default_first() -> None:
    reconciliation = us_release_reconciliation()

    outcomes = reconciliation.run()

    assert outcomes_by_source(outcomes) == [
        (CURRENT_ID, "built"),
        (OLDER_ID, "built"),
    ]
    assert outcome_of(outcomes, CURRENT_ID).current_default is True
    # A withdrawn build is built too, as the publisher builds it.
    assert outcome_of(outcomes, OLDER_ID).withdrawn is True
    assert outcome_of(outcomes, OLDER_ID).current_default is False
    assert [call["source"] for call in reconciliation.bun.steps("build")] == [
        CURRENT_ID,
        OLDER_ID,
    ]
    head = head_commit(reconciliation.hugging_face, US_RELEASES)
    request = reconciliation.bun.build_of(CURRENT_ID)["request"]
    assert request["kind"] == "release"
    assert request["releaseId"] == CURRENT_ID
    assert request["releaseDirectory"] == CURRENT_ID
    assert request["hfRepo"] == US_RELEASES
    assert request["hfCommitSha"] == head
    assert request["createdAt"] == "2026-11-01T12:00:00.000Z"
    assert reconciliation.bun.build_of(OLDER_ID)["request"]["createdAt"] == (
        "2026-10-19T08:33:23.000Z"
    )
    assert set(request["artifacts"]) >= {"calibrationDiagnostics", "releaseManifest"}


def test_nothing_this_command_publishes_moves_the_latest() -> None:
    reconciliation = us_release_reconciliation()

    reconciliation.run()

    assert [
        call["publication"]["makeLatest"]
        for call in reconciliation.bun.steps("publication")
    ] == [False, False]


def test_a_build_the_store_holds_is_already_stored_with_no_further_read() -> None:
    reconciliation = us_release_reconciliation()
    stored = {
        "buildArtifactId": "a" * 64,
        "kind": "release",
        "releaseId": OLDER_ID,
        "stagingRunId": None,
        "hfRepo": US_RELEASES,
        "hfCommitSha": "9" * 40,
        "createdAt": "2026-10-19T08:33:23.000Z",
    }
    reconciliation.bun.store = store_manifest("us", [stored])

    outcomes = reconciliation.run()

    assert outcomes_by_source(outcomes) == [
        (CURRENT_ID, "built"),
        (OLDER_ID, "already_stored"),
    ]
    assert outcome_of(outcomes, OLDER_ID).build_artifact_id == "a" * 64
    assert not any(
        OLDER_BUILD in path for path in requested_paths(reconciliation.hugging_face)
    )
    assert [call["source"] for call in reconciliation.bun.steps("build")] == [
        CURRENT_ID
    ]


def test_a_build_s_commit_is_the_newest_last_commit_of_its_folder_s_files() -> None:
    folder = f"releases/{US_RELEASE}/{CURRENT_BUILD}"
    files = build_folder(US_RELEASE, CURRENT_BUILD)
    reconciliation = us_release_reconciliation(
        releases_json({CURRENT_BUILD: ("2026-11-01T12:00:00Z", False)}), files
    )
    dataset = reconciliation.hugging_face.datasets[US_RELEASES]
    dataset.last_commits.update(
        {
            f"{folder}/calibration_diagnostics.json": (
                FOLDER_COMMIT,
                "2026-11-01T11:00:00.000Z",
            ),
            f"{folder}/release_manifest.json": (
                NEWER_FOLDER_COMMIT,
                "2026-11-01T11:30:00.000Z",
            ),
            f"{folder}/build_manifest.json": (
                FOLDER_COMMIT,
                "2026-11-01T10:00:00.000Z",
            ),
        }
    )
    dataset.revisions[NEWER_FOLDER_COMMIT] = dict(files)

    [outcome] = reconciliation.run()

    assert outcome.outcome == "built"
    assert outcome.hf_commit == NEWER_FOLDER_COMMIT
    assert reconciliation.bun.build_of(CURRENT_ID)["request"]["hfCommitSha"] == (
        NEWER_FOLDER_COMMIT
    )
    assert (
        f"/datasets/{US_RELEASES}/resolve/{NEWER_FOLDER_COMMIT}/"
        f"{folder}/release_manifest.json"
    ) in requested_paths(reconciliation.hugging_face)


def test_a_build_folder_without_calibration_diagnostics_is_ineligible() -> None:
    reconciliation = us_release_reconciliation(
        releases_json({CURRENT_BUILD: ("2026-11-01T12:00:00Z", False)}),
        build_folder(US_RELEASE, CURRENT_BUILD, diagnostics=False),
    )

    [outcome] = reconciliation.run()

    assert outcome.outcome == "ineligible"
    assert "calibration diagnostics" in outcome.reason
    assert outcome.incomplete is False
    assert reconciliation.bun.steps("build") == []


def test_a_build_releases_json_lists_without_a_folder_is_ineligible() -> None:
    reconciliation = us_release_reconciliation(
        releases_json({CURRENT_BUILD: ("2026-11-01T12:00:00Z", False)}), {}
    )

    [outcome] = reconciliation.run()

    assert outcome.outcome == "ineligible"
    assert outcome.incomplete is False


@pytest.mark.parametrize(
    "folder",
    [
        build_folder(US_RELEASE, CURRENT_BUILD, names=(US_RELEASE, OLDER_BUILD)),
        build_folder(
            US_RELEASE, CURRENT_BUILD, names=("microcosm-us-2024", CURRENT_BUILD)
        ),
        build_folder(
            US_RELEASE, CURRENT_BUILD, manifest=b'{"build": {"build_id": "x"}}'
        ),
        build_folder(US_RELEASE, CURRENT_BUILD, manifest=b"not json"),
        {
            path: content
            for path, content in build_folder(US_RELEASE, CURRENT_BUILD).items()
            if not path.endswith("release_manifest.json")
        },
    ],
    ids=["another build", "another release", "old form", "not json", "missing"],
)
def test_a_release_manifest_that_does_not_name_the_build_makes_it_ineligible(
    folder: dict[str, bytes],
) -> None:
    reconciliation = us_release_reconciliation(
        releases_json({CURRENT_BUILD: ("2026-11-01T12:00:00Z", False)}), folder
    )

    [outcome] = reconciliation.run()

    assert outcome.outcome == "ineligible"
    assert "release_manifest.json" in outcome.reason
    assert reconciliation.bun.steps("build") == []


@pytest.mark.parametrize(
    ("published_at", "recorded"),
    [
        ("2026-11-01T12:00:00Z", "2026-11-01T12:00:00.000Z"),
        ("2026-11-01T13:00:00+01:00", "2026-11-01T12:00:00.000Z"),
        ("2026-11-01T12:00:00.123456Z", "2026-11-01T12:00:00.123Z"),
        ("2026-11-01T12:00:00", "2026-11-01T12:00:00.000Z"),
        ("not a time", None),
    ],
)
def test_published_at_becomes_the_tree_s_time_as_the_publisher_records_it(
    published_at: str, recorded: str | None
) -> None:
    assert reconciliation_attribute("publication_time")(published_at) == recorded


def test_a_repo_without_releases_json_builds_nothing() -> None:
    reconciliation = Reconciliation()
    at_head(
        reconciliation.hugging_face,
        BE_RELEASES,
        {"releases/microcosm-be-2025/calibration_diagnostics.json": DIAGNOSTICS},
    )
    reconciliation.bun.store = store_manifest("be")

    outcomes = reconciliation.run(country="be")

    assert [(outcome.kind, outcome.outcome) for outcome in outcomes] == [
        ("release", "no_releases_record")
    ]
    assert outcomes[0].incomplete is False
    assert reconciliation.bun.steps("build") == []
    assert not any(
        "/tree/" in path for path in requested_paths(reconciliation.hugging_face)
    )


def test_a_releases_json_the_rules_refuse_builds_nothing() -> None:
    reconciliation = us_release_reconciliation(
        releases_json(TWO_BUILDS, current="us-calibration-attempt-unlisted")
    )

    [outcome] = reconciliation.run()

    assert outcome.outcome == "releases_record_refused"
    assert "is not listed" in outcome.reason
    assert outcome.incomplete is False
    assert reconciliation.bun.steps("build") == []


def test_a_failed_read_of_the_release_repo_is_an_error_and_the_run_incomplete() -> None:
    reconciliation = us_release_reconciliation()
    reconciliation.hugging_face.datasets[US_RELEASES].forced_status = 500

    outcomes = reconciliation.run()

    assert [(outcome.kind, outcome.outcome) for outcome in outcomes] == [
        ("release", "error")
    ]
    assert outcomes[0].incomplete is True
    assert (
        reconciliation_attribute("summarize")(
            outcomes, country="us", kinds=["release"], request_id=None, dry_run=False
        )["incomplete"]
        is True
    )


def test_one_build_s_failed_read_does_not_stop_the_others() -> None:
    reconciliation = us_release_reconciliation()
    reconciliation.hugging_face.datasets[US_RELEASES].file_answers[
        f"releases/{OLDER_ID}/release_manifest.json"
    ] = (500, None)

    outcomes = reconciliation.run()

    assert outcomes_by_source(outcomes) == [(CURRENT_ID, "built"), (OLDER_ID, "error")]
    assert outcome_of(outcomes, OLDER_ID).incomplete is True


def test_a_build_a_submission_is_still_processing_is_skipped() -> None:
    reconciliation = us_release_reconciliation()
    reconciliation.submissions.folders.add(("us", f"releases/{CURRENT_ID}"))

    outcomes = reconciliation.run()

    assert outcomes_by_source(outcomes) == [
        (CURRENT_ID, "submission_in_progress"),
        (OLDER_ID, "built"),
    ]
    assert outcome_of(outcomes, CURRENT_ID).incomplete is False
    assert ("us", f"releases/{OLDER_ID}") in reconciliation.submissions.asked


def test_a_release_run_s_summary_reports_its_latest_step() -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()
    outcomes = reconciliation.run()
    [step] = reconciliation.latest_steps

    summary = reconciliation_attribute("summarize")(
        [*outcomes, step],
        country="us",
        kinds=["release"],
        request_id="calibration-trees-us-release-2960000",
        dry_run=False,
    )

    assert summary == {
        "country": "us",
        "kinds": ["release"],
        "request_id": "calibration-trees-us-release-2960000",
        "dry_run": False,
        # Builds only: the latest step is reported on its own.
        "outcomes": {"built": 2},
        "incomplete": False,
        "incomplete_sources": [],
        "latest": step.to_json()["latest"],
    }


# The latest step ------------------------------------------------------------------


def test_a_release_run_ends_by_following_releases_json_inside_its_lock() -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()

    reconciliation.run()

    # After the run's builds, under the manifest lock that
    # follow_releases_record takes itself, and inside the run's own lock.
    assert reconciliation.bun.events == [
        "release lock taken",
        "manifest read",
        f"build {CURRENT_ID}",
        "manifest lock taken",
        f"publication {CURRENT_ID}",
        "manifest lock released",
        f"build {OLDER_ID}",
        "manifest lock taken",
        f"publication {OLDER_ID}",
        "manifest lock released",
        "manifest lock taken",
        f"latest {CURRENT_ID}",
        "manifest lock released",
        "release lock released",
    ]
    [latest_call] = reconciliation.bun.steps("latest")
    assert latest_call["request"] == {"country": "us", "releaseId": CURRENT_ID}


@pytest.mark.parametrize("moved", [True, False])
def test_the_latest_step_reports_what_moved(moved: bool) -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()
    reconciliation.latest.moved = moved

    reconciliation.run()

    [step] = reconciliation.latest_steps
    assert step.to_json() == {
        "latest": {
            "country": "us",
            "outcome": "followed",
            "current_build": CURRENT_ID,
            "build_artifact_id": build_id("us", "release", CURRENT_ID),
            "made_latest": True,
            "latest_moved": moved,
            "incomplete": False,
        }
    }
    assert step.incomplete is False


def test_the_latest_step_says_why_the_latest_stayed() -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()
    # The current default build can't be built, so no tree is recorded for it.
    reconciliation.bun.worker_exits[CURRENT_ID] = 2

    reconciliation.run()

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert latest["outcome"] == "followed"
    assert latest["current_build"] == CURRENT_ID
    assert latest["made_latest"] is False
    assert latest["latest_moved"] is False
    assert latest["latest_unchanged"] == "no_recorded_tree"
    assert CURRENT_ID in latest["latest_unchanged_reason"]
    assert latest["incomplete"] is False


def test_a_releases_json_the_rules_refuse_leaves_the_latest_with_its_reason() -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation(
        releases_json(TWO_BUILDS, current="us-calibration-attempt-unlisted")
    )

    reconciliation.run()

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert latest["outcome"] == "followed"
    assert latest["latest_unchanged"] == "releases_record_refused"
    assert "is not listed" in latest["latest_unchanged_reason"]
    assert latest["incomplete"] is False
    assert reconciliation.bun.steps("latest") == []


def test_a_country_without_releases_json_skips_the_latest_step() -> None:
    require_latest_step()
    reconciliation = Reconciliation()
    reconciliation.bun.store = store_manifest("be")

    reconciliation.run(country="be")

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert "releases.json" in latest.pop("reason")
    assert latest == {
        "country": "be",
        "outcome": "skipped",
        "skipped": "no_releases_record",
        "incomplete": False,
    }
    assert reconciliation.bun.steps("latest") == []
    assert "manifest lock taken" not in reconciliation.bun.events


def test_a_dry_run_skips_the_latest_step() -> None:
    require_latest_step()
    reconciliation = both_kinds()

    reconciliation.run(kinds=["release", "staging"], dry_run=True)

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert (latest["outcome"], latest["skipped"], latest["incomplete"]) == (
        "skipped",
        "dry_run",
        False,
    )
    assert reconciliation.bun.events == ["manifest read", "manifest read"]


def test_a_lock_wait_that_expires_skips_the_latest_step() -> None:
    require_latest_step()
    reconciliation = both_kinds()
    reconciliation.held_locks.add(("us", "release"))

    outcomes = reconciliation.run(kinds=["release", "staging"])

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert (latest["outcome"], latest["skipped"]) == ("skipped", "lock_wait_expired")
    assert reconciliation.bun.steps("latest") == []
    # The lock's own outcome leaves the run incomplete, so Cloud Run retries.
    assert outcomes[0].incomplete is True


def test_the_latest_step_runs_after_the_time_limit() -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()
    reconciliation.bun.clock = reconciliation.clock
    reconciliation.bun.seconds_per_build = 45 * MINUTE

    outcomes = reconciliation.run(time_limit_seconds=40 * MINUTE)

    assert outcome_of(outcomes, OLDER_ID).outcome == "deferred"
    [step] = reconciliation.latest_steps
    assert step.to_json()["latest"]["outcome"] == "followed"
    assert step.to_json()["latest"]["made_latest"] is True


@pytest.mark.parametrize("exit_code", [1, 3])
def test_a_latest_command_that_fails_leaves_the_run_incomplete(exit_code: int) -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()
    reconciliation.latest.exit_code = exit_code

    outcomes = reconciliation.run()

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert latest["outcome"] == "error"
    assert "CalibrationTreeCommandError" in latest["reason"]
    assert latest["incomplete"] is True
    summary = reconciliation_attribute("summarize")(
        [*outcomes, step],
        country="us",
        kinds=["release"],
        request_id=None,
        dry_run=False,
    )
    assert summary["incomplete"] is True
    assert summary["latest"]["outcome"] == "error"


def test_any_other_failure_of_the_latest_step_leaves_the_run_incomplete() -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()

    def unavailable(country: str) -> FollowedLatest:
        raise RuntimeError("The database is unavailable.")

    reconciliation.run(follow_latest=unavailable)

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert latest["outcome"] == "error"
    assert "The database is unavailable." in latest["reason"]
    assert latest["incomplete"] is True


def test_a_country_the_collector_does_not_list_is_an_error_no_retry_gets_past() -> None:
    require_latest_step()
    reconciliation = us_release_reconciliation()

    def not_listed(country: str) -> FollowedLatest:
        raise ValueError(f"{country} is not a country the collector lists.")

    outcomes = reconciliation.run(follow_latest=not_listed)

    [step] = reconciliation.latest_steps
    latest = step.to_json()["latest"]
    assert latest["outcome"] == "error"
    assert latest["incomplete"] is False
    summary = reconciliation_attribute("summarize")(
        [*outcomes, step],
        country="us",
        kinds=["release"],
        request_id=None,
        dry_run=False,
    )
    assert summary["incomplete"] is False


def test_a_staging_run_has_no_latest_step() -> None:
    require_latest_step()
    reconciliation = us_staging_reconciliation(structured_staging_run(RUN_ID))

    outcomes = reconciliation.run(kinds=["staging"])

    assert reconciliation.latest_steps == []
    assert reconciliation.bun.steps("latest") == []
    summary = reconciliation_attribute("summarize")(
        outcomes, country="us", kinds=["staging"], request_id=None, dry_run=False
    )
    assert summary["latest"] is None


# Staging runs ------------------------------------------------------------------


def us_staging_reconciliation(files: Mapping[str, bytes]) -> Reconciliation:
    reconciliation = Reconciliation()
    at_head(reconciliation.hugging_face, US_STAGING, files)
    return reconciliation


def test_a_finalized_run_is_built_with_the_publisher_s_label_and_time() -> None:
    reconciliation = us_staging_reconciliation(structured_staging_run(RUN_ID))

    [outcome] = reconciliation.run(kinds=["staging"])

    assert (outcome.kind, outcome.source_id, outcome.outcome) == (
        "staging",
        RUN_ID,
        "built",
    )
    request = reconciliation.bun.build_of(RUN_ID)["request"]
    assert request["kind"] == "staging"
    assert request["runId"] == RUN_ID
    assert request["label"] == US_RELEASE
    # The run's progress time, before its manifest's.
    assert request["createdAt"] == "2026-11-02T09:15:00.000Z"
    assert request["hfRepo"] == US_STAGING
    assert request["hfCommitSha"] == head_commit(
        reconciliation.hugging_face, US_STAGING
    )
    assert reconciliation.bun.publication_of(RUN_ID)["publication"]["makeLatest"] is (
        False
    )


def test_a_run_without_a_candidate_or_a_progress_time_uses_its_id_and_manifest() -> (
    None
):
    reconciliation = us_staging_reconciliation(
        structured_staging_run(RUN_ID, candidate=None, progress_updated_at=None)
    )

    reconciliation.run(kinds=["staging"])

    request = reconciliation.bun.build_of(RUN_ID)["request"]
    assert request["label"] == RUN_ID
    assert request["createdAt"] == "2026-11-02T09:00:00.000Z"


@pytest.mark.parametrize("status", ["running", "failed", "blocked"])
def test_a_run_that_is_not_final_is_ineligible(status: str) -> None:
    reconciliation = us_staging_reconciliation(
        structured_staging_run(RUN_ID, status=status)
    )

    [outcome] = reconciliation.run(kinds=["staging"])

    assert outcome.outcome == "ineligible"
    assert status in outcome.reason
    assert reconciliation.bun.steps("build") == []


def test_a_schema_1_run_s_status_comes_from_its_progress() -> None:
    running = schema_1_run(OTHER_RUN_ID)
    running[f"runs/{OTHER_RUN_ID}/progress.json"] = json.dumps(
        {"schema_version": 1, "run_id": OTHER_RUN_ID, "status": "running"}
    ).encode()
    reconciliation = us_staging_reconciliation({**schema_1_run(RUN_ID), **running})

    outcomes = reconciliation.run(kinds=["staging"])

    assert outcome_of(outcomes, RUN_ID).outcome == "built"
    assert outcome_of(outcomes, OTHER_RUN_ID).outcome == "ineligible"
    assert reconciliation.bun.build_of(RUN_ID)["request"]["label"] == RUN_ID


def test_a_schema_1_run_s_status_in_runs_json_comes_before_its_progress() -> None:
    run = schema_1_run(RUN_ID)
    run[f"runs/{RUN_ID}/progress.json"] = json.dumps(
        {"schema_version": 1, "run_id": RUN_ID, "status": "running"}
    ).encode()
    run["runs.json"] = json.dumps(
        {"schema_version": 1, "runs": [{"run_id": RUN_ID, "status": "published"}]}
    ).encode()
    reconciliation = us_staging_reconciliation(run)

    [outcome] = reconciliation.run(kinds=["staging"])

    assert outcome.outcome == "built"


def schema_1_folder_without_manifest(
    run_id: str, *, status: str | None = "completed"
) -> dict[str, bytes]:
    """A schema-1 run folder with no run manifest of its own. Its status is in
    its ``progress.json``, or, when ``status`` is None, only in ``runs.json``."""

    folder = f"runs/{run_id}"
    files = {
        f"{folder}/calibration_diagnostics.json": DIAGNOSTICS,
        f"{folder}/build_manifest.json": BUILD_MANIFEST,
    }
    if status is not None:
        files[f"{folder}/progress.json"] = json.dumps(
            {"schema_version": 1, "run_id": run_id, "status": status}
        ).encode()
    return files


def schema_1_index(statuses: Mapping[str, str]) -> dict[str, bytes]:
    """A schema-1 ``runs.json`` recording each run's status."""

    rows = [{"run_id": run_id, "status": status} for run_id, status in statuses.items()]
    return {"runs.json": json.dumps({"schema_version": 1, "runs": rows}).encode()}


def stored_staging_build(run_id: str) -> dict[str, Any]:
    return {
        "buildArtifactId": "b" * 64,
        "kind": "staging",
        "releaseId": None,
        "stagingRunId": run_id,
        "hfRepo": US_STAGING,
        "hfCommitSha": "9" * 40,
        "createdAt": "2026-11-02T09:15:00.000Z",
    }


@pytest.mark.parametrize("status_in", ["progress.json", "runs.json"])
def test_a_folder_without_a_manifest_is_listed_once_every_schema_1_run_is_stored(
    status_in: str,
) -> None:
    # Review round 1, bug 1: runs.json was read only beside a schema-1 run
    # manifest of a run the store lacks, so this folder was dropped.
    in_index = status_in == "runs.json"
    reconciliation = us_staging_reconciliation(
        {
            **schema_1_run(RUN_ID),
            **schema_1_folder_without_manifest(
                OTHER_RUN_ID, status=None if in_index else "completed"
            ),
            **schema_1_index(
                {RUN_ID: "passed", **({OTHER_RUN_ID: "published"} if in_index else {})}
            ),
        }
    )
    reconciliation.bun.store = store_manifest("us", [stored_staging_build(RUN_ID)])

    outcomes = reconciliation.run(kinds=["staging"])

    assert outcomes_by_source(outcomes) == [
        (RUN_ID, "already_stored"),
        (OTHER_RUN_ID, "built"),
    ]
    request = reconciliation.bun.build_of(OTHER_RUN_ID)["request"]
    assert request["label"] == OTHER_RUN_ID
    assert request["artifacts"]["calibrationDiagnostics"]["path"] == (
        f"runs/{OTHER_RUN_ID}/calibration_diagnostics.json"
    )
    # The stored run's manifest is still never read (S7).
    assert f"runs/{RUN_ID}/run_manifest.json" not in (
        reconciliation.hugging_face.downloaded_paths()
    )


def test_a_folder_without_a_manifest_beside_a_schema_1_run_is_a_schema_1_run() -> None:
    reconciliation = us_staging_reconciliation(
        {
            **schema_1_run(RUN_ID),
            **schema_1_folder_without_manifest(OTHER_RUN_ID),
            **schema_1_index({RUN_ID: "passed"}),
        }
    )

    outcomes = reconciliation.run(kinds=["staging"])

    assert sorted(outcomes_by_source(outcomes)) == sorted(
        [(RUN_ID, "built"), (OTHER_RUN_ID, "built")]
    )


@pytest.mark.parametrize(
    "index",
    [None, {"schema_version": 2, "runs": []}],
    ids=["no-runs-json", "structured-runs-json"],
)
def test_without_a_schema_1_runs_json_a_folder_without_a_manifest_is_not_a_run(
    index: dict[str, Any] | None,
) -> None:
    files = {
        **structured_staging_run(RUN_ID),
        **schema_1_folder_without_manifest(OTHER_RUN_ID),
    }
    if index is not None:
        files["runs.json"] = json.dumps(index).encode()
    reconciliation = us_staging_reconciliation(files)

    outcomes = reconciliation.run(kinds=["staging"])

    assert outcomes_by_source(outcomes) == [(RUN_ID, "built")]


def test_a_run_the_store_holds_is_already_stored_without_reading_its_files() -> None:
    reconciliation = us_staging_reconciliation(
        {**structured_staging_run(RUN_ID), **structured_staging_run(OTHER_RUN_ID)}
    )
    reconciliation.bun.store = store_manifest(
        "us",
        [
            {
                "buildArtifactId": "b" * 64,
                "kind": "staging",
                "releaseId": None,
                "stagingRunId": RUN_ID,
                "hfRepo": US_STAGING,
                "hfCommitSha": "9" * 40,
                "createdAt": "2026-11-02T09:15:00.000Z",
            }
        ],
    )

    outcomes = reconciliation.run(kinds=["staging"])

    assert outcome_of(outcomes, RUN_ID).outcome == "already_stored"
    assert outcome_of(outcomes, OTHER_RUN_ID).outcome == "built"
    assert not any(
        f"runs/{RUN_ID}/" in path
        for path in reconciliation.hugging_face.downloaded_paths()
    )


@pytest.mark.parametrize(
    "incompatible",
    [
        structured_staging_run(RUN_ID, manifest_run_id=OTHER_RUN_ID),
        {f"runs/{RUN_ID}/run_manifest.json": b"not json"},
        {
            f"runs/{RUN_ID}/run_manifest.json": json.dumps(
                {"schema_version": 4, "run_id": RUN_ID, "status": "completed"}
            ).encode()
        },
        {
            f"runs/{RUN_ID}/run_manifest.json": json.dumps(
                {"schema_version": 2, "run_id": RUN_ID, "status": "completed"}
            ).encode()
        },
    ],
    ids=["names another run", "not json", "unknown version", "no schema name"],
)
def test_an_incompatible_run_is_reported_on_its_own_and_the_others_continue(
    incompatible: dict[str, bytes],
) -> None:
    reconciliation = us_staging_reconciliation(
        {**incompatible, **structured_staging_run(OTHER_RUN_ID)}
    )

    outcomes = reconciliation.run(kinds=["staging"])

    assert outcome_of(outcomes, RUN_ID).outcome == "unbuildable"
    assert outcome_of(outcomes, RUN_ID).incomplete is False
    assert outcome_of(outcomes, OTHER_RUN_ID).outcome == "built"


BUNDLE_COMMIT = "5" * 40


def staged_dataset_receipt(**changes: Any) -> bytes:
    """A run's staged_dataset receipt for a bundle in the US release repo."""

    return json.dumps(
        {
            "status": "uploaded",
            "run_id": RUN_ID,
            RECORD_REPO_KEY: US_RELEASES,
            "prefix": f"staged/{RUN_ID}",
            "revision": BUNDLE_COMMIT,
            "files": {"calibration_diagnostics.json": {"sha256": sha256(DIAGNOSTICS)}},
            **changes,
        }
    ).encode()


def without_diagnostics(files: Mapping[str, bytes]) -> dict[str, bytes]:
    return {
        path: content
        for path, content in files.items()
        if not path.endswith("calibration_diagnostics.json")
    }


# Review round 1, bug 2: the publisher's resolveStagingCalibrationSource finds
# no calibration source for these final runs, and reconcileStagingBuilds
# reports them ineligible ("Finalized staging run has no calibration
# diagnostics.").
RUNS_WITHOUT_DIAGNOSTICS = {
    "schema-1-run-without-diagnostics": without_diagnostics(schema_1_run(RUN_ID)),
    "structured-run-declaring-neither": structured_staging_run(RUN_ID, artifacts={}),
    "receipt-of-a-skipped-staging": structured_staging_run(
        RUN_ID,
        artifacts={"staged_dataset": staged_dataset_receipt(status="skipped")},
    ),
    "receipt-recording-no-diagnostics": structured_staging_run(
        RUN_ID,
        artifacts={
            "staged_dataset": staged_dataset_receipt(
                files={"microcosm_us_2025.h5": {"sha256": sha256(b"rows")}}
            )
        },
    ),
}


@pytest.mark.parametrize("case", sorted(RUNS_WITHOUT_DIAGNOSTICS))
def test_a_final_run_without_calibration_diagnostics_is_ineligible(case: str) -> None:
    reconciliation = us_staging_reconciliation(RUNS_WITHOUT_DIAGNOSTICS[case])

    [outcome] = reconciliation.run(kinds=["staging"])

    assert (outcome.source_id, outcome.outcome) == (RUN_ID, "ineligible")
    assert outcome.reason.startswith(
        "Finalized staging run has no calibration diagnostics"
    )
    assert outcome.incomplete is False
    assert reconciliation.bun.steps("build") == []


def test_a_dry_run_reports_a_final_run_without_diagnostics_as_ineligible() -> None:
    reconciliation = us_staging_reconciliation(
        RUNS_WITHOUT_DIAGNOSTICS["structured-run-declaring-neither"]
    )

    [outcome] = reconciliation.run(kinds=["staging"], dry_run=True)

    assert outcome.outcome == "ineligible"


DAMAGED_RUNS = {
    "declared-diagnostics-absent": structured_staging_run(
        RUN_ID, artifacts={}, declared={"calibration_diagnostics": DIAGNOSTICS}
    ),
    "recorded-bundle-file-absent": structured_staging_run(
        RUN_ID, artifacts={"staged_dataset": staged_dataset_receipt()}
    ),
}


@pytest.mark.parametrize("case", sorted(DAMAGED_RUNS))
def test_a_final_run_whose_declared_diagnostics_are_absent_stays_unbuildable(
    case: str,
) -> None:
    reconciliation = us_staging_reconciliation(DAMAGED_RUNS[case])
    # The bundle's commit exists, without the diagnostics its receipt records.
    reconciliation.hugging_face.datasets[US_RELEASES].revisions[BUNDLE_COMMIT] = {
        f"staged/{RUN_ID}/microcosm_us_2025.h5": b"rows"
    }

    [outcome] = reconciliation.run(kinds=["staging"])

    assert (outcome.source_id, outcome.outcome) == (RUN_ID, "unbuildable")
    assert outcome.incomplete is False


def test_a_staged_run_a_submission_is_still_processing_is_skipped() -> None:
    reconciliation = us_staging_reconciliation(structured_staging_run(RUN_ID))
    reconciliation.submissions.folders.add(("us", f"staged/{RUN_ID}"))

    [outcome] = reconciliation.run(kinds=["staging"])

    assert outcome.outcome == "submission_in_progress"
    assert reconciliation.bun.steps("build") == []


def test_the_newest_finalized_runs_are_built_first() -> None:
    reconciliation = us_staging_reconciliation(
        {
            **structured_staging_run(
                RUN_ID, progress_updated_at="2026-11-02T09:15:00Z"
            ),
            **structured_staging_run(
                OTHER_RUN_ID, progress_updated_at="2026-11-03T09:15:00Z"
            ),
        }
    )

    reconciliation.run(kinds=["staging"])

    assert [call["source"] for call in reconciliation.bun.steps("build")] == [
        OTHER_RUN_ID,
        RUN_ID,
    ]


def test_belgium_has_no_staging_repo_to_reconcile() -> None:
    with pytest.raises(ValueError):
        Reconciliation().run(country="be", kinds=["staging"])


# Both kinds, the lock and the store's manifest ------------------------------------


def both_kinds() -> Reconciliation:
    reconciliation = us_release_reconciliation(
        releases_json({CURRENT_BUILD: ("2026-11-01T12:00:00Z", False)}),
        build_folder(US_RELEASE, CURRENT_BUILD),
    )
    at_head(reconciliation.hugging_face, US_STAGING, structured_staging_run(RUN_ID))
    return reconciliation


def test_each_kind_reads_the_store_s_manifest_once_inside_its_own_lock() -> None:
    reconciliation = both_kinds()

    outcomes = reconciliation.run(kinds=["release", "staging"])

    assert outcomes_by_source(outcomes) == [(CURRENT_ID, "built"), (RUN_ID, "built")]
    assert reconciliation.bun.events == [
        "release lock taken",
        "manifest read",
        f"build {CURRENT_ID}",
        "manifest lock taken",
        f"publication {CURRENT_ID}",
        "manifest lock released",
        # The release run's latest step, inside the release lock.
        "manifest lock taken",
        f"latest {CURRENT_ID}",
        "manifest lock released",
        "release lock released",
        "staging lock taken",
        "manifest read",
        f"build {RUN_ID}",
        "manifest lock taken",
        f"publication {RUN_ID}",
        "manifest lock released",
        "staging lock released",
    ]


def test_a_lock_another_execution_holds_past_the_wait_leaves_the_run_incomplete() -> (
    None
):
    reconciliation = both_kinds()
    reconciliation.held_locks.add(("us", "release"))

    outcomes = reconciliation.run(kinds=["release", "staging"])

    assert [(outcome.kind, outcome.outcome) for outcome in outcomes] == [
        ("release", "lock_wait_expired"),
        ("staging", "built"),
    ]
    assert outcomes[0].incomplete is True


def test_a_dry_run_downloads_nothing_builds_nothing_and_takes_no_lock() -> None:
    reconciliation = both_kinds()

    outcomes = reconciliation.run(kinds=["release", "staging"], dry_run=True)

    assert outcomes_by_source(outcomes) == [
        (CURRENT_ID, "would_build"),
        (RUN_ID, "would_build"),
    ]
    assert reconciliation.bun.events == ["manifest read", "manifest read"]
    assert not any(
        path.endswith("diagnostics.json")
        for path in reconciliation.hugging_face.downloaded_paths()
    )


@dataclass
class UnreadableStore(StandInBun):
    """The tree package's processes, with a manifest read that fails."""

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if Path(argv[2]).name == "calibration-tree-manifest-read.ts":
            self.events.append("manifest read")
            return subprocess.CompletedProcess(argv, 1, "", "The store is unavailable.")
        return super().__call__(argv, env, timeout)


def test_an_unreadable_store_manifest_is_an_error_for_the_kind() -> None:
    reconciliation = both_kinds()
    reconciliation.bun = UnreadableStore()

    outcomes = reconciliation.run(kinds=["release", "staging"])

    assert [(outcome.kind, outcome.outcome) for outcome in outcomes] == [
        ("release", "error"),
        ("staging", "error"),
    ]
    assert all(outcome.incomplete for outcome in outcomes)
    assert reconciliation.bun.steps("build") == []


def test_no_build_starts_after_the_time_limit() -> None:
    reconciliation = us_release_reconciliation()
    reconciliation.bun.clock = reconciliation.clock
    reconciliation.bun.seconds_per_build = 45 * MINUTE

    outcomes = reconciliation.run(time_limit_seconds=40 * MINUTE)

    assert outcomes_by_source(outcomes) == [
        (CURRENT_ID, "built"),
        (OLDER_ID, "deferred"),
    ]
    assert outcome_of(outcomes, OLDER_ID).incomplete is True


def test_a_file_over_the_size_limit_is_too_large_and_never_downloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backfill = importlib.import_module(BACKFILL_MODULE)
    monkeypatch.setattr(backfill, "MAX_SOURCE_FILE_BYTES", len(DIAGNOSTICS) - 1)
    reconciliation = us_release_reconciliation(
        releases_json({CURRENT_BUILD: ("2026-11-01T12:00:00Z", False)}),
        build_folder(US_RELEASE, CURRENT_BUILD),
    )

    [outcome] = reconciliation.run()

    assert outcome.outcome == "too_large"
    assert outcome.incomplete is False
    assert not any(
        path.endswith("diagnostics.json")
        for path in reconciliation.hugging_face.downloaded_paths()
    )


def test_worker_and_publication_refusals_are_final() -> None:
    reconciliation = us_release_reconciliation()
    reconciliation.bun.worker_exits[CURRENT_ID] = 2
    reconciliation.bun.publication_exits[OLDER_ID] = 2

    outcomes = reconciliation.run()

    assert outcomes_by_source(outcomes) == [
        (CURRENT_ID, "unbuildable"),
        (OLDER_ID, "refused"),
    ]
    assert not any(outcome.incomplete for outcome in outcomes)


def test_each_outcome_is_one_json_document() -> None:
    reconciliation = us_release_reconciliation()

    outcomes = reconciliation.run()

    document = json.loads(json.dumps(outcomes[0].to_json()))
    assert document["country"] == "us"
    assert document["kind"] == "release"
    assert document["source_id"] == CURRENT_ID
    assert document["outcome"] == "built"
    assert document["build_artifact_id"]
    assert document["incomplete"] is False


def test_the_last_commit_date_the_fake_lists_is_parsed() -> None:
    # Hugging Face writes its last commit dates with milliseconds and Z.
    assert reconciliation_attribute("publication_time")(DEFAULT_COMMIT_DATE) == (
        DEFAULT_COMMIT_DATE
    )


# Postgres: the lock and the submissions in progress ------------------------------


def test_each_country_and_kind_has_its_own_lock_key() -> None:
    lock_key = reconciliation_attribute("reconciliation_lock_key")

    keys = {
        (country, kind): lock_key(country, kind)
        for country in ("us", "uk", "be")
        for kind in ("release", "staging")
    }

    assert len(set(keys.values())) == len(keys)
    assert MANIFEST_LOCK_KEY not in keys.values()
    assert all(-(2**63) <= key < 2**63 for key in keys.values())
    assert reconciliation_attribute("LOCK_WAIT_SECONDS") == 15 * MINUTE


class SteppedTime:
    """A clock that moves only when the lock sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_a_second_execution_waits_for_the_lock_and_gives_up_after_the_wait(
    postgres_url: str,
) -> None:
    postgres_lock = reconciliation_attribute("postgres_reconciliation_lock")
    timeout_error = reconciliation_attribute("ReconciliationLockTimeoutError")
    engine = create_database_engine(postgres_url)
    time = SteppedTime()
    try:
        first = postgres_lock(engine)
        second = postgres_lock(
            engine, wait_seconds=30, poll_seconds=5, clock=time, sleep=time.sleep
        )
        with first("uk", "staging"):
            with pytest.raises(timeout_error), second("uk", "staging"):
                pass
            # Another country or kind is not held.
            with second("uk", "release"), second("us", "staging"):
                pass
        assert time.sleeps == [5, 5, 5, 5, 5, 5]
        # Released when the first execution's run ends.
        with second("uk", "staging"):
            pass
    finally:
        engine.dispose()


def submission_in_folder(
    store: PostgresSubmissionStore, folder: str, repo: str = UK_STAGING
) -> str:
    submission = calibration_submission()
    submission["provenance"]["huggingface"] = {
        "repo": repo,
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "folder": folder,
    }
    record, _ = store.create_submission(
        SubmissionRequest.model_validate(submission),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    return record.submission_id


def test_only_queued_or_processing_submissions_of_the_folder_count(
    postgres_url: str,
) -> None:
    in_progress_class = reconciliation_attribute("PostgresSubmissionsInProgress")
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    try:
        store = PostgresSubmissionStore(engine)
        in_progress: Callable[[str, str], bool] = in_progress_class(engine).in_progress
        run_folder = "staged/uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
        submission_id = submission_in_folder(store, run_folder)

        awaiting_upload = in_progress("uk", run_folder)
        accepted = store.accept_submission(
            submission_id,
            {
                "calibration_diagnostics.json": "1001",
                "build_manifest.json": "1002",
            },
        )
        queued = in_progress("uk", run_folder)
        assert accepted.job_id is not None
        store.claim_job(accepted.job_id, holder="execution-1", lease_seconds=60)
        processing = in_progress("uk", run_folder)
        other_folder = in_progress("uk", "staged/another-run")
        other_country = in_progress("us", run_folder)
        store.finish_job(
            accepted.job_id,
            holder="execution-1",
            status="succeeded",
            result={"processed_by": "calibration-tree"},
            rejection=None,
        )
        finished = in_progress("uk", run_folder)

        assert (awaiting_upload, queued, processing, finished) == (
            False,
            True,
            True,
            False,
        )
        assert other_folder is False
        assert other_country is False
    finally:
        engine.dispose()
