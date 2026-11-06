"""The calibration tree package is checked on every pull request and published
to npm from main only, through npm's trusted publishing, once switched on."""

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PULL_REQUEST_WORKFLOW = WORKFLOWS / "pr.yaml"
PUBLISH_WORKFLOW = WORKFLOWS / "publish-calibration-tree.yaml"
PUBLISH_SCRIPT = ROOT / "tools" / "publish-calibration-tree.sh"
TOOLCHAIN_SCRIPT = ROOT / "tools" / "check-npm-toolchain.sh"
PACKAGE_DIRECTORY = "packages/calibration-tree"
PUBLISH_SWITCH = "vars.CALIBRATION_TREE_NPM_PUBLISH_ENABLED == 'true'"
MAIN_GUARD = "github.ref == 'refs/heads/main'"
# A full commit SHA, then the version tag as a comment, as every action here.
PINNED_ACTION = r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$"


def load_workflow(workflow_path: Path) -> dict:
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def step_commands(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def step_using(job: dict, action: str) -> dict:
    steps = [step for step in job["steps"] if step.get("uses", "").startswith(action)]
    assert len(steps) == 1, action
    return steps[0]


def step_running(job: dict, command_start: str) -> dict:
    steps = [
        step for step in job["steps"] if step.get("run", "").startswith(command_start)
    ]
    assert len(steps) == 1, command_start
    return steps[0]


def test_pull_requests_test_and_typecheck_the_package_with_a_pinned_bun() -> None:
    jobs = load_workflow(PULL_REQUEST_WORKFLOW)["jobs"]

    assert "calibration-tree" in jobs
    job = jobs["calibration-tree"]
    assert job["name"] == "Calibration tree package"
    assert job["defaults"]["run"]["working-directory"] == PACKAGE_DIRECTORY
    bun = step_using(job, "oven-sh/setup-bun@")
    assert bun["uses"].split("@")[1] == "0c5077e51419868618aeaa5fe8019c62421857d6"
    assert bun["with"]["bun-version"] == "1.4.2"
    assert step_commands(job) == [
        "bun install --frozen-lockfile",
        "bun test",
        "bun run typecheck",
    ]
    assert "calibration-tree" in jobs["required"]["needs"]


def test_publishing_runs_only_on_main_and_only_once_switched_on() -> None:
    assert PUBLISH_WORKFLOW.is_file()
    workflow = load_workflow(PUBLISH_WORKFLOW)

    assert workflow["on"] == {
        "push": {"branches": ["main"], "paths": [f"{PACKAGE_DIRECTORY}/**"]},
        "workflow_dispatch": "",
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"test", "publish"}
    for job in workflow["jobs"].values():
        assert job["if"] == f"{MAIN_GUARD} && {PUBLISH_SWITCH}"
    assert "environment" not in workflow["jobs"]["test"]
    job = workflow["jobs"]["publish"]
    assert job["needs"] == "test"
    assert job["environment"] == "npm"
    assert job["concurrency"] == {
        "group": "calibration-tree-npm-publication",
        "cancel-in-progress": "false",
    }


def test_publishing_uses_npm_trusted_publishing_and_no_stored_credential() -> None:
    assert PUBLISH_WORKFLOW.is_file()
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    jobs = load_workflow(PUBLISH_WORKFLOW)["jobs"]
    assert set(jobs) == {"test", "publish"}
    job = jobs["publish"]

    # npm accepts the job's GitHub OIDC token instead of a stored npm token.
    # Only the publish job, which installs and runs nothing from npm, can
    # request one; the test job installs dependencies and runs the tests.
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["test"]["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "NODE_AUTH_TOKEN" not in text
    assert "NPM_TOKEN" not in text
    assert "google-github-actions" not in text
    for workflow_job in jobs.values():
        for step in workflow_job["steps"]:
            if "uses" in step:
                assert re.match(PINNED_ACTION, step["uses"]), step["uses"]
        checkout = step_using(workflow_job, "actions/checkout@")
        assert checkout["with"]["persist-credentials"] == "false"
    node = step_using(job, "actions/setup-node@")
    assert node["with"]["node-version"] == "24.21.0"
    assert node["with"]["registry-url"] == "https://registry.npmjs.org"


def test_publishing_tests_the_package_before_publishing_it() -> None:
    assert PUBLISH_WORKFLOW.is_file()
    jobs = load_workflow(PUBLISH_WORKFLOW)["jobs"]

    assert set(jobs) == {"test", "publish"}
    assert step_commands(jobs["test"]) == [
        "bun install --frozen-lockfile",
        "bun test",
        "bun run typecheck",
    ]
    assert jobs["publish"]["needs"] == "test"
    assert step_commands(jobs["publish"]) == [
        "bash ../../tools/check-npm-toolchain.sh 24.21.0 11.19.0",
        "bash ../../tools/publish-calibration-tree.sh",
    ]
    assert not [
        step
        for step in jobs["publish"]["steps"]
        if step.get("uses", "").startswith("oven-sh/setup-bun@")
    ]
    for job in jobs.values():
        assert job["defaults"]["run"]["working-directory"] == PACKAGE_DIRECTORY


def test_publishing_checks_the_npm_toolchain_after_installing_node() -> None:
    assert PUBLISH_WORKFLOW.is_file()
    job = load_workflow(PUBLISH_WORKFLOW)["jobs"]["publish"]
    steps = job["steps"]
    node = step_using(job, "actions/setup-node@")
    toolchain_check = step_running(job, "bash ../../tools/check-npm-toolchain.sh ")
    publication = step_running(job, "bash ../../tools/publish-calibration-tree.sh")

    # Trusted publishing needs npm 11.5.1 or later; Node 24.21.0 bundles 11.19.0.
    assert toolchain_check["run"].split()[2:] == [
        node["with"]["node-version"],
        "11.19.0",
    ]
    assert steps.index(node) < steps.index(toolchain_check) < steps.index(publication)


def run_toolchain_script(
    tmp_path: Path, *, node_version: str, npm_version: str
) -> subprocess.CompletedProcess[str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for command_name, printed_version in (("node", node_version), ("npm", npm_version)):
        fake_command = bin_directory / command_name
        fake_command.write_text(
            "#!/usr/bin/env bash\n"
            '[[ "$*" == --version ]] || exit 3\n'
            f"echo {printed_version}\n"
        )
        fake_command.chmod(0o755)
    return subprocess.run(
        ["bash", str(TOOLCHAIN_SCRIPT), "24.21.0", "11.19.0"],
        env={"PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        check=False,
        text=True,
    )


def test_toolchain_check_accepts_the_pinned_node_and_npm(tmp_path: Path) -> None:
    assert TOOLCHAIN_SCRIPT.is_file()

    result = run_toolchain_script(
        tmp_path, node_version="v24.21.0", npm_version="11.19.0"
    )

    assert result.returncode == 0, result.stderr
    assert "v24.21.0" in result.stdout
    assert "11.19.0" in result.stdout


def test_toolchain_check_refuses_another_node(tmp_path: Path) -> None:
    assert TOOLCHAIN_SCRIPT.is_file()

    result = run_toolchain_script(
        tmp_path, node_version="v24.20.0", npm_version="11.19.0"
    )

    assert result.returncode == 1
    assert "v24.21.0" in result.stderr
    assert "v24.20.0" in result.stderr


def test_toolchain_check_refuses_another_npm(tmp_path: Path) -> None:
    assert TOOLCHAIN_SCRIPT.is_file()

    result = run_toolchain_script(
        tmp_path, node_version="v24.21.0", npm_version="11.4.2"
    )

    assert result.returncode == 1
    assert "11.19.0" in result.stderr
    assert "11.4.2" in result.stderr


def run_publish_script(
    tmp_path: Path, *, view_exit: int, view_output: str, view_error: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(
        json.dumps({"name": "@policyengine/calibration-tree", "version": "0.2.0"})
    )
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    calls = tmp_path / "npm-calls"
    fake_npm = bin_directory / "npm"
    fake_npm.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'if [[ "$1" == view ]]; then\n'
        f"  printf %s {json.dumps(view_output)}\n"
        f"  printf %s {json.dumps(view_error)} >&2\n"
        f"  exit {view_exit}\n"
        "fi\n"
    )
    fake_npm.chmod(0o755)
    result = subprocess.run(
        ["bash", str(PUBLISH_SCRIPT)],
        cwd=package,
        env={"PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        check=False,
        text=True,
    )
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return result, recorded


def test_publish_script_publishes_a_version_npm_does_not_have(tmp_path: Path) -> None:
    assert PUBLISH_SCRIPT.is_file()

    result, calls = run_publish_script(tmp_path, view_exit=0, view_output="")

    assert result.returncode == 0, result.stderr
    assert calls == [
        "view @policyengine/calibration-tree@0.2.0 version",
        "publish --access public",
    ]


def test_publish_script_publishes_the_first_version_of_a_new_package(
    tmp_path: Path,
) -> None:
    assert PUBLISH_SCRIPT.is_file()

    result, calls = run_publish_script(
        tmp_path, view_exit=1, view_output="", view_error="npm error code E404\n"
    )

    assert result.returncode == 0, result.stderr
    assert calls[-1] == "publish --access public"


def test_publish_script_skips_a_version_already_published(tmp_path: Path) -> None:
    assert PUBLISH_SCRIPT.is_file()

    result, calls = run_publish_script(tmp_path, view_exit=0, view_output="0.2.0\n")

    assert result.returncode == 0, result.stderr
    assert calls == ["view @policyengine/calibration-tree@0.2.0 version"]
    assert "@policyengine/calibration-tree@0.2.0 is already on npm" in result.stdout


def test_publish_script_stops_when_npm_cannot_be_asked(tmp_path: Path) -> None:
    assert PUBLISH_SCRIPT.is_file()

    result, calls = run_publish_script(
        tmp_path, view_exit=1, view_output="", view_error="npm error code ETIMEDOUT\n"
    )

    assert result.returncode == 1
    assert calls == ["view @policyengine/calibration-tree@0.2.0 version"]
    assert "ETIMEDOUT" in result.stderr
