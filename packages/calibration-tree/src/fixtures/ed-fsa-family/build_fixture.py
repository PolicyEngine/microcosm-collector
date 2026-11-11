"""Build ed-fsa-family-schema-8.json from PolicyEngine/microcosm#1357's own data.

The fixture is the ED Federal Student Aid target family that microcosm#1357
compiles, written as schema 8 diagnostics rows: all 132 targets of the fit
receipt, each with the hierarchy Microcosm derives from its Chronicle member
facts. Nothing here imports Microcosm; the derivation below restates the
rules of the files it cites, at SOURCE_COMMIT, so that a reviewer can check
each step against them.

Usage (the source files are fetched from raw.githubusercontent.com into a
directory of their own, at their repository paths):

    python3 -I build_fixture.py <microcosm-checkout-dir> > ed-fsa-family-schema-8.json

The script refuses any source file whose sha256 differs from SOURCES.

Where each field comes from, at SOURCE_COMMIT:

- target name and value: the receipt's ``fsa_targets[].name`` and ``target``;
  ``final_estimate`` is ``b_with_fsa`` (the fit with the FSA targets) and
  ``initial_estimate`` is ``release`` (the host release's weights, before
  the refit). ``relative_error`` is ``b_with_fsa_error``.
- provider and category: ``fsa_student_loan_targets.py:133-138`` (measure to
  category) and ``:875-888`` (the seed), with labels from
  ``provider_labels.py`` and ``variable_labels.py``.
- member facts: a state or band target is one fact whose source record id is
  the target's name (``:685``, ``:758``); Parent PLUS is the loan-type fact
  whose groupby value is ``parent_plus`` (``:812-816``, ``:837``); the balance
  excluding Parent PLUS is every other ``dollars_outstanding`` detail row of
  the same loan-type record set (``:797-824``, ``:841-842``).
- geography: the member facts' one ``geography`` level, id and name
  (``ledger_targets.py:1375-1418``); no FSA reference declares a composed or
  aliased geography (``:1355-1373``).
- dimensions: the layout groupby dimension and value when every member fact
  shares both, then every other dimension they all share
  (``ledger_targets.py:1580-1610``, ``_constant_text`` at ``:1649-1651``).
- target label: the plan's label (``fsa_student_loan_targets.py:394``).
- source: the representative fact's citation (``ledger_targets.py:5842-5852``,
  ``_source_name`` at ``:5771-5774``). The representative of a single-fact
  target is its fact; of the linear combination, the last member (all share
  one period, ``:1232-1245``), in the operand order of
  ``fsa_student_loan_targets.py:819-824``.
- metadata: every key the diagnostics row of a release carries, in the order
  it writes them (``row_metadata``). The release this matches is one built
  by ``tools/build_us_fiscal_refresh_release.py`` with
  ``--scf-education-debt`` (the only way FSA targets compile,
  ``:13002-13013``) and the default ``--age-targets`` (``:1867-1871``).
  The compile merges ``_ledger_metadata`` of the representative fact,
  ``_multi_fact_reference_metadata``, ``publication_metadata``,
  ``_diagnostic_target_label_metadata``, ``ledger_resolved_assertion`` and
  ``_reference_metadata`` (``ledger_targets.py:1085-1095``);
  ``apply_fsa_student_loan_plan`` then drops ``diagnostic_target_label`` and
  adds the plan's reconciliation keys, ``fsa_published_value`` and
  ``target_period`` (``fsa_student_loan_targets.py:398-413``); and
  ``age_us_dollar_targets`` then adds ``basis``, ``aged_to``,
  ``aging_factor`` and ``aging_factor_source`` (``fiscal_targets.py:1126-1135``,
  ``target_aging.py:337-369``, ``:455-483``, ``:664-682``). The plan's own
  keys, its reference period (``source_period``, ``:579``), its controls and
  each table's partition are restated in ``fsa_plan`` from ``:262-364`` and
  ``:572-910``; the script checks the restated reference period, label,
  controls, every target value and the excluding-Parent-PLUS operands against
  the receipt's ``fsa`` block and ``fsa_targets`` and against the member facts,
  and refuses to write the fixture if any differs.
- keys left out of the metadata:
  - ``diagnostic_target_label``: ``_diagnostic_target_label_metadata``
    (``ledger_targets.py:5998-6018``) stamps only that key, and
    ``fsa_student_loan_targets.py:398-402`` drops it, so no release carries
    it for an FSA target.
  - none of the ``publication_metadata`` keys (``ledger_targets.py:1036-1056``):
    ``identity`` and ``linear_combination`` leave ``value_metadata`` empty
    (``:933``, ``:965-975``), and the publication keys are added only for the
    monthly window operations.
  - ``period_contract_waiver``: ``enforce_period_contract``
    (``fiscal_targets.py:1136-1140``, ``target_aging.py:300-334``) adds it
    only to a violating spec, and ``find_period_contract_violations`` skips
    every spec ``_not_ageable_reason`` names (``target_aging.py:256-258``),
    which is every FSA spec.
  - a release built with ``--no-age-targets`` carries none of the four aging
    keys; this fixture matches the default release.
- what runs between ``apply_fsa_student_loan_plan`` and the diagnostics row,
  at SOURCE_COMMIT, and why only aging is restated:
  - ``fiscal_targets.py:1104-1125``: seven passes, none of which changes an FSA
    spec. ``_uprate_cross_period_eitc_decompositions`` rewrites only specs
    with ``requires_total_eitc_uprating == "true"`` (``:1465-1468``);
    ``_with_derived_chip_enrollment_targets`` keeps every existing spec and
    appends CHIP specs derived from ``family == "cms_medicaid"`` ones
    (``:1295-1330``, ``:1388-1397``), and
    ``_without_m_chip_state_chip_enrollment_targets`` drops only
    ``cms_medicaid`` specs (``:1347-1351``, ``:1357-1362``); the taxable-interest and capital-gains
    rebases pass on every spec whose ``source_measure_id`` is not one of
    theirs (``:1810-1814``, ``:1560-1564``, ``:363-364``, ``:1659-1664``),
    and FSA's are ``dollars_outstanding`` and ``borrowers``;
    ``_rebase_soi_state_agi_bands`` passes on every spec without its flag
    (``:2108-2112``); and ``apply_ledger_target_profile``
    (``ledger_targets.py:740-774``, ``reconcile_ledger_target_hierarchy`` at
    ``:777-864``) replaces only ``congressional_district`` children under the
    US profile's one rule (``us/fiscal_target_references.json``), and FSA
    rows are ``country`` or ``state``. The rule also indexes every ``state``
    spec as a parent, FSA's 102 state rows included, and raises if two share
    a parent key (``ledger_targets.py:785-796``); FSA's 102 keys are
    distinct, and the key includes ``family``, one of the ``match.spec_fields``
    that rule lists (read at ``:6031-6054``).
  - ``fiscal_targets.py:1126-1135``: aging, restated in ``row_metadata``.
  - ``fiscal_targets.py:1136-1140``: the period contract, which adds nothing
    (above).
  - in the release tool, between ``:13002`` and the diagnostics write
    (``:15656``), every use of the registry reads, filters, appends to or
    rebuilds it, and passes every FSA spec through unchanged.
    ``apply_us_medicaid_enrollment_substitutions``
    (``:13041-13043``; ``us_runtime/medicaid_take_up.py:677-715``) keeps
    every spec as it is and only appends Medicaid specs.
    ``_without_opt_in_student_loan_targets`` (``:12619-12632``, called at
    ``:13054``) drops FSA only from the registry the parity gate reads;
    ``target_specs`` comes from the full registry (``:13065``). The
    ``TargetRegistry`` rebuilds at ``:13066`` and ``:13097`` keep the specs
    (``registry.py:281-300``). ``_select_target_surface`` (``:9118-9145``)
    under ``national_state`` drops what ``is_congressional_district_target``
    names (``microcosm-data``'s ``us_critical_targets.py:68-83``), none of
    the FSA rows. ``--selection-mass-protection`` appends non-FSA specs
    (``:14899-14902``). The checkpoint reload (``:2899``) and the fresh build
    (``:7113``) go through ``_compile_materialized_target_registry``
    (``:6195-6213``), which only filters specs. The frame stages take the
    specs and return frames (e.g. ``:13306-13309``, ``:14551-14556``), the
    loss weights read them (``:15042-15050``), and
    ``_without_calibrated_frames`` replaces only the result's frame
    (``:7468-7482``).
  - ``TargetSpec.to_target`` (``registry.py:242-255``, via
    ``to_target_set``, ``:370-372``) passes ``measure``, ``filter``,
    ``tolerance``, ``source``, ``metadata`` and ``hierarchy`` through;
    ``Target.__post_init__`` (``target.py:88-138``) copies the metadata as
    the same str-to-str mapping, in order.
  - the solve: ``calibrate`` and ``rebuild_calibration_result`` build the
    problem with ``build_constraint_matrix`` (``solve.py:2505``, ``:2815``),
    which appends each compiled ``Target`` object unchanged to
    ``problem.targets`` and its ``value`` to ``target_vector``
    (``matrix.py:298-343``, ``:368-376``); ``verify_monetary_binding``
    (``monetary_binding.py:185-200``) only checks, and returns early for a
    target without receipt keys. ``refit_l0_selection`` and
    ``calibrate_l0_refit`` solve through ``calibrate`` (``solve.py:3197``,
    ``:3335``).
  - the ``--exact-k`` solve: ``calibrate_exact_k_ladder`` (release tool
    ``:15131``) passes the same ``TargetSet`` to ``calibrate`` and
    ``refit_l0_selection`` (``us_runtime/exact_k_ladder.py:190-252``).
  - a target whose compile raises ``KeyError`` or ``ValueError`` goes to
    ``skipped`` (``matrix.py:326-334``), and the release drops its row rather
    than changing it. The release gate also fails the release when
    calibration skips a target (``_release_gate_failures``, release tool
    ``:9398-9399``); it leaves congressional district targets out of that
    count unless the build gates them (``:9389-9397``). Any other exception,
    and any failure of a target with ``monetary_binding`` metadata (no FSA
    target has it), propagate instead (``matrix.py:301-306``, ``:327-332``),
    so no row changes there either.
  - Apart from aging, nothing on that path changes a target's metadata or
    fields. After ``_target_row``, the diagnostics write still adds fields to
    the row (the attribution merge, below) and orders them (the typed writer,
    below).
- ``fsa_published_value``: the compiled value, one fact's value or the sum of
  the five loan-type values, every weight 1.0
  (``fsa_student_loan_targets.py:606``). The values are integers below 2**53,
  so the sum is exact whichever way it is added.
- row shape: ``_target_row``'s fields (``diagnostics.py:212-245``, with
  ``registry`` from ``:236-244``) and ``hierarchy`` (``:401-403``,
  ``:159-164``), in the order the writer puts them on disk, without the
  attribution fields:
  - order: ``write_calibration_diagnostics`` validates the payload as
    ``CalibrationDiagnosticsV8`` (``diagnostics.py:525``), and the writer
    dumps it with ``model_dump(mode="python", exclude_defaults=True)``
    (``microcosm-diagnostics``'s ``writer.py:103-104``). That follows
    ``TargetDiagnosticV8``'s declared order (``schema.py:128-149``):
    ``... within_tolerance, hierarchy, registry``. ``filter`` and
    ``within_tolerance`` have no default, so the dump keeps them as ``null``.
  - left out: when ``assemble_target_loss_attribution`` succeeds,
    ``diagnostics_payload`` writes ``target_loss_basis`` beside the targets
    and merges ``target_loss_weight``, ``target_loss_weight_share``,
    ``target_loss_scale``, ``final_capped_scaled_error`` and
    ``final_loss_contribution`` into every row (``diagnostics.py:482-485``,
    ``_target_loss_attribution.py:167-175``); when it raises, the release
    logs a ``diagnostic_warnings`` entry and leaves them out (``:450-481``).
    They depend on the whole release's loss basis, so this fixture of one
    family leaves them out.
  - ``measure``: ``_selector_payload(target.measure)`` (``:89-102``), a
    column named for the target: the plan sets ``measure=name``
    (``fsa_student_loan_targets.py:593``, ``:611``) and the compile copies it
    (``ledger_targets.py:1075``); the apply step and aging replace only the
    value, period, metadata and hierarchy
    (``fsa_student_loan_targets.py:403-415``, ``target_aging.py:682``).
  - ``filter``: ``null``. FSA references set none and
    ``LedgerTargetReference.filter`` defaults to ``None``
    (``ledger_targets.py:267``, copied at ``:1077``).
  - ``target`` and ``compiled_target``: both the receipt's ``target``.
    ``compiled_target`` is ``problem.target_vector[index]``
    (``diagnostics.py:398``, ``:230``), and ``target`` is the diagnostic's,
    which ``solve.py:931`` and ``:946`` set from that same entry.
  - ``within_tolerance``: ``null``. ``solve.py:939-940`` gives ``None`` for
    a target without a tolerance, and FSA references declare none
    (``ledger_targets.py:273``, copied at ``:1083``).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sys
from pathlib import Path

SOURCE_REPOSITORY = "PolicyEngine/microcosm"
SOURCE_COMMIT = "0ff293b1a335a29d46f1598f2039beffa242e632"
RECEIPT = "experiments/receipts/1074-fsa-calibration-fit.json"
FACTS = "packages/microcosm-build/tests/fixtures/us_fsa_portfolio_chronicle_347_facts.jsonl.gz"
SOURCES = {
    RECEIPT: "7298c3a858e2bccd514605bbf9d4f35041b4a25b7fcdc9ab916e3cdb51a73046",
    FACTS: "9faed0e397ed20c5b2ccddebfbe58ea83a81f1d4ed896b1bee9569c6f98ecdaa",
    # Read for the rules restated here, not parsed.
    "packages/microcosm-build/src/microcosm/build/us_runtime/fsa_student_loan_targets.py": (
        "574203e5588ff1e44cd46bd4b42a6a63abece94ba4235659ef842adf5f3c824b"
    ),
    "packages/microcosm-build/src/microcosm/build/ledger_targets.py": (
        "b8bd1fb6e06747b80e6096f13e6599341c42c45f9b93bb6dd89ea744abbc2cf0"
    ),
    "packages/microcosm-calibrate/src/microcosm/calibrate/diagnostics.py": (
        "c4c418f18f0577fceaeca0ffdba4ae555469d25e973f7dde672ce36c4cf4e153"
    ),
    "packages/microcosm-calibrate/src/microcosm/calibrate/hierarchy.py": (
        "d9a79463f954b42af2e89014cc681ab7a27085581a9ed5d0a0db70adeeaa0112"
    ),
    "packages/microcosm-calibrate/src/microcosm/calibrate/provider_labels.py": (
        "65ed20e0c1a53652320820c922563a47e50a0ca64ee91c319188e7a3d255dff3"
    ),
    "packages/microcosm-calibrate/src/microcosm/calibrate/variable_labels.py": (
        "a336ce350ced1843b6b16d89aae154de42560d3a471d33529cc094cc73c86475"
    ),
    "packages/microcosm-build/tests/engine_free/us/test_us_fsa_student_loan_targets.py": (
        "56a94d46388a4bf666f3ab4d4130c969a10b75593042217ac8d714d121549968"
    ),
    # The release's aging stage and its default (row_metadata), and the solve
    # that sets target, compiled_target and within_tolerance (main).
    "packages/microcosm-build/src/microcosm/build/us_runtime/target_aging.py": (
        "b4dc33e1cabbebdec806325a24ca7d863068d517b39e2724ee6562164c1c9170"
    ),
    "packages/microcosm-build/src/microcosm/build/us_runtime/fiscal_targets.py": (
        "19ec9db17110ef8d8db16c7c7d23cee8efdc4231d2d59fc1221cba7d7317df41"
    ),
    "tools/build_us_fiscal_refresh_release.py": (
        "1b7cb863448bb96c00af126b34ff2754fb006e5c6a99669d7750b815d30bb7a1"
    ),
    "packages/microcosm-calibrate/src/microcosm/calibrate/solve.py": (
        "6cea5b8cb68e28f16f3589b6273b6a471b653d967cd4e62d49ff369dce45c470"
    ),
    # The typed row model and the writer that set the row's field order on disk.
    "packages/microcosm-diagnostics/src/microcosm/diagnostics/schema.py": (
        "fa1173b27c6d3aae67624f8f5b7ac6b7bde8535d0f058df9b6525d14d39566d9"
    ),
    "packages/microcosm-diagnostics/src/microcosm/diagnostics/writer.py": (
        "3376b9dea2fb1c78206c001cff155778276cd00250fe92e1615fb618ff1da701"
    ),
}

PROVIDER = {"id": "ed_fsa", "label": "ED · Federal Student Aid"}  # provider_labels.py:35
CATEGORY_LABELS = {  # variable_labels.py:77-84
    "federal_student_loan_balance": "Federal student loan balance",
    "federal_student_loan_borrowers": "Federal student loan borrowers",
    "loan_balance_excluding_parent_plus": "Federal student loan balance excluding Parent PLUS",
    "parent_plus_loan_balance": "Parent PLUS loan balance",
}
# fsa_student_loan_targets.py:133-138, keyed by the receipt's target_role
# (built at :690, :768-771 and :850).
CATEGORY_BY_ROLE = {
    "fsa_state_balance": "federal_student_loan_balance",
    "fsa_age_band_balance": "federal_student_loan_balance",
    "fsa_debt_size_band_balance": "federal_student_loan_balance",
    "fsa_state_borrowers": "federal_student_loan_borrowers",
    "fsa_age_band_borrowers": "federal_student_loan_borrowers",
    "fsa_debt_size_band_borrowers": "federal_student_loan_borrowers",
    "fsa_parent_plus_balance": "parent_plus_loan_balance",
    "fsa_balance_excluding_parent_plus": "loan_balance_excluding_parent_plus",
}
EXCLUDING_PARENT_PLUS_SUFFIX = ".excluding_parent_plus.dollars_outstanding"
# The build period the plan restamps every spec to (receipt host release).
TARGET_PERIOD = 2024


def _at(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _text(obj, *path) -> str:
    value = _at(obj, *path)
    return "" if value is None else str(value)


def _constant_text(facts, *path) -> str:
    values = {_text(fact, *path).strip() for fact in facts}
    return next(iter(values)) if len(values) == 1 else ""


def _dimensions(fact) -> dict:
    filters = fact.get("filters")
    if isinstance(filters, dict):
        return filters
    dimensions = fact.get("dimensions")
    return dimensions if isinstance(dimensions, dict) else {}


def _value_id(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _single_label(labels: set[str], what: str) -> str:
    labels = {label.strip() for label in labels if label and label.strip()}
    if len(labels) != 1:
        raise ValueError(f"{what} needs exactly one label, got {sorted(labels)!r}.")
    return next(iter(labels))


def inherited_dimensions(facts) -> list[dict]:
    """ledger_targets.py:1580-1610."""

    first = _dimensions(facts[0])
    common = {
        key: value
        for key, value in first.items()
        if all(key in _dimensions(f) and _dimensions(f)[key] == value for f in facts[1:])
    }
    out = []
    groupby_id = _constant_text(facts, "layout", "groupby_dimension")
    groupby_value_id = _constant_text(facts, "layout", "groupby_value_id")
    if groupby_id and groupby_value_id:
        raw_value = common.pop(groupby_id, groupby_value_id)
        out.append(
            {
                "id": groupby_id,
                "label": _constant_text(facts, "layout", "groupby_dimension_label")
                or _single_label(
                    {_text(f, "dimension_labels", groupby_id) for f in facts}, groupby_id
                ),
                "value_id": _value_id(groupby_value_id),
                "value_label": _constant_text(facts, "layout", "groupby_value_label")
                or _single_label(
                    {
                        _text(f, "dimension_value_labels", groupby_id, _value_id(raw_value))
                        for f in facts
                    },
                    f"{groupby_id}={raw_value}",
                ),
            }
        )
    for dimension_id in sorted(common):
        value = common[dimension_id]
        out.append(
            {
                "id": dimension_id,
                "label": _single_label(
                    {_text(f, "dimension_labels", dimension_id) for f in facts}, dimension_id
                ),
                "value_id": _value_id(value),
                "value_label": _single_label(
                    {
                        _text(f, "dimension_value_labels", dimension_id, _value_id(value))
                        for f in facts
                    },
                    f"{dimension_id}={value}",
                ),
            }
        )
    return out


def geography(facts) -> dict:
    """ledger_targets.py:1375-1418 (no composed or aliased geography)."""

    pairs = {(_text(f, "geography", "level"), _text(f, "geography", "id")) for f in facts}
    if len(pairs) != 1:
        raise ValueError(f"Member facts span geographies {sorted(pairs)!r}.")
    level, geography_id = next(iter(pairs))
    names = {_text(f, "geography", "name").strip() for f in facts} - {""}
    if len(names) != 1:
        raise ValueError(f"Member facts name {geography_id} {sorted(names)!r}.")
    return {"id": geography_id, "label": next(iter(names)), "level": level}


def source_citation(fact) -> str:
    """ledger_targets.py:5842-5852."""

    parts = [
        _text(fact, "source", "source_name") or _text(fact, "observed_measure", "source_name"),
        _text(fact, "source", "source_table") or _text(fact, "observed_measure", "source_table"),
        _text(fact, "source", "source_file"),
        _text(fact, "source", "vintage"),
        _text(fact, "source", "url"),
    ]
    return " | ".join(part for part in parts if part) or "PolicyEngine Ledger source-backed fact"


# --- The plan (fsa_student_loan_targets.py) --------------------------------
#
# The plan's reference period, its controls and each table's partition, which
# the plan's metadata and its reconciliation keys carry. Restated from
# plan_fsa_student_loan_targets (:262-364) and its three planners.

FSA_SOURCE_NAME = "ed_fsa"  # fsa_student_loan_targets.py:105
FSA_MATERIALIZER = "fsa_stored_student_loan_balances"  # :108
FSA_PERIOD_ALIGNMENT = "stock_at_source_reference_date"  # :111
FSA_MEASURE_KEY = "fsa_measure"  # :120
# :112-117 and :126-131: measure -> (person columns summed, indicator count).
FSA_BALANCE_COLUMNS = ("federal_student_loan_balance", "federal_parent_plus_loan_balance")
FSA_MEASURES = {
    "total_balance": (FSA_BALANCE_COLUMNS, False),
    "borrowers": (FSA_BALANCE_COLUMNS, True),
    "parent_plus_balance": (("federal_parent_plus_loan_balance",), False),
    "balance_excluding_parent_plus": (("federal_student_loan_balance",), False),
}
BOUND_TABLES = (  # :149-153
    "portfolio_by_location",
    "portfolio_by_age",
    "portfolio_by_debt_size",
    "portfolio_by_loan_type",
)
MEASURE_BY_SOURCE_MEASURE = {"dollars_outstanding": "total_balance", "borrowers": "borrowers"}  # :156-159


def _format_float(value) -> str:
    """fsa_student_loan_targets.py:1003-1004."""

    return repr(float(value))


def _format_bound(value: float) -> str:
    """fsa_student_loan_targets.py:1007-1008."""

    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _fsa_source_name(fact) -> str:
    """fsa_student_loan_targets.py:1011-1014."""

    return _text(fact, "observed_measure", "source_name") or _text(fact, "source", "source_name")


def _measure_id(fact) -> str:
    """fsa_student_loan_targets.py:1017-1020."""

    return _text(fact, "observed_measure", "source_measure_id") or _text(fact, "layout", "measure_id")


def _table(fact) -> str:
    """fsa_student_loan_targets.py:969-971."""

    parts = _text(fact, "layout", "record_set_id").split(".")
    return parts[1] if len(parts) > 1 else ""


def _month_key(period: str) -> tuple:
    """fsa_student_loan_targets.py:974-979."""

    year, _, month = period.partition("-")
    try:
        return (int(year), int(month or 0))
    except ValueError:
        return (0, 0)


def _value(fact) -> float:
    """fsa_student_loan_targets.py:991-1000."""

    value = fact.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Non-numeric value {value!r}.")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Value {value!r}.")
    return value


def _fsa_constraint_rows(fact) -> tuple:
    """fsa_student_loan_targets.py:1035-1037."""

    constraints = _at(fact, "universe_constraints", "constraints")
    return tuple(constraints) if isinstance(constraints, (list, tuple)) else ()


def _constraint_bounds(fact, variable: str):
    """fsa_student_loan_targets.py:930-955."""

    lower, upper = -math.inf, math.inf
    found = False
    for row in _fsa_constraint_rows(fact):
        if _text(row, "variable") != variable:
            continue
        operator = _text(row, "operator")
        value = _at(row, "value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Non-numeric {variable} bound {value!r}.")
        if operator == ">=":
            lower = float(value)
        elif operator == "<":
            upper = float(value)
        else:
            raise ValueError(f"Unreviewed {variable} operator {operator!r}.")
        found = True
    return (lower, upper) if found else None


def _has_constraint(fact, variable: str) -> bool:
    """fsa_student_loan_targets.py:958-959."""

    return any(_text(row, "variable") == variable for row in _fsa_constraint_rows(fact))


def _state_fips(fact):
    """fsa_student_loan_targets.py:982-988."""

    geography_id = _text(fact, "geography", "id")
    if _text(fact, "geography", "level") != "state" or not geography_id.startswith("0400000US"):
        return None
    return geography_id.removeprefix("0400000US")


def _age_band_metadata(lower: float, upper: float) -> dict:
    """fsa_student_loan_targets.py:901-907 (keys at :144-147)."""

    metadata = {"age_bound_stamp_source": "constraint_rows"}
    if lower != -math.inf:
        metadata["age_lower_bound"] = _format_bound(lower)
    if upper != math.inf:
        metadata["age_upper_bound"] = _format_bound(upper)
    return metadata


def _balance_band_metadata(lower: float, upper: float) -> dict:
    """fsa_student_loan_targets.py:910-916 (keys at :140-141)."""

    metadata = {}
    if lower != -math.inf:
        metadata["fsa_balance_lower_bound"] = _format_bound(lower)
    if upper != math.inf:
        metadata["fsa_balance_upper_bound"] = _format_bound(upper)
    return metadata


def fsa_plan(facts: list) -> dict:
    """What the plan stamps on each target it names: its reference period and
    label, and per target name the compiled keys (:583), the reconciliation
    keys (:620) and the reconciled value (:619).

    Reference period: :275-304. Reference label: :309, :962-966. Locations:
    :624-710. Bands: :720-788 (age and debt size, :344-345). Loan types:
    :791-862.
    """

    fsa_facts = [fact for fact in facts if _fsa_source_name(fact) == FSA_SOURCE_NAME]
    by_table: dict = {}
    for fact in fsa_facts:
        by_table.setdefault(_table(fact), []).append(fact)
    periods = [
        {
            _text(fact, "period", "value")
            for fact in by_table[table]
            if _measure_id(fact) in MEASURE_BY_SOURCE_MEASURE
        }
        for table in BOUND_TABLES
    ]
    reference = max(set.intersection(*periods), key=_month_key)
    at_reference = {
        table: [f for f in by_table[table] if _text(f, "period", "value") == reference]
        for table in BOUND_TABLES
    }
    labels = {
        _text(f, "period_coverage", "source_period_label") for f in at_reference[BOUND_TABLES[0]]
    } - {""}
    reference_label = next(iter(labels)) if len(labels) == 1 else None
    planned: dict = {}

    def add(name, measure, source_measure, value, compiled, reconciliation, operands=None):
        if name in planned:
            raise ValueError(f"FSA target {name!r} planned twice.")
        planned[name] = {
            "measure": measure,
            "source_measure_id": source_measure,
            "value": float(value),
            "compiled": compiled,
            "reconciliation": reconciliation,
            # The linear combination's operands, by source record id (:605-608).
            "operands": operands,
        }

    # Locations, :624-710.
    controls: dict = {}
    for source_measure, measure in MEASURE_BY_SOURCE_MEASURE.items():
        rows = [f for f in at_reference["portfolio_by_location"] if _measure_id(f) == source_measure]
        states, out_of_scope, not_reported = {}, [], []
        for fact in rows:
            value_id = _text(fact, "layout", "groupby_value_id")
            fips = _state_fips(fact)
            if value_id == "not_reported":
                not_reported.append(fact)
            elif fips is not None and fips != "72":
                states[fips] = fact
            elif fips == "72" or _text(fact, "geography", "id").endswith("US72"):
                out_of_scope.append(fact)
            elif value_id == "other":
                out_of_scope.append(fact)
            else:
                raise ValueError(f"Unreviewed location row {_text(fact, 'lineage', 'source_record_id')!r}.")
        state_sum = math.fsum(_value(f) for f in states.values())
        out_of_scope_sum = math.fsum(_value(f) for f in out_of_scope)
        not_reported_value = math.fsum(_value(f) for f in not_reported)
        reported = state_sum + out_of_scope_sum
        published_total = reported + not_reported_value
        allocation = published_total / reported
        in_scope = state_sum * allocation
        controls[measure] = {
            "published_states_and_dc": state_sum,
            "published_out_of_scope": out_of_scope_sum,
            "published_not_reported": not_reported_value,
            "published_total": published_total,
            "not_reported_allocation_factor": allocation,
            "in_scope_control": in_scope,
            "in_scope_share_of_total": in_scope / published_total,
        }
        for fips, fact in sorted(states.items()):
            add(
                _text(fact, "lineage", "source_record_id"),
                measure,
                source_measure,
                _value(fact) * allocation,
                {"state_fips": fips},
                {
                    "fsa_reconciliation": "not_reported_allocated_pro_rata",
                    "fsa_reconciliation_factor": _format_float(allocation),
                    "fsa_control_value": _format_float(in_scope),
                },
            )

    # Age and debt-size bands, :720-788.
    for table, variable in (
        ("portfolio_by_age", "age"),
        ("portfolio_by_debt_size", "federal_student_loan_balance"),
    ):
        for source_measure, measure in MEASURE_BY_SOURCE_MEASURE.items():
            bands = []
            for fact in at_reference[table]:
                if _measure_id(fact) != source_measure:
                    continue
                bounds = _constraint_bounds(fact, variable)
                if bounds is None:
                    if table == "portfolio_by_age" and _has_constraint(fact, "age_reporting_status"):
                        continue
                    raise ValueError(f"{table} row carries no {variable} band.")
                bands.append((*bounds, fact))
            bands.sort(key=lambda band: band[0])
            partition = math.fsum(_value(fact) for _, _, fact in bands)
            control = controls[measure]["in_scope_control"]
            for lower, upper, fact in bands:
                share = _value(fact) / partition
                add(
                    _text(fact, "lineage", "source_record_id"),
                    measure,
                    source_measure,
                    control * share,
                    _age_band_metadata(lower, upper)
                    if table == "portfolio_by_age"
                    else _balance_band_metadata(lower, upper),
                    {
                        "fsa_reconciliation": "share_of_in_scope_location_control",
                        "fsa_share": _format_float(share),
                        "fsa_partition_value": _format_float(partition),
                        "fsa_control_value": _format_float(control),
                    },
                )

    # Loan types, :791-862.
    rows = [
        f
        for f in at_reference["portfolio_by_loan_type"]
        if _measure_id(f) == "dollars_outstanding"
        and _text(f, "layout", "table_record_kind") in {"detail", ""}
    ]
    parent_plus = [f for f in rows if _text(f, "layout", "groupby_value_id") == "parent_plus"]
    if len(parent_plus) != 1:  # :817-818
        raise ValueError("FSA loan-type table needs exactly one Parent PLUS row.")
    # :819-824: every other loan-type balance row, sorted by source record id.
    others = sorted(
        (f for f in rows if f is not parent_plus[0]),
        key=lambda f: _text(f, "lineage", "source_record_id"),
    )
    partition = math.fsum(_value(f) for f in rows)
    control = controls["total_balance"]["in_scope_control"]
    record_set = _text(parent_plus[0], "layout", "record_set_id")
    for measure, members, name in (
        ("parent_plus_balance", parent_plus, _text(parent_plus[0], "lineage", "source_record_id")),
        (
            "balance_excluding_parent_plus",
            others,
            f"{record_set}.excluding_parent_plus.dollars_outstanding",
        ),
    ):
        share = math.fsum(_value(f) for f in members) / partition
        add(
            name,
            measure,
            "dollars_outstanding",
            control * share,
            {},
            {
                "fsa_reconciliation": "share_of_in_scope_location_control",
                "fsa_share": _format_float(share),
                "fsa_partition_value": _format_float(partition),
                "fsa_control_value": _format_float(control),
            },
            [_text(f, "lineage", "source_record_id") for f in members] if len(members) > 1 else None,
        )
    return {
        "reference_period": reference,
        "reference_label": reference_label,
        "controls": controls,
        "targets": planned,
    }


def plan_reference_metadata(planned: dict, plan: dict, target_role: str) -> dict:
    """The metadata the plan gives its reference: fsa_student_loan_targets.py:572-586."""

    measure = planned["measure"]
    _, indicator = FSA_MEASURES[measure]
    metadata = {
        "materializer": FSA_MATERIALIZER,
        FSA_MEASURE_KEY: measure,
        "measure_mode": "indicator_sum" if indicator else "sum",
        "fsa_columns": ",".join(FSA_MEASURES[measure][0]),
        "target_role": target_role,
        # The planned row's source measure (:691, :772, :851).
        "source_measure_id": planned["source_measure_id"],
        "source_period": plan["reference_period"],
        "target_period": str(TARGET_PERIOD),
        "period_alignment": FSA_PERIOD_ALIGNMENT,
        "fsa_reference_period": plan["reference_period"],
        **planned["compiled"],
    }
    if plan["reference_label"]:
        metadata["fsa_reference_label"] = plan["reference_label"]
    return metadata


# --- The compile (ledger_targets.py) ----------------------------------------


def _fact_key(fact) -> str:
    """ledger_targets.py:5488-5493."""

    return _text(fact, "aggregate_fact_key") or _text(fact, "fact_key") or _text(fact, "legacy_fact_key")


def _source_record_id(fact) -> str:
    """ledger_targets.py:5496-5499."""

    return _text(fact, "source_record_id") or _text(fact, "lineage", "source_record_id")


def _primary_measure_concept(fact) -> str:
    """ledger_targets.py:5748-5760."""

    concepts = (
        _text(fact, "measure", "concept"),
        _text(fact, "concept_alignment", "canonical_concept"),
        _text(fact, "observed_measure", "source_concept"),
        _text(fact, "measure", "source_concept"),
    )
    concepts = tuple(dict.fromkeys(concept for concept in concepts if concept))
    return concepts[0] if concepts else ""


def _source_measure_concept(fact) -> str:
    """ledger_targets.py:5763-5768."""

    return (
        _text(fact, "observed_measure", "source_concept")
        or _text(fact, "measure", "source_concept")
        or _primary_measure_concept(fact)
    )


def _domain(fact) -> str:
    """ledger_targets.py:5787-5788."""

    return _text(fact, "domain") or _text(fact, "universe_constraints", "domain")


def _constraint_rows(fact) -> tuple:
    """ledger_targets.py:5791-5798."""

    constraints = fact.get("constraints")
    if isinstance(constraints, (list, tuple)):
        return tuple(constraints)
    universe = _at(fact, "universe_constraints", "constraints")
    return tuple(universe) if isinstance(universe, (list, tuple)) else ()


def _comparable_period_value(fact):
    """ledger_targets.py:5177-5198."""

    value = _at(fact, "period", "value")
    if _text(fact, "period", "type") != "fiscal_year":
        return value
    start = _text(fact, "period_coverage", "start_date")
    if len(start) < 4 or not start[:4].isdigit():
        return value
    if not str(value).strip().isdigit():
        return value
    return int(start[:4])


# ledger_targets.py:5928-5933.
_CONSTRAINT_BOUND_SUFFIXES = {
    ">=": "_lower_bound",
    ">": "_lower_bound_exclusive",
    "<": "_upper_bound",
    "<=": "_upper_bound_inclusive",
}


def _constraint_bound_filters(fact, dimensions) -> dict:
    """ledger_targets.py:5936-5967."""

    bounds = {}
    for row in _constraint_rows(fact):
        if (_text(row, "role") or "filter") != "filter":
            continue
        variable = _text(row, "variable")
        suffix = _CONSTRAINT_BOUND_SUFFIXES.get(_text(row, "operator"))
        if not variable or suffix is None or variable in dimensions:
            continue
        value = _at(row, "value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(float(value)):
            continue
        bounds[f"{variable}{suffix}"] = str(value)
    return bounds


def ledger_metadata(fact, *, fact_key: str) -> dict:
    """ledger_targets.py:5855-5920."""

    comparable_period = str(_comparable_period_value(fact))
    metadata = {
        "ledger_source": "policyengine-ledger-data",
        "ledger_fact_key": fact_key,
        "ledger_source_record_id": _source_record_id(fact),
        "ledger_aggregate_fact_key": _text(fact, "aggregate_fact_key"),
        "ledger_semantic_fact_key": _text(fact, "semantic_fact_key"),
        "ledger_legacy_fact_key": _text(fact, "legacy_fact_key"),
        "ledger_observed_measure_key": _text(fact, "observed_measure_key"),
        "ledger_dimension_set_key": _text(fact, "dimension_set_key"),
        "ledger_universe_constraint_set_key": _text(fact, "universe_constraint_set_key"),
        "ledger_measure_concept": _primary_measure_concept(fact),
        "ledger_measure_unit": _text(fact, "measure", "unit") or _text(fact, "observed_measure", "unit"),
        "ledger_source_concept": _source_measure_concept(fact),
        "ledger_concept_relation": _text(fact, "measure", "concept_relation")
        or _text(fact, "concept_alignment", "relation"),
        "ledger_concept_authority": _text(fact, "measure", "concept_authority")
        or _text(fact, "concept_alignment", "authority"),
        "ledger_legal_vintage": _text(fact, "measure", "legal_vintage")
        or _text(fact, "concept_alignment", "legal_vintage"),
        "ledger_period_type": _text(fact, "period", "type"),
        "ledger_fact_period": comparable_period,
        "ledger_fact_period_label": (
            _text(fact, "period", "value")
            if comparable_period != _text(fact, "period", "value")
            else ""
        ),
        "ledger_assertion": _text(fact, "assertion"),
        "ledger_geography_level": _text(fact, "geography", "level"),
        "ledger_geography_id": _text(fact, "geography", "id"),
        "ledger_geography_name": _text(fact, "geography", "name"),
        "ledger_geography_vintage": _text(fact, "geography", "vintage"),
        "ledger_fact_label": _text(fact, "label"),
        "ledger_entity_name": _text(fact, "entity", "name"),
        "ledger_entity_role": _text(fact, "entity", "role"),
        "ledger_domain": _domain(fact),
        "ledger_layout_record_set_id": _text(fact, "layout", "record_set_id"),
        "ledger_layout_record_set_spec_id": _text(fact, "layout", "record_set_spec_id"),
        "ledger_layout_groupby_dimension": _text(fact, "layout", "groupby_dimension"),
        "ledger_layout_groupby_value_id": _text(fact, "layout", "groupby_value_id"),
        "ledger_layout_groupby_value_label": _text(fact, "layout", "groupby_value_label"),
        "ledger_layout_measure_id": _text(fact, "layout", "measure_id"),
        "ledger_aggregation_method": _text(fact, "aggregation", "method"),
    }
    constraint_rows = _constraint_rows(fact)
    if constraint_rows:
        metadata["ledger_universe_constraint_count"] = str(len(constraint_rows))
    dimensions = _dimensions(fact)
    for key, value in sorted(dimensions.items()):
        if value is not None:
            metadata[f"ledger_filter_{key}"] = str(value)
    for key, value in sorted(_constraint_bound_filters(fact, dimensions).items()):
        metadata.setdefault(f"ledger_filter_{key}", value)
    return {key: value for key, value in metadata.items() if value}


def _varying_dimension_ids(facts) -> list:
    """ledger_targets.py:1626-1646."""

    if len(facts) < 2:
        return []
    dimension_ids = sorted({str(key) for fact in facts for key in _dimensions(fact)})
    return [
        dimension_id
        for dimension_id in dimension_ids
        if len(
            {
                json.dumps(_dimensions(fact).get(dimension_id), sort_keys=True, separators=(",", ":"))
                for fact in facts
            }
        )
        > 1
    ]


def multi_fact_reference_metadata(facts) -> dict:
    """ledger_targets.py:1318-1341."""

    if len(facts) == 1:
        return {}
    member_keys = tuple(_fact_key(fact) or _source_record_id(fact) for fact in facts)
    payload = json.dumps(member_keys, separators=(",", ":"))
    varying = _varying_dimension_ids(facts)
    return {
        "ledger_member_fact_count": str(len(facts)),
        "ledger_member_aggregate_fact_keys": payload,
        "ledger_member_fact_keys": payload,
        "ledger_member_fact_digest": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        **(
            {"ledger_aggregation_varying_dimensions": json.dumps(varying, separators=(",", ":"))}
            if varying
            else {}
        ),
    }


def _linear_combination_formula(operands) -> str:
    """ledger_targets.py:3679-3689, with each operand's label from
    _linear_combination_operand_label (:3660-3676). The plan's operands carry
    only a source record id and a weight (fsa_student_loan_targets.py:605-608),
    so each is labelled ``operand_<index>``."""

    terms = []
    for index, operand in enumerate(operands):
        weight = float(operand["weight"])
        sign = "+" if weight > 0 else "-"
        magnitude = abs(weight)
        scale = "" if magnitude == 1 else f"{magnitude:g}*"
        terms.append(f"{sign}{scale}operand_{index}")
    return " ".join(terms)


def reference_metadata(plan_metadata: dict, members: list) -> dict:
    """ledger_targets.py:5388-5453, for the plan's two reference shapes
    (fsa_student_loan_targets.py:588-617).

    Both set period_match_policy "exact" and leave assertion_policy
    ("observed_only", ledger_targets.py:277), observation_period and the three
    uprating fields (:283-286) at their defaults, so :5393-5394 and
    :5447-5452 stamp nothing. Neither operation is a monthly window, a
    difference, a ratio or a calendar-year window (:5395-5418, :5421-5432),
    and the plan's metadata has no composed_from_level (:5433-5441). A
    single-fact reference names its fact by ledger_source_record_id, not by
    ledger_selector, so it stamps no ledger_selector_* key (:5442-5446); the
    linear combination's selector is {"source_name": "ed_fsa"} (:603).
    """

    metadata = dict(plan_metadata)
    if len(members) == 1:
        metadata["ledger_value_operation"] = "identity"  # ledger_targets.py:262
    else:
        metadata["ledger_value_operation"] = "linear_combination"  # fsa :604
    metadata["ledger_assertion_policy"] = "observed_only"
    metadata["ledger_period_match_policy"] = "exact"
    if len(members) > 1:
        operands = [
            {"source_record_id": _source_record_id(fact), "weight": 1.0} for fact in members
        ]
        metadata["ledger_value_formula"] = _linear_combination_formula(operands)
        metadata["ledger_selector_source_name"] = FSA_SOURCE_NAME
    return metadata


def compiled_value(members: list) -> float:
    """The compiled spec's value, which fsa_published_value records
    (fsa_student_loan_targets.py:411): one fact's value (ledger_targets.py:1011)
    or the weighted sum of the linear combination (:965-975), every weight 1.0
    (fsa_student_loan_targets.py:606)."""

    values = [float(fact["value"]) for fact in members]
    if len(values) == 1:
        return values[0]
    return sum(1.0 * value for value in values)


#: target_aging.py:89-91.
DECLARED_UNAGED_PERIOD_ALIGNMENTS = frozenset({"stock_at_source_reference_date"})


def _aging_format_float(value: float) -> str:
    """target_aging.py:711-712."""

    return f"{value:.15g}"


def _not_ageable_reason(metadata: dict) -> str:
    """target_aging.py:455-483, for the two branches an FSA spec reaches.

    Every FSA spec stops at one of them: a borrower count is ``indicator_sum``
    and a balance is ``sum`` with the plan's ``period_alignment``. A spec that
    reached the unit or assertion checks below them would not be an FSA spec
    this script knows, so it refuses one.
    """

    if metadata.get("measure_mode") != "sum":
        return "not_dollar_amount"
    alignment = metadata.get("period_alignment", "")
    if alignment in DECLARED_UNAGED_PERIOD_ALIGNMENTS:
        return alignment
    raise ValueError(f"Unexpected ageable FSA spec, period_alignment {alignment!r}.")


def aged_metadata(metadata: dict) -> dict:
    """age_us_dollar_targets on one FSA spec, as a default release runs it
    (fiscal_targets.py:1126-1135; --age-targets on by default,
    tools/build_us_fiscal_refresh_release.py:1867-1871, passed at :13008).

    _age_spec (target_aging.py:337-369) checks uprating_factor (:346), which no
    FSA spec carries, then takes the not-ageable branch (:357-369):
    _with_aging_metadata (:664-682) appends basis, aged_to, aging_factor and
    aging_factor_source after every existing key, then sets source_period
    again to the value it already holds, which keeps its position (:680-681).
    aged_to is str(target_period) (:345), and the release passes PERIOD, 2024
    (tools/build_us_fiscal_refresh_release.py:428, :13004).
    """

    if "uprating_factor" in metadata:
        raise ValueError("An FSA spec carries uprating_factor.")
    aged = {
        **metadata,
        "basis": "fact",
        "aged_to": str(TARGET_PERIOD),
        "aging_factor": _aging_format_float(1.0),
        "aging_factor_source": _not_ageable_reason(metadata),
    }
    source_period = metadata.get("source_period", "")
    if source_period:
        aged["source_period"] = source_period
    return aged


def row_metadata(target: dict, members: list, representative: dict, plan: dict) -> dict:
    """The metadata a default release's diagnostics row writes for one FSA
    target, key for key (see the module docstring for the release).

    The compile merges, in this order (ledger_targets.py:1085-1095):
    _ledger_metadata of the representative fact, _multi_fact_reference_metadata,
    publication_metadata, _diagnostic_target_label_metadata,
    ledger_resolved_assertion and _reference_metadata. Then
    apply_fsa_student_loan_plan drops diagnostic_target_label and adds the
    plan's reconciliation keys, fsa_published_value and target_period
    (fsa_student_loan_targets.py:398-413). Then age_us_dollar_targets adds the
    four aging keys (aged_metadata). Nothing after it changes the metadata
    (module docstring).
    """

    planned = plan["targets"][target["name"]]
    metadata = {
        **ledger_metadata(representative, fact_key=_fact_key(representative)),
        **multi_fact_reference_metadata(members),
        # publication_metadata is value_metadata (ledger_targets.py:1036),
        # which identity and linear_combination leave empty (:933, :965-975);
        # :1037-1056 adds keys only for the monthly window operations.
        # _diagnostic_target_label_metadata (:1092, :5998-6018) is left out:
        # whatever it stamps is diagnostic_target_label, which
        # fsa_student_loan_targets.py:398-402 drops.
        "ledger_resolved_assertion": _text(representative, "assertion") or "observation",  # :5004-5005
        **reference_metadata(plan_reference_metadata(planned, plan, target["target_role"]), members),
    }
    # apply_fsa_student_loan_plan (fsa_student_loan_targets.py:408-413);
    # target_period is already present, so it keeps its position.
    applied = {
        **metadata,
        **planned["reconciliation"],
        "fsa_published_value": _format_float(compiled_value(members)),
        "target_period": str(TARGET_PERIOD),
    }
    return aged_metadata(applied)


def member_facts(target: dict, facts_by_id: dict, facts: list) -> list:
    name = target["name"]
    if target["target_role"] == "fsa_balance_excluding_parent_plus":
        if not name.endswith(EXCLUDING_PARENT_PLUS_SUFFIX):
            raise ValueError(f"Unexpected excluding-Parent-PLUS name {name!r}.")
        record_set = name[: -len(EXCLUDING_PARENT_PLUS_SUFFIX)]
        members = [
            f
            for f in facts
            if _text(f, "layout", "record_set_id") == record_set
            and (_text(f, "observed_measure", "source_measure_id") or _text(f, "layout", "measure_id"))
            == "dollars_outstanding"
            and _text(f, "layout", "table_record_kind") in {"detail", ""}
            and _text(f, "layout", "groupby_value_id") != "parent_plus"
        ]
        if len(members) < 2:
            raise ValueError(f"{name!r} has {len(members)} member facts.")
        return sorted(members, key=lambda f: _text(f, "lineage", "source_record_id"))
    if name not in facts_by_id:
        raise ValueError(f"No Chronicle fact has source record id {name!r}.")
    return [facts_by_id[name]]


def main(root: Path) -> dict:
    for path, expected in SOURCES.items():
        actual = hashlib.sha256((root / path).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"{path}: sha256 {actual}, expected {expected}.")
    receipt = json.loads((root / RECEIPT).read_text())
    with gzip.open(root / FACTS, "rt", encoding="utf-8") as handle:
        facts = [json.loads(line) for line in handle if line.strip()]
    facts_by_id: dict[str, dict] = {}
    for fact in facts:
        record_id = _text(fact, "lineage", "source_record_id")
        if record_id in facts_by_id:
            raise ValueError(f"Two facts share source record id {record_id!r}.")
        facts_by_id[record_id] = fact

    plan = fsa_plan(facts)
    # The receipt records the same plan: check the restatement against it.
    receipt_fsa = receipt["fsa"]
    if (plan["reference_period"], plan["reference_label"], plan["controls"]) != (
        receipt_fsa["reference_period"],
        receipt_fsa["reference_label"],
        receipt_fsa["controls"],
    ):
        raise ValueError("The restated plan's reference or controls differ from the receipt's.")
    if sorted(plan["targets"]) != sorted(target["name"] for target in receipt["fsa_targets"]):
        raise ValueError("The restated plan names other targets than the receipt.")

    rows = []
    for target in receipt["fsa_targets"]:
        if plan["targets"][target["name"]]["value"] != target["target"]:
            raise ValueError(f"{target['name']!r}: restated value differs from the receipt's.")
        members = member_facts(target, facts_by_id, facts)
        # The balance excluding Parent PLUS: the member facts selected above
        # must be the plan's operands, in order (fsa_student_loan_targets.py:819-824).
        operands = plan["targets"][target["name"]]["operands"]
        if (operands is not None) != (target["target_role"] == "fsa_balance_excluding_parent_plus"):
            raise ValueError(f"{target['name']!r}: unexpected plan operands {operands!r}.")
        if operands is not None and [
            _text(f, "lineage", "source_record_id") for f in members
        ] != operands:
            raise ValueError(f"{target['name']!r}: member facts differ from the plan's operands.")
        category_id = CATEGORY_BY_ROLE[target["target_role"]]
        representative = members[-1]
        # _target_row's fields (diagnostics.py:212-245) and hierarchy
        # (:401-403), in the order the writer dumps them (schema.py:128-149,
        # writer.py:103-104): hierarchy before registry. The module docstring
        # says where measure, filter, compiled_target and within_tolerance
        # come from, and why the attribution fields are left out.
        rows.append(
            {
                "name": f"{target['name']}@{TARGET_PERIOD}",
                "target_name": target["name"],
                "period": TARGET_PERIOD,
                "entity": "household",
                "measure": {"kind": "column", "name": target["name"]},
                "filter": None,
                "source": source_citation(representative),
                "metadata": row_metadata(target, members, representative, plan),
                "target": target["target"],
                "compiled_target": target["target"],
                "initial_estimate": target["release"],
                "final_estimate": target["b_with_fsa"],
                "relative_error": target["b_with_fsa_error"],
                "within_tolerance": None,
                "hierarchy": {
                    "provider": dict(PROVIDER),
                    "category": {
                        "id": f"{PROVIDER['id']}.{category_id}",
                        "label": CATEGORY_LABELS[category_id],
                        "provider_id": PROVIDER["id"],
                    },
                    "geography": geography(members),
                    "dimensions": inherited_dimensions(members),
                    "target": {"id": target["name"], "label": target["label"]},
                },
                "registry": {"family": "ed_fsa", "se": None, "signed": False, "notes": ""},
            }
        )
    if len(rows) != 132:
        raise ValueError(f"Expected 132 FSA targets, got {len(rows)}.")
    return {
        "fixture_provenance": {
            "repository": SOURCE_REPOSITORY,
            "commit": SOURCE_COMMIT,
            "sha256": SOURCES,
            "script": "build_fixture.py",
        },
        "schema_version": 8,
        "targets": rows,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    json.dump(main(Path(sys.argv[1])), sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
