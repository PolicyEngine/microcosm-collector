"""Each merge to main is released in two passes: version, then verify, tag, deploy."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PUSH_WORKFLOW = ROOT / ".github" / "workflows" / "push.yaml"
TAG_SCRIPT = ROOT / "tools" / "publish-git-tag.sh"
RELEASE_COMMIT = "Update microcosm-collector"
APP_TOKEN = "${{ steps.app-token.outputs.token }}"


def push_jobs() -> dict:
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(PUSH_WORKFLOW.read_text(), Loader=yaml.BaseLoader)["jobs"]


def secrets_used_by(job: dict) -> set[str]:
    return set(re.findall(r"secrets\.(\w+)", yaml.safe_dump(job)))


def test_release_commit_is_verified_then_tagged_then_deployed() -> None:
    jobs = push_jobs()

    assert set(jobs) == {
        "versioning",
        "verify",
        "tag",
        "deploy-staging",
        "deploy-production",
    }
    collector_only = "github.repository == 'PolicyEngine/microcosm-collector'"
    release_message = "github.event.head_commit.message"
    for job_name in ("versioning", "verify"):
        assert collector_only in jobs[job_name]["if"]
    assert f"{release_message} != '{RELEASE_COMMIT}'" in jobs["versioning"]["if"]
    assert f"{release_message} == '{RELEASE_COMMIT}'" in jobs["verify"]["if"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["tag"]["needs"] == "verify"
    assert jobs["tag"]["permissions"] == {"contents": "write"}
    assert jobs["tag"]["steps"][-1]["run"] == "bash tools/publish-git-tag.sh"
    assert jobs["deploy-staging"]["needs"] == "tag"
    assert jobs["deploy-production"]["needs"] == "deploy-staging"


def test_versioning_commits_the_release_with_the_app_token() -> None:
    steps = push_jobs()["versioning"]["steps"]

    assert steps[0]["id"] == "app-token"
    assert steps[0]["uses"].startswith("actions/create-github-app-token@")
    assert steps[0]["with"] == {
        "app-id": "${{ secrets.APP_ID }}",
        "private-key": "${{ secrets.APP_PRIVATE_KEY }}",
    }
    assert steps[1]["uses"].startswith("actions/checkout@")
    assert steps[1]["with"]["token"] == APP_TOKEN
    assert steps[-2]["run"] == "bash tools/update-version.sh"
    assert steps[-1]["uses"].startswith("EndBug/add-and-commit@")
    assert steps[-1]["with"]["message"] == RELEASE_COMMIT
    assert steps[-1]["with"]["github_token"] == APP_TOKEN


def test_only_versioning_and_deployment_read_secrets() -> None:
    jobs = push_jobs()

    assert {job_name: secrets_used_by(job) for job_name, job in jobs.items()} == {
        "versioning": {"APP_ID", "APP_PRIVATE_KEY"},
        "verify": set(),
        "tag": set(),
        "deploy-staging": {
            "TELEMETRY_GCP_WIF_PROVIDER",
            "TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT",
        },
        "deploy-production": {
            "TELEMETRY_GCP_WIF_PROVIDER",
            "TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT",
        },
    }


def isolated_git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Release Author",
        "GIT_AUTHOR_EMAIL": "release@example.invalid",
        "GIT_COMMITTER_NAME": "Release Author",
        "GIT_COMMITTER_EMAIL": "release@example.invalid",
    }


def run_git(work_tree: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(work_tree), *arguments],
        env=isolated_git_environment(),
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def commit_release(checkout: Path, version: str) -> str:
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "collector"\nversion = "{version}"\n'
    )
    run_git(checkout, "add", "pyproject.toml")
    run_git(checkout, "commit", "--quiet", "--message", RELEASE_COMMIT)
    return run_git(checkout, "rev-parse", "HEAD")


@pytest.fixture
def release_checkout(tmp_path: Path) -> Path:
    """A checkout of a release commit whose origin is a local bare store."""

    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "--quiet", "--bare", str(origin))
    checkout = tmp_path / "checkout"
    run_git(tmp_path, "clone", "--quiet", str(origin), str(checkout))
    return checkout


def run_tag_script(checkout: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(TAG_SCRIPT)],
        cwd=checkout,
        env=isolated_git_environment(),
        capture_output=True,
        check=False,
        text=True,
    )


def test_tag_script_tags_the_release_commit_with_the_bare_version(
    release_checkout: Path,
) -> None:
    release_commit = commit_release(release_checkout, "0.2.0")

    result = run_tag_script(release_checkout)

    assert result.returncode == 0, result.stderr
    remote_tags = run_git(release_checkout, "ls-remote", "--tags", "origin")
    assert remote_tags == f"{release_commit}\trefs/tags/0.2.0"


def test_tag_script_accepts_a_rerun_on_the_same_release_commit(
    release_checkout: Path,
) -> None:
    commit_release(release_checkout, "0.2.0")
    assert run_tag_script(release_checkout).returncode == 0

    result = run_tag_script(release_checkout)

    assert result.returncode == 0, result.stderr
    assert "Tag 0.2.0 already exists." in result.stdout


def test_tag_script_refuses_a_version_tagged_on_another_commit(
    release_checkout: Path,
) -> None:
    commit_release(release_checkout, "0.2.0")
    assert run_tag_script(release_checkout).returncode == 0
    (release_checkout / "notes.txt").write_text("A later change.\n")
    run_git(release_checkout, "add", "notes.txt")
    run_git(release_checkout, "commit", "--quiet", "--message", RELEASE_COMMIT)

    result = run_tag_script(release_checkout)

    assert result.returncode == 1
    assert "not the release commit" in result.stderr
