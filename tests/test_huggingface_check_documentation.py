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


def test_the_readme_names_every_variable_only_production_reads() -> None:
    section = readme_section()

    assert production_only_variables() == {
        "TELEMETRY_HF_CHECK_JOB",
        "TELEMETRY_WORKER_SERVICE_ACCOUNT",
        "TELEMETRY_HF_READ_SECRET_NAME",
    }
    for variable_name in production_only_variables():
        assert f"`{variable_name}`" in section


def test_the_readme_says_how_the_check_runs_and_reaches_a_person() -> None:
    section = readme_section()

    assert "python -m telemetry_collector.huggingface_check" in section
    assert "Cloud Scheduler" in section
    assert "Cloud Monitoring" in section
    assert "configure-huggingface-check-job" in section


def test_the_readme_says_what_switching_on_the_releases_json_check_needs() -> None:
    section = readme_section()

    assert "`checks_releases_record`" in section
    assert RELEASE_RULES_DISTRIBUTION in section
    assert f"`{RELEASE_RULES_MODULE}`" in section
    assert "`manifest_file_digests`" in section
    assert "tests/test_releases_record_check.py" in section


def test_the_readme_gives_the_schedule_the_absence_alert_relies_on() -> None:
    section = readme_section()

    assert "`0 6,18 * * *`" in section
    assert "no execution has succeeded for 23 hours" in section
