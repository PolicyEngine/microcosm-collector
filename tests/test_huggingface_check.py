"""The daily check requests every dataset the collector reads with its token."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from telemetry_collector.huggingface_check import (
    CONFIGURATION_CHECK,
    TOKEN_CHECK,
    Finding,
    check_token_access,
    main,
)
from telemetry_collector.huggingface_read import (
    HUGGINGFACE_DATASETS,
    TOKEN_VARIABLE,
    HuggingFaceReader,
    HuggingFaceReadSettings,
)
from tests.huggingface_fakes import FakeHuggingFace

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_TOKEN = "hf_test_collector_read_token"
UK_RELEASES = "policyengine/populace-uk-private"


def hub_with_every_dataset() -> FakeHuggingFace:
    hub = FakeHuggingFace(token=EXAMPLE_TOKEN)
    for dataset in HUGGINGFACE_DATASETS:
        hub.add_dataset(dataset.name, private=not dataset.public)
    return hub


def run_main(
    hub: FakeHuggingFace, environment: dict[str, str] | None = None
) -> tuple[int, list[dict]]:
    output = io.StringIO()
    exit_code = main(
        {TOKEN_VARIABLE: EXAMPLE_TOKEN} if environment is None else environment,
        client=hub.client(),
        output=output,
    )
    return exit_code, [json.loads(entry) for entry in output.getvalue().splitlines()]


def problems(log_entries: list[dict]) -> list[dict]:
    return [entry for entry in log_entries if "code" in entry]


def test_every_dataset_answering_200_passes_the_token_check() -> None:
    hub = hub_with_every_dataset()

    exit_code, log_entries = run_main(hub)

    assert exit_code == 0
    assert problems(log_entries) == []
    (summary,) = log_entries
    assert summary["severity"] == "INFO"
    assert summary["checked_datasets"] == [
        dataset.name for dataset in HUGGINGFACE_DATASETS
    ]
    assert sorted(request.url.path for request in hub.requests) == sorted(
        f"/api/datasets/{dataset.name}" for dataset in HUGGINGFACE_DATASETS
    )
    assert all(
        request.headers["Authorization"] == f"Bearer {EXAMPLE_TOKEN}"
        for request in hub.requests
    )


@pytest.mark.parametrize("status_code", [302, 401, 403, 404, 429, 500, 503])
def test_a_dataset_answering_anything_but_200_fails_the_check(
    status_code: int,
) -> None:
    hub = hub_with_every_dataset()
    hub.datasets[UK_RELEASES].forced_status = status_code

    exit_code, log_entries = run_main(hub)

    assert exit_code == 1
    (problem,) = problems(log_entries)
    assert problem == {
        "severity": "ERROR",
        "message": problem["message"],
        "check": TOKEN_CHECK,
        "dataset": UK_RELEASES,
        "code": f"HTTP_{status_code}",
    }
    assert f"HTTP {status_code}" in problem["message"]
    summary = log_entries[-1]
    assert summary["severity"] == "ERROR"
    assert summary["problems"] == 1
    assert len(hub.requests) == len(HUGGINGFACE_DATASETS)


def test_an_unreachable_hugging_face_fails_the_check_for_every_dataset() -> None:
    def refuse_connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    output = io.StringIO()
    exit_code = main(
        {TOKEN_VARIABLE: EXAMPLE_TOKEN},
        client=httpx.Client(transport=httpx.MockTransport(refuse_connection)),
        output=output,
    )

    log_entries = [json.loads(entry) for entry in output.getvalue().splitlines()]
    assert exit_code == 1
    assert [(entry["dataset"], entry["code"]) for entry in problems(log_entries)] == [
        (dataset.name, "UNREACHABLE") for dataset in HUGGINGFACE_DATASETS
    ]


def test_a_200_without_a_head_commit_fails_the_check() -> None:
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"private": True})
            )
        ),
    )

    findings, head_commits = check_token_access(reader, HUGGINGFACE_DATASETS[:1])

    assert findings == [
        Finding(
            dataset=HUGGINGFACE_DATASETS[0].name,
            check=TOKEN_CHECK,
            code="UNUSABLE_RESPONSE",
            message=findings[0].message,
        )
    ]
    assert head_commits == {}


def test_the_head_commit_of_each_dataset_that_answered_is_returned() -> None:
    hub = hub_with_every_dataset()
    hub.datasets[UK_RELEASES].head_commit = "e" * 40
    hub.datasets["policyengine/populace-us-staging"].forced_status = 404
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN), client=hub.client()
    )

    findings, head_commits = check_token_access(reader, HUGGINGFACE_DATASETS)

    assert [finding.dataset for finding in findings] == [
        "policyengine/populace-us-staging"
    ]
    assert head_commits[UK_RELEASES] == "e" * 40
    assert set(head_commits) == {dataset.name for dataset in HUGGINGFACE_DATASETS} - {
        "policyengine/populace-us-staging"
    }


@pytest.mark.parametrize(
    "environment",
    [{}, {TOKEN_VARIABLE: EXAMPLE_TOKEN, "HF_TOKEN": "hf_test_generic_token"}],
    ids=["missing token", "generic token"],
)
def test_an_unusable_token_setting_fails_the_job_before_any_request(
    environment: dict[str, str],
) -> None:
    hub = hub_with_every_dataset()

    exit_code, log_entries = run_main(hub, environment)

    assert exit_code == 2
    (entry,) = log_entries
    assert entry["severity"] == "ERROR"
    assert entry["check"] == CONFIGURATION_CHECK
    assert hub.requests == []
    assert "hf_test_generic_token" not in json.dumps(entry)


def test_no_log_entry_contains_the_token() -> None:
    hub = hub_with_every_dataset()
    for dataset in hub.datasets.values():
        dataset.forced_status = 401

    exit_code, log_entries = run_main(hub)

    assert exit_code == 1
    assert EXAMPLE_TOKEN not in json.dumps(log_entries)


def test_every_log_entry_has_a_severity_and_a_message() -> None:
    hub = hub_with_every_dataset()
    hub.datasets[UK_RELEASES].forced_status = 403

    _, log_entries = run_main(hub)

    assert log_entries
    for entry in log_entries:
        assert entry["severity"] in {"INFO", "ERROR"}
        assert isinstance(entry["message"], str) and entry["message"]


def test_the_check_runs_as_a_module_and_exits_nonzero_without_a_token() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "telemetry_collector.huggingface_check"],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 2, completed.stderr
    (entry,) = [json.loads(entry) for entry in completed.stdout.splitlines()]
    assert TOKEN_VARIABLE in entry["message"]
