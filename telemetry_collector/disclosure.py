"""Who may read an output derived from a submission (decisions D5 and D10).

Workers record the visibility this module gives for every output, so the
calibration-tree (K4) and Orrery (O4) handlers apply the same rules:

- calibration trees may be public, including those built from licensed data
  (D10);
- an Orrery artifact of a licensed build stays private until output clearance,
  which no worker grants (D5), and one of an unlicensed build may be public;
- native evidence is never published, whatever ``licensed_data`` says.
"""

from __future__ import annotations

from typing import Literal

OutputKind = Literal["calibration_tree", "orrery_artifact", "native_evidence"]
OutputVisibility = Literal["public", "private"]


class NeverPublishedError(ValueError):
    """Native evidence stays in private intake storage and is never published."""


def output_visibility(output: OutputKind, *, licensed_data: bool) -> OutputVisibility:
    """The visibility a worker records for ``output``; refuses native evidence."""

    if output == "native_evidence":
        raise NeverPublishedError("Native evidence is never published.")
    if output == "orrery_artifact" and licensed_data:
        return "private"
    return "public"
