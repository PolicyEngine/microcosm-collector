"""Start the daily Hugging Face check and keep one GitHub issue per problem.

``.github/workflows/huggingface-check.yaml`` runs this once a day, and by hand,
in the ``huggingface-check`` environment, after authenticating to Google Cloud
as an account that may only start the check job and read its logs. It:

1. starts an execution of the check job, which runs
   ``python -m telemetry_collector.huggingface_check`` and which the production
   deploy configures, and waits for it to finish;
2. reads that execution's log entries: one ``ERROR`` entry per problem, then
   one summary entry, and checks that it read as many problems as the summary
   counts;
3. keeps one issue per problem, keyed by its check, dataset and code, all
   labelled ``huggingface-check``. It opens an issue for a new problem,
   updates the open one for a problem found again, reopens a closed one for a
   problem that returns (unless it was closed as not planned), and closes the
   open one, with a comment, for a problem a completed run no longer finds.

A run that reports no result (the job couldn't start, failed before writing its
summary, didn't finish in time, couldn't read its token setting, or its entries
couldn't be read) opens or updates one "did not report a result" issue and
leaves the problem issues as they are, since what they report is then unknown.
The next run that reports a result closes it.

The repo is public, so no issue names the project, region, job, execution
or account, and the workflow's log hides them: the workflow passes them as
secrets, and this command also masks them before it runs anything.

Run it with ``python -m telemetry_collector.huggingface_issues``. It reads
``PROJECT_ID``, ``REGION``, ``HF_CHECK_JOB``, ``GITHUB_TOKEN`` and
``GITHUB_REPO`` (the repo's ``owner/name``); ``DRY_RUN=true`` reports what would change in the issues
without changing any.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import IO, Any, Final

import httpx

from telemetry_collector.huggingface_check import CONFIGURATION_CHECK

LABEL: Final = "huggingface-check"
LABEL_COLOR: Final = "d93f0b"
LABEL_DESCRIPTION: Final = "Opened by the daily Hugging Face check"
TITLE_PREFIX: Final = "[huggingface-check]"
#: The workflow's GITHUB_TOKEN opens issues as this account.
BOT_LOGIN: Final = "github-actions[bot]"
NO_RESULT_KEY: Final = "no-result"
NO_RESULT_TITLE: Final = f"{TITLE_PREFIX} The check did not report a result"
STATE_PATTERN: Final = re.compile(r"<!-- huggingface-check:state (\{[^\n]*?\}) -->")
SECTION_START: Final = "<!-- huggingface-check:start -->"
SECTION_END: Final = "<!-- huggingface-check:end -->"
#: A run that finds many new problems at once, such as a revoked token, opens
#: this many issues and leaves the rest for the next run.
MAX_NEW_ISSUES_PER_RUN: Final = 10
MAX_MESSAGES_PER_ISSUE: Final = 20
MAX_MESSAGE_LENGTH: Final = 500
#: The check job's task times out after 30 minutes; this leaves room for its
#: start and for Cloud Run to record the result.
EXECUTION_TIMEOUT_SECONDS: Final = 45 * 60
EXECUTION_POLL_SECONDS: Final = 30
#: Cloud Logging can show an execution's entries a little after it finishes.
LOG_READ_ATTEMPTS: Final = 10
LOG_READ_INTERVAL_SECONDS: Final = 30
SETTINGS_EXIT_CODE: Final = 2
REQUIRED_SETTINGS: Final = (
    "PROJECT_ID",
    "REGION",
    "HF_CHECK_JOB",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
)
REOPENABLE_CLOSE_REASONS: Final = frozenset({None, "completed"})

GITHUB_API: Final = "https://api.github.com"
GITHUB_API_VERSION: Final = "2022-11-28"
GITHUB_PAGE_SIZE: Final = 100
# GitHub asks clients to send content-creating requests one at a time, at
# least a second apart, and to wait as its rate-limit answers say.
MIN_SECONDS_BETWEEN_WRITES: Final = 1.0
MAX_RATE_LIMIT_RETRIES: Final = 3
DEFAULT_RATE_LIMIT_WAIT_SECONDS: Final = 60
MAX_RATE_LIMIT_WAIT_SECONDS: Final = 300
RATE_LIMIT_STATUSES: Final = frozenset(
    {HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS}
)
_EXECUTION_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

Gcloud = Callable[[Sequence[str]], str]


class GcloudError(RuntimeError):
    """A gcloud command exited with a non-zero status."""


class SettingsError(RuntimeError):
    """A setting the workflow passes in the environment is missing."""


class GitHubError(RuntimeError):
    """A GitHub request failed."""


class _NoResultError(RuntimeError):
    """The run ended without a result; the message says why, safe to publish."""


@dataclass(frozen=True)
class Problem:
    """One problem the check found, with every message it logged for it."""

    check: str
    dataset: str
    code: str
    messages: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.check}/{self.dataset}/{self.code}"


@dataclass(frozen=True)
class Report:
    """What one execution reported: its problems, or why there is no result."""

    problems: tuple[Problem, ...] | None
    reason: str = ""

    @property
    def completed(self) -> bool:
        return self.problems is not None


@dataclass(frozen=True)
class CheckJob:
    """The check job's project, region and name, which no issue repeats."""

    project: str
    region: str
    name: str

    def scope(self) -> list[str]:
        return ["--project", self.project, "--region", self.region]


@dataclass(frozen=True)
class RunContext:
    """When this run started, and the workflow run that issues link to."""

    started_at: datetime
    run_url: str | None = None


@dataclass(frozen=True)
class SyncAction:
    """What a run did, or in a dry run would do, for one issue."""

    key: str
    action: str
    issue_number: int | None


# -- Reading one execution's log entries --------------------------------------


def _is_summary(entry: Mapping[str, Any]) -> bool:
    return "problems" in entry and "checked_datasets" in entry


def _is_configuration_failure(entry: Mapping[str, Any]) -> bool:
    return entry.get("check") == CONFIGURATION_CHECK


def _has_result(entries: Iterable[Mapping[str, Any]]) -> bool:
    return any(
        _is_summary(entry) or _is_configuration_failure(entry) for entry in entries
    )


def parse_report(entries: Iterable[Mapping[str, Any]]) -> Report:
    """Read one execution's log payloads into the problems it found.

    Each payload carries its entry's ``severity``. There is a result only when
    the summary entry is there and as many problem entries were read as it
    counts.
    """

    messages: dict[tuple[str, str, str], list[str]] = {}
    problem_entries = 0
    summary: Mapping[str, Any] | None = None
    for entry in entries:
        if _is_configuration_failure(entry):
            return Report(
                None,
                "The check could not read its Hugging Face token setting: "
                f"{entry.get('message', '')}",
            )
        if _is_summary(entry):
            summary = entry
            continue
        fields = (entry.get("check"), entry.get("dataset"), entry.get("code"))
        if entry.get("severity") != "ERROR" or not all(
            isinstance(value, str) and value for value in fields
        ):
            continue
        problem_entries += 1
        messages.setdefault(fields, []).append(str(entry.get("message", "")))
    if summary is None:
        return Report(
            None,
            "The check's log has no summary entry: the check did not finish, or "
            "its entries could not be read.",
        )
    counted = summary.get("problems")
    if counted != problem_entries:
        return Report(
            None,
            f"The check counted {counted} problems, but {problem_entries} problem "
            "entries could be read.",
        )
    return Report(
        tuple(
            Problem(check, dataset, code, tuple(found))
            for (check, dataset, code), found in sorted(messages.items())
        )
    )


# -- Starting the job and reading its entries ----------------------------------


def gcloud_command(arguments: Sequence[str]) -> str:
    """Run gcloud and return its output; its errors go to the masked log."""

    completed = subprocess.run(
        ["gcloud", *arguments], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        msg = f"gcloud {' '.join(arguments[:3])} exited with {completed.returncode}"
        raise GcloudError(msg)
    return completed.stdout


def _completion(execution: Mapping[str, Any]) -> str | None:
    """Return the execution's Completed condition, True or False, once known."""

    conditions = (execution.get("status") or {}).get("conditions") or []
    for condition in conditions:
        if condition.get("type") == "Completed" and condition.get("status") in {
            "True",
            "False",
        }:
            return condition["status"]
    return None


def _start(gcloud: Gcloud, job: CheckJob) -> str:
    try:
        execution = gcloud(
            [
                "run",
                "jobs",
                "execute",
                job.name,
                *job.scope(),
                "--format=value(metadata.name)",
            ]
        ).strip()
    except GcloudError as error:
        raise _NoResultError("The workflow could not start the check job.") from error
    if not _EXECUTION_NAME.fullmatch(execution):
        msg = "Starting the check job did not name an execution."
        raise _NoResultError(msg)
    return execution


def _wait(
    gcloud: Gcloud,
    job: CheckJob,
    execution: str,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    deadline = clock() + EXECUTION_TIMEOUT_SECONDS
    while True:
        try:
            described = json.loads(
                gcloud(
                    [
                        "run",
                        "jobs",
                        "executions",
                        "describe",
                        execution,
                        *job.scope(),
                        "--format=json",
                    ]
                )
            )
        except (GcloudError, json.JSONDecodeError) as error:
            msg = "The workflow could not follow the check job's execution."
            raise _NoResultError(msg) from error
        if _completion(described) is not None:
            return
        if clock() >= deadline:
            minutes = EXECUTION_TIMEOUT_SECONDS // 60
            msg = f"The check job did not finish within {minutes} minutes."
            raise _NoResultError(msg)
        sleep(EXECUTION_POLL_SECONDS)


def _read_entries(
    gcloud: Gcloud,
    job: CheckJob,
    execution: str,
    *,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    log_filter = (
        f'resource.type="cloud_run_job" AND resource.labels.job_name="{job.name}"'
        f' AND labels."run.googleapis.com/execution_name"="{execution}"'
    )
    payloads: list[dict[str, Any]] = []
    for attempt in range(LOG_READ_ATTEMPTS):
        try:
            entries = json.loads(
                gcloud(
                    [
                        "logging",
                        "read",
                        log_filter,
                        "--project",
                        job.project,
                        "--format=json",
                        "--order=asc",
                        "--freshness=1d",
                    ]
                )
                or "[]"
            )
        except (GcloudError, json.JSONDecodeError) as error:
            msg = "The workflow could not read the check job's log entries."
            raise _NoResultError(msg) from error
        # Cloud Logging lifts a JSON line's severity out of its payload.
        payloads = [
            {**entry["jsonPayload"], "severity": entry.get("severity")}
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("jsonPayload"), dict)
        ]
        if _has_result(payloads):
            return payloads
        if attempt + 1 < LOG_READ_ATTEMPTS:
            sleep(LOG_READ_INTERVAL_SECONDS)
    return payloads


def run_check(
    gcloud: Gcloud,
    job: CheckJob,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    """Start the check job, wait for it, and read what its execution logged."""

    try:
        execution = _start(gcloud, job)
        _wait(gcloud, job, execution, sleep=sleep, clock=clock)
        entries = _read_entries(gcloud, job, execution, sleep=sleep)
    except _NoResultError as no_result:
        return Report(None, str(no_result))
    return parse_report(entries)


# -- The GitHub client ------------------------------------------------------------


def _seconds_until_reset(response: httpx.Response) -> int | None:
    reset = response.headers.get("x-ratelimit-reset", "")
    sent = response.headers.get("date")
    if not (reset.isascii() and reset.isdigit()) or not sent:
        return None
    try:
        sent_at = parsedate_to_datetime(sent)
    except (TypeError, ValueError):
        return None
    return max(int(reset) - int(sent_at.timestamp()), 0)


def _rate_limit_wait(response: httpx.Response) -> int | None:
    """Return the seconds to wait before retrying, or None not to retry.

    GitHub's guidance: wait for ``retry-after``; when no requests remain, wait
    until ``x-ratelimit-reset``; otherwise wait a minute. A 403 that mentions
    no rate limit is a permission failure, and isn't retried.
    """

    if response.status_code not in RATE_LIMIT_STATUSES:
        return None
    retry_after = response.headers.get("retry-after", "")
    if retry_after.isascii() and retry_after.isdigit():
        seconds = int(retry_after)
    elif response.headers.get("x-ratelimit-remaining") == "0":
        until_reset = _seconds_until_reset(response)
        seconds = (
            DEFAULT_RATE_LIMIT_WAIT_SECONDS if until_reset is None else until_reset
        )
    elif (
        response.status_code == HTTPStatus.TOO_MANY_REQUESTS
        or b"rate limit" in response.content.lower()
    ):
        seconds = DEFAULT_RATE_LIMIT_WAIT_SECONDS
    else:
        return None
    return seconds if seconds <= MAX_RATE_LIMIT_WAIT_SECONDS else None


class GitHubClient:
    """Reads and writes one repo's issues and labels.

    Writes go one at a time, at least a second apart. A rate-limited request is
    retried after the wait GitHub asks for, up to three times.
    """

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo_url = f"{GITHUB_API}/repos/{repo}"
        self._client = client or httpx.Client(timeout=30)
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "microcosm-collector-huggingface-check",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        self._sleep = sleep
        self._clock = clock
        self._last_write: float | None = None

    def list_issues(self, creator: str, label: str) -> list[dict[str, Any]]:
        """Return the open and closed issues ``creator`` opened with ``label``."""

        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                "/issues",
                params={
                    "state": "all",
                    "creator": creator,
                    "labels": label,
                    "per_page": GITHUB_PAGE_SIZE,
                    "page": page,
                },
            ).json()
            if not isinstance(batch, list):
                msg = "GitHub answered the issue list with something else."
                raise GitHubError(msg)
            issues.extend(issue for issue in batch if "pull_request" not in issue)
            if len(batch) < GITHUB_PAGE_SIZE:
                return issues
            page += 1

    def list_labels(self) -> set[str]:
        names: set[str] = set()
        page = 1
        while True:
            batch = self._request(
                "GET", "/labels", params={"per_page": GITHUB_PAGE_SIZE, "page": page}
            ).json()
            names.update(label["name"] for label in batch)
            if len(batch) < GITHUB_PAGE_SIZE:
                return names
            page += 1

    def create_label(self, name: str, color: str, description: str) -> None:
        self._request(
            "POST",
            "/labels",
            json={"name": name, "color": color, "description": description},
        )

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        response = self._request(
            "POST", "/issues", json={"title": title, "body": body, "labels": labels}
        )
        return response.json()

    def update_issue(self, number: int, fields: Mapping[str, object]) -> None:
        self._request("PATCH", f"/issues/{number}", json=dict(fields))

    def add_comment(self, number: int, body: str) -> None:
        self._request("POST", f"/issues/{number}/comments", json={"body": body})

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        writes = method != "GET"
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            if writes:
                self._wait_between_writes()
            response = self._client.request(
                method,
                f"{self._repo_url}{path}",
                params=params,
                json=json,
                headers=self._headers,
            )
            if writes:
                self._last_write = self._clock()
            wait = _rate_limit_wait(response)
            if wait is None or attempt == MAX_RATE_LIMIT_RETRIES:
                break
            self._sleep(wait)
        if response.is_error:
            msg = f"GitHub answered {response.status_code} to {method} {path}"
            raise GitHubError(msg)
        return response

    def _wait_between_writes(self) -> None:
        if self._last_write is None:
            return
        elapsed = self._clock() - self._last_write
        if elapsed < MIN_SECONDS_BETWEEN_WRITES:
            self._sleep(MIN_SECONDS_BETWEEN_WRITES - elapsed)


# -- Writing the issues --------------------------------------------------------------


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _run_reference(context: RunContext) -> str:
    when = _utc(context.started_at)
    return f"{when} ([workflow run]({context.run_url}))" if context.run_url else when


def _state(key: str, context: RunContext) -> str:
    state = {"key": key, "seen_at": context.started_at.astimezone(UTC).isoformat()}
    return f"<!-- huggingface-check:state {json.dumps(state, sort_keys=True)} -->"


def _issue_state(issue: Mapping[str, Any]) -> dict[str, Any] | None:
    match = STATE_PATTERN.search(issue.get("body") or "")
    if match is None:
        return None
    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def _quoted(messages: Sequence[str]) -> list[str]:
    shown = [
        message[:MAX_MESSAGE_LENGTH].replace("```", "'''")
        for message in messages[:MAX_MESSAGES_PER_ISSUE]
    ]
    lines = ["```text", *shown, "```"]
    hidden = len(messages) - len(shown)
    if hidden:
        lines.append(f"…and {hidden} more.")
    return lines


def problem_title(problem: Problem) -> str:
    return f"{TITLE_PREFIX} {problem.dataset}: {problem.code} ({problem.check})"


def problem_section(problem: Problem, context: RunContext) -> str:
    lines = [
        SECTION_START,
        f"- **Dataset:** `{problem.dataset}`",
        f"- **Check:** `{problem.check}`",
        f"- **Code:** `{problem.code}`",
        f"- **Last found:** {_run_reference(context)}",
        "",
        "What the check logged:",
        "",
        *_quoted(problem.messages),
        _state(problem.key, context),
        SECTION_END,
    ]
    return "\n".join(lines)


def problem_body(problem: Problem, context: RunContext) -> str:
    return (
        "The daily Hugging Face check found this problem. Each run that finds it "
        "again updates this issue, and the next completed run that no longer "
        "finds it closes it.\n\n" + problem_section(problem, context) + "\n"
    )


def no_result_section(reason: str, context: RunContext) -> str:
    lines = [
        SECTION_START,
        f"- **Latest run:** {_run_reference(context)}",
        f"- **Why:** {reason}",
        _state(NO_RESULT_KEY, context),
        SECTION_END,
    ]
    return "\n".join(lines)


def no_result_body(reason: str, context: RunContext) -> str:
    return (
        "The daily Hugging Face check ran without reporting a result, so the "
        "issues for the problems it found before are left as they were. The "
        "workflow run's log has the details. The next run that reports a "
        "result closes this issue.\n\n" + no_result_section(reason, context) + "\n"
    )


def replace_section(body: str, section: str) -> str:
    """Replace the marked section of ``body``, or add it at the end."""

    start = body.find(SECTION_START)
    end = body.find(SECTION_END, start) if start >= 0 else -1
    if end >= 0:
        return f"{body[:start]}{section}{body[end + len(SECTION_END) :]}"
    kept = body.rstrip()
    return f"{kept}\n\n{section}\n" if kept else f"{section}\n"


class IssueSync:
    """Keeps one issue per problem, and one for a run with no result."""

    def __init__(self, github: Any, context: RunContext, *, dry_run: bool) -> None:
        self._github = github
        self._context = context
        self._dry_run = dry_run
        self._label_ready = False

    def sync(self, report: Report) -> list[SyncAction]:
        issues = self._issues_by_key()
        if not report.completed:
            return [self._keep_no_result(issues.get(NO_RESULT_KEY), report.reason)]
        actions: list[SyncAction] = []
        found = {problem.key: problem for problem in report.problems or ()}
        opened = 0
        for key, problem in sorted(found.items()):
            issue = issues.get(key)
            if issue is None:
                if opened >= MAX_NEW_ISSUES_PER_RUN:
                    actions.append(SyncAction(key, "deferred", None))
                    continue
                opened += 1
                number = self._open(
                    problem_title(problem), problem_body(problem, self._context)
                )
                actions.append(SyncAction(key, "opened", number))
            else:
                section = problem_section(problem, self._context)
                actions.append(
                    self._keep(issue, key, section, "found this problem again")
                )
        for key, issue in sorted(issues.items()):
            if key not in found and issue.get("state") == "open":
                self._close(issue["number"])
                actions.append(SyncAction(key, "closed", issue["number"]))
        return actions

    def _issues_by_key(self) -> dict[str, Mapping[str, Any]]:
        """Index the workflow's issues by key, preferring open, then newest."""

        index: dict[str, Mapping[str, Any]] = {}
        for issue in self._github.list_issues(BOT_LOGIN, LABEL):
            if (issue.get("user") or {}).get("login") != BOT_LOGIN:
                continue
            state = _issue_state(issue)
            key = state.get("key") if state else None
            if not isinstance(key, str):
                continue
            rank = (issue.get("state") == "open", issue.get("number", 0))
            current = index.get(key)
            if current is None or rank > (
                current.get("state") == "open",
                current.get("number", 0),
            ):
                index[key] = issue
        return index

    def _keep_no_result(
        self, issue: Mapping[str, Any] | None, reason: str
    ) -> SyncAction:
        if issue is None:
            number = self._open(NO_RESULT_TITLE, no_result_body(reason, self._context))
            return SyncAction(NO_RESULT_KEY, "opened", number)
        section = no_result_section(reason, self._context)
        return self._keep(issue, NO_RESULT_KEY, section, "reported no result again")

    def _keep(
        self, issue: Mapping[str, Any], key: str, section: str, again: str
    ) -> SyncAction:
        number = issue["number"]
        body = replace_section(issue.get("body") or "", section)
        if issue.get("state") == "open":
            if not self._dry_run:
                self._github.update_issue(number, {"body": body})
            return SyncAction(key, "updated", number)
        if issue.get("state_reason") not in REOPENABLE_CLOSE_REASONS:
            return SyncAction(key, "kept-closed", number)
        if not self._dry_run:
            self._github.update_issue(
                number, {"body": body, "state": "open", "state_reason": "reopened"}
            )
            self._github.add_comment(
                number,
                f"The check's run of {_run_reference(self._context)} {again}.",
            )
        return SyncAction(key, "reopened", number)

    def _open(self, title: str, body: str) -> int | None:
        if self._dry_run:
            return None
        self._ensure_label()
        return self._github.create_issue(title, body, [LABEL])["number"]

    def _close(self, number: int) -> None:
        if self._dry_run:
            return
        self._github.add_comment(
            number,
            f"The check's run of {_run_reference(self._context)} no longer finds "
            "this, so this issue is closed. A later run that finds it again "
            "reopens it.",
        )
        self._github.update_issue(
            number, {"state": "closed", "state_reason": "completed"}
        )

    def _ensure_label(self) -> None:
        if self._label_ready:
            return
        known = {name.casefold() for name in self._github.list_labels()}
        if LABEL.casefold() not in known:
            self._github.create_label(LABEL, LABEL_COLOR, LABEL_DESCRIPTION)
        self._label_ready = True


# -- The command ---------------------------------------------------------------------


def render_summary(
    report: Report, actions: Sequence[SyncAction], *, dry_run: bool
) -> str:
    """The job summary: the run's result and what happened to each issue."""

    heading = "## Daily Hugging Face check" + (
        " (dry run: no issue changed)" if dry_run else ""
    )
    if report.completed:
        result = f"The check reported {len(report.problems or ())} problems."
    else:
        result = f"The check did not report a result. {report.reason}"
    lines = [heading, "", result, ""]
    if actions:
        lines += ["| Problem | Action | Issue |", "| --- | --- | --- |"]
        for action in actions:
            issue = f"#{action.issue_number}" if action.issue_number else "–"
            lines.append(f"| `{action.key}` | {action.action} | {issue} |")
    else:
        lines.append("No issue to open, update or close.")
    return "\n".join(lines) + "\n"


def _settings(environment: Mapping[str, str]) -> dict[str, str]:
    missing = [name for name in REQUIRED_SETTINGS if not environment.get(name)]
    if missing:
        msg = f"Missing settings: {' '.join(missing)}"
        raise SettingsError(msg)
    return {name: environment[name] for name in REQUIRED_SETTINGS}


def main(
    environment: Mapping[str, str] | None = None,
    *,
    gcloud: Gcloud | None = None,
    github: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
    output: IO[str] | None = None,
    errors: IO[str] | None = None,
) -> int:
    """Run the check once and sync its issues; non-zero only if that failed."""

    environment = os.environ if environment is None else environment
    output = output or sys.stdout
    errors = errors or sys.stderr
    try:
        settings = _settings(environment)
    except SettingsError as error:
        print(error, file=errors)
        return SETTINGS_EXIT_CODE
    job = CheckJob(settings["PROJECT_ID"], settings["REGION"], settings["HF_CHECK_JOB"])
    if environment.get("GITHUB_ACTIONS") == "true":
        for value in (job.project, job.region, job.name):
            print(f"::add-mask::{value}", file=output)
        output.flush()
    run_url = None
    if environment.get("GITHUB_SERVER_URL") and environment.get("GITHUB_RUN_ID"):
        run_url = (
            f"{environment['GITHUB_SERVER_URL']}/{settings['GITHUB_REPO']}"
            f"/actions/runs/{environment['GITHUB_RUN_ID']}"
        )
    context = RunContext((now or (lambda: datetime.now(UTC)))(), run_url)
    dry_run = environment.get("DRY_RUN") == "true"

    report = run_check(gcloud or gcloud_command, job, sleep=sleep, clock=clock)
    client = github or GitHubClient(
        settings["GITHUB_REPO"], settings["GITHUB_TOKEN"], sleep=sleep
    )
    actions = IssueSync(client, context, dry_run=dry_run).sync(report)

    summary = render_summary(report, actions, dry_run=dry_run)
    print(summary, file=output)
    summary_path = environment.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary_file:
            summary_file.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
