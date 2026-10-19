"""Artifact intake settings, and the storage and queue services intake uses.

Intake is off unless ``COLLECTOR_INTAKE_ENABLED`` is exactly ``1``. While it
is off, the collector reads none of the other intake settings, mounts no
submission endpoints, and refuses to issue ``artifacts:submit`` credentials.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

INTAKE_SWITCH = "COLLECTOR_INTAKE_ENABLED"
SERVICE_ACCOUNT_PATTERN = (
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\.iam\.gserviceaccount\.com$"
)
BUCKET_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$"
QUEUE_PATTERN = r"^projects/[^/]+/locations/[^/]+/queues/[A-Za-z0-9-]{1,100}$"
JOB_PATTERN = r"^projects/[^/]+/locations/[^/]+/jobs/[a-z][a-z0-9-]{0,62}$"


def _setting(name: str, pattern: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required when {INTAKE_SWITCH} is 1.")
    if not re.fullmatch(pattern, value):
        raise RuntimeError(f"{name} is not a valid resource name.")
    return value


@dataclass(frozen=True)
class IntakeSettings:
    """Google Cloud resources used by artifact intake."""

    bucket: str
    signing_service_account: str
    worker_queue: str
    worker_job: str
    worker_invoker_service_account: str

    @classmethod
    def from_environment(cls) -> IntakeSettings | None:
        """Return the intake settings, or ``None`` while intake is off."""

        if os.environ.get(INTAKE_SWITCH) != "1":
            return None
        return cls(
            bucket=_setting("COLLECTOR_INTAKE_BUCKET", BUCKET_PATTERN),
            signing_service_account=_setting(
                "COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT", SERVICE_ACCOUNT_PATTERN
            ),
            worker_queue=_setting("COLLECTOR_WORKER_QUEUE", QUEUE_PATTERN),
            worker_job=_setting("COLLECTOR_WORKER_JOB", JOB_PATTERN),
            worker_invoker_service_account=_setting(
                "COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT", SERVICE_ACCOUNT_PATTERN
            ),
        )
