"""The daily workflow that starts the Hugging Face check and keeps its issues."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CHECK_WORKFLOW = WORKFLOWS / "huggingface-check.yaml"
PUSH_WORKFLOW = WORKFLOWS / "push.yaml"
CHECK_ENVIRONMENT = "huggingface-check"
#: Everything the job reads from its environment is a secret, so that GitHub
#: hides each value in the public workflow log: the Workload Identity provider,
#: the account that may only start the check job and read its logs, and the
#: project, region and job names.
CHECK_SECRETS = {
    "TELEMETRY_GCP_WIF_PROVIDER",
    "TELEMETRY_HF_CHECK_SERVICE_ACCOUNT",
    "TELEMETRY_GCP_PROJECT_ID",
    "TELEMETRY_GCP_REGION",
    "TELEMETRY_HF_CHECK_JOB",
}
RUN_COMMAND = (
    "uv run --frozen --no-dev python -m telemetry_collector.huggingface_issues"
)


def load_workflow(path: Path) -> dict:
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def check_workflow() -> dict:
    assert CHECK_WORKFLOW.exists(), CHECK_WORKFLOW
    return load_workflow(CHECK_WORKFLOW)


def check_job() -> dict:
    jobs = check_workflow()["jobs"]
    assert set(jobs) == {"check"}
    return jobs["check"]


def step_using(job: dict, action: str) -> dict:
    matching = [
        step for step in job["steps"] if step.get("uses", "").startswith(f"{action}@")
    ]
    assert len(matching) == 1, action
    return matching[0]


def test_the_workflow_runs_daily_off_the_hour_and_by_hand() -> None:
    triggers = check_workflow()["on"]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 6 * * *"}]
    dry_run = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    # A run by hand only reports what would change unless told otherwise.
    assert dry_run["default"] == "true"


def test_only_main_in_this_repository_runs_the_check() -> None:
    job = check_job()

    assert "github.repository == 'PolicyEngine/microcosm-collector'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert job["environment"] == CHECK_ENVIRONMENT


def test_the_job_alone_holds_the_token_and_issue_permissions() -> None:
    workflow = check_workflow()
    job = check_job()

    assert workflow["permissions"] == {}
    assert job["permissions"] == {
        "contents": "read",
        "issues": "write",
        "id-token": "write",
    }


def test_one_run_at_a_time_and_none_cancelled() -> None:
    concurrency = check_workflow()["concurrency"]

    assert concurrency == {"group": "huggingface-check", "cancel-in-progress": "false"}


def test_every_setting_is_a_secret_so_the_public_log_hides_it() -> None:
    job_text = (
        CHECK_WORKFLOW.read_text(encoding="utf-8") if CHECK_WORKFLOW.exists() else ""
    )
    job = check_job()

    assert set(re.findall(r"secrets\.(\w+)", job_text)) == CHECK_SECRETS
    assert "vars." not in job_text
    assert job["env"] == {
        "PROJECT_ID": "${{ secrets.TELEMETRY_GCP_PROJECT_ID }}",
        "REGION": "${{ secrets.TELEMETRY_GCP_REGION }}",
        "HF_CHECK_JOB": "${{ secrets.TELEMETRY_HF_CHECK_JOB }}",
    }


def test_the_job_authenticates_as_the_check_account_then_runs_the_command() -> None:
    job = check_job()
    auth = step_using(job, "google-github-actions/auth")
    run_steps = [step for step in job["steps"] if "run" in step]

    assert auth["with"] == {
        "workload_identity_provider": "${{ secrets.TELEMETRY_GCP_WIF_PROVIDER }}",
        "service_account": "${{ secrets.TELEMETRY_HF_CHECK_SERVICE_ACCOUNT }}",
    }
    assert len(run_steps) == 1
    command = run_steps[0]
    assert command["run"] == RUN_COMMAND
    assert command["env"] == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "DRY_RUN": "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run }}",
    }
    steps = job["steps"]
    assert steps.index(auth) < steps.index(
        step_using(job, "google-github-actions/setup-gcloud")
    )
    assert steps.index(
        step_using(job, "google-github-actions/setup-gcloud")
    ) < steps.index(command)
    assert int(job["timeout-minutes"]) >= 45


def test_the_workflow_pins_the_actions_the_deploy_pins() -> None:
    pinned = re.compile(r"uses: ([\w./-]+)@([0-9a-f]{40}) # (v[\d.]+)")
    deploy_pins = {
        action: (commit, version)
        for action, commit, version in pinned.findall(PUSH_WORKFLOW.read_text())
    }
    job = check_job()
    check_text = CHECK_WORKFLOW.read_text(encoding="utf-8")

    used = {step["uses"].split("@")[0] for step in job["steps"] if "uses" in step}
    assert used == {
        "actions/checkout",
        "astral-sh/setup-uv",
        "google-github-actions/auth",
        "google-github-actions/setup-gcloud",
    }
    for action, commit, version in pinned.findall(check_text):
        assert deploy_pins.get(action, (commit, version))[0] == commit, action
    assert len(pinned.findall(check_text)) == len(used)
