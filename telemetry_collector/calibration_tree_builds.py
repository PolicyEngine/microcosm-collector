"""Queue calibration tree builds for the dashboard's Hugging Face webhook.

``POST /v1/calibration-tree-builds`` asks the collector to build the trees of
one country and kind that its store lacks. The caller presents its Vercel
OIDC token, which ``VercelOidcVerifier.caller`` checks; its project and
environment must be in ``TELEMETRY_VERCEL_OIDC_TREE_BUILD_CALLERS``, a list of
its own, so that a reader of read API v2 can never queue builds. The token is
checked in a dependency before the body is read.

Each request falls in a 10-minute window of Unix time, ``floor(t / 600)``. The
first request of a country and kind in a window creates one Cloud Task named
``calibration-trees-<country>-<kind>-<window>`` on the worker queue,
scheduled for the window's end, with the invoker account every dispatch
uses. The task runs the worker job with its arguments overridden to
``uv run --no-sync python -m telemetry_collector.operations
reconcile-calibration-trees --country <cc> --kind <kind> --request-id <task
ID>`` (``telemetry_collector.calibration_tree_reconciliation``); the arguments
are built only from constants and checked values. Every later request in the
window gets Cloud Tasks' 409 and is answered ``already_queued``, so at most
one execution per country and kind is pending, and it starts after every
request it answers.

The ``calibration_tree_build_requests`` table keeps one row per task: the
service adds one to its request count before it creates the task and records
when Cloud Tasks accepted it, and the execution records its attempts, finish
and summary. Each request also writes one JSON document to standard output,
``{"calibration_tree_build_request": {...}}``. No answer, log document or
row holds the token.

The route exists only while intake is on and the callers setting is set;
otherwise the path answers 404 ``calibration_tree_builds_not_enabled``
(``intake_api.calibration_tree_builds_not_enabled_router``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from telemetry_collector.calibration_trees import RELEASE_REPOS, STAGING_REPOS
from telemetry_collector.database import (
    CALIBRATION_TREE_BUILD_KINDS,
    CalibrationTreeBuildRequest,
)
from telemetry_collector.intake import WorkerQueueUnavailableError
from telemetry_collector.intake_api import (
    CALIBRATION_TREE_BUILDS_PATH,
    QUEUE_RETRY_AFTER_SECONDS,
    IntakeProblemError,
)
from telemetry_collector.submissions import (
    GIT_COMMIT_PATTERN,
    HUGGINGFACE_REPO_PATTERN,
)
from telemetry_collector.vercel_oidc import (
    InvalidVercelOidcTokenError,
    VercelOidcKeysUnavailableError,
    VercelOidcReader,
    VercelOidcTeamNotAllowedError,
    VercelOidcVerifier,
)
from telemetry_collector.worker import storable_text

#: Requests of one country and kind within this many seconds share a task.
WINDOW_SECONDS: Final = 600
#: The operations command the task runs, and the module that runs it.
RECONCILE_COMMAND: Final = "reconcile-calibration-trees"
OPERATIONS_MODULE: Final = "telemetry_collector.operations"
#: A ref the dashboard reports: a branch or a tag. Only recorded.
HF_REF_PATTERN: Final = r"^refs/(heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

TOKEN_REQUIRED: Final = "A Vercel OIDC token is required."
TOKEN_INVALID: Final = "The Vercel OIDC token is invalid or expired."
CALLER_NOT_ALLOWED: Final = (
    "This Vercel project and environment may not queue calibration tree builds."
)
KEYS_UNAVAILABLE: Final = "Vercel's token signing keys could not be read."
QUEUE_UNAVAILABLE: Final = "The worker queue is unavailable; retry the request."

BuildKind = Literal["release", "staging"]


# The window, the task and its arguments -------------------------------------------


def task_window(at: datetime) -> int:
    """The 10-minute window of Unix time ``at`` falls in; ``ValueError`` for a
    time without an offset."""

    if at.utcoffset() is None:
        raise ValueError("A request's time must carry its offset.")
    return (at - _EPOCH) // timedelta(seconds=WINDOW_SECONDS)


def window_end(window: int) -> datetime:
    """When ``window`` ends, which is when its task runs."""

    return _EPOCH + timedelta(seconds=(window + 1) * WINDOW_SECONDS)


def _check_country_and_kind(country: str, kind: str) -> None:
    if country not in RELEASE_REPOS:
        raise ValueError("The country is not one the collector builds trees for.")
    if kind not in CALIBRATION_TREE_BUILD_KINDS:
        raise ValueError("The kind is neither release nor staging.")
    if kind == "staging" and country not in STAGING_REPOS:
        raise ValueError("The country has no staging repo.")


def build_task_id(country: str, kind: str, window: int) -> str:
    """The Cloud Task ID of a country's and kind's build in ``window``."""

    _check_country_and_kind(country, kind)
    return f"calibration-trees-{country}-{kind}-{window}"


def is_request_id(value: str, country: str, kind: str) -> bool:
    """Whether ``value`` is a task ID of this country and kind."""

    pattern = rf"calibration-trees-{re.escape(country)}-{re.escape(kind)}-[0-9]{{1,15}}"
    return re.fullmatch(pattern, value) is not None


def reconcile_arguments(country: str, kind: str, task_id: str) -> list[str]:
    """The worker job's arguments that reconcile ``country``'s ``kind`` builds
    for the request ``task_id``. Raises ``ValueError`` for a country, kind or
    task ID that isn't one the collector makes."""

    _check_country_and_kind(country, kind)
    if not is_request_id(task_id, country, kind):
        raise ValueError("The task ID is not this country's and kind's.")
    return [
        "run",
        "--no-sync",
        "python",
        "-m",
        OPERATIONS_MODULE,
        RECONCILE_COMMAND,
        "--country",
        country,
        "--kind",
        kind,
        "--request-id",
        task_id,
    ]


def collector_repo(country: str, kind: str) -> str:
    """The repo the collector builds ``country``'s ``kind`` trees from."""

    return RELEASE_REPOS[country] if kind == "release" else STAGING_REPOS[country]


# The request --------------------------------------------------------------------


class TreeBuildRequestBody(BaseModel):
    """What the caller asks to build: a country's release or staging trees.

    ``hf_repo`` must be the collector's own repo for the country and kind,
    which the route checks; ``hf_ref`` and ``hf_commit`` are only recorded,
    since the execution reads each repo at its head.
    """

    model_config = ConfigDict(extra="forbid")

    country: str
    kind: BuildKind
    hf_repo: str = Field(pattern=HUGGINGFACE_REPO_PATTERN)
    hf_ref: str | None = Field(default=None, pattern=HF_REF_PATTERN)
    hf_commit: str | None = Field(default=None, pattern=GIT_COMMIT_PATTERN)

    @field_validator("country")
    @classmethod
    def _registered_country(cls, country: str) -> str:
        if country not in RELEASE_REPOS:
            raise ValueError(f"country must be one of {', '.join(RELEASE_REPOS)}.")
        return country

    @model_validator(mode="after")
    def _staging_needs_a_staging_repo(self) -> TreeBuildRequestBody:
        if self.kind == "staging" and self.country not in STAGING_REPOS:
            raise ValueError(f"{self.country} has no staging builds.")
        return self


# The request table ------------------------------------------------------------------


class BuildRequestStoreUnavailableError(RuntimeError):
    """The request table could not be read or written."""


@dataclass(frozen=True)
class CalibrationTreeBuildRequestRecord:
    """One row of ``calibration_tree_build_requests``."""

    task_id: str
    country: str
    kind: str
    first_requested_at: datetime
    last_requested_at: datetime
    request_count: int
    last_caller_project_id: str
    last_caller_environment: str
    hf_repo: str
    last_hf_ref: str | None
    last_hf_commit: str | None
    not_before: datetime
    queued_at: datetime | None
    execution: str | None
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    summary: dict[str, Any] | None


class CalibrationTreeBuildRequestStore(Protocol):
    """What the service writes to the request table."""

    def record_request(
        self,
        *,
        task_id: str,
        country: str,
        kind: str,
        caller: VercelOidcReader,
        hf_repo: str,
        hf_ref: str | None,
        hf_commit: str | None,
        not_before: datetime,
        requested_at: datetime,
    ) -> None:
        """Create the task's row, or add one to its request count."""

    def mark_queued(self, task_id: str, *, at: datetime) -> None:
        """Record when Cloud Tasks first accepted the task."""


def _storable(value: Any) -> Any:
    """A JSON value with ``storable_text`` applied to every string, keys too."""

    if isinstance(value, str):
        return storable_text(value)
    if isinstance(value, Mapping):
        return {_storable(key): _storable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_storable(item) for item in value]
    return value


class PostgresCalibrationTreeBuildRequests:
    """The request table in the collector's database.

    The service records requests and their tasks; the worker job's execution
    records its attempts. Any database error raises
    ``BuildRequestStoreUnavailableError``.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _execute(self, statement: sa.Executable) -> int:
        try:
            with self._engine.begin() as connection:
                return connection.execute(statement).rowcount
        except SQLAlchemyError as error:
            raise BuildRequestStoreUnavailableError(
                f"The request table could not be written: {type(error).__name__}."
            ) from error

    def record_request(
        self,
        *,
        task_id: str,
        country: str,
        kind: str,
        caller: VercelOidcReader,
        hf_repo: str,
        hf_ref: str | None,
        hf_commit: str | None,
        not_before: datetime,
        requested_at: datetime,
    ) -> None:
        table = CalibrationTreeBuildRequest.__table__
        statement = insert(table).values(
            task_id=task_id,
            country=country,
            kind=kind,
            first_requested_at=requested_at,
            last_requested_at=requested_at,
            request_count=1,
            last_caller_project_id=caller.project_id,
            last_caller_environment=caller.environment,
            hf_repo=hf_repo,
            last_hf_ref=hf_ref,
            last_hf_commit=hf_commit,
            not_before=not_before,
            attempts=0,
        )
        self._execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.task_id],
                set_={
                    "last_requested_at": statement.excluded.last_requested_at,
                    "request_count": table.c.request_count + 1,
                    "last_caller_project_id": statement.excluded.last_caller_project_id,
                    "last_caller_environment": (
                        statement.excluded.last_caller_environment
                    ),
                    "hf_repo": statement.excluded.hf_repo,
                    "last_hf_ref": statement.excluded.last_hf_ref,
                    "last_hf_commit": statement.excluded.last_hf_commit,
                },
            )
        )

    def mark_queued(self, task_id: str, *, at: datetime) -> None:
        table = CalibrationTreeBuildRequest.__table__
        self._execute(
            sa.update(table)
            .where(table.c.task_id == task_id)
            .values(queued_at=sa.func.coalesce(table.c.queued_at, at))
        )

    def record_start(
        self, task_id: str, *, execution: str | None, at: datetime
    ) -> bool:
        """Count an attempt of the task's execution; False without a row.

        A new attempt clears the last attempt's finish and summary.
        """

        table = CalibrationTreeBuildRequest.__table__
        return (
            self._execute(
                sa.update(table)
                .where(table.c.task_id == task_id)
                .values(
                    attempts=table.c.attempts + 1,
                    execution=execution,
                    started_at=at,
                    finished_at=sa.null(),
                    summary=sa.null(),
                )
            )
            == 1
        )

    def record_finish(
        self, task_id: str, *, at: datetime, summary: Mapping[str, Any]
    ) -> bool:
        """Record the attempt's finish and summary; False without a row."""

        table = CalibrationTreeBuildRequest.__table__
        return (
            self._execute(
                sa.update(table)
                .where(table.c.task_id == task_id)
                .values(finished_at=at, summary=_storable(summary))
            )
            == 1
        )

    def get_request(self, task_id: str) -> CalibrationTreeBuildRequestRecord | None:
        table = CalibrationTreeBuildRequest.__table__
        try:
            with self._engine.connect() as connection:
                row = (
                    connection.execute(
                        sa.select(table).where(table.c.task_id == task_id)
                    )
                    .mappings()
                    .first()
                )
        except SQLAlchemyError as error:
            raise BuildRequestStoreUnavailableError(
                f"The request table could not be read: {type(error).__name__}."
            ) from error
        return None if row is None else CalibrationTreeBuildRequestRecord(**row)


# The route ----------------------------------------------------------------------------


class CalibrationTreeBuildQueue(Protocol):
    """The worker queue's named, scheduled runs of the worker job."""

    def schedule_run(
        self, task_id: str, *, run_at: datetime, arguments: Sequence[str]
    ) -> bool:
        """Create the task; False when it exists already."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CalibrationTreeBuildServices:
    """What the endpoint needs: the request table, the queue and a clock."""

    requests: CalibrationTreeBuildRequestStore
    queue: CalibrationTreeBuildQueue
    clock: Callable[[], datetime] = _utc_now


# The caller's Vercel OIDC token. The route is left out of the OpenAPI
# document, so this scheme never joins read API v2's.
CALLER_BEARER = HTTPBearer(scheme_name="VercelOidcCallerToken", auto_error=False)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _log_request(
    outcome: str,
    status: int,
    *,
    code: str | None = None,
    caller: VercelOidcReader | None = None,
    body: TreeBuildRequestBody | None = None,
    task_id: str | None = None,
    not_before: datetime | None = None,
) -> None:
    """One JSON document on standard output per request. It holds only checked
    values: never the token, and nothing from a body that failed its checks."""

    entry = {
        "caller_project_id": None if caller is None else caller.project_id,
        "caller_environment": None if caller is None else caller.environment,
        "country": None if body is None else body.country,
        "kind": None if body is None else body.kind,
        "hf_repo": None if body is None else body.hf_repo,
        "hf_ref": None if body is None else body.hf_ref,
        "hf_commit": None if body is None else body.hf_commit,
        "task_id": task_id,
        "not_before": None if not_before is None else _timestamp(not_before),
        "outcome": outcome,
        "status": status,
        "code": code,
    }
    print(
        json.dumps({"calibration_tree_build_request": entry}, sort_keys=True),
        flush=True,
    )


def _invalid_body(errors: list[dict[str, Any]]) -> RequestValidationError:
    """FastAPI's refusal of a body, each error without its ``input``, which
    could repeat whatever the caller sent."""

    return RequestValidationError(
        [
            {"type": error["type"], "loc": ("body", *error["loc"]), "msg": error["msg"]}
            for error in errors
        ]
    )


def calibration_tree_build_router(
    *,
    services: CalibrationTreeBuildServices,
    verifier: VercelOidcVerifier,
    callers: frozenset[VercelOidcReader],
) -> APIRouter:
    """``POST /v1/calibration-tree-builds`` for the projects in ``callers``."""

    router = APIRouter()

    def refusal(
        status: int,
        detail: str,
        code: str,
        caller: VercelOidcReader | None = None,
    ) -> HTTPException:
        _log_request("refused", status, code=code, caller=caller)
        return HTTPException(status_code=status, detail=detail)

    def allowed_caller(
        bearer: HTTPAuthorizationCredentials | None = Security(CALLER_BEARER),
    ) -> VercelOidcReader:
        if bearer is None:
            raise refusal(401, TOKEN_REQUIRED, "vercel_oidc_token_required")
        # Each refusal is a fixed sentence, raised without the verifier's
        # error.
        try:
            caller = verifier.caller(bearer.credentials)
        except InvalidVercelOidcTokenError:
            raise refusal(401, TOKEN_INVALID, "vercel_oidc_token_invalid") from None
        except VercelOidcTeamNotAllowedError:
            raise refusal(403, CALLER_NOT_ALLOWED, "caller_not_allowed") from None
        except VercelOidcKeysUnavailableError:
            raise refusal(
                503, KEYS_UNAVAILABLE, "vercel_oidc_keys_unavailable"
            ) from None
        if caller not in callers:
            raise refusal(403, CALLER_NOT_ALLOWED, "caller_not_allowed", caller)
        return caller

    async def checked_request(
        request: Request,
        caller: VercelOidcReader = Depends(allowed_caller),
    ) -> tuple[VercelOidcReader, TreeBuildRequestBody]:
        # Read only once the caller is allowed.
        content = await request.body()
        try:
            document = json.loads(content)
        except (ValueError, RecursionError):
            _log_request("refused", 422, code="invalid_request", caller=caller)
            raise _invalid_body(
                [{"type": "json_invalid", "loc": (0,), "msg": "JSON decode error"}]
            ) from None
        try:
            body = TreeBuildRequestBody.model_validate(document)
        except ValidationError as error:
            _log_request("refused", 422, code="invalid_request", caller=caller)
            raise _invalid_body(error.errors(include_url=False)) from None
        return caller, body

    @router.post(CALIBRATION_TREE_BUILDS_PATH, status_code=202, include_in_schema=False)
    def queue_build(
        checked: tuple[VercelOidcReader, TreeBuildRequestBody] = Depends(
            checked_request
        ),
    ) -> dict[str, Any]:
        caller, body = checked
        if body.hf_repo != collector_repo(body.country, body.kind):
            _log_request(
                "refused", 422, code="repo_not_registered", caller=caller, body=body
            )
            raise IntakeProblemError(
                422,
                "repo_not_registered",
                f"{body.hf_repo} is not the collector's {body.kind} repo for "
                f"{body.country.upper()}.",
            )
        requested_at = services.clock()
        window = task_window(requested_at)
        task_id = build_task_id(body.country, body.kind, window)
        not_before = window_end(window)
        try:
            # The row first, so that every task created has one.
            services.requests.record_request(
                task_id=task_id,
                country=body.country,
                kind=body.kind,
                caller=caller,
                hf_repo=body.hf_repo,
                hf_ref=body.hf_ref,
                hf_commit=body.hf_commit,
                not_before=not_before,
                requested_at=requested_at,
            )
            created = services.queue.schedule_run(
                task_id,
                run_at=not_before,
                arguments=reconcile_arguments(body.country, body.kind, task_id),
            )
            services.requests.mark_queued(task_id, at=services.clock())
        except (BuildRequestStoreUnavailableError, WorkerQueueUnavailableError):
            _log_request(
                "refused",
                503,
                code="worker_queue_unavailable",
                caller=caller,
                body=body,
                task_id=task_id,
                not_before=not_before,
            )
            raise IntakeProblemError(
                503,
                "worker_queue_unavailable",
                QUEUE_UNAVAILABLE,
                headers={"Retry-After": str(QUEUE_RETRY_AFTER_SECONDS)},
            ) from None
        outcome = "queued" if created else "already_queued"
        _log_request(
            outcome,
            202,
            caller=caller,
            body=body,
            task_id=task_id,
            not_before=not_before,
        )
        return {
            "status": outcome,
            "task_id": task_id,
            "country": body.country,
            "kind": body.kind,
            "not_before": _timestamp(not_before),
        }

    return router
