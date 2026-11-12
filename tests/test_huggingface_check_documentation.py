"""The README says how the daily check runs, alerts and is switched on."""

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
