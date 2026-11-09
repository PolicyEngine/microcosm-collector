// Reform-validation data layer. Where calibration_diagnostics.json answers
// "does the dataset reproduce its calibration targets", reform_validation.json
// answers a downstream question: "does the dataset reproduce the budget effects
// of policy reforms that an authority (JCT) has officially scored?".
//
// The cross-release external-comparison surface (the retired "External
// checks" tab, its committed overrides, and the backfill automation) moved to
// PolicyEngine/policyengine-scorecard (scorecard issue #15). What remains here
// is the producer/consumer contract parsing the staging view uses to render a
// candidate run's own reform_validation.json as release-gate telemetry.
//
// ---------------------------------------------------------------------------
// reform_validation.json schema (v1) — the producer/consumer contract:
//
//   {
//     "schema_version": 1,
//     "release_id": "populace-us-2024-<sha>-...",
//     "baseline_period": 2026,
//     "scoring_window": "FY2025-2034",
//     "reforms": [
//       {
//         "id": "obbba",                       // stable across releases
//         "name": "One Big Beautiful Bill Act",
//         "category": "OBBBA",                 // grouping label
//         "description": "…",                  // optional
//         "jct": {
//           "score": -3700000000000,           // budget effect, USD (− = cost)
//           "score_type": "conventional",      // or "dynamic"
//           "window": "FY2025-2034",
//           "source": "JCX-29-25",
//           "source_url": "https://www.jct.gov/…",
//           "published": "2025-05-..."         // optional ISO date
//         },
//         "microcosm": {
//           "budget_effect": -3650000000000,   // Microcosm microsim, same window
//           "window": "FY2025-2034",
//           "annual": { "2025": -1.2e11, … }   // optional per-year series
//         }
//       }
//     ]
//   }
// ---------------------------------------------------------------------------

import { asObject } from "./latest-artifact";

type JsonObject = Record<string, unknown>;

export const REFORM_VALIDATION_FILE = "reform_validation.json";

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

export interface ReformValidationRow {
  id: string;
  name: string;
  category: string | null;
  description: string | null;
  // in-sample reforms are JCT tax-expenditure *calibration targets* (the
  // dataset was tuned to them); out-of-sample reforms (OBBBA provisions) are
  // the genuine fidelity test.
  in_sample: boolean;
  period: number | null;
  // How score/estimate are denominated. Budget-effect rows are
  // "currency-USD"; baseline-level rate backtests (Census state SPM poverty
  // rates) are "percent", carrying fractions (0.134 = 13.4%) in the same
  // score fields. Missing unit (older payloads) means currency.
  unit: "currency-USD" | "percent";
  // The JCT figure we benchmark against — JCT's first full fiscal year
  // (FY2027) for provisions effective 1/1/2026, since FY2026 is a partial ramp
  // year vs microcosm's calendar-year liability. In-sample rows fall back to
  // their annual figure. Error metrics below are all relative to this.
  jct_score: number | null;
  // The FY2026 figure, kept for reference (the partial ramp year).
  jct_score_fy2026: number | null;
  jct_score_type: string | null;
  jct_window: string | null;
  // The year/window the benchmark (jct_score) actually refers to — "FY2027"
  // for OBBBA rows (the full-year default), the annual window for in-sample.
  // Differs row to row, so it's shown as its own column.
  jct_benchmark_window: string | null;
  jct_source: string | null;
  jct_source_url: string | null;
  jct_published: string | null;
  microcosm_estimate: number | null;
  microcosm_window: string | null;
  microcosm_annual: Record<string, number> | null;
  // Derived (relative to jct_score, i.e. the FY2027 benchmark where available).
  abs_error: number | null; // microcosm − jct (USD)
  relative_error: number | null; // (microcosm − jct) / |jct|
  abs_relative_error: number | null;
  within_10pct: boolean | null;
  direction: "over" | "under" | "exact" | null;
}

export interface ReformValidation {
  available: true;
  source: "huggingface_live";
  release_id: string;
  updated_at: string | null;
  schema_version: unknown;
  baseline_period: number | null;
  scoring_window: string | null;
  rows: ReformValidationRow[];
  summary: {
    n_reforms: number;
    n_scored: number; // reforms with both a JCT score and a microcosm estimate
    within_10pct: number;
    mean_abs_relative_error: number | null;
    median_abs_relative_error: number | null;
    // The out-of-sample reforms only — the genuine fidelity test.
    n_out_of_sample: number;
    n_out_of_sample_scored: number;
    out_of_sample_within_10pct: number;
    out_of_sample_mean_abs_relative_error: number | null;
  };
}

function enrichReform(raw: JsonObject): ReformValidationRow {
  const jct = asObject(raw.jct);
  const microcosm = asObject(raw.microcosm);
  const calculation = Object.keys(microcosm).length
    ? microcosm
    : asObject(raw.populace);
  const jctFy2026 = numberOrNull(jct.score);
  const jctFy2027 = numberOrNull(jct.score_fy2027);
  // Benchmark defaults to JCT's first full fiscal year (FY2027). FY2026 is a
  // partial ramp year for provisions effective 1/1/2026, so error vs FY2026
  // overstates the gap against microcosm's calendar-year liability. In-sample
  // rows have no FY2027 figure, so they fall back to their annual (FY2026) one.
  const benchmark = jctFy2027 ?? jctFy2026;
  const estimate = numberOrNull(calculation.budget_effect);
  const absError = benchmark != null && estimate != null ? estimate - benchmark : null;
  // A zero benchmark has no meaningful relative error — leave it unscored
  // rather than storing the raw dollar delta, which would otherwise be treated
  // as a fraction and blow up within_10pct and the mean/median aggregates.
  const relError =
    benchmark != null && estimate != null && benchmark !== 0
      ? (estimate - benchmark) / Math.abs(benchmark)
      : null;
  const absRel = relError == null ? null : Math.abs(relError);
  const annual = asObject(calculation.annual);
  const annualClean: Record<string, number> = {};
  for (const [k, v] of Object.entries(annual)) {
    const n = numberOrNull(v);
    if (n != null) annualClean[k] = n;
  }
  return {
    id: String(raw.id ?? raw.name ?? ""),
    name: String(raw.name ?? raw.id ?? ""),
    category: stringOrNull(raw.category),
    description: stringOrNull(raw.description),
    in_sample: raw.in_sample === true,
    period: numberOrNull(raw.period),
    unit: raw.unit === "percent" ? "percent" : "currency-USD",
    jct_score: benchmark,
    jct_score_fy2026: jctFy2026,
    jct_score_type: stringOrNull(jct.score_type),
    jct_window: stringOrNull(jct.window),
    jct_benchmark_window: jctFy2027 != null ? "FY2027" : stringOrNull(jct.window),
    jct_source: stringOrNull(jct.source),
    jct_source_url: stringOrNull(jct.source_url),
    jct_published: stringOrNull(jct.published),
    microcosm_estimate: estimate,
    microcosm_window: stringOrNull(calculation.window),
    microcosm_annual: Object.keys(annualClean).length ? annualClean : null,
    abs_error: absError,
    relative_error: relError,
    abs_relative_error: absRel,
    within_10pct: absRel == null ? null : absRel <= 0.1,
    direction:
      absError == null ? null : absError > 0 ? "over" : absError < 0 ? "under" : "exact",
  };
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function buildReformValidation(
  raw: JsonObject,
  releaseId: string,
  updatedAt: string | null = null,
): ReformValidation {
  const reforms = Array.isArray(raw.reforms) ? (raw.reforms as JsonObject[]) : [];
  const rows = reforms.map((r) => enrichReform(asObject(r)));
  const scored = rows.filter((r) => r.abs_relative_error != null);
  const absRels = scored.map((r) => r.abs_relative_error as number);
  const oos = rows.filter((r) => !r.in_sample);
  const oosScored = oos.filter((r) => r.abs_relative_error != null);
  const oosAbsRels = oosScored.map((r) => r.abs_relative_error as number);
  return {
    available: true,
    source: "huggingface_live",
    release_id: String(raw.release_id ?? releaseId),
    updated_at: updatedAt,
    schema_version: raw.schema_version ?? null,
    baseline_period: numberOrNull(raw.baseline_period),
    scoring_window: stringOrNull(raw.scoring_window),
    rows,
    summary: {
      n_reforms: rows.length,
      n_scored: scored.length,
      within_10pct: scored.filter((r) => r.within_10pct === true).length,
      mean_abs_relative_error: absRels.length
        ? absRels.reduce((s, v) => s + v, 0) / absRels.length
        : null,
      median_abs_relative_error: median(absRels),
      n_out_of_sample: oos.length,
      n_out_of_sample_scored: oosScored.length,
      out_of_sample_within_10pct: oosScored.filter((r) => r.within_10pct === true).length,
      out_of_sample_mean_abs_relative_error: oosAbsRels.length
        ? oosAbsRels.reduce((s, v) => s + v, 0) / oosAbsRels.length
        : null,
    },
  };
}
