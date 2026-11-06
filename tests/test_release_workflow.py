"""Each merge to main is released in two passes: version, then verify, tag, deploy."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PUSH_WORKFLOW = ROOT / ".github" / "workflows" / "push.yaml"
TAG_SCRIPT = ROOT / "tools" / "publish-git-tag.sh"
PUBLISH_CLIENT_SCRIPT = ROOT / "tools" / "publish-client.sh"
CLIENT_PACKAGE_NAME = "@policyengine/microcosm-collector-client"
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
        "publish-client",
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
    assert jobs["publish-client"]["needs"] == "deploy-production"


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
        "publish-client": set(),
    }


def test_client_is_published_after_production_only_when_enabled() -> None:
    jobs = push_jobs()
    assert "publish-client" in jobs
    job = jobs["publish-client"]
    steps = job["steps"]

    # Published only once production runs the release the client describes,
    # and only once publishing is switched on.
    assert job["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
        " && vars.COLLECTOR_CLIENT_PUBLISH_ENABLED == 'true'"
    )
    # npm's trusted publisher names this workflow file and this environment.
    assert job["environment"] == "npm"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["concurrency"]["cancel-in-progress"] == "false"
    assert job["defaults"]["run"]["working-directory"] == "packages/collector-client"
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0]["with"] == {"persist-credentials": "false"}
    assert steps[1]["uses"].startswith("actions/setup-node@")
    assert steps[1]["with"] == {
        "node-version": "24.22.1",
        "registry-url": "https://registry.npmjs.org",
        "package-manager-cache": "false",
    }
    assert [step["run"] for step in steps[2:]] == [
        "npm ci",
        "bash ../../tools/publish-client.sh",
    ]


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


# Stands in for npm: records each call and answers `npm view` from
# STUB_PUBLISHED, the versions on npm. With none, the package does not exist.
STUB_NPM = """#!/usr/bin/env bash
echo "$*" >> "$NPM_CALLS"
case "$1" in
  --version) echo "$STUB_NPM_VERSION" ;;
  view)
    if [ -z "$STUB_PUBLISHED" ]; then echo "npm error code E404" >&2; exit 1; fi
    for published in $STUB_PUBLISHED; do
      if [ "$published" = "${2##*@}" ]; then echo "$published"; fi
    done ;;
  publish) ;;
  *) exit 64 ;;
esac
"""


def run_publish_script(
    tmp_path: Path,
    *,
    client_version: str = "0.9.0",
    release_version: str = "0.9.0",
    npm_version: str = "11.19.1",
    published_versions: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    collector_root = tmp_path / "collector"
    client_dir = collector_root / "packages" / "collector-client"
    client_dir.mkdir(parents=True)
    (collector_root / "pyproject.toml").write_text(
        f'[project]\nname = "microcosm-collector"\nversion = "{release_version}"\n'
    )
    (client_dir / "package.json").write_text(
        json.dumps({"name": CLIENT_PACKAGE_NAME, "version": client_version})
    )
    stub_directory = tmp_path / "stub-bin"
    stub_directory.mkdir()
    (stub_directory / "npm").write_text(STUB_NPM)
    (stub_directory / "npm").chmod(0o755)
    calls_path = tmp_path / "npm-calls.txt"
    calls_path.touch()
    result = subprocess.run(
        ["bash", str(PUBLISH_CLIENT_SCRIPT), str(collector_root)],
        env={
            "PATH": f"{stub_directory}{os.pathsep}{os.environ['PATH']}",
            "HOME": os.environ.get("HOME", ""),
            "NPM_CALLS": str(calls_path),
            "STUB_NPM_VERSION": npm_version,
            "STUB_PUBLISHED": " ".join(published_versions),
        },
        capture_output=True,
        check=False,
        text=True,
    )
    return result, calls_path.read_text().splitlines()


@pytest.mark.parametrize(
    "published_versions", [(), ("0.8.0",)], ids=["new package", "new version"]
)
def test_publish_script_publishes_a_version_npm_lacks(
    tmp_path: Path, published_versions: tuple[str, ...]
) -> None:
    result, npm_calls = run_publish_script(
        tmp_path, published_versions=published_versions
    )

    assert result.returncode == 0, result.stderr
    assert npm_calls == [
        "--version",
        f"view {CLIENT_PACKAGE_NAME}@0.9.0 version",
        "publish --access public",
    ]


def test_publish_script_accepts_a_rerun_after_publishing(tmp_path: Path) -> None:
    result, npm_calls = run_publish_script(
        tmp_path, published_versions=("0.8.0", "0.9.0")
    )

    assert result.returncode == 0, result.stderr
    assert f"{CLIENT_PACKAGE_NAME}@0.9.0 is already on npm." in result.stdout
    assert "publish --access public" not in npm_calls


def test_publish_script_refuses_a_client_version_other_than_the_release(
    tmp_path: Path,
) -> None:
    result, npm_calls = run_publish_script(
        tmp_path, client_version="0.8.0", release_version="0.9.0"
    )

    assert result.returncode == 1
    assert "not the release's version 0.9.0" in result.stderr
    assert npm_calls == []


def test_publish_script_refuses_npm_without_trusted_publishing(
    tmp_path: Path,
) -> None:
    result, npm_calls = run_publish_script(tmp_path, npm_version="11.5.0")

    assert result.returncode == 1
    assert "needs 11.5.1 or later" in result.stderr
    assert npm_calls == ["--version"]
