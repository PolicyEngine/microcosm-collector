"""Calibration trees are built from calibration-diagnostics submissions.

The handler plans a release or staging tree from the submission's provenance,
runs the tree package's Bun worker on the downloaded files without the Blob
token, then runs its publication command under one Postgres lock. These tests
replace the two Bun processes with a recording command runner and run the
handler through the worker framework, so each exit code ends as the framework
records it: succeeded, rejected, or returned for another attempt. A release
build becomes its country's latest when the release repo's ``releases.json``,
read from a stand-in for Hugging Face under the same lock, names it as its
default release's current build. A ``releases-record`` submission, which
Microcosm sends after a change made a build current, moves the latest to the
build the head's ``releases.json`` names, through the package's latest
command, which the runner also stands in for.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import runpy
import subprocess
import threading
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
import yaml

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import create_database_engine
from telemetry_collector.huggingface_read import (
    GENERIC_TOKEN_VARIABLES,
    TOKEN_VARIABLE,
    HuggingFaceReadConfigurationError,
    HuggingFaceReader,
    HuggingFaceReadError,
    HuggingFaceReadSettings,
)
from telemetry_collector.submissions import (
    ROLE_RULES,
    SubmissionRequest,
    intake_object_name,
)
from telemetry_collector.worker import DEFAULT_HANDLERS, JobOutcome, run_job
from tests.fakes import FakeIntakeObjectStore, FakeSubmissionStore
from tests.huggingface_fakes import FakeHuggingFace
from tests.test_submission_store import SteppingClock
from tests.test_submissions import calibration_submission, releases_record_submission

TREES_MODULE = "telemetry_collector.calibration_trees"
RUN_ID = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
UK_DEFAULT_RELEASE = "microcosm-uk-2024-25-national"
UK_RELEASE_REPO = "policyengine/populace-uk-private"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# SteppingClock's time, when the fake store accepts a submission.
ACCEPTED_AT = "2026-10-20T09:00:00Z"
BLOB_TOKEN = "vercel_blob_rw_test_token"
READ_TOKEN = "hf_collector_read_token"
UK_DENSE_RELEASE = "microcosm-uk-2024-25-dense"
US_RELEASE_REPO = "policyengine/populace-us"
US_DEFAULT_RELEASE = "microcosm-us-2024-national"
US_BUILD = "populace-us-2024-spm-20261101"
BE_RELEASE_REPO = "policyengine/populace-be-private"
BE_RELEASE = "microcosm-be-2024-national"
OTHER_BUILD = "uk-frs-calibration-attempt-20261101T000000Z-0a1b2c3d"
CONTENTS = {
    "calibration_diagnostics.json": b'{"schema_version": 8, "targets": []}',
    "build_manifest.json": b'{"build": "uk-frs-calibration-attempt-1"}',
}


def trees() -> ModuleType:
    assert importlib.util.find_spec(TREES_MODULE) is not None
    return importlib.import_module(TREES_MODULE)


def provenance(
    folder: str,
    *,
    release: str | None = None,
    repo: str = UK_RELEASE_REPO,
    country_code: str = "UK",
) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "build_id": RUN_ID,
        "country_code": country_code,
        "release": release,
        "huggingface": {"repo": repo, "commit": COMMIT, "folder": folder},
    }


STAGED = provenance(f"staged/{RUN_ID}", release=UK_DEFAULT_RELEASE)
RELEASED = provenance(
    f"releases/{UK_DEFAULT_RELEASE}/{RUN_ID}", release=UK_DEFAULT_RELEASE
)


@dataclass
class Accepted:
    submissions: FakeSubmissionStore
    objects: FakeIntakeObjectStore
    submission_id: str
    job_id: str
    kind: str = "calibration-diagnostics"

    def run(self, handler: Any) -> JobOutcome:
        return run_job(
            self.job_id,
            submissions=self.submissions,
            objects=self.objects,
            handlers={**DEFAULT_HANDLERS, self.kind: handler},
            holder="exec-1",
        )

    def record(self) -> Any:
        record = self.submissions.get_submission(self.submission_id)
        assert record is not None
        return record


def accepted(submission_provenance: Mapping[str, Any]) -> Accepted:
    submission = calibration_submission()
    submission["provenance"] = dict(submission_provenance)
    return accepted_submission(submission, CONTENTS)


def accepted_submission(
    submission: dict[str, Any], contents: Mapping[str, bytes]
) -> Accepted:
    """``submission``, accepted with each file's bytes from ``contents``."""

    for file in submission["files"]:
        file["size"] = len(contents[file["name"]])
        file["sha256"] = hashlib.sha256(contents[file["name"]]).hexdigest()
    submissions = FakeSubmissionStore(clock=SteppingClock())
    objects = FakeIntakeObjectStore()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(submission),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    generations = {
        file["name"]: objects.upload(
            intake_object_name(record.submission_id, file["name"]),
            contents[file["name"]],
            sha256=file["sha256"],
        )
        for file in record.files
    }
    acceptance = submissions.accept_submission(record.submission_id, generations)
    assert acceptance.job_id is not None
    submissions.record_dispatch(acceptance.job_id, 1)
    return Accepted(
        submissions,
        objects,
        record.submission_id,
        acceptance.job_id,
        kind=submission["kind"],
    )


def plan_for(submission_provenance: Mapping[str, Any]) -> Any:
    return trees().plan_tree_build(accepted(submission_provenance).record())


@dataclass
class RecordingRunner:
    """Stands in for the Bun processes, recording what each one is given.

    The worker is ``build``, the publication command ``publication`` and the
    latest command ``latest``.
    """

    build_exit: int = 0
    build_stderr: str = ""
    publication_exit: int = 0
    publication_stderr: str = ""
    latest_exit: int = 0
    latest_stderr: str = ""
    #: The latest command's answer; by default it moved the latest.
    latest_answer: dict[str, Any] | None = None
    #: Printed by the latest command in place of its answer, when set.
    latest_stdout: str | None = None
    #: Raised instead of running the latest command, when set.
    latest_raises: BaseException | None = None
    events: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if argv[2].endswith("calibration-tree-latest.ts"):
            step = "latest"
        elif argv[2].endswith("publication.ts"):
            step = "publication"
        else:
            step = "build"
        self.events.append(step)
        inputs = {
            Path(argument).name: json.loads(Path(argument).read_text())
            for argument in argv[3:]
            if argument.endswith(".json")
        }
        self.calls.append(
            {"step": step, "argv": list(argv), "env": dict(env), "inputs": inputs}
        )
        if step == "build":
            worker_result = {"buildArtifactId": "a" * 64, "files": []}
            return subprocess.CompletedProcess(
                argv,
                self.build_exit,
                json.dumps(worker_result) if self.build_exit == 0 else "",
                self.build_stderr,
            )
        if step == "latest":
            if self.latest_raises is not None:
                raise self.latest_raises
            request = inputs["latest.json"]
            answer = self.latest_answer or {
                "country": request["country"],
                "releaseId": request["releaseId"],
                "buildArtifactId": "b" * 64,
                "moved": True,
            }
            stdout = json.dumps(answer) if self.latest_exit == 0 else ""
            return subprocess.CompletedProcess(
                argv,
                self.latest_exit,
                stdout if self.latest_stdout is None else self.latest_stdout,
                self.latest_stderr,
            )
        published = {
            "buildArtifactId": "a" * 64,
            "kind": "staging",
            "uploadedParts": 7,
            "keptParts": 0,
            "madeLatest": False,
        }
        return subprocess.CompletedProcess(
            argv,
            self.publication_exit,
            json.dumps(published) if self.publication_exit == 0 else "",
            self.publication_stderr,
        )

    def call(self, step: str) -> dict[str, Any]:
        [call] = [call for call in self.calls if call["step"] == step]
        return call


def handler_with(
    runner: RecordingRunner,
    *,
    hugging_face: FakeHuggingFace | None = None,
    **settings: Any,
) -> Any:
    """The handler, reading Hugging Face from ``hugging_face``.

    A release build's tree reads releases.json to decide the latest; without
    a stand-in given, it reads the UK release repo without one.
    """

    @contextmanager
    def recorded_lock() -> Iterator[None]:
        runner.events.append("lock taken")
        yield
        runner.events.append("lock released")

    make_handler = trees().calibration_tree_handler
    # The handler reads releases.json to decide the latest, so it takes a reader.
    assert "reader" in inspect.signature(make_handler).parameters
    hugging_face = hugging_face or uk_release_repo(record=None)
    return make_handler(
        blob_token=BLOB_TOKEN,
        manifest_lock=recorded_lock,
        bun="/usr/local/bin/bun",
        run_command=runner,
        reader=HuggingFaceReader(
            HuggingFaceReadSettings(token=READ_TOKEN), client=hugging_face.client()
        ),
        **settings,
    )


def releases_record(
    *,
    default: str | None = UK_DEFAULT_RELEASE,
    release: str = UK_DEFAULT_RELEASE,
    current: str | None = RUN_ID,
    builds: Sequence[str] = (RUN_ID,),
    withdrawn: Sequence[str] = (),
) -> bytes:
    """A releases.json with one release, as Microcosm's release writer writes it.

    The builds in ``withdrawn`` are recorded as withdrawn.
    """

    return json.dumps(
        {
            "schema_version": 1,
            "default": default,
            "releases": {
                release: {
                    "role": "national",
                    "current": current,
                    "builds": {
                        build: {
                            "published_at": "2026-10-20T09:00:00Z",
                            "withdrawn": build in withdrawn,
                        }
                        for build in builds
                    },
                }
            },
        }
    ).encode()


def uk_release_repo(record: bytes | None) -> FakeHuggingFace:
    """The UK release repo, with ``record`` as its releases.json when given."""

    return release_repo(UK_RELEASE_REPO, record)


def release_repo(repo: str, record: bytes | None) -> FakeHuggingFace:
    """``repo``, with ``record`` as its releases.json when given."""

    hugging_face = FakeHuggingFace(token=READ_TOKEN)
    hugging_face.add_dataset(
        repo,
        {} if record is None else {"releases.json": record},
        head_commit="c" * 40,
    )
    return hugging_face


def record_reads_in(events: list[str], hugging_face: FakeHuggingFace) -> None:
    """Note each request to ``hugging_face`` in ``events`` as ``read <path>``.

    Beside the lock's and the Bun steps' events, this shows where each read
    happens. Call it before the handler makes its client.
    """

    answer = hugging_face.handle

    def recorded_answer(request: httpx.Request) -> httpx.Response:
        events.append(f"read {request.url.path}")
        return answer(request)

    hugging_face.handle = recorded_answer  # type: ignore[method-assign]


HEAD_READS = [
    f"read /api/datasets/{UK_RELEASE_REPO}",
    f"read /datasets/{UK_RELEASE_REPO}/resolve/{'c' * 40}/releases.json",
]


def test_a_build_folder_submission_plans_a_tree_of_that_build() -> None:
    plan = plan_for(RELEASED)

    assert plan.kind == "release"
    assert plan.release_id == f"{UK_DEFAULT_RELEASE}/{RUN_ID}"
    assert plan.country == "uk"
    assert plan.hf_repo == UK_RELEASE_REPO
    assert plan.hf_commit == COMMIT
    assert plan.created_at == ACCEPTED_AT
    # The UK's default release: its builds become the UK's latest.
    assert plan.make_latest is True


def test_a_build_of_another_release_is_recorded_without_becoming_the_latest() -> None:
    other_release = "microcosm-uk-2024-25-dense"

    plan = plan_for(
        provenance(f"releases/{other_release}/{RUN_ID}", release=other_release)
    )

    assert plan.release_id == f"{other_release}/{RUN_ID}"
    assert plan.make_latest is False


def test_only_the_uk_has_a_default_release_until_the_us_releases_are_agreed() -> None:
    assert trees().DEFAULT_RELEASES == {"uk": UK_DEFAULT_RELEASE}


def test_a_staged_bundle_plans_a_staging_tree_labelled_with_its_release() -> None:
    plan = plan_for(STAGED)

    assert plan.kind == "staging"
    assert plan.run_id == RUN_ID
    assert plan.label == UK_DEFAULT_RELEASE
    assert plan.created_at == ACCEPTED_AT
    assert plan.make_latest is False


def test_a_staged_bundle_without_a_release_is_labelled_with_its_run() -> None:
    plan = plan_for(provenance(f"staged/{RUN_ID}"))

    assert plan.label == RUN_ID


def test_a_staged_bundle_may_come_from_the_country_s_staging_repo() -> None:
    plan = plan_for(
        provenance(f"staged/{RUN_ID}", repo="policyengine/populace-uk-staging")
    )

    assert plan.hf_repo == "policyengine/populace-uk-staging"


@pytest.mark.parametrize(
    "contradicting",
    [
        provenance(f"releases/{UK_DEFAULT_RELEASE}/{RUN_ID}", release="other-release"),
        provenance(
            f"releases/{UK_DEFAULT_RELEASE}/other-build", release=UK_DEFAULT_RELEASE
        ),
        provenance(f"releases/{UK_DEFAULT_RELEASE}/{RUN_ID}"),
        provenance("staged/another-run"),
    ],
    ids=["other-release", "other-build", "no-release", "other-run"],
)
def test_provenance_that_contradicts_its_folder_is_rejected(
    contradicting: dict[str, Any],
) -> None:
    submission = accepted(contradicting)
    runner = RecordingRunner()

    submission.run(handler_with(runner))

    assert submission.record().status == "rejected"
    assert submission.record().rejection["code"] == "provenance_mismatch"
    assert runner.calls == []


@pytest.mark.parametrize(
    "unlisted_repo",
    [
        provenance(
            f"releases/{UK_DEFAULT_RELEASE}/{RUN_ID}",
            release=UK_DEFAULT_RELEASE,
            repo="policyengine/populace-uk-staging",
        ),
        provenance(f"staged/{RUN_ID}", repo="someone/populace-uk-private"),
        provenance(f"staged/{RUN_ID}", repo="policyengine/populace-us"),
    ],
    ids=["release-from-staging-repo", "another-owner", "another-country"],
)
def test_files_from_a_repo_not_registered_for_the_country_are_rejected(
    unlisted_repo: dict[str, Any],
) -> None:
    submission = accepted(unlisted_repo)
    runner = RecordingRunner()

    submission.run(handler_with(runner))

    assert submission.record().status == "rejected"
    assert submission.record().rejection["code"] == "repo_not_registered"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("kept", "reason"),
    [
        (provenance(f"exports/{RUN_ID}"), "folder_not_recognised"),
        # The tree worker never takes "latest" as a run ID.
        ({**provenance("staged/latest"), "run_id": "latest"}, "folder_not_recognised"),
        (
            provenance(
                f"staged/{RUN_ID}", repo="policyengine/populace-fr", country_code="FR"
            ),
            "country_not_registered",
        ),
    ],
    ids=["other-folder", "staged-latest", "other-country"],
)
def test_other_folders_and_countries_stay_in_intake_storage_with_the_reason(
    kept: dict[str, Any], reason: str
) -> None:
    submission = accepted(kept)
    runner = RecordingRunner()

    submission.run(handler_with(runner))

    assert submission.record().status == "succeeded"
    assert submission.record().result == {
        "processed_by": "intake-only",
        "verified_files": 2,
        "reason": reason,
    }
    assert runner.calls == []


def test_the_worker_request_names_each_file_by_its_hugging_face_path() -> None:
    runner = RecordingRunner()

    accepted(STAGED).run(handler_with(runner))

    request = runner.call("build")["inputs"]["request.json"]
    folder = f"staged/{RUN_ID}"
    assert {key: value for key, value in request.items() if key != "artifacts"} == {
        "kind": "staging",
        "country": "uk",
        "runId": RUN_ID,
        "label": UK_DEFAULT_RELEASE,
        "hfRepo": UK_RELEASE_REPO,
        "hfCommitSha": COMMIT,
        "createdAt": ACCEPTED_AT,
    }
    artifacts = request["artifacts"]
    assert artifacts["releaseManifest"] is None
    assert artifacts["demographics"] is None
    for role, name in [
        ("calibrationDiagnostics", "calibration_diagnostics.json"),
        ("buildManifest", "build_manifest.json"),
    ]:
        assert artifacts[role]["path"] == f"{folder}/{name}"
        assert Path(artifacts[role]["file"]).name == name


def test_a_release_build_s_request_names_its_build_folder() -> None:
    runner = RecordingRunner()

    # The latest rule reads releases.json; this repo has none, so the UK's
    # default release in DEFAULT_RELEASES decides.
    accepted(RELEASED).run(
        handler_with(runner, hugging_face=uk_release_repo(record=None))
    )

    request = runner.call("build")["inputs"]["request.json"]
    build_folder = f"{UK_DEFAULT_RELEASE}/{RUN_ID}"
    assert request["kind"] == "release"
    assert request["releaseId"] == build_folder
    assert request["releaseDirectory"] == build_folder
    assert request["artifacts"]["calibrationDiagnostics"]["path"] == (
        f"releases/{build_folder}/calibration_diagnostics.json"
    )
    publication = runner.call("publication")["inputs"]["publication.json"]
    assert publication["makeLatest"] is True


def test_a_built_tree_is_published_under_the_lock_and_recorded_as_the_result() -> None:
    submission = accepted(STAGED)
    runner = RecordingRunner()

    outcome = submission.run(handler_with(runner))

    assert outcome is JobOutcome.FINISHED
    assert runner.events == ["build", "lock taken", "publication", "lock released"]
    publication = runner.call("publication")["inputs"]["publication.json"]
    assert publication["worker"] == {"buildArtifactId": "a" * 64, "files": []}
    assert publication["makeLatest"] is False
    assert Path(publication["bundleDirectory"]).name == "trees"
    assert submission.record().status == "succeeded"
    assert submission.record().result == {
        "processed_by": "calibration-tree",
        "build_artifact_id": "a" * 64,
        "kind": "staging",
        "uploaded_parts": 7,
        "kept_parts": 0,
        "made_latest": False,
    }


def test_files_the_builder_refuses_reject_the_submission() -> None:
    submission = accepted(STAGED)
    runner = RecordingRunner(build_exit=2, build_stderr="Unsupported schema " * 100)

    submission.run(handler_with(runner))

    assert submission.record().status == "rejected"
    rejection = submission.record().rejection
    assert rejection["code"] == "calibration_tree_unbuildable"
    assert rejection["message"].startswith("Unsupported schema")
    assert len(rejection["message"]) <= 500
    assert "publication" not in runner.events


@pytest.mark.parametrize("build_exit", [1, 3, 137])
def test_other_build_failures_return_the_job_for_another_attempt(
    build_exit: int,
) -> None:
    submission = accepted(STAGED)
    runner = RecordingRunner(build_exit=build_exit, build_stderr="Out of memory.")

    outcome = submission.run(handler_with(runner))

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"
    assert "publication" not in runner.events


def test_an_entry_the_manifest_refuses_rejects_the_submission() -> None:
    submission = accepted(STAGED)
    runner = RecordingRunner(
        publication_exit=2,
        publication_stderr="Calibration source already resolves to another build.",
    )

    submission.run(handler_with(runner))

    assert submission.record().status == "rejected"
    assert submission.record().rejection == {
        "code": "calibration_tree_refused",
        "message": "Calibration source already resolves to another build.",
    }


@pytest.mark.parametrize("publication_exit", [1, 3])
def test_a_failed_publication_returns_the_job_for_another_attempt(
    publication_exit: int,
) -> None:
    submission = accepted(STAGED)
    runner = RecordingRunner(
        publication_exit=publication_exit, publication_stderr="Manifest changed."
    )

    outcome = submission.run(handler_with(runner))

    assert outcome is JobOutcome.RETRY
    assert runner.events[-1] == "lock released"


def test_the_builder_never_sees_the_blob_token_and_the_publication_does() -> None:
    runner = RecordingRunner()

    accepted(STAGED).run(
        handler_with(runner, size_limits={"CALIBRATION_TREE_MAX_GZIP_BYTES": "1000"})
    )

    build_env = runner.call("build")["env"]
    publication_env = runner.call("publication")["env"]
    assert set(build_env) == {"PATH", "LANG", "CALIBRATION_TREE_MAX_GZIP_BYTES"}
    assert build_env["LANG"] == "C.UTF-8"
    assert BLOB_TOKEN not in json.dumps(build_env)
    assert set(publication_env) == {
        "PATH",
        "LANG",
        "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN",
    }
    assert publication_env["CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN"] == BLOB_TOKEN
    assert runner.call("build")["argv"][:2] == ["/usr/local/bin/bun", "run"]


def test_each_attempt_builds_in_a_new_directory_that_is_removed_afterwards() -> None:
    submission = accepted(STAGED)
    runner = RecordingRunner(build_exit=3)

    submission.run(handler_with(runner))
    submission.run(handler_with(runner))

    request_files = [
        Path(next(arg for arg in call["argv"] if arg.endswith("request.json")))
        for call in runner.calls
    ]
    assert len(request_files) == 2
    assert request_files[0].parent != request_files[1].parent
    assert not any(path.parent.exists() for path in request_files)


def test_the_tree_handler_is_registered_only_where_its_blob_token_is_set() -> None:
    # The worker's one registry, which each handler module adds its own to.
    registry = importlib.import_module("telemetry_collector.handlers")

    without_token = registry.handlers_from_environment(engine=None, environ={})
    # The handler also reads releases.json, so it needs the read token too.
    with_token = registry.handlers_from_environment(
        engine=None,
        environ={
            "COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN": BLOB_TOKEN,
            TOKEN_VARIABLE: READ_TOKEN,
        },
    )

    assert trees().registered_handlers(engine=None, environ={}) == {}
    assert without_token == DEFAULT_HANDLERS
    assert with_token["orrery-inputs"] is DEFAULT_HANDLERS["orrery-inputs"]
    assert (
        with_token["calibration-diagnostics"]
        is not DEFAULT_HANDLERS["calibration-diagnostics"]
    )


def test_the_tree_handler_needs_the_read_token_beside_its_blob_token() -> None:
    with pytest.raises(HuggingFaceReadConfigurationError, match=TOKEN_VARIABLE):
        trees().registered_handlers(
            engine=None,
            environ={"COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN": BLOB_TOKEN},
        )


def test_the_tree_handler_cannot_be_made_without_a_hugging_face_reader() -> None:
    # A handler without one could build no release tree, so it is refused when
    # it is made rather than on each release build's attempt.
    @contextmanager
    def unused_lock() -> Iterator[None]:
        yield

    with pytest.raises(TypeError, match="reader"):
        trees().calibration_tree_handler(
            blob_token=BLOB_TOKEN, manifest_lock=unused_lock
        )


# The latest build, from releases.json ------------------------------------------


def published_make_latest(runner: RecordingRunner) -> bool:
    return runner.call("publication")["inputs"]["publication.json"]["makeLatest"]


def releases_record_reads(hugging_face: FakeHuggingFace) -> list[str]:
    return [
        request.url.path
        for request in hugging_face.requests
        if request.url.path.endswith("/releases.json")
    ]


def test_a_build_releases_json_names_current_becomes_the_latest() -> None:
    # Not the UK's default release in DEFAULT_RELEASES: only releases.json
    # can make this build the latest.
    hugging_face = uk_release_repo(
        releases_record(default=UK_DENSE_RELEASE, release=UK_DENSE_RELEASE)
    )
    runner = RecordingRunner()
    submission = accepted(
        provenance(f"releases/{UK_DENSE_RELEASE}/{RUN_ID}", release=UK_DENSE_RELEASE)
    )

    submission.run(handler_with(runner, hugging_face=hugging_face))

    assert submission.record().status == "succeeded"
    assert published_make_latest(runner) is True
    # Read at the head commit when the tree is built.
    assert releases_record_reads(hugging_face) == [
        f"/datasets/{UK_RELEASE_REPO}/resolve/{'c' * 40}/releases.json"
    ]


def test_a_build_releases_json_does_not_name_current_is_not_the_latest() -> None:
    # The UK's default release in DEFAULT_RELEASES, whose current build is
    # another one.
    hugging_face = uk_release_repo(
        releases_record(current=OTHER_BUILD, builds=(RUN_ID, OTHER_BUILD))
    )
    runner = RecordingRunner()
    submission = accepted(RELEASED)

    submission.run(handler_with(runner, hugging_face=hugging_face))

    assert submission.record().status == "succeeded"
    assert published_make_latest(runner) is False
    assert "latest_skipped" not in submission.record().result


def test_without_releases_json_the_default_release_rule_decides() -> None:
    hugging_face = uk_release_repo(record=None)
    default_runner = RecordingRunner()
    other_runner = RecordingRunner()

    accepted(RELEASED).run(handler_with(default_runner, hugging_face=hugging_face))
    accepted(
        provenance(f"releases/{UK_DENSE_RELEASE}/{RUN_ID}", release=UK_DENSE_RELEASE)
    ).run(handler_with(other_runner, hugging_face=hugging_face))

    assert published_make_latest(default_runner) is True
    assert published_make_latest(other_runner) is False
    assert len(releases_record_reads(hugging_face)) == 2


# Each record names RUN_ID as the current build of the UK's default release, so
# only the broken rule keeps it from becoming the latest.
ACCEPTED_RECORD = releases_record()


@pytest.mark.parametrize(
    ("refused_record", "reason"),
    [
        (
            releases_record(current="uk-frs-calibration-attempt-unlisted"),
            "is not listed",
        ),
        (releases_record(default="microcosm-uk-unlisted"), "is not listed"),
        (
            json.dumps({"schema_version": 2, "default": None, "releases": {}}).encode(),
            "schema_version must be 1",
        ),
        (b"not json", "not a readable JSON document"),
        # Whitespace after the record keeps it valid JSON, over 4 MiB.
        (ACCEPTED_RECORD + b" " * (4 * 1024 * 1024), "larger than 4194304 bytes"),
        (releases_record(withdrawn=(RUN_ID,)), "is withdrawn"),
        (
            json.dumps({**json.loads(ACCEPTED_RECORD), "notes": "extra"}).encode(),
            "must have exactly default, releases, schema_version",
        ),
        # Python's True equals 1, which the record's rule does not accept.
        (
            ACCEPTED_RECORD.replace(b'"schema_version": 1', b'"schema_version": true'),
            "schema_version must be 1",
        ),
        (
            ACCEPTED_RECORD.replace(b'"national"', b'"nation\xffal"'),
            "not a readable JSON document",
        ),
        (
            ACCEPTED_RECORD.replace(b'"schema_version": 1', b'"schema_version": NaN'),
            "not a readable JSON document",
        ),
    ],
    ids=[
        "unlisted-current",
        "unlisted-default",
        "schema-2",
        "not-json",
        "over-4-mib",
        "withdrawn-current",
        "extra-key",
        "boolean-schema-version",
        "invalid-utf-8",
        "nan",
    ],
)
def test_a_releases_json_the_rules_refuse_publishes_without_moving_the_latest(
    refused_record: bytes, reason: str
) -> None:
    hugging_face = uk_release_repo(refused_record)
    runner = RecordingRunner()
    submission = accepted(RELEASED)

    submission.run(handler_with(runner, hugging_face=hugging_face))

    assert submission.record().status == "succeeded"
    assert published_make_latest(runner) is False
    result = submission.record().result
    assert result["latest_skipped"] == "releases_record_refused"
    assert reason in result["latest_skipped_reason"]


def test_the_accepted_record_the_refusals_change_makes_the_build_the_latest() -> None:
    runner = RecordingRunner()

    accepted(RELEASED).run(
        handler_with(runner, hugging_face=uk_release_repo(ACCEPTED_RECORD))
    )

    assert published_make_latest(runner) is True


def test_releases_json_is_read_under_the_lock_just_before_the_publication() -> None:
    # Read under the lock the releases-record handler also takes, so a build
    # made current while this tree was being built is never missed.
    hugging_face = uk_release_repo(releases_record())
    runner = RecordingRunner()
    record_reads_in(runner.events, hugging_face)

    accepted(RELEASED).run(handler_with(runner, hugging_face=hugging_face))

    assert runner.events == [
        "build",
        "lock taken",
        *HEAD_READS,
        "publication",
        "lock released",
    ]
    assert published_make_latest(runner) is True


@pytest.mark.parametrize("status_code", [401, 429, 500, 503])
def test_a_failed_head_commit_read_returns_the_job_before_anything_is_published(
    status_code: int,
) -> None:
    hugging_face = uk_release_repo(releases_record())
    hugging_face.datasets[UK_RELEASE_REPO].forced_status = status_code
    runner = RecordingRunner()
    submission = accepted(RELEASED)

    outcome = submission.run(handler_with(runner, hugging_face=hugging_face))

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"
    assert [call["step"] for call in runner.calls] == ["build"]


@pytest.mark.parametrize(
    "answer",
    [(401, None), (429, None), (500, None), (503, None)]
    # A 404 for the repo or the revision is not a repo without releases.json.
    + [(404, "RepoNotFound"), (404, "RevisionNotFound")],
    ids=["401", "429", "500", "503", "404-repo", "404-revision"],
)
def test_a_failed_read_of_releases_json_itself_returns_the_job_before_anything_is_published(
    answer: tuple[int, str | None],
) -> None:
    hugging_face = uk_release_repo(releases_record())
    hugging_face.datasets[UK_RELEASE_REPO].file_answers["releases.json"] = answer
    runner = RecordingRunner()
    submission = accepted(RELEASED)

    outcome = submission.run(handler_with(runner, hugging_face=hugging_face))

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"
    assert [call["step"] for call in runner.calls] == ["build"]
    # The head commit was read, and then the record at it.
    assert [request.url.path for request in hugging_face.requests] == [
        f"/api/datasets/{UK_RELEASE_REPO}",
        f"/datasets/{UK_RELEASE_REPO}/resolve/{'c' * 40}/releases.json",
    ]


def test_a_404_without_an_error_code_means_the_repo_has_no_record() -> None:
    hugging_face = uk_release_repo(releases_record())
    hugging_face.datasets[UK_RELEASE_REPO].file_answers["releases.json"] = (404, None)
    runner = RecordingRunner()
    submission = accepted(RELEASED)

    submission.run(handler_with(runner, hugging_face=hugging_face))

    assert submission.record().status == "succeeded"
    # DEFAULT_RELEASES's rule: a build of the UK's default release.
    assert published_make_latest(runner) is True


def test_a_staged_bundle_reads_nothing_from_hugging_face() -> None:
    hugging_face = uk_release_repo(releases_record())
    runner = RecordingRunner()

    accepted(STAGED).run(handler_with(runner, hugging_face=hugging_face))

    assert hugging_face.requests == []
    assert published_make_latest(runner) is False


# The latest after a change of releases.json (releases-record) -------------------

#: The commit that wrote a submitted releases.json.
RECORD_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
LATEST_RESULT_KEYS = {
    "processed_by",
    "current_build",
    "build_artifact_id",
    "made_latest",
    "latest_moved",
}


def two_release_record(
    *, national_current: str | None, dense_current: str | None
) -> bytes:
    """A releases.json with the UK's national (default) and dense releases."""

    builds = {
        build: {"published_at": "2026-10-20T09:00:00Z", "withdrawn": False}
        for build in (RUN_ID, OTHER_BUILD)
    }
    return json.dumps(
        {
            "schema_version": 1,
            "default": UK_DEFAULT_RELEASE,
            "releases": {
                UK_DEFAULT_RELEASE: {
                    "role": "national",
                    "current": national_current,
                    "builds": builds,
                },
                UK_DENSE_RELEASE: {
                    "role": "dense",
                    "current": dense_current,
                    "builds": builds,
                },
            },
        }
    ).encode()


def accepted_releases_record(
    record: bytes,
    *,
    release: str = UK_DEFAULT_RELEASE,
    build: str = RUN_ID,
    repo: str = UK_RELEASE_REPO,
    country_code: str = "UK",
) -> Accepted:
    """A releases-record submission of ``record``, after ``build`` was made current."""

    assert "releases-record" in ROLE_RULES
    submission = releases_record_submission()
    submission["provenance"] = {
        "run_id": "uk-promotion-20261113T120000Z",
        "build_id": build,
        "country_code": country_code,
        "release": release,
        "huggingface": {
            "repo": repo,
            "commit": RECORD_COMMIT,
            "folder": f"releases/{release}/{build}",
        },
    }
    return accepted_submission(submission, {"releases.json": record})


def latest_handler_with(
    runner: RecordingRunner, hugging_face: FakeHuggingFace, **settings: Any
) -> Any:
    """The releases-record handler, reading Hugging Face from ``hugging_face``.

    Every read and the lock are recorded in ``runner.events``.
    """

    module = trees()
    assert hasattr(module, "calibration_tree_latest_handler")

    @contextmanager
    def recorded_lock() -> Iterator[None]:
        runner.events.append("lock taken")
        yield
        runner.events.append("lock released")

    record_reads_in(runner.events, hugging_face)
    return module.calibration_tree_latest_handler(
        blob_token=BLOB_TOKEN,
        manifest_lock=recorded_lock,
        bun="/usr/local/bin/bun",
        run_command=runner,
        reader=HuggingFaceReader(
            HuggingFaceReadSettings(token=READ_TOKEN), client=hugging_face.client()
        ),
        **settings,
    )


def test_a_releases_record_moves_the_latest_to_the_current_build_under_the_lock() -> (
    None
):
    record = releases_record()
    hugging_face = uk_release_repo(record)
    runner = RecordingRunner()
    submission = accepted_releases_record(record)

    submission.run(latest_handler_with(runner, hugging_face))

    assert submission.record().status == "succeeded"
    assert runner.events == [
        "lock taken",
        *HEAD_READS,
        "latest",
        "lock released",
    ]
    latest = runner.call("latest")
    assert latest["argv"][:3] == [
        "/usr/local/bin/bun",
        "run",
        str(trees().PACKAGE_DIRECTORY / "src" / "calibration-tree-latest.ts"),
    ]
    assert latest["inputs"]["latest.json"] == {
        "country": "uk",
        "releaseId": f"{UK_DEFAULT_RELEASE}/{RUN_ID}",
    }
    # Only the store's token and the base settings, as for the publication.
    assert latest["env"]["CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN"] == BLOB_TOKEN
    assert set(latest["env"]) == {
        "PATH",
        "LANG",
        "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN",
    }
    assert submission.record().result == {
        "processed_by": "calibration-tree-latest",
        "current_build": f"{UK_DEFAULT_RELEASE}/{RUN_ID}",
        "build_artifact_id": "b" * 64,
        "made_latest": True,
        "latest_moved": True,
    }


def test_the_latest_follows_the_head_s_record_over_the_submitted_one() -> None:
    # The submitted record made RUN_ID current; a later change made
    # OTHER_BUILD current, and the head's record decides.
    submitted = releases_record(builds=(RUN_ID, OTHER_BUILD))
    hugging_face = uk_release_repo(
        releases_record(current=OTHER_BUILD, builds=(RUN_ID, OTHER_BUILD))
    )
    runner = RecordingRunner()
    submission = accepted_releases_record(submitted)

    submission.run(latest_handler_with(runner, hugging_face))

    assert runner.call("latest")["inputs"]["latest.json"]["releaseId"] == (
        f"{UK_DEFAULT_RELEASE}/{OTHER_BUILD}"
    )
    assert submission.record().result["current_build"] == (
        f"{UK_DEFAULT_RELEASE}/{OTHER_BUILD}"
    )


def test_a_change_to_another_release_follows_the_default_release() -> None:
    # Making a dense build current leaves the national (default) release's
    # current build as the latest.
    record = two_release_record(national_current=OTHER_BUILD, dense_current=RUN_ID)
    hugging_face = uk_release_repo(record)
    runner = RecordingRunner()
    submission = accepted_releases_record(record, release=UK_DENSE_RELEASE)

    submission.run(latest_handler_with(runner, hugging_face))

    assert submission.record().status == "succeeded"
    assert runner.call("latest")["inputs"]["latest.json"]["releaseId"] == (
        f"{UK_DEFAULT_RELEASE}/{OTHER_BUILD}"
    )


def test_a_build_already_the_latest_is_left_as_it_is() -> None:
    record = releases_record()
    runner = RecordingRunner(
        latest_answer={
            "country": "uk",
            "releaseId": f"{UK_DEFAULT_RELEASE}/{RUN_ID}",
            "buildArtifactId": "b" * 64,
            "moved": False,
        }
    )
    submission = accepted_releases_record(record)

    submission.run(latest_handler_with(runner, uk_release_repo(record)))

    result = submission.record().result
    assert result["made_latest"] is True
    assert result["latest_moved"] is False
    assert "latest_unchanged" not in result


def test_a_current_build_without_a_recorded_tree_leaves_the_latest() -> None:
    # Its tree may still be building; that build then reads releases.json
    # under the same lock and becomes the latest itself.
    record = releases_record()
    runner = RecordingRunner(
        latest_exit=2,
        latest_stderr="The manifest records no uk release build "
        f"{UK_DEFAULT_RELEASE}/{RUN_ID}.",
    )
    submission = accepted_releases_record(record)

    submission.run(latest_handler_with(runner, uk_release_repo(record)))

    assert submission.record().status == "succeeded"
    assert submission.record().result == {
        "processed_by": "calibration-tree-latest",
        "current_build": f"{UK_DEFAULT_RELEASE}/{RUN_ID}",
        "build_artifact_id": None,
        "made_latest": False,
        "latest_moved": False,
        "latest_unchanged": "no_recorded_tree",
        "latest_unchanged_reason": "The manifest records no uk release build "
        f"{UK_DEFAULT_RELEASE}/{RUN_ID}.",
    }


@pytest.mark.parametrize(
    ("head_record", "unchanged", "reason"),
    [
        (None, "no_releases_record", None),
        (releases_record(withdrawn=(RUN_ID,)), "releases_record_refused", "withdrawn"),
        (releases_record(current=None), "no_current_build", None),
        (releases_record(default=None), "no_current_build", None),
    ],
    ids=["no-record", "refused-record", "no-current-build", "no-default-release"],
)
def test_a_head_record_naming_no_usable_current_build_leaves_the_latest(
    head_record: bytes | None, unchanged: str, reason: str | None
) -> None:
    runner = RecordingRunner()
    submission = accepted_releases_record(releases_record())

    submission.run(latest_handler_with(runner, uk_release_repo(head_record)))

    assert submission.record().status == "succeeded"
    assert "latest" not in runner.events
    result = submission.record().result
    assert result["latest_unchanged"] == unchanged
    assert result["current_build"] is None
    assert result["made_latest"] is False
    assert result["latest_moved"] is False
    if reason is None:
        assert set(result) == LATEST_RESULT_KEYS | {"latest_unchanged"}
    else:
        assert reason in result["latest_unchanged_reason"]


@pytest.mark.parametrize(
    "answer",
    [("forced", 500), ("forced", 429), ("forced", 401)]
    + [("file", (500, None)), ("file", (404, "RepoNotFound"))]
    + [("file", (404, "RevisionNotFound"))],
    ids=[
        "head-500",
        "head-429",
        "head-401",
        "file-500",
        "file-404-repo",
        "file-404-revision",
    ],
)
def test_a_failed_read_of_the_head_s_record_returns_the_job(
    answer: tuple[str, Any],
) -> None:
    record = releases_record()
    hugging_face = uk_release_repo(record)
    where, value = answer
    if where == "forced":
        hugging_face.datasets[UK_RELEASE_REPO].forced_status = value
    else:
        hugging_face.datasets[UK_RELEASE_REPO].file_answers["releases.json"] = value
    runner = RecordingRunner()
    submission = accepted_releases_record(record)

    outcome = submission.run(latest_handler_with(runner, hugging_face))

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"
    assert "latest" not in runner.events


@pytest.mark.parametrize(
    ("exit_code", "stdout"),
    [(3, None), (1, None), (0, "not json"), (0, '["not", "an", "object"]')],
    ids=["store-error", "request-error", "no-json", "not-an-object"],
)
def test_other_failures_of_the_latest_command_return_the_job(
    exit_code: int, stdout: str | None
) -> None:
    record = releases_record()
    runner = RecordingRunner(
        latest_exit=exit_code, latest_stderr="store error", latest_stdout=stdout
    )
    submission = accepted_releases_record(record)

    outcome = submission.run(latest_handler_with(runner, uk_release_repo(record)))

    assert outcome is JobOutcome.RETRY
    assert submission.record().status == "queued"


@pytest.mark.parametrize(
    ("submitted", "code"),
    [
        (b"not json", "releases_record_refused"),
        (releases_record(withdrawn=(RUN_ID,)), "releases_record_refused"),
        # Another build is its release's current build.
        (
            releases_record(current=OTHER_BUILD, builds=(RUN_ID, OTHER_BUILD)),
            "provenance_mismatch",
        ),
        # Its release is not in the record.
        (
            releases_record(release=UK_DENSE_RELEASE, default=None),
            "provenance_mismatch",
        ),
    ],
    ids=["not-json", "refused", "another-current-build", "release-not-listed"],
)
def test_a_submitted_record_that_does_not_make_its_build_current_is_rejected(
    submitted: bytes, code: str
) -> None:
    hugging_face = uk_release_repo(releases_record())
    runner = RecordingRunner()
    submission = accepted_releases_record(submitted)

    submission.run(latest_handler_with(runner, hugging_face))

    assert submission.record().status == "rejected"
    assert submission.record().rejection["code"] == code
    assert hugging_face.requests == []
    assert runner.calls == []


def test_a_releases_record_from_a_repo_not_registered_is_rejected() -> None:
    record = releases_record()
    hugging_face = uk_release_repo(record)
    runner = RecordingRunner()
    # The UK's staging repo holds staged bundles, never releases.json.
    submission = accepted_releases_record(
        record, repo="policyengine/populace-uk-staging"
    )

    submission.run(latest_handler_with(runner, hugging_face))

    assert submission.record().status == "rejected"
    assert submission.record().rejection["code"] == "repo_not_registered"
    assert hugging_face.requests == []


def test_a_releases_record_of_a_country_not_registered_stays_in_intake_storage() -> (
    None
):
    record = releases_record()
    hugging_face = uk_release_repo(record)
    runner = RecordingRunner()
    submission = accepted_releases_record(
        record, repo="policyengine/populace-fr", country_code="FR"
    )

    submission.run(latest_handler_with(runner, hugging_face))

    assert submission.record().status == "succeeded"
    assert submission.record().result == {
        "processed_by": "intake-only",
        "verified_files": 1,
        "reason": "country_not_registered",
    }
    assert hugging_face.requests == []


def test_the_releases_record_handler_is_registered_beside_the_tree_handler() -> None:
    registry = importlib.import_module("telemetry_collector.handlers")
    environ = {
        "COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN": BLOB_TOKEN,
        TOKEN_VARIABLE: READ_TOKEN,
    }

    without_token = registry.handlers_from_environment(engine=None, environ={})
    with_token = registry.handlers_from_environment(engine=None, environ=environ)

    assert "releases-record" in DEFAULT_HANDLERS
    assert without_token.get("releases-record") is DEFAULT_HANDLERS.get(
        "releases-record"
    )
    assert set(trees().registered_handlers(engine=None, environ=environ)) == {
        "calibration-diagnostics",
        "releases-record",
    }
    assert with_token["releases-record"] is not DEFAULT_HANDLERS["releases-record"]


# Every country the collector lists ----------------------------------------------

US_RECORD = releases_record(
    default=US_DEFAULT_RELEASE,
    release=US_DEFAULT_RELEASE,
    current=US_BUILD,
    builds=(US_BUILD,),
)


def test_a_us_build_releases_json_names_current_becomes_the_us_latest() -> None:
    # DEFAULT_RELEASES names no US release: only releases.json decides.
    runner = RecordingRunner()
    submission = accepted(
        provenance(
            f"releases/{US_DEFAULT_RELEASE}/{US_BUILD}",
            release=US_DEFAULT_RELEASE,
            repo=US_RELEASE_REPO,
            country_code="US",
        )
        | {"build_id": US_BUILD}
    )

    submission.run(
        handler_with(runner, hugging_face=release_repo(US_RELEASE_REPO, US_RECORD))
    )

    assert submission.record().status == "succeeded"
    assert published_make_latest(runner) is True


def test_a_be_build_without_releases_json_does_not_become_the_latest() -> None:
    # Without releases.json, DEFAULT_RELEASES decides, and it names no BE
    # release yet.
    runner = RecordingRunner()
    submission = accepted(
        provenance(
            f"releases/{BE_RELEASE}/{RUN_ID}",
            release=BE_RELEASE,
            repo=BE_RELEASE_REPO,
            country_code="BE",
        )
    )

    submission.run(
        handler_with(runner, hugging_face=release_repo(BE_RELEASE_REPO, None))
    )

    assert submission.record().status == "succeeded"
    assert published_make_latest(runner) is False


def test_a_us_releases_record_moves_the_us_latest() -> None:
    runner = RecordingRunner()
    submission = accepted_releases_record(
        US_RECORD,
        release=US_DEFAULT_RELEASE,
        build=US_BUILD,
        repo=US_RELEASE_REPO,
        country_code="US",
    )

    submission.run(
        latest_handler_with(runner, release_repo(US_RELEASE_REPO, US_RECORD))
    )

    assert submission.record().status == "succeeded"
    assert runner.call("latest")["inputs"]["latest.json"] == {
        "country": "us",
        "releaseId": f"{US_DEFAULT_RELEASE}/{US_BUILD}",
    }
    assert submission.record().result["made_latest"] is True


def test_a_be_releases_record_without_releases_json_at_the_head_leaves_the_latest() -> (
    None
):
    record = releases_record(
        default=BE_RELEASE, release=BE_RELEASE, current=RUN_ID, builds=(RUN_ID,)
    )
    runner = RecordingRunner()
    submission = accepted_releases_record(
        record, release=BE_RELEASE, repo=BE_RELEASE_REPO, country_code="BE"
    )

    submission.run(latest_handler_with(runner, release_repo(BE_RELEASE_REPO, None)))

    assert submission.record().status == "succeeded"
    assert submission.record().result["latest_unchanged"] == "no_releases_record"
    assert "latest" not in runner.events


# Following releases.json without a submission -----------------------------------


def follow_with(
    runner: RecordingRunner, hugging_face: FakeHuggingFace, country: str
) -> Any:
    """``follow_releases_record`` for ``country``, as another collector job calls it.

    Every read and the lock are recorded in ``runner.events``.
    """

    module = trees()
    assert hasattr(module, "follow_releases_record")

    @contextmanager
    def recorded_lock() -> Iterator[None]:
        runner.events.append("lock taken")
        yield
        runner.events.append("lock released")

    record_reads_in(runner.events, hugging_face)
    return module.follow_releases_record(
        country,
        blob_token=BLOB_TOKEN,
        manifest_lock=recorded_lock,
        bun="/usr/local/bin/bun",
        run_command=runner,
        reader=HuggingFaceReader(
            HuggingFaceReadSettings(token=READ_TOKEN), client=hugging_face.client()
        ),
    )


def test_a_country_s_latest_can_follow_releases_json_without_a_submission() -> None:
    # As a backfill or switch-over job does at its end: nothing is built.
    runner = RecordingRunner()

    followed = follow_with(runner, release_repo(US_RELEASE_REPO, US_RECORD), "us")

    assert runner.events == [
        "lock taken",
        f"read /api/datasets/{US_RELEASE_REPO}",
        f"read /datasets/{US_RELEASE_REPO}/resolve/{'c' * 40}/releases.json",
        "latest",
        "lock released",
    ]
    assert runner.call("latest")["inputs"]["latest.json"] == {
        "country": "us",
        "releaseId": f"{US_DEFAULT_RELEASE}/{US_BUILD}",
    }
    assert followed == trees().FollowedLatest(
        country="us",
        current_build=f"{US_DEFAULT_RELEASE}/{US_BUILD}",
        build_artifact_id="b" * 64,
        made_latest=True,
        latest_moved=True,
    )


def test_following_releases_json_says_why_the_latest_stayed() -> None:
    runner = RecordingRunner()

    followed = follow_with(runner, release_repo(BE_RELEASE_REPO, None), "be")

    assert followed == trees().FollowedLatest(
        country="be",
        current_build=None,
        build_artifact_id=None,
        made_latest=False,
        latest_moved=False,
        unchanged="no_releases_record",
    )
    assert "latest" not in runner.events


def test_following_releases_json_of_a_country_the_collector_does_not_list_is_refused() -> (
    None
):
    runner = RecordingRunner()
    hugging_face = uk_release_repo(releases_record())

    with pytest.raises(ValueError, match="fr is not a country the collector lists"):
        follow_with(runner, hugging_face, "fr")

    assert hugging_face.requests == []
    assert runner.events == []


@pytest.mark.parametrize("failure", ["read", "command"])
def test_a_failed_follow_raises_for_the_caller_to_try_again(failure: str) -> None:
    hugging_face = uk_release_repo(releases_record())
    runner = RecordingRunner()
    if failure == "read":
        hugging_face.datasets[UK_RELEASE_REPO].forced_status = 503
        expected: type[Exception] = HuggingFaceReadError
    else:
        runner.latest_exit = 3
        expected = trees().CalibrationTreeCommandError

    with pytest.raises(expected):
        follow_with(runner, hugging_face, "uk")


@pytest.mark.parametrize(
    "raised",
    [subprocess.TimeoutExpired(["bun"], 300), FileNotFoundError("bun")],
    ids=["timeout", "bun-missing"],
)
def test_a_latest_command_that_cannot_finish_raises_the_documented_error(
    raised: BaseException,
) -> None:
    # A caller retries on the two documented errors, so a command that runs
    # out of time or cannot start raises one of them too.
    runner = RecordingRunner(latest_raises=raised)

    with pytest.raises(Exception) as caught:
        follow_with(runner, uk_release_repo(releases_record()), "uk")

    assert isinstance(caught.value, trees().CalibrationTreeCommandError)
    assert caught.value.__cause__ is raised


# Deeper than Python's JSON decoder goes; the package's parser refuses it too.
DEEPLY_NESTED_RECORD = b"[" * 100_000 + b"]" * 100_000


def test_a_head_record_nested_too_deeply_publishes_without_moving_the_latest() -> None:
    runner = RecordingRunner()
    submission = accepted(RELEASED)

    submission.run(
        handler_with(runner, hugging_face=uk_release_repo(DEEPLY_NESTED_RECORD))
    )

    assert submission.record().status == "succeeded"
    assert published_make_latest(runner) is False
    assert submission.record().result["latest_skipped"] == "releases_record_refused"


def test_a_submitted_record_nested_too_deeply_is_rejected() -> None:
    runner = RecordingRunner()
    submission = accepted_releases_record(DEEPLY_NESTED_RECORD)

    submission.run(latest_handler_with(runner, uk_release_repo(releases_record())))

    assert submission.record().status == "rejected"
    assert submission.record().rejection["code"] == "releases_record_refused"


def test_the_manifest_lock_lets_one_writer_in_at_a_time(postgres_url: str) -> None:
    trees_module = trees()
    engine = create_database_engine(postgres_url)
    lock = trees_module.postgres_manifest_lock(engine)
    holding = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with lock():
            holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert holding.wait(timeout=10)
        with engine.connect() as connection:
            taken_while_held = connection.execute(
                sa.text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": trees_module.MANIFEST_LOCK_KEY},
            ).scalar_one()
        release.set()
        holder.join(timeout=10)
        with engine.connect() as connection:
            taken_after = connection.execute(
                sa.text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": trees_module.MANIFEST_LOCK_KEY},
            ).scalar_one()
    finally:
        release.set()
        holder.join(timeout=10)
        engine.dispose()

    assert taken_while_held is False
    assert taken_after is True


def test_the_worker_job_builds_trees_where_its_blob_token_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telemetry_collector import worker

    given: dict[str, Any] = {}

    class StandInReader:
        def __init__(self, **settings: Any) -> None:
            pass

    def recorded_run_job(job_id: str, **arguments: Any) -> JobOutcome:
        # run_job uses DEFAULT_HANDLERS when it is given none.
        given["handlers"] = arguments.get("handlers", DEFAULT_HANDLERS)
        return JobOutcome.FINISHED

    monkeypatch.setattr(worker, "GoogleCloudStorageIntake", StandInReader)
    monkeypatch.setattr(worker, "run_job", recorded_run_job)
    monkeypatch.setenv("COLLECTOR_WORKER_JOB_ID", "job-0123")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    monkeypatch.setenv("COLLECTOR_INTAKE_BUCKET", "collector-intake")
    monkeypatch.setenv("COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN", BLOB_TOKEN)
    # The handler reads releases.json with the worker job's read token.
    monkeypatch.setenv(TOKEN_VARIABLE, READ_TOKEN)
    for name in GENERIC_TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COLLECTOR_ORRERY_BUCKET", raising=False)

    assert worker.main() == 0
    assert (
        given["handlers"]["calibration-diagnostics"]
        is not DEFAULT_HANDLERS["calibration-diagnostics"]
    )


ROOT = Path(__file__).resolve().parents[1]
BUN_VERSION = "1.4.2"
TREE_IMAGE_CHECK = ROOT / "tools" / "check-calibration-tree-image.sh"
IMAGE = "microcosm-collector:validation"


def test_the_image_holds_pinned_bun_and_the_tree_package_s_production_install() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    trees_module = trees()

    # The Bun release the package's CI job uses, from the official image by
    # digest, on the same Debian release as the image.
    assert f"FROM oven/bun:{BUN_VERSION}-slim@sha256:" in dockerfile
    assert (
        "RUN bun install --frozen-lockfile --production --ignore-scripts" in dockerfile
    )
    assert "COPY --from=calibration-tree /usr/local/bin/bun /usr/local/bin/bun" in (
        dockerfile
    )
    assert (
        "COPY --from=calibration-tree /calibration-tree ./packages/calibration-tree"
        in dockerfile
    )
    # The image's package is where the handler runs it from.
    assert trees_module.WORKER_SCRIPT.relative_to(ROOT) == Path(
        "packages/calibration-tree/src/calibration-tree-worker.ts"
    )
    assert trees_module.PUBLICATION_SCRIPT.relative_to(ROOT) == Path(
        "packages/calibration-tree/src/calibration-tree-publication.ts"
    )
    # Its tests, fixtures and test support stay out of the image.
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert {
        "packages/calibration-tree/src/**/*.test.ts",
        "packages/calibration-tree/src/fixtures",
        "packages/calibration-tree/src/test-support",
    } <= set(dockerignore)


def test_the_image_build_context_lets_in_only_the_tree_package() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    # The last matching rule wins: every package under packages/ stays out of
    # the build context (the read API client's), the tree package is let back
    # in, and then its local install, tests, fixtures and test support are
    # left out again.
    exception = "!packages/calibration-tree"
    assert "packages" in dockerignore
    assert exception in dockerignore
    assert dockerignore.index("packages") < dockerignore.index(exception)
    for left_out in (
        "packages/calibration-tree/node_modules",
        "packages/calibration-tree/src/**/*.test.ts",
        "packages/calibration-tree/src/fixtures",
        "packages/calibration-tree/src/test-support",
    ):
        assert left_out in dockerignore
        assert dockerignore.index(left_out) > dockerignore.index(exception), left_out


def test_ci_checks_the_tree_package_in_the_built_image() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    runs = [step.get("run") for step in workflow["jobs"]["validate"]["steps"]]
    tree_check = f"bash tools/check-calibration-tree-image.sh {IMAGE}"

    assert tree_check in runs
    assert runs.index(f"docker build --tag {IMAGE} .") < runs.index(tree_check)


TOKEN_REFUSAL = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN is required."


def run_tree_image_check(
    tmp_path: Path,
    *,
    build_exit: int,
    build_answer: str,
    publication_exit: int,
    latest_exit: int = 1,
    latest_stderr: str = TOKEN_REFUSAL,
) -> subprocess.CompletedProcess[str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    calls = tmp_path / "docker-calls"
    fake_docker = bin_directory / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'if [[ "$*" == *calibration-tree-worker.ts* ]]; then\n'
        f"  printf '%s\\n' '{build_answer}'\n"
        f"  exit {build_exit}\n"
        "fi\n"
        'if [[ "$*" == *calibration-tree-latest.ts* ]]; then\n'
        f"  printf '%s\\n' '{latest_stderr}' >&2\n"
        f"  exit {latest_exit}\n"
        "fi\n"
        f"exit {publication_exit}\n"
    )
    fake_docker.chmod(0o755)
    return subprocess.run(
        ["bash", str(TREE_IMAGE_CHECK), IMAGE],
        cwd=ROOT,
        env={"PATH": f"{bin_directory}:/usr/bin:/bin"},
        capture_output=True,
        check=False,
        text=True,
    )


BUILT_STAGING_TREE = json.dumps(
    {
        "buildArtifactId": "a" * 64,
        "files": [{"part": "filter-index"}, {"part": "index"}],
        "manifestEntry": {"kind": "staging"},
    }
)


def test_the_tree_image_check_passes_when_the_image_builds_a_tree(
    tmp_path: Path,
) -> None:
    assert TREE_IMAGE_CHECK.is_file()

    result = run_tree_image_check(
        tmp_path, build_exit=0, build_answer=BUILT_STAGING_TREE, publication_exit=1
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "docker-calls").read_text().splitlines()
    assert len(calls) == 3
    assert all(call.startswith("run --rm ") and IMAGE in call for call in calls)
    assert (
        "bun run /app/packages/calibration-tree/src/calibration-tree-worker.ts"
        in calls[0]
    )
    assert (
        "bun run /app/packages/calibration-tree/src/calibration-tree-publication.ts"
        in calls[1]
    )
    # The publication command runs without a token, so it can write nothing.
    assert "BLOB" not in calls[1]
    # So does the latest command, which must ship in the image and refuse.
    assert (
        "bun run /app/packages/calibration-tree/src/calibration-tree-latest.ts"
        in calls[2]
    )
    assert "BLOB" not in calls[2]


@pytest.mark.parametrize(
    ("latest_exit", "latest_stderr"),
    [(0, ""), (1, "error: Module not found"), (3, TOKEN_REFUSAL)],
    ids=["latest-ran", "latest-missing", "latest-other-exit"],
)
def test_the_tree_image_check_fails_unless_the_latest_command_refuses(
    latest_exit: int, latest_stderr: str, tmp_path: Path
) -> None:
    result = run_tree_image_check(
        tmp_path,
        build_exit=0,
        build_answer=BUILT_STAGING_TREE,
        publication_exit=1,
        latest_exit=latest_exit,
        latest_stderr=latest_stderr,
    )

    assert result.returncode != 0
    assert "latest command" in result.stderr


@pytest.mark.parametrize(
    ("build_exit", "build_answer", "publication_exit"),
    [
        (3, "", 1),
        (0, "not json", 1),
        (0, json.dumps({"files": [], "manifestEntry": {"kind": "release"}}), 1),
        (0, BUILT_STAGING_TREE, 127),
        (0, BUILT_STAGING_TREE, 0),
    ],
    ids=["build-fails", "no-answer", "wrong-tree", "no-bun", "publication-ran"],
)
def test_the_tree_image_check_fails_otherwise(
    build_exit: int, build_answer: str, publication_exit: int, tmp_path: Path
) -> None:
    assert TREE_IMAGE_CHECK.is_file()

    result = run_tree_image_check(
        tmp_path,
        build_exit=build_exit,
        build_answer=build_answer,
        publication_exit=publication_exit,
    )

    assert result.returncode != 0


def test_run_as_a_module_the_worker_uses_the_module_its_handlers_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker job runs `python -m telemetry_collector.worker`, which runs the
    # file as `__main__`: a second copy of the module, with its own
    # SubmissionRejectedError. The tree handler raises the class of
    # telemetry_collector.worker, so the job must run through that module, or
    # every rejection would be retried as an error.
    from telemetry_collector import worker

    ran: list[str] = []

    def imported_main() -> int:
        ran.append("telemetry_collector.worker")
        return 0

    monkeypatch.setattr(worker, "main", imported_main)
    monkeypatch.delenv("COLLECTOR_WORKER_JOB_ID", raising=False)
    exit_code: object = "did not exit"
    with warnings.catch_warnings():
        # runpy warns that the module is already imported, as it is here.
        warnings.simplefilter("ignore", RuntimeWarning)
        try:
            runpy.run_module(
                "telemetry_collector.worker", run_name="__main__", alter_sys=True
            )
        except SystemExit as exit_request:
            exit_code = exit_request.code
        except RuntimeError as error:
            exit_code = f"{type(error).__name__}: {error}"

    assert ran == ["telemetry_collector.worker"]
    assert exit_code == 0
