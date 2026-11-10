"""Who may read an output derived from a submission.

Workers record the visibility this module gives for every output, so the
calibration tree handler and the Orrery graph handler apply the same rules:

- calibration trees may be public, including those built from licensed data;
- an Orrery graph file of a licensed build is private: it stays in the
  collector's private Orrery bucket, served only to microcosm-runs by the
  Orrery read service, a separate Cloud Run service whose IAM policy lets
  only microcosm-runs' service account call it
  (``telemetry_collector.orrery_read_app``). microcosm-runs shows licensed
  runs' files only to people signed in with policyengine.org Google
  accounts (tracker 2b · 7 and 4b · 3). There is no clearance step that
  makes it public. One of an unlicensed build may be public: the read
  service serves it only to microcosm-runs too, which may show it to
  anyone. When serving, the read service also treats a document as private
  when a recorded submission of a run or a build it comes from had licensed
  data (``telemetry_collector.orrery_serving``);
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
