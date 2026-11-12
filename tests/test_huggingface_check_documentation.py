"""The README says how the daily check runs, opens issues and is switched on."""

import re
from pathlib import Path

from telemetry_collector.huggingface_check import (
    RELEASE_RULES_DISTRIBUTION,
    RELEASE_RULES_MODULE,
)

ROOT = Path(__file__).resolve().parents[1]
SECTION_HEADING = "## Daily Hugging Face check"


def readme_section() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert SECTION_HEADING in readme
    return readme.split(SECTION_HEADING, 1)[1].split("\n## ", 1)[0]


def production_only_variables() -> set[str]:
    workflow = (ROOT / ".github" / "workflows" / "push.yaml").read_text()
    staging, production = workflow.split("  deploy-production:", 1)
    variable_pattern = r"vars\.(TELEMETRY_\w+)"
    return set(re.findall(variable_pattern, production)) - set(
        re.findall(variable_pattern, staging)
    )


#: The variables the check job's production deploy reads. Each environment
#: has its own read token and worker service account, so staging reads the
#: last two as well; only the job's name is production's alone.
CHECK_JOB_VARIABLES = {
    "TELEMETRY_HF_CHECK_JOB",
    "TELEMETRY_WORKER_SERVICE_ACCOUNT",
    "TELEMETRY_HF_READ_SECRET_NAME",
}


def test_the_readme_names_every_variable_the_check_job_reads() -> None:
    section = readme_section()

    assert production_only_variables() == {"TELEMETRY_HF_CHECK_JOB"}
    for variable_name in CHECK_JOB_VARIABLES:
        assert f"`{variable_name}`" in section


def test_the_readme_says_how_the_check_runs_and_reaches_a_person() -> None:
    section = readme_section()

    assert "python -m telemetry_collector.huggingface_check" in section
    assert "python -m telemetry_collector.huggingface_issues" in section
    assert "`.github/workflows/huggingface-check.yaml`" in section
    assert "configure-huggingface-check-job" in section
    assert "GitHub issue" in section
    # Issues replace the email alerts, and the workflow replaces the schedule.
    assert "Cloud Scheduler" not in section
    assert "Cloud Monitoring" not in section
    assert "notification channel" not in section


def test_the_readme_says_what_switching_on_the_releases_json_check_needs() -> None:
    section = readme_section()

    assert "`checks_releases_record`" in section
    assert RELEASE_RULES_DISTRIBUTION in section
    assert f"`{RELEASE_RULES_MODULE}`" in section
    assert "`manifest_file_digests`" in section
    assert "tests/test_releases_record_check.py" in section


def test_the_readme_says_which_manifest_fields_the_file_check_reads() -> None:
    # Decided 2026-10-29: a build's release manifest keeps its `artifacts` map,
    # each entry giving the file's bare `path` in the build folder and its
    # `sha256`; the check no longer assumes a `files` list.
    section = " ".join(readme_section().split())

    assert "`artifacts` map" in section
    assert "`path`" in section
    assert "`build_manifest.json`" in section
    assert "`sha256sums.txt`" in section
    assert "`files` list" not in section


def test_the_readme_gives_the_workflow_schedule_and_its_dry_run() -> None:
    section = " ".join(readme_section().split())

    assert "`17 6 * * *`" in section
    assert "dry run" in section
    assert "no successful execution" not in section


def test_the_readme_says_when_issues_open_update_reopen_and_close() -> None:
    section = " ".join(readme_section().split())

    for verb in ("opens", "updates", "reopens", "closes"):
        assert verb in section, verb
    assert "did not report a result" in section
    assert "`huggingface-check` label" in section


def test_the_readme_names_the_check_environment_and_its_secrets() -> None:
    section = readme_section()

    assert "`huggingface-check` environment" in section
    for secret in (
        "TELEMETRY_GCP_WIF_PROVIDER",
        "TELEMETRY_HF_CHECK_SERVICE_ACCOUNT",
        "TELEMETRY_GCP_PROJECT_ID",
        "TELEMETRY_GCP_REGION",
        "TELEMETRY_HF_CHECK_JOB",
    ):
        assert f"`{secret}`" in section, secret


#: The deploy's Workload Identity provider must check the environment before
#: the check's job first runs: without the clause, that job's token passes the
#: deploy's provider too, and the deployment account trusts the whole repo.
DEPLOY_PROVIDER_CONDITION = (
    "assertion.repository=='PolicyEngine/microcosm-collector'"
    " && assertion.ref=='refs/heads/main'"
    " && assertion.environment in ['staging', 'production']"
)


def test_the_readme_says_the_deploy_provider_must_check_the_environment_first() -> None:
    section = " ".join(readme_section().split())

    assert DEPLOY_PROVIDER_CONDITION in section
    assert "before the workflow first runs" in section


def test_no_comment_or_runbook_still_describes_the_schedule_and_alerts() -> None:
    deploy_script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    function = deploy_script.split("configure_huggingface_check_job() {", 1)[1]
    function = function.split("\n}\n", 1)[0]
    push_workflow = (ROOT / ".github" / "workflows" / "push.yaml").read_text()
    check_variable = push_workflow.index(
        "HF_CHECK_JOB: ${{ vars.TELEMETRY_HF_CHECK_JOB }}"
    )
    comment_start = push_workflow.rindex("\n      # Only production", 0, check_variable)
    comment_above = push_workflow[comment_start:check_variable]
    runbook = (ROOT / "docs" / "project-move-runbook.md").read_text(encoding="utf-8")
    check_item = runbook.split("1. **The collector's own Hugging Face token", 1)[1]
    check_item = check_item.split("\n2. ", 1)[0]

    assert "Cloud Scheduler" not in function and "alert" not in function
    assert "alerts" not in comment_above
    for old_resource in ("Cloud Scheduler", "notification channel", "Cloud Monitoring"):
        assert old_resource not in check_item, old_resource
    assert "`huggingface-check` environment" in " ".join(check_item.split())
    # Step 4.2 creates the deploy's provider without the environment clause,
    # so the item gives the command that adds it.
    assert DEPLOY_PROVIDER_CONDITION in " ".join(check_item.split())
    assert "providers update-oidc" in check_item
