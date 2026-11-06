"""Disclosure rules for outputs derived from submissions (decisions D5 and D10)."""

from __future__ import annotations

from typing import Any

import pytest

from telemetry_collector.disclosure import NeverPublishedError, output_visibility
from telemetry_collector.worker import JobContext
from tests.test_worker import accepted_submission


@pytest.mark.parametrize("licensed_data", [True, False])
def test_calibration_trees_may_be_public_whether_or_not_licensed(
    licensed_data: bool,
) -> None:
    assert (
        output_visibility("calibration_tree", licensed_data=licensed_data) == "public"
    )


def test_orrery_artifacts_of_licensed_builds_stay_private_until_cleared() -> None:
    assert output_visibility("orrery_artifact", licensed_data=True) == "private"


def test_orrery_artifacts_of_unlicensed_builds_may_be_public() -> None:
    assert output_visibility("orrery_artifact", licensed_data=False) == "public"


@pytest.mark.parametrize("licensed_data", [True, False])
def test_native_evidence_is_never_published(licensed_data: bool) -> None:
    with pytest.raises(NeverPublishedError):
        output_visibility("native_evidence", licensed_data=licensed_data)


def test_handlers_get_the_rule_for_their_submission() -> None:
    accepted = accepted_submission()
    seen: dict[str, Any] = {}

    def build_outputs(context: JobContext) -> dict[str, Any]:
        seen["tree"] = context.output_visibility("calibration_tree")
        seen["orrery"] = context.output_visibility("orrery_artifact")
        return {}

    accepted.run({"calibration-diagnostics": build_outputs})

    assert accepted.record().licensed_data is True
    assert seen == {"tree": "public", "orrery": "private"}
