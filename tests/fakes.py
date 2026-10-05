"""In-memory protocol fake for HTTP contract unit tests."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Any

from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.models import RunRegistration, TelemetryEvent
from telemetry_collector.repository import (
    RunRegistrationConflictError,
    canonical_events,
    initial_run_state,
    materialize_run,
    run_documents,
    run_summary,
)


class FakeTelemetryRepository:
    """Thread-safe in-memory implementation of the repository protocol."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, dict[str, dict[str, Any]]] = {}
        self._producers: dict[tuple[str, str], datetime] = {}
        self._sequences: set[tuple[str, str, int]] = set()
        self._lock = RLock()

    def register_run(
        self,
        registration: RunRegistration,
        principal: HuggingFacePrincipal,
    ) -> None:
        with self._lock:
            existing = self._runs.get(registration.run_id)
            if existing is None:
                existing = initial_run_state(registration, principal)
                self._runs[registration.run_id] = existing
                self._events[registration.run_id] = {}
            elif existing["owner_hf_id"] != principal.user_id:
                raise PermissionError("Run belongs to another Hugging Face user.")
            elif (
                existing["country_code"],
                existing["pipeline"],
                existing["candidate_id"],
                existing["release_id"],
                existing["run_kind"],
            ) != (
                registration.country_code,
                registration.pipeline,
                registration.candidate_id,
                registration.release_id,
                registration.run_kind,
            ):
                raise RunRegistrationConflictError(
                    "Run identifier is already registered with different metadata."
                )
            self._producers.setdefault(
                (registration.run_id, registration.producer_id),
                existing["registered_at"],
            )

    def append_events(
        self,
        run_id: str,
        owner_hf_id: str,
        events: Iterable[TelemetryEvent],
    ) -> tuple[int, int]:
        accepted = 0
        duplicates = 0
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            if self._runs[run_id]["owner_hf_id"] != owner_hf_id:
                raise PermissionError("Run belongs to another Hugging Face user.")
            stored = self._events[run_id]
            for model in events:
                event = model.model_dump(mode="python")
                sequence_key = (run_id, event["producer_id"], event["sequence"])
                if event["event_id"] in stored or sequence_key in self._sequences:
                    duplicates += 1
                    continue
                producer_key = (run_id, event["producer_id"])
                if producer_key not in self._producers:
                    raise PermissionError(
                        "Telemetry producer is not registered for this run."
                    )
                event["producer_registered_at"] = self._producers[producer_key]
                stored[event["event_id"]] = event
                self._sequences.add(sequence_key)
                accepted += 1
            self._runs[run_id] = materialize_run(
                self._runs[run_id], canonical_events(stored.values())
            )
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
                and (before is None or (run["registered_at"], run["run_id"]) < before)
            ]
            runs.sort(
                key=lambda run: (run["registered_at"], run["run_id"]), reverse=True
            )
            return [run_summary(deepcopy(run)) for run in runs[:limit]]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return run_documents(
                deepcopy(run), deepcopy(list(self._events[run_id].values()))
            )

    def is_ready(self) -> bool:
        return True
