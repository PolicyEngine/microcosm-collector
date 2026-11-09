"""Calibration trees are built from calibration-diagnostics submissions.

The handler plans a release or staging tree from the submission's provenance,
runs the tree package's Bun worker on the downloaded files without the Blob
token, then runs its publication command under one Postgres lock. These tests
replace the two Bun processes with a recording command runner and run the
handler through the worker framework, so each exit code ends as the framework
records it: succeeded, rejected, or returned for another attempt.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
import yaml

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.database import create_database_engine
from telemetry_collector.submissions import SubmissionRequest, intake_object_name
from telemetry_collector.worker import DEFAULT_HANDLERS, JobOutcome, run_job
from tests.fakes import FakeIntakeObjectStore, FakeSubmissionStore
from tests.test_submission_store import SteppingClock
from tests.test_submissions import calibration_submission

TREES_MODULE = "telemetry_collector.calibration_trees"
RUN_ID = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
UK_DEFAULT_RELEASE = "microcosm-uk-2024-25-national"
UK_RELEASE_REPO = "policyengine/populace-uk-private"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# SteppingClock's time, when the fake store accepts a submission.
ACCEPTED_AT = "2026-10-20T09:00:00Z"
BLOB_TOKEN = "vercel_blob_rw_test_token"
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

    def run(self, handler: Any) -> JobOutcome:
        return run_job(
            self.job_id,
            submissions=self.submissions,
            objects=self.objects,
            handlers={**DEFAULT_HANDLERS, "calibration-diagnostics": handler},
            holder="exec-1",
        )

    def record(self) -> Any:
        record = self.submissions.get_submission(self.submission_id)
        assert record is not None
        return record


def accepted(submission_provenance: Mapping[str, Any]) -> Accepted:
    submission = calibration_submission()
    submission["provenance"] = dict(submission_provenance)
    for file in submission["files"]:
        file["size"] = len(CONTENTS[file["name"]])
        file["sha256"] = hashlib.sha256(CONTENTS[file["name"]]).hexdigest()
    submissions = FakeSubmissionStore(clock=SteppingClock())
    objects = FakeIntakeObjectStore()
    record, _ = submissions.create_submission(
        SubmissionRequest.model_validate(submission),
        HuggingFacePrincipal("hf-user-1", "builder", ("policyengine",)),
    )
    generations = {
        file["name"]: objects.upload(
            intake_object_name(record.submission_id, file["name"]),
            CONTENTS[file["name"]],
            sha256=file["sha256"],
        )
        for file in record.files
    }
    acceptance = submissions.accept_submission(record.submission_id, generations)
    assert acceptance.job_id is not None
    submissions.record_dispatch(acceptance.job_id, 1)
    return Accepted(submissions, objects, record.submission_id, acceptance.job_id)


def plan_for(submission_provenance: Mapping[str, Any]) -> Any:
    return trees().plan_tree_build(accepted(submission_provenance).record())


@dataclass
class RecordingRunner:
    """Stands in for the two Bun processes, recording what each one is given."""

    build_exit: int = 0
    build_stderr: str = ""
    publication_exit: int = 0
    publication_stderr: str = ""
    events: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        step = "publication" if argv[2].endswith("publication.ts") else "build"
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


def handler_with(runner: RecordingRunner, **settings: Any) -> Any:
    @contextmanager
    def recorded_lock() -> Iterator[None]:
        runner.events.append("lock taken")
        yield
        runner.events.append("lock released")

    return trees().calibration_tree_handler(
        blob_token=BLOB_TOKEN,
        manifest_lock=recorded_lock,
        bun="/usr/local/bin/bun",
        run_command=runner,
        **settings,
    )


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
        (
            provenance(
                f"staged/{RUN_ID}", repo="policyengine/populace-fr", country_code="FR"
            ),
            "country_not_registered",
        ),
    ],
    ids=["other-folder", "other-country"],
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

    accepted(RELEASED).run(handler_with(runner))

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
    trees_module = trees()

    without_token = trees_module.handlers_from_environment(engine=None, environ={})
    with_token = trees_module.handlers_from_environment(
        engine=None,
        environ={"COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN": BLOB_TOKEN},
    )

    assert without_token == DEFAULT_HANDLERS
    assert with_token["orrery-inputs"] is DEFAULT_HANDLERS["orrery-inputs"]
    assert (
        with_token["calibration-diagnostics"]
        is not DEFAULT_HANDLERS["calibration-diagnostics"]
    )


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


def run_tree_image_check(
    tmp_path: Path, *, build_exit: int, build_answer: str, publication_exit: int
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
    assert len(calls) == 2
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
