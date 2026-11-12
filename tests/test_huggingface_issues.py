"""The daily workflow starts the Hugging Face check and keeps its issues."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from telemetry_collector.huggingface_check import CONFIGURATION_CHECK, TOKEN_CHECK
from telemetry_collector.huggingface_issues import (
    BOT_LOGIN,
    EXECUTION_POLL_SECONDS,
    LABEL,
    LOG_READ_ATTEMPTS,
    LOG_READ_INTERVAL_SECONDS,
    MAX_NEW_ISSUES_PER_RUN,
    NO_RESULT_KEY,
    NO_RESULT_TITLE,
    SETTINGS_EXIT_CODE,
    STATE_PATTERN,
    TITLE_PREFIX,
    CheckJob,
    GcloudError,
    GitHubClient,
    GitHubError,
    IssueSync,
    Problem,
    Report,
    RunContext,
    main,
    parse_report,
    run_check,
)

JOB = CheckJob(project="example-project", region="example-region", name="example-job")
EXECUTION = "example-job-x7k2p"
CONTEXT = RunContext(
    started_at=datetime(2026, 11, 12, 6, 17, tzinfo=UTC),
    run_url="https://github.com/PolicyEngine/microcosm-collector/actions/runs/1",
)
US = "policyengine/populace-us"
UK = "policyengine/populace-uk-private"


def summary_payload(problems: int) -> dict[str, Any]:
    return {
        "severity": "ERROR" if problems else "INFO",
        "message": "Hugging Face check passed." if not problems else "found",
        "checked_datasets": [US, UK],
        "releases_record_checked": [],
        "problems": problems,
    }


def finding_payload(dataset: str, code: str, message: str = "") -> dict[str, Any]:
    return {
        "severity": "ERROR",
        "message": message or f"{dataset} answered {code}.",
        "check": TOKEN_CHECK,
        "dataset": dataset,
        "code": code,
    }


def log_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """A log entry as `gcloud logging read` prints it: severity lifted out."""
    fields = dict(payload)
    severity = fields.pop("severity")
    return {"severity": severity, "jsonPayload": fields, "labels": {}}


def problem(dataset: str, code: str, *messages: str) -> Problem:
    return Problem(TOKEN_CHECK, dataset, code, messages or (f"{dataset} {code}",))


class FakeGcloud:
    """Answers the three gcloud commands the workflow runs, and records them."""

    def __init__(
        self,
        *,
        completion: Sequence[str] = ("True",),
        log_reads: Sequence[list[dict[str, Any]]] = ([],),
        failing_command: tuple[str, ...] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._completion: Iterator[str] = iter(completion)
        self._last_completion = completion[-1]
        self._log_reads = list(log_reads)
        self._failing_command = failing_command

    def __call__(self, arguments: Sequence[str]) -> str:
        arguments = list(arguments)
        self.calls.append(arguments)
        if (
            self._failing_command
            and tuple(arguments[: len(self._failing_command)]) == self._failing_command
        ):
            raise GcloudError("gcloud exited with status 1")
        if arguments[:3] == ["run", "jobs", "execute"]:
            return f"{EXECUTION}\n"
        if arguments[:4] == ["run", "jobs", "executions", "describe"]:
            status = next(self._completion, self._last_completion)
            return json.dumps(
                {"status": {"conditions": [{"type": "Completed", "status": status}]}}
            )
        if arguments[:2] == ["logging", "read"]:
            entries = (
                self._log_reads.pop(0)
                if len(self._log_reads) > 1
                else (self._log_reads[0])
            )
            return json.dumps([log_entry(payload) for payload in entries])
        raise AssertionError(f"unexpected gcloud command: {arguments}")


class FakeClock:
    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeGitHub:
    """Keeps issues, comments and labels in memory, as the GitHub API would."""

    def __init__(self, labels: set[str] | None = None) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.labels = set(labels or set())
        self.writes: list[tuple[str, Any]] = []

    def add_issue(self, title: str, body: str, *, login: str = BOT_LOGIN) -> int:
        number = len(self.issues) + 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "state_reason": None,
            "user": {"login": login},
            "labels": [{"name": LABEL}],
        }
        return number

    def close(self, number: int, reason: str) -> None:
        self.issues[number].update(state="closed", state_reason=reason)

    def list_issues(self, creator: str, label: str) -> list[dict[str, Any]]:
        # GitHub filters by creator; the fake doesn't, so the sync's own check
        # of who opened an issue is what these tests exercise.
        return [
            json.loads(json.dumps(issue))
            for issue in self.issues.values()
            if label in {entry["name"] for entry in issue["labels"]}
        ]

    def list_labels(self) -> set[str]:
        return set(self.labels)

    def create_label(self, name: str, color: str, description: str) -> None:
        self.writes.append(("create_label", name))
        self.labels.add(name)

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        self.writes.append(("create_issue", (title, body, labels)))
        number = self.add_issue(title, body)
        self.issues[number]["labels"] = [{"name": name} for name in labels]
        return json.loads(json.dumps(self.issues[number]))

    def update_issue(self, number: int, fields: dict[str, object]) -> None:
        self.writes.append(("update_issue", (number, dict(fields))))
        self.issues[number].update(fields)

    def add_comment(self, number: int, body: str) -> None:
        self.writes.append(("add_comment", (number, body)))
        self.comments.setdefault(number, []).append(body)

    def written_text(self) -> str:
        return json.dumps(self.writes)


def sync(github: FakeGitHub, report: Report, *, dry_run: bool = False) -> dict:
    actions = IssueSync(github, CONTEXT, dry_run=dry_run).sync(report)
    return {action.key: (action.action, action.issue_number) for action in actions}


def issue_key(issue: dict[str, Any]) -> str | None:
    match = STATE_PATTERN.search(issue["body"])
    return json.loads(match.group(1)).get("key") if match else None


# -- Reading one execution's log entries --------------------------------------


def test_a_summary_with_no_problems_is_a_completed_run_without_problems() -> None:
    report = parse_report([summary_payload(0)])

    assert report.completed
    assert report.problems == ()


def test_problem_entries_become_one_problem_per_check_dataset_and_code() -> None:
    report = parse_report(
        [
            finding_payload(US, "HTTP_404", "first"),
            finding_payload(UK, "HTTP_401"),
            finding_payload(US, "HTTP_404", "second"),
            summary_payload(3),
        ]
    )

    assert report.completed
    assert [item.key for item in report.problems] == sorted(
        [f"{TOKEN_CHECK}/{UK}/HTTP_401", f"{TOKEN_CHECK}/{US}/HTTP_404"]
    )
    us_problem = next(item for item in report.problems if item.dataset == US)
    assert us_problem.messages == ("first", "second")


def test_a_configuration_entry_means_the_run_reported_no_result() -> None:
    report = parse_report(
        [
            {
                "severity": "ERROR",
                "message": "COLLECTOR_HF_READ_TOKEN is not set.",
                "check": CONFIGURATION_CHECK,
            }
        ]
    )

    assert not report.completed
    assert report.problems is None
    assert "COLLECTOR_HF_READ_TOKEN is not set." in report.reason


def test_a_log_without_its_summary_entry_means_no_result() -> None:
    report = parse_report([finding_payload(US, "HTTP_404")])

    assert not report.completed
    assert "summary" in report.reason


def test_a_summary_counting_more_problems_than_were_read_means_no_result() -> None:
    report = parse_report([finding_payload(US, "HTTP_404"), summary_payload(2)])

    assert not report.completed
    assert "2" in report.reason and "1" in report.reason


# -- Starting the job and reading its entries ----------------------------------


def test_run_check_starts_the_job_waits_and_reads_that_executions_entries() -> None:
    gcloud = FakeGcloud(
        log_reads=[[finding_payload(US, "HTTP_404"), summary_payload(1)]]
    )
    clock = FakeClock()

    report = run_check(gcloud, JOB, sleep=clock.sleep, clock=clock)

    assert report.completed
    assert [item.key for item in report.problems] == [f"{TOKEN_CHECK}/{US}/HTTP_404"]
    start, describe, read = gcloud.calls
    assert start == [
        "run",
        "jobs",
        "execute",
        "example-job",
        "--project",
        "example-project",
        "--region",
        "example-region",
        "--format=value(metadata.name)",
    ]
    assert describe[:5] == ["run", "jobs", "executions", "describe", EXECUTION]
    assert ["--project", "example-project", "--region", "example-region"] == (
        describe[5:9]
    )
    assert read[:2] == ["logging", "read"]
    assert 'resource.labels.job_name="example-job"' in read[2]
    assert f'labels."run.googleapis.com/execution_name"="{EXECUTION}"' in read[2]
    assert "--project" in read and "example-project" in read


def test_run_check_waits_until_the_execution_completes() -> None:
    gcloud = FakeGcloud(
        completion=("Unknown", "Unknown", "True"), log_reads=[[summary_payload(0)]]
    )
    clock = FakeClock()

    report = run_check(gcloud, JOB, sleep=clock.sleep, clock=clock)

    assert report.completed
    assert clock.sleeps == [EXECUTION_POLL_SECONDS, EXECUTION_POLL_SECONDS]


def test_a_failed_execution_that_logged_its_problems_is_a_completed_run() -> None:
    # The check exits 1 when it finds problems, so its execution fails.
    gcloud = FakeGcloud(
        completion=("False",),
        log_reads=[[finding_payload(UK, "HTTP_401"), summary_payload(1)]],
    )
    clock = FakeClock()

    report = run_check(gcloud, JOB, sleep=clock.sleep, clock=clock)

    assert report.completed
    assert [item.dataset for item in report.problems] == [UK]


def test_a_job_that_cannot_be_started_reports_no_result() -> None:
    gcloud = FakeGcloud(failing_command=("run", "jobs", "execute"))
    clock = FakeClock()

    report = run_check(gcloud, JOB, sleep=clock.sleep, clock=clock)

    assert not report.completed
    assert "start" in report.reason
    assert len(gcloud.calls) == 1


def test_an_execution_that_does_not_finish_in_time_reports_no_result() -> None:
    gcloud = FakeGcloud(completion=("Unknown",))
    clock = FakeClock(step=600)

    report = run_check(gcloud, JOB, sleep=clock.sleep, clock=clock)

    assert not report.completed
    assert "finish" in report.reason
    assert not any(call[:2] == ["logging", "read"] for call in gcloud.calls)


def test_the_log_is_read_again_until_its_summary_entry_arrives() -> None:
    gcloud = FakeGcloud(
        log_reads=[
            [],
            [finding_payload(US, "HTTP_404")],
            [finding_payload(US, "HTTP_404"), summary_payload(1)],
        ]
    )
    clock = FakeClock()

    report = run_check(gcloud, JOB, sleep=clock.sleep, clock=clock)

    assert report.completed
    assert clock.sleeps == [LOG_READ_INTERVAL_SECONDS, LOG_READ_INTERVAL_SECONDS]


def test_a_log_that_never_shows_its_summary_reports_no_result() -> None:
    gcloud = FakeGcloud(log_reads=[[]])
    clock = FakeClock()

    report = run_check(gcloud, JOB, sleep=clock.sleep, clock=clock)

    assert not report.completed
    reads = [call for call in gcloud.calls if call[:2] == ["logging", "read"]]
    assert len(reads) == LOG_READ_ATTEMPTS


# -- Keeping the issues ---------------------------------------------------------


def test_a_new_problem_opens_one_labelled_issue() -> None:
    github = FakeGitHub()
    found = problem(US, "HTTP_404", "policyengine/populace-us answered HTTP 404.")

    actions = sync(github, Report((found,)))

    assert actions == {found.key: ("opened", 1)}
    assert ("create_label", LABEL) in github.writes
    issue = github.issues[1]
    assert issue["title"] == f"{TITLE_PREFIX} {US}: HTTP_404 ({TOKEN_CHECK})"
    assert [label["name"] for label in issue["labels"]] == [LABEL]
    assert issue_key(issue) == found.key
    assert "policyengine/populace-us answered HTTP 404." in issue["body"]
    assert "2026-11-12 06:17 UTC" in issue["body"]
    assert CONTEXT.run_url in issue["body"]


def test_a_problem_found_again_updates_its_open_issue() -> None:
    github = FakeGitHub()
    sync(github, Report((problem(US, "HTTP_404", "first"),)))
    github.writes.clear()

    actions = sync(github, Report((problem(US, "HTTP_404", "second"),)))

    assert actions == {f"{TOKEN_CHECK}/{US}/HTTP_404": ("updated", 1)}
    assert len(github.issues) == 1
    assert "second" in github.issues[1]["body"]
    assert "first" not in github.issues[1]["body"]
    assert github.issues[1]["state"] == "open"


def test_a_problem_a_completed_run_no_longer_finds_is_closed_with_a_comment() -> None:
    github = FakeGitHub()
    sync(github, Report((problem(US, "HTTP_404"),)))

    actions = sync(github, Report(()))

    assert actions == {f"{TOKEN_CHECK}/{US}/HTTP_404": ("closed", 1)}
    assert github.issues[1]["state"] == "closed"
    assert github.issues[1]["state_reason"] == "completed"
    assert "no longer finds" in github.comments[1][-1]


def test_a_returning_problem_reopens_its_closed_issue() -> None:
    github = FakeGitHub()
    sync(github, Report((problem(US, "HTTP_404"),)))
    github.close(1, "completed")

    actions = sync(github, Report((problem(US, "HTTP_404"),)))

    assert actions == {f"{TOKEN_CHECK}/{US}/HTTP_404": ("reopened", 1)}
    assert github.issues[1]["state"] == "open"
    assert "again" in github.comments[1][-1]
    assert len(github.issues) == 1


def test_an_issue_closed_as_not_planned_stays_closed() -> None:
    github = FakeGitHub()
    sync(github, Report((problem(US, "HTTP_404"),)))
    github.close(1, "not_planned")
    github.writes.clear()

    actions = sync(github, Report((problem(US, "HTTP_404"),)))

    assert actions == {f"{TOKEN_CHECK}/{US}/HTTP_404": ("kept-closed", 1)}
    assert github.issues[1]["state"] == "closed"
    assert github.writes == []


def test_a_run_without_a_result_opens_one_issue_and_leaves_the_others() -> None:
    github = FakeGitHub()
    sync(github, Report((problem(US, "HTTP_404"),)))
    github.writes.clear()

    actions = sync(github, Report(None, "The check job could not be started."))

    assert actions == {NO_RESULT_KEY: ("opened", 2)}
    assert github.issues[2]["title"] == NO_RESULT_TITLE
    assert "The check job could not be started." in github.issues[2]["body"]
    assert github.issues[1]["state"] == "open"
    assert all(
        write[1][0] != 1 for write in github.writes if write[0] != "create_issue"
    )


def test_the_next_completed_run_closes_the_no_result_issue() -> None:
    github = FakeGitHub()
    sync(github, Report(None, "The check job could not be started."))

    actions = sync(github, Report(()))

    assert actions == {NO_RESULT_KEY: ("closed", 1)}
    assert github.issues[1]["state"] == "closed"


def test_issues_the_workflow_did_not_open_are_ignored() -> None:
    github = FakeGitHub(labels={LABEL})
    found = problem(US, "HTTP_404")
    github.add_issue(
        f"{TITLE_PREFIX} {US}: HTTP_404 ({TOKEN_CHECK})",
        f'<!-- huggingface-check:state {{"key": "{found.key}"}} -->',
        login="someone",
    )

    actions = sync(github, Report((found,)))

    assert actions == {found.key: ("opened", 2)}
    assert github.issues[1]["body"].startswith("<!--")


def test_a_dry_run_reports_what_would_change_and_writes_nothing() -> None:
    github = FakeGitHub()
    sync(github, Report((problem(US, "HTTP_404"),)))
    github.writes.clear()

    actions = sync(
        github,
        Report((problem(UK, "HTTP_401"),)),
        dry_run=True,
    )

    assert actions == {
        f"{TOKEN_CHECK}/{UK}/HTTP_401": ("opened", None),
        f"{TOKEN_CHECK}/{US}/HTTP_404": ("closed", 1),
    }
    assert github.writes == []


def test_at_most_ten_new_issues_open_in_one_run() -> None:
    github = FakeGitHub()
    problems = tuple(
        problem(f"policyengine/dataset-{index:02d}", "HTTP_404") for index in range(12)
    )

    actions = sync(github, Report(problems))

    outcomes = [action for action, _ in actions.values()]
    assert outcomes.count("opened") == MAX_NEW_ISSUES_PER_RUN == 10
    assert outcomes.count("deferred") == 2
    assert len(github.issues) == 10


# -- The GitHub client ------------------------------------------------------------


def github_client(handler, clock: FakeClock | None = None) -> GitHubClient:
    timer = clock or FakeClock()
    return GitHubClient(
        "PolicyEngine/microcosm-collector",
        "github-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=timer.sleep,
        clock=timer,
    )


def test_listing_issues_reads_every_page_and_leaves_out_pull_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        issues = (
            [{"number": number} for number in range(100)]
            if page == 1
            else [{"number": 100}, {"number": 101, "pull_request": {}}]
        )
        return httpx.Response(200, json=issues)

    issues = github_client(handler).list_issues(BOT_LOGIN, LABEL)

    assert [issue["number"] for issue in issues] == list(range(101))
    first = requests[0]
    assert first.url.path == "/repos/PolicyEngine/microcosm-collector/issues"
    assert first.url.params["state"] == "all"
    assert first.url.params["creator"] == BOT_LOGIN
    assert first.url.params["labels"] == LABEL
    assert first.url.params["per_page"] == "100"
    assert first.headers["authorization"] == "Bearer github-token"
    assert len(requests) == 2


def test_a_rate_limited_write_is_retried_after_the_wait_github_asks_for() -> None:
    answers = iter(
        [
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(201, json={"number": 5, "labels": []}),
        ]
    )
    clock = FakeClock()

    issue = github_client(lambda request: next(answers), clock).create_issue(
        "title", "body", [LABEL]
    )

    assert issue["number"] == 5
    assert 7 in clock.sleeps


def test_a_permission_failure_is_not_retried() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, json={"message": "Resource not accessible"})

    with pytest.raises(GitHubError):
        github_client(handler).add_comment(3, "body")
    assert len(requests) == 1


def test_writes_are_sent_at_least_a_second_apart() -> None:
    clock = FakeClock()
    client = github_client(lambda request: httpx.Response(201, json={}), clock)

    client.add_comment(1, "first")
    client.add_comment(1, "second")

    assert clock.sleeps and clock.sleeps[-1] == pytest.approx(1.0)


# -- The command ---------------------------------------------------------------------


def command_environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    environment = {
        "PROJECT_ID": "example-project",
        "REGION": "example-region",
        "HF_CHECK_JOB": "example-job",
        "GITHUB_TOKEN": "github-token",
        "GITHUB_REPOSITORY": "PolicyEngine/microcosm-collector",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "1",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_ACTIONS": "true",
        "DRY_RUN": "false",
    }
    environment.update(overrides)
    return environment


def run_main(environment: dict[str, str], gcloud, github) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    clock = FakeClock()
    exit_code = main(
        environment,
        gcloud=gcloud,
        github=github,
        sleep=clock.sleep,
        clock=clock,
        now=lambda: CONTEXT.started_at,
        output=output,
        errors=errors,
    )
    return exit_code, output.getvalue(), errors.getvalue()


def test_the_command_requires_its_settings(tmp_path: Path) -> None:
    environment = command_environment(tmp_path)
    del environment["HF_CHECK_JOB"]
    del environment["GITHUB_TOKEN"]

    exit_code, _, errors = run_main(environment, FakeGcloud(), FakeGitHub())

    assert exit_code == SETTINGS_EXIT_CODE == 2
    assert "HF_CHECK_JOB" in errors and "GITHUB_TOKEN" in errors


def test_the_command_hides_the_infrastructure_names_in_the_actions_log(
    tmp_path: Path,
) -> None:
    gcloud = FakeGcloud(log_reads=[[summary_payload(0)]])

    exit_code, output, _ = run_main(command_environment(tmp_path), gcloud, FakeGitHub())

    assert exit_code == 0
    masks = [line for line in output.splitlines() if line.startswith("::add-mask::")]
    assert masks == [
        "::add-mask::example-project",
        "::add-mask::example-region",
        "::add-mask::example-job",
    ]
    assert output.index("::add-mask::") == 0


def test_the_command_opens_issues_that_name_no_infrastructure(tmp_path: Path) -> None:
    gcloud = FakeGcloud(
        log_reads=[[finding_payload(US, "HTTP_404"), summary_payload(1)]]
    )
    github = FakeGitHub()

    exit_code, _, _ = run_main(command_environment(tmp_path), gcloud, github)

    assert exit_code == 0
    assert len(github.issues) == 1
    assert (
        "https://github.com/PolicyEngine/microcosm-collector/actions/runs/1"
        in (github.issues[1]["body"])
    )
    for name in ("example-project", "example-region", "example-job", EXECUTION):
        assert name not in github.written_text()
    summary = (tmp_path / "summary.md").read_text()
    assert f"{TOKEN_CHECK}/{US}/HTTP_404" in summary
    assert "opened" in summary


def test_a_dry_run_of_the_command_changes_no_issue(tmp_path: Path) -> None:
    gcloud = FakeGcloud(
        log_reads=[[finding_payload(US, "HTTP_404"), summary_payload(1)]]
    )
    github = FakeGitHub()

    exit_code, _, _ = run_main(
        command_environment(tmp_path, DRY_RUN="true"), gcloud, github
    )

    assert exit_code == 0
    assert github.writes == []
    assert "dry run" in (tmp_path / "summary.md").read_text().lower()
