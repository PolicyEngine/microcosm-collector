import type { Calibration } from "./latest-artifact";
import type { ComparisonFit } from "./calibration-explorer";
import {
  matchTargetSurfaces,
  type TargetMatchingSummary,
  type TargetSurfaceStatus,
} from "./target-surface-matcher";
import type { TargetRowRepresentation } from "./target-representation";

type TargetRow = Calibration["rows"][number];
export type TargetChangeDetailRow = TargetRow;

export const TARGET_CHANGE_EPSILON = 1e-12;
const COMPARISON_FIT_EPSILON = 1e-9;

export type TargetChangeMode = "reported" | "shared";
export type { TargetSurfaceStatus } from "./target-surface-matcher";

export interface TargetChangeSide {
  target: number | null;
  finalEstimate: number | null;
  weight: number;
  weightShare: number;
  scale: number;
  cappedError: number;
  contribution: number;
}

export interface TargetChangeRow extends Record<string, unknown> {
  name: string;
  base_name: string;
  comparison_id: string;
  match_kind: "base_name" | "chronicle_fact_key" | "structured_identity" | null;
  current_name: string | null;
  candidate_name: string | null;
  current_representation: TargetRowRepresentation | null;
  candidate_representation: TargetRowRepresentation | null;
  current_target_ordinal: number | null;
  candidate_target_ordinal: number | null;
  comparison_status: TargetSurfaceStatus;
  comparison_fit: ComparisonFit;
  current: TargetChangeSide | null;
  candidate: TargetChangeSide | null;
  currentDetail: TargetChangeDetailRow | null;
  candidateDetail: TargetChangeDetailRow | null;
  reported_change: number;
  pooled_weight_share: number | null;
  shared_current_contribution: number | null;
  shared_candidate_contribution: number | null;
  shared_change: number | null;
}

export interface TargetChangeAttributionSide {
  releaseId: string;
  calibrationProvenance: Calibration["calibration_provenance"];
  status: Calibration["target_loss_attribution"]["status"];
  aggregate: number | null;
  cap: number | null;
  basisIdentifier: string | null;
}

export interface TargetChangeMethodology {
  comparable: boolean;
  warning: string | null;
}

export interface TargetChangeSummary {
  mode: TargetChangeMode;
  currentScore: number;
  candidateScore: number;
  netChange: number;
  grossIncrease: number;
  grossReduction: number;
  reconciliationDifference: number;
  comparisonTargets: number;
  shared: number;
  added: number;
  removed: number;
  changed: number;
  unchanged: number;
}

export interface TargetChangeDataset {
  available: boolean;
  reason: string | null;
  current: TargetChangeAttributionSide;
  candidate: TargetChangeAttributionSide;
  methodology: TargetChangeMethodology;
  matching: TargetMatchingSummary;
  rows: TargetChangeRow[];
  summaries: Record<TargetChangeMode, TargetChangeSummary | null>;
  modeReasons: Record<TargetChangeMode, string | null>;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function hierarchyFields(row: TargetRow): Record<string, unknown> {
  return {
    source: row.source ?? null,
    source_label: row.source_label ?? null,
    variable: row.variable ?? null,
    variable_label: row.variable_label ?? null,
    target_label: row.target_label ?? null,
    target_representation: row.target_representation ?? null,
    variable_key: row.variable_key ?? null,
    measure: row.measure ?? null,
    source_measure_id: row.source_measure_id ?? null,
    level: row.level ?? null,
    geography: row.geography ?? null,
    family: row.family ?? null,
    breakdown: row.breakdown ?? null,
    dims: row.dims ?? null,
    target_dimensions: row.target_dimensions ?? null,
    calibration_status: row.calibration_status ?? null,
    abs_relative_error: row.abs_relative_error ?? null,
  };
}

function attributionSide(calibration: Calibration): TargetChangeAttributionSide {
  const attribution = calibration.target_loss_attribution;
  return {
    releaseId: calibration.release_id,
    calibrationProvenance: calibration.calibration_provenance,
    status: attribution.status,
    aggregate: finiteNumber(attribution.aggregate),
    cap: finiteNumber(attribution.cap),
    basisIdentifier: attribution.basis_identifier,
  };
}

function targetSide(row: TargetRow | undefined): TargetChangeSide | null {
  if (!row) return null;
  const weight = finiteNumber(row.target_loss_weight);
  const weightShare = finiteNumber(row.target_loss_weight_share);
  const scale = finiteNumber(row.target_loss_scale);
  const cappedError = finiteNumber(row.final_capped_scaled_error);
  const contribution = finiteNumber(row.final_loss_contribution);
  if (
    weight == null || weight < 0 ||
    weightShare == null || weightShare < 0 ||
    scale == null || scale <= 0 ||
    cappedError == null || cappedError < 0 ||
    contribution == null || contribution < 0
  ) {
    return null;
  }
  return {
    target: finiteNumber(row.target ?? row.value),
    finalEstimate: finiteNumber(row.final_estimate ?? row.estimate),
    weight,
    weightShare,
    scale,
    cappedError,
    contribution,
  };
}

function comparisonFit(
  currentRow: TargetRow | undefined,
  candidateRow: TargetRow | undefined,
  current: TargetChangeSide | null,
  candidate: TargetChangeSide | null,
): ComparisonFit {
  if (!currentRow || !candidateRow || !current || !candidate) {
    return "not_applicable";
  }
  const currentReportedError = finiteNumber(currentRow.abs_relative_error);
  const candidateReportedError = finiteNumber(candidateRow.abs_relative_error);
  const hasReportedErrors = currentReportedError != null && candidateReportedError != null;
  const currentError = hasReportedErrors ? currentReportedError : current.cappedError;
  const candidateError = hasReportedErrors ? candidateReportedError : candidate.cappedError;
  const change = Math.abs(candidateError) - Math.abs(currentError);
  if (change < -COMPARISON_FIT_EPSILON) return "improved";
  if (change > COMPARISON_FIT_EPSILON) return "regressed";
  return "unchanged";
}

function close(left: number | null, right: number | null): boolean {
  if (left == null || right == null) return left === right;
  return Math.abs(left - right) <= TARGET_CHANGE_EPSILON;
}

function methodology(
  current: TargetChangeAttributionSide,
  candidate: TargetChangeAttributionSide,
): TargetChangeMethodology {
  const capMatches = close(current.cap, candidate.cap);
  const basisMatches = current.basisIdentifier === candidate.basisIdentifier;
  const comparable = capMatches && basisMatches;
  return {
    comparable,
    warning: comparable
      ? null
      : "The current release and candidate use different target-loss caps or weighting methods. The comparison still uses each artifact's verified values, but it includes that methodology difference.",
  };
}

function summarize(
  rows: TargetChangeRow[],
  mode: TargetChangeMode,
  currentScore: number,
  candidateScore: number,
): TargetChangeSummary {
  const included = mode === "reported"
    ? rows
    : rows.filter((row) => row.comparison_status === "shared");
  const changeOf = (row: TargetChangeRow) =>
    mode === "reported" ? row.reported_change : row.shared_change ?? 0;
  const rowTotal = included.reduce((sum, row) => sum + changeOf(row), 0);
  const grossIncrease = included.reduce(
    (sum, row) => sum + Math.max(changeOf(row), 0),
    0,
  );
  const grossReduction = included.reduce(
    (sum, row) => sum + Math.max(-changeOf(row), 0),
    0,
  );
  const shared = rows.filter((row) => row.comparison_status === "shared").length;
  const added = rows.filter((row) => row.comparison_status === "added").length;
  const removed = rows.filter((row) => row.comparison_status === "removed").length;
  const changed = included.filter(
    (row) => Math.abs(changeOf(row)) > TARGET_CHANGE_EPSILON,
  ).length;
  return {
    mode,
    currentScore,
    candidateScore,
    netChange: candidateScore - currentScore,
    grossIncrease,
    grossReduction,
    reconciliationDifference: rowTotal - (candidateScore - currentScore),
    comparisonTargets: included.length,
    shared,
    added,
    removed,
    changed,
    unchanged: included.length - changed,
  };
}

function unavailableDataset(
  current: TargetChangeAttributionSide,
  candidate: TargetChangeAttributionSide,
  matching: TargetMatchingSummary,
  reason: string,
): TargetChangeDataset {
  return {
    available: false,
    reason,
    current,
    candidate,
    methodology: methodology(current, candidate),
    matching,
    rows: [],
    summaries: { reported: null, shared: null },
    modeReasons: { reported: reason, shared: reason },
  };
}

export function targetChangeForMode(
  row: TargetChangeRow,
  mode: TargetChangeMode,
): number | null {
  return mode === "reported" ? row.reported_change : row.shared_change;
}

export function buildTargetChangeDataset(
  currentCalibration: Calibration,
  candidateCalibration: Calibration,
): TargetChangeDataset {
  const current = attributionSide(currentCalibration);
  const candidate = attributionSide(candidateCalibration);
  const matched = matchTargetSurfaces(currentCalibration, candidateCalibration);
  if (current.status === "unavailable" || current.aggregate == null) {
    return unavailableDataset(
      current,
      candidate,
      matched.matching,
      "Weighted target-error attribution is unavailable for the current release.",
    );
  }
  if (candidate.status === "unavailable" || candidate.aggregate == null) {
    return unavailableDataset(
      current,
      candidate,
      matched.matching,
      "Weighted target-error attribution is unavailable for the candidate.",
    );
  }

  const rows: TargetChangeRow[] = [];

  for (const match of matched.matches) {
    const currentRow = match.current ?? undefined;
    const candidateRow = match.candidate ?? undefined;
    const name = String(
      candidateRow?.base_name ?? candidateRow?.name ??
      currentRow?.base_name ?? currentRow?.name ??
      match.comparison_id,
    );
    const currentTarget = targetSide(currentRow);
    const candidateTarget = targetSide(candidateRow);
    if ((currentRow && !currentTarget) || (candidateRow && !candidateTarget)) {
      return unavailableDataset(
        current,
        candidate,
        matched.matching,
        `Weighted target-error attribution is incomplete for target ${name}.`,
      );
    }
    const categoryRow = candidateRow ?? currentRow;
    rows.push({
      ...(categoryRow ? hierarchyFields(categoryRow) : {}),
      name,
      base_name: name,
      comparison_id: match.comparison_id,
      match_kind: match.match_kind,
      current_name: match.current_name,
      candidate_name: match.candidate_name,
      current_representation: match.current_representation,
      candidate_representation: match.candidate_representation,
      current_target_ordinal: match.current_target_ordinal,
      candidate_target_ordinal: match.candidate_target_ordinal,
      comparison_status: match.comparison_status,
      comparison_fit: comparisonFit(
        currentRow,
        candidateRow,
        currentTarget,
        candidateTarget,
      ),
      current: currentTarget,
      candidate: candidateTarget,
      currentDetail: currentRow ?? null,
      candidateDetail: candidateRow ?? null,
      reported_change:
        (candidateTarget?.contribution ?? 0) -
        (currentTarget?.contribution ?? 0),
      pooled_weight_share: null,
      shared_current_contribution: null,
      shared_candidate_contribution: null,
      shared_change: null,
    });
  }

  const sharedRows = rows.filter(
    (row) => row.comparison_status === "shared" && row.current && row.candidate,
  );
  const reportedSummary = summarize(
    rows,
    "reported",
    current.aggregate,
    candidate.aggregate,
  );
  if (Math.abs(reportedSummary.reconciliationDifference) > TARGET_CHANGE_EPSILON) {
    return unavailableDataset(
      current,
      candidate,
      matched.matching,
      "Per-target weighted error changes do not reconcile to the two attribution aggregates.",
    );
  }
  const currentSharedWeight = sharedRows.reduce(
    (sum, row) => sum + (row.current?.weightShare ?? 0),
    0,
  );
  const candidateSharedWeight = sharedRows.reduce(
    (sum, row) => sum + (row.candidate?.weightShare ?? 0),
    0,
  );
  let sharedReason: string | null = null;
  let sharedSummary: TargetChangeSummary | null = null;
  if (!sharedRows.length) {
    sharedReason = "The current release and candidate have no shared targets.";
  } else if (currentSharedWeight <= 0 || candidateSharedWeight <= 0) {
    sharedReason = "Shared targets do not have positive total weight on both sides.";
  } else {
    for (const row of sharedRows) {
      const pooledShare = (
        (row.current?.weightShare ?? 0) / currentSharedWeight +
        (row.candidate?.weightShare ?? 0) / candidateSharedWeight
      ) / 2;
      const currentContribution = pooledShare * (row.current?.cappedError ?? 0);
      const candidateContribution = pooledShare * (row.candidate?.cappedError ?? 0);
      row.pooled_weight_share = pooledShare;
      row.shared_current_contribution = currentContribution;
      row.shared_candidate_contribution = candidateContribution;
      row.shared_change = candidateContribution - currentContribution;
    }
    const currentSharedScore = sharedRows.reduce(
      (sum, row) => sum + (row.shared_current_contribution ?? 0),
      0,
    );
    const candidateSharedScore = sharedRows.reduce(
      (sum, row) => sum + (row.shared_candidate_contribution ?? 0),
      0,
    );
    sharedSummary = summarize(
      rows,
      "shared",
      currentSharedScore,
      candidateSharedScore,
    );
  }

  return {
    available: true,
    reason: null,
    current,
    candidate,
    methodology: methodology(current, candidate),
    matching: matched.matching,
    rows,
    summaries: {
      reported: reportedSummary,
      shared: sharedSummary,
    },
    modeReasons: { reported: null, shared: sharedReason },
  };
}
