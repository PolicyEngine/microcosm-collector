"""Collector storage implementations and run-document materialization."""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.models import RunRegistration, TelemetryEvent


class TelemetryRepository(Protocol):
    """Storage operations used by the HTTP service."""

    def register_run(
        self,
        registration: RunRegistration,
        principal: HuggingFacePrincipal,
    ) -> None: ...

    def append_events(
        self, run_id: str, events: Iterable[TelemetryEvent]
    ) -> tuple[int, int]: ...

    def list_runs(
        self,
        *,
        country: str | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[dict[str, Any]]: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def stage_statistics(
        self, *, country: str | None, pipeline: str | None
    ) -> list[dict[str, Any]]: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _initial_run(
    registration: RunRegistration,
    principal: HuggingFacePrincipal,
) -> dict[str, Any]:
    now = _now()
    return {
        **registration.model_dump(),
        "owner_hf_id": principal.user_id,
        "owner_hf_username": principal.username,
        "status": "running",
        "current_stage": "created",
        "started_at": now,
        "updated_at": now,
        "ended_at": None,
        "heartbeat_at": None,
        "resources": None,
        "work": None,
        "failure": None,
    }


def _apply_event(run: dict[str, Any], event: dict[str, Any]) -> None:
    timestamp = event["timestamp"]
    run["started_at"] = min(run["started_at"], timestamp)
    run["updated_at"] = max(run["updated_at"], timestamp)
    if event.get("resources") is not None:
        run["resources"] = event["resources"]
    if event["event_type"] == "heartbeat":
        run["heartbeat_at"] = timestamp
    stage_id = event.get("stage_id")
    if stage_id and stage_id not in {"complete", "failed"}:
        run["current_stage"] = stage_id
    details = event.get("details") or {}
    if event["event_type"] == "progress" and {
        "done",
        "total",
    }.issubset(details):
        run["work"] = {
            "stage": stage_id,
            "done": details["done"],
            "total": details["total"],
            "unit": details.get("unit"),
            "elapsed_seconds": details.get("elapsed_seconds", 0),
            "updated_at": timestamp,
            "details": {
                key: value
                for key, value in details.items()
                if key not in {"done", "total", "unit", "elapsed_seconds"}
            },
        }
    if (event["status"] == "failed" and event["event_type"] == "run") or (
        stage_id == "failed"
    ):
        run["status"] = "failed"
        run["current_stage"] = "failed"
        run["ended_at"] = timestamp
        run["failure"] = {
            "message": event.get("message"),
            "error_type": details.get("error_type"),
            "failure_class": details.get("failure_class"),
            "failed_during": details.get("failed_during") or stage_id,
        }
    elif event["status"] == "completed" and (
        event["event_type"] == "run" or stage_id == "complete"
    ):
        run["status"] = "completed"
        run["current_stage"] = "complete"
        run["ended_at"] = timestamp


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "country_code": run["country_code"],
        "pipeline": run["pipeline"],
        "candidate_id": run.get("candidate_id"),
        "release_id": run.get("release_id"),
        "run_kind": run["run_kind"],
        "status": run["status"],
        "current_stage": run["current_stage"],
        "started_at": run["started_at"],
        "updated_at": run["updated_at"],
        "ended_at": run.get("ended_at"),
        "heartbeat_at": run.get("heartbeat_at"),
    }


def _run_documents(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        events,
        key=lambda event: (
            event["timestamp"],
            event["producer_id"],
            event["sequence"],
        ),
    )
    lifecycle_events: list[dict[str, Any]] = []
    calibration_events: list[dict[str, Any]] = []
    identity: dict[str, Any] | None = None
    for sequence, event in enumerate(ordered, start=1):
        details = deepcopy(event.get("details") or {})
        if event["event_type"] == "run" and isinstance(details.get("identity"), dict):
            identity = details["identity"]
        event_type = "calibration" if event["event_type"] == "calibration" else "stage"
        lifecycle_events.append(
            {
                "schema_version": 2,
                "sequence": sequence,
                "timestamp": event["timestamp"],
                "time": event["timestamp"],
                "event_type": event_type,
                "run_id": event["run_id"],
                "stage_id": event.get("stage_id"),
                "stage": event.get("stage_id"),
                "status": event["status"],
                "message": event.get("message"),
                "details": details,
                "resources": event.get("resources"),
                "producer_id": event["producer_id"],
                "event_id": event["event_id"],
            }
        )
        if event["event_type"] == "calibration":
            calibration_events.append(
                {"time": event["timestamp"], "timestamp": event["timestamp"], **details}
            )
    common = {
        "schema_version": 2,
        "run_id": run["run_id"],
        "country_code": run["country_code"],
        "operation_id": run["run_id"],
        "pipeline": {"id": run["pipeline"], "version": "collector-v1"},
        "candidate_id": run.get("candidate_id"),
        "release_id": run.get("release_id"),
        "run_kind": run["run_kind"],
        "non_release": run.get("release_id") is None,
        "started_at": run["started_at"],
        "updated_at": run["updated_at"],
        "status": run["status"],
        "current_stage": run["current_stage"],
    }
    progress = {
        **common,
        "heartbeat_at": run.get("heartbeat_at"),
        "resources": run.get("resources"),
        "work": run.get("work"),
        "failure": run.get("failure"),
    }
    manifest = {
        **common,
        "identity": identity,
        "failure": run.get("failure"),
        "delivery": {
            "mode": "collector",
            "upload_attempts": None,
            "upload_successes": None,
        },
    }
    return {
        "run_id": run["run_id"],
        "country_code": run["country_code"],
        "progress": progress,
        "run_manifest": manifest,
        "calibration_progress": {
            "schema_version": 2,
            "run_id": run["run_id"],
            "candidate_id": run.get("candidate_id"),
            "updated_at": run["updated_at"],
            "events": calibration_events,
        }
        if calibration_events
        else None,
        "events": lifecycle_events,
    }


class MemoryTelemetryRepository:
    """Thread-safe in-memory repository used by contract tests."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, dict[str, dict[str, Any]]] = {}
        self._sequences: set[tuple[str, str, int]] = set()
        self._lock = RLock()

    def register_run(
        self,
        registration: RunRegistration,
        principal: HuggingFacePrincipal,
    ) -> None:
        with self._lock:
            existing = self._runs.get(registration.run_id)
            if existing is not None:
                if existing["owner_hf_id"] != principal.user_id:
                    raise PermissionError("Run belongs to another Hugging Face user.")
                return
            self._runs[registration.run_id] = _initial_run(registration, principal)
            self._events[registration.run_id] = {}

    def append_events(
        self, run_id: str, events: Iterable[TelemetryEvent]
    ) -> tuple[int, int]:
        accepted = 0
        duplicates = 0
        with self._lock:
            run = self._runs[run_id]
            stored = self._events[run_id]
            for model in events:
                event = model.model_dump(mode="python")
                sequence_key = (run_id, event["producer_id"], event["sequence"])
                if event["event_id"] in stored or sequence_key in self._sequences:
                    duplicates += 1
                    continue
                stored[event["event_id"]] = event
                self._sequences.add(sequence_key)
                _apply_event(run, event)
                accepted += 1
        return accepted, duplicates

    def list_runs(
        self,
        *,
        country: str | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            runs = [
                run
                for run in self._runs.values()
                if (country is None or run["country_code"] == country)
                and (before is None or (run["updated_at"], run["run_id"]) < before)
            ]
            runs.sort(
                key=lambda run: (run["updated_at"], run["run_id"]),
                reverse=True,
            )
            return [_summary(deepcopy(run)) for run in runs[:limit]]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            events = list(self._events[run_id].values())
            return _run_documents(deepcopy(run), deepcopy(events))

    def stage_statistics(
        self, *, country: str | None, pipeline: str | None
    ) -> list[dict[str, Any]]:
        with self._lock:
            documents = [
                _run_documents(deepcopy(run), list(self._events[run_id].values()))
                for run_id, run in self._runs.items()
                if (country is None or run["country_code"] == country)
                and (pipeline is None or run["pipeline"] == pipeline)
            ]
        return _stage_statistics(documents)


class PostgresTelemetryRepository:
    """Postgres-backed repository used by the Cloud Run collector."""

    def __init__(self, database_url: str) -> None:
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    def ensure_schema(self) -> None:
        migration = (
            Path(__file__).parent / "migrations" / "001_initial.sql"
        ).read_text(encoding="utf-8")
        with self._pool.connection() as connection:
            connection.execute(migration)

    def register_run(
        self,
        registration: RunRegistration,
        principal: HuggingFacePrincipal,
    ) -> None:
        payload = _initial_run(registration, principal)
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO telemetry_runs (
                    run_id, country_code, pipeline, candidate_id, release_id,
                    run_kind, owner_hf_id, owner_hf_username, status,
                    current_stage, started_at, updated_at
                ) VALUES (
                    %(run_id)s, %(country_code)s, %(pipeline)s, %(candidate_id)s,
                    %(release_id)s, %(run_kind)s, %(owner_hf_id)s,
                    %(owner_hf_username)s, %(status)s, %(current_stage)s,
                    %(started_at)s, %(updated_at)s
                ) ON CONFLICT (run_id) DO NOTHING
                """,
                payload,
            )
            # Read ownership after the insert attempt. Concurrent exchanges
            # for the same run id then observe the row that actually won the
            # unique-key race before either caller receives a run credential.
            existing = connection.execute(
                "SELECT owner_hf_id FROM telemetry_runs WHERE run_id = %s",
                (registration.run_id,),
            ).fetchone()
            if existing is None or existing["owner_hf_id"] != principal.user_id:
                raise PermissionError("Run belongs to another Hugging Face user.")

    def append_events(
        self, run_id: str, events: Iterable[TelemetryEvent]
    ) -> tuple[int, int]:
        accepted = 0
        duplicates = 0
        with self._pool.connection() as connection:
            run = connection.execute(
                "SELECT * FROM telemetry_runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            mutable = dict(run)
            for model in events:
                event = model.model_dump(mode="python")
                inserted = connection.execute(
                    """
                    INSERT INTO telemetry_events (
                        event_id, run_id, producer_id, sequence, emitted_at,
                        event_type, stage_id, status, message, details, resources
                    ) VALUES (
                        %(event_id)s, %(run_id)s, %(producer_id)s, %(sequence)s,
                        %(timestamp)s, %(event_type)s, %(stage_id)s, %(status)s,
                        %(message)s, %(details)s::jsonb, %(resources)s::jsonb
                    ) ON CONFLICT DO NOTHING
                    """,
                    {
                        **event,
                        "details": json.dumps(event["details"]),
                        "resources": json.dumps(event.get("resources")),
                    },
                ).rowcount
                if not inserted:
                    duplicates += 1
                    continue
                _apply_event(mutable, event)
                accepted += 1
            connection.execute(
                """
                UPDATE telemetry_runs SET
                    status = %(status)s,
                    current_stage = %(current_stage)s,
                    started_at = %(started_at)s,
                    updated_at = %(updated_at)s,
                    ended_at = %(ended_at)s,
                    heartbeat_at = %(heartbeat_at)s,
                    resources = %(resources)s::jsonb,
                    work = %(work)s::jsonb,
                    failure = %(failure)s::jsonb
                WHERE run_id = %(run_id)s
                """,
                {
                    **mutable,
                    "resources": json.dumps(mutable.get("resources")),
                    "work": json.dumps(mutable.get("work"), default=str),
                    "failure": json.dumps(mutable.get("failure")),
                },
            )
        return accepted, duplicates

    def list_runs(
        self,
        *,
        country: str | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if country is not None:
            clauses.append("country_code = %s")
            values.append(country)
        if before is not None:
            clauses.append("(updated_at, run_id) < (%s, %s)")
            values.extend(before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with self._pool.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM telemetry_runs"
                + where
                + " ORDER BY updated_at DESC, run_id DESC LIMIT %s",
                values,
            ).fetchall()
        return [_summary(dict(row)) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            run = connection.execute(
                "SELECT * FROM telemetry_runs WHERE run_id = %s", (run_id,)
            ).fetchone()
            if run is None:
                return None
            rows = connection.execute(
                """
                SELECT event_id, run_id, producer_id, sequence,
                       emitted_at AS timestamp, event_type, stage_id, status,
                       message, details, resources
                FROM telemetry_events
                WHERE run_id = %s
                ORDER BY emitted_at, producer_id, sequence
                """,
                (run_id,),
            ).fetchall()
        return _run_documents(dict(run), [dict(row) for row in rows])

    def stage_statistics(
        self, *, country: str | None, pipeline: str | None
    ) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        before: tuple[datetime, str] | None = None
        while True:
            page = self.list_runs(country=country, limit=500, before=before)
            runs.extend(page)
            if len(page) < 500:
                break
            before = (page[-1]["updated_at"], page[-1]["run_id"])
        documents = []
        for summary in runs:
            if pipeline is not None and summary["pipeline"] != pipeline:
                continue
            detail = self.get_run(summary["run_id"])
            if detail is not None:
                documents.append(detail)
        return _stage_statistics(documents)


def _stage_statistics(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    durations: dict[tuple[str, str], list[float]] = {}
    for document in documents:
        open_stages: dict[tuple[str, str], datetime] = {}
        pipeline = document["progress"]["pipeline"]["id"]
        for event in document["events"]:
            stage = event.get("stage_id")
            if not stage:
                continue
            producer = event.get("producer_id") or "default"
            key = (producer, stage)
            if event["status"] == "started":
                open_stages[key] = event["timestamp"]
            elif event["status"] in {"completed", "failed"} and key in open_stages:
                started = open_stages.pop(key)
                seconds = (event["timestamp"] - started).total_seconds()
                durations.setdefault((pipeline, stage), []).append(max(0.0, seconds))
            elif event["status"] in {"completed", "failed"}:
                elapsed = (event.get("details") or {}).get("elapsed_seconds")
                if isinstance(elapsed, (int, float)) and elapsed >= 0:
                    durations.setdefault((pipeline, stage), []).append(float(elapsed))
    result = []
    for (pipeline, stage), values in sorted(durations.items()):
        ordered = sorted(values)
        result.append(
            {
                "pipeline": pipeline,
                "stage": stage,
                "samples": len(ordered),
                "median_seconds": ordered[(len(ordered) - 1) // 2],
                "p90_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))],
            }
        )
    return result
