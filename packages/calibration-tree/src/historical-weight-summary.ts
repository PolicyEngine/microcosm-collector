export interface CalibrationWeightSummary {
  weight_entity: string;
  n_nonzero: number;
  n_records: number;
  effective_sample_size: number;
  top_1pct_weight_share: number;
}

interface HistoricalWeightSummaryRecord {
  releaseId: string;
  diagnosticsSha256: string;
  sourceH5Sha256: string;
  summary: CalibrationWeightSummary;
}

// Historical releases cannot be changed in place. This registry supplies only
// weight summaries reconstructed from an immutable published H5 artifact, and
// only when the release id and diagnostics digest both match. The September
// local-area values were calculated from `household_weight` in the H5 whose
// SHA-256 is pinned below. Its calibration conserved total mass, so the
// canonical survivor threshold is 1e-6 times the final mean weight as well as
// 1e-6 times the original mean weight used by the historical solver.
const HISTORICAL_WEIGHT_SUMMARIES: readonly HistoricalWeightSummaryRecord[] = [
  {
    releaseId: "populace-us-2024-buildo-acs-local-767312d60-20260923T074941Z",
    diagnosticsSha256:
      "f39d10a72415eb85ac0faa231f7970bffcc408bd53ef0f05985ea8dd0ac94903",
    sourceH5Sha256:
      "769756c31f3ca646d12c272511744dec04c0e68870c6946dd945fbba65b6a7ec",
    summary: {
      weight_entity: "household",
      n_nonzero: 1_585_847,
      n_records: 1_588_854,
      effective_sample_size: 13_631.348394968125,
      top_1pct_weight_share: 0.6930830701181169,
    },
  },
] as const;

export function historicalWeightSummaryForDiagnostics(
  releaseId: string,
  diagnosticsSha256: string | null,
): CalibrationWeightSummary | null {
  if (diagnosticsSha256 == null) return null;
  return (
    HISTORICAL_WEIGHT_SUMMARIES.find(
      (record) =>
        record.releaseId === releaseId &&
        record.diagnosticsSha256 === diagnosticsSha256,
    )?.summary ?? null
  );
}

