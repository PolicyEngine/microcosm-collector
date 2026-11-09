"""Who may read an output derived from a submission.

Workers record the visibility this module gives for every output, so the
calibration tree handler and the Orrery graph handler apply the same rules:

- calibration trees may be public, including those built from licensed data;
- an Orrery graph file of a licensed build is private: it stays in the
  collector's private Orrery bucket, to be served only to signed-in members of
  the PolicyEngine organization (tracker 2b · 5). There is no clearance step
  that makes it public. One of an unlicensed build may be public;
- native evidence is never published, whatever ``licensed_data`` says.
"""

from __future__ import annotations

from typing import Literal, get_args

OutputKind = Literal["calibration_tree", "orrery_artifact", "native_evidence"]
OutputVisibility = Literal["public", "private"]
#: Every visibility a worker records, in the order the database checks them.
OUTPUT_VISIBILITIES: tuple[OutputVisibility, ...] = get_args(OutputVisibility)


class NeverPublishedError(ValueError):
    """Native evidence stays in private intake storage and is never published."""


def output_visibility(output: OutputKind, *, licensed_data: bool) -> OutputVisibility:
    """The visibility a worker records for ``output``; refuses native evidence."""

    if output == "native_evidence":
        raise NeverPublishedError("Native evidence is never published.")
    if output == "orrery_artifact" and licensed_data:
        return "private"
    return "public"
