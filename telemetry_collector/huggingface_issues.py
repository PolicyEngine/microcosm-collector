"""Start the daily Hugging Face check and keep one GitHub issue per problem.

Not implemented yet: the next commit fills in each function. The names and
signatures are the ones the tests use.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any, Final

import httpx

LABEL: Final = "huggingface-check"
TITLE_PREFIX: Final = "[huggingface-check]"
BOT_LOGIN: Final = "github-actions[bot]"
NO_RESULT_KEY: Final = "no-result"
NO_RESULT_TITLE: Final = f"{TITLE_PREFIX} The check did not report a result"
STATE_PATTERN: Final = re.compile(r"<!-- huggingface-check:state (\{[^\n]*?\}) -->")
MAX_NEW_ISSUES_PER_RUN: Final = 10
EXECUTION_TIMEOUT_SECONDS: Final = 45 * 60
EXECUTION_POLL_SECONDS: Final = 30
LOG_READ_ATTEMPTS: Final = 10
LOG_READ_INTERVAL_SECONDS: Final = 30
SETTINGS_EXIT_CODE: Final = 2

Gcloud = Callable[[Sequence[str]], str]


class GcloudError(RuntimeError):
    """A gcloud command exited with a non-zero status."""


class SettingsError(RuntimeError):
    """A setting the workflow passes in the environment is missing."""


class GitHubError(RuntimeError):
    """A GitHub request failed."""


@dataclass(frozen=True)
class Problem:
    check: str
    dataset: str
    code: str
    messages: tuple[str, ...]

    @property
    def key(self) -> str:
        return ""


@dataclass(frozen=True)
class Report:
    problems: tuple[Problem, ...] | None
    reason: str = ""

    @property
    def completed(self) -> bool:
        return False


@dataclass(frozen=True)
class CheckJob:
    project: str
    region: str
    name: str


@dataclass(frozen=True)
class RunContext:
    started_at: datetime
    run_url: str | None = None


@dataclass(frozen=True)
class SyncAction:
    key: str
    action: str
    issue_number: int | None


def parse_report(entries: Iterable[Mapping[str, Any]]) -> Report:
    return Report(None)


def run_check(
    gcloud: Gcloud,
    job: CheckJob,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    return Report(None)


class GitHubClient:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository

    def list_issues(self, creator: str, label: str) -> list[dict[str, Any]]:
        return []

    def list_labels(self) -> set[str]:
        return set()

    def create_label(self, name: str, color: str, description: str) -> None:
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return {}

    def update_issue(self, number: int, fields: Mapping[str, object]) -> None:
        return None

    def add_comment(self, number: int, body: str) -> None:
        return None


class IssueSync:
    def __init__(self, github: Any, context: RunContext, *, dry_run: bool) -> None:
        self._github = github

    def sync(self, report: Report) -> list[SyncAction]:
        return []


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
