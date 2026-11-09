import { createHash } from "node:crypto";

import {
  HISTORICAL_ATTRIBUTION_SUPPORT_BY_RELEASE,
  type AttributionVerificationTolerance,
  type HistoricalAttributionRecipe,
  type HistoricalAttributionSupport,
} from "./target-loss-attribution-manifest";

type JsonObject = Record<string, unknown>;
export type TargetLossAttributionStatus =
  | "reported"
  | "exact_reconstructed"
  | "derived"
  | "unavailable";

export interface TargetLossDiagnosticWarning {
  code: string;
  severity: string | null;
  message: string;
}

export interface FinalTargetLossAttributionTarget {
  index: number;
  name: string;
  target_loss_weight: number;
  target_loss_weight_share: number;
  target_loss_scale: number;
  final_capped_scaled_error: number;
  final_loss_contribution: number;
}

export interface TargetLossAttributionVerification {
  valid: boolean;
  difference: number | null;
  tolerance: AttributionVerificationTolerance;
}

export interface FinalTargetLossAttribution {
  status: TargetLossAttributionStatus;
  aggregate: number | null;
  historical_final_loss: number | null;
  cap: number | null;
  basis_identifier: string | null;
  basis_hash: string | null;
  verification: TargetLossAttributionVerification | null;
  producer_warnings: TargetLossDiagnosticWarning[];
  reason: string | null;
  targets: FinalTargetLossAttributionTarget[];
}

export interface NormalizeTargetLossAttributionInput {
  diagnostics: JsonObject;
  rows: JsonObject[];
  releaseId: string;
  buildManifest: JsonObject;
  releaseFamily: "national" | "local_area";
  diagnosticsSha256?: string | null;
}

export interface NormalizedTargetLossAttributionResult {
  attribution: FinalTargetLossAttribution;
  provenance: CalibrationProvenance;
  rows: JsonObject[];
}

export interface CalibrationProvenance {
  mode: "direct" | "inherited";
  dataset_release_id: string;
  calibration_source_id: string | null;
  diagnostics_sha256: string | null;
  target_surface_sha256: string | null;
  validation: {
    status: "not_applicable" | "verified" | "failed";
    reason: string | null;
  };
}

export interface HistoricalAttributionEvidence {
  releaseId: string;
  buildId: string | null;
  buildSha: string | null;
  producerCommit: string | null;
  releaseFamily: "national" | "local_area";
  diagnosticsSchema: number | null;
  targetCount: number;
  orderedTargetNamesSha256: string;
  producerTargetSurfaceSha256: string | null;
  weightingIdentifier: string | null;
  lossCap: number | null;
}

export interface InheritedAttributionEvidence {
  parentBuildId: string | null;
  declaredDiagnosticsSchema: number | null;
  declaredDiagnosticsSha256: string | null;
  observedDiagnosticsSha256: string | null;
  releaseFamily: "national" | "local_area";
  diagnosticsSchema: number | null;
  targetCount: number;
  orderedTargetNamesSha256: string;
  producerTargetSurfaceSha256: string | null;
  weightingIdentifier: string | null;
  lossCap: number | null;
}

export interface HistoricalAttributionClassification {
  status: "exact_reconstructed" | "derived" | "unavailable";
  recipe: HistoricalAttributionRecipe | null;
  reason: string | null;
}

export const TARGET_LOSS_ATTRIBUTION_ABS_TOLERANCE = 1e-12;
export const TARGET_LOSS_ATTRIBUTION_REL_TOLERANCE = 1e-12;
export const TARGET_LOSS_BASIS_HASH_ALGORITHM = "sha256_utf8len32_f64be_v1";
export const TARGET_LOSS_FORMULA =
  "weighted_mean(min(abs((estimate - target) / scale), cap))";

const TARGET_LOSS_WARNING_PREFIX = "target_loss_attribution_";
const SHA256_RE = /^[0-9a-f]{64}$/i;
const ATTRIBUTION_FIELDS = [
  "target_loss_weight",
  "target_loss_weight_share",
  "target_loss_scale",
  "final_capped_scaled_error",
  "final_loss_contribution",
] as const;
const NOT_APPLICABLE_TOLERANCE: AttributionVerificationTolerance = {
  kind: "not_applicable",
  absolute: 0,
  relative: 0,
};

const CONCEPT_METADATA_EXCLUSIONS = new Set([
  "congressional_district_geoid",
  "geography_scope",
  "hierarchy_child_ids",
  "hierarchy_child_sum_raw",
  "hierarchy_coverage_ratio",
  "hierarchy_expected_child_count",
  "hierarchy_observed_child_count",
  "hierarchy_parent_geography_id",
  "hierarchy_parent_geography_level",
  "hierarchy_parent_key",
  "hierarchy_parent_target_name",
  "hierarchy_parent_target_period",
  "hierarchy_parent_value",
  "hierarchy_raw_value",
  "hierarchy_reconciliation_factor",
  "hierarchy_reconciliation_method",
  "hierarchy_reconciliation_rule",
  "ledger_aggregate_fact_key",
  "ledger_dimension_set_key",
  "ledger_fact_key",
  "ledger_geography_id",
  "ledger_geography_level",
  "ledger_geography_name",
  "ledger_geography_vintage",
  "ledger_legacy_fact_key",
  "ledger_layout_groupby_dimension",
  "ledger_layout_groupby_value_id",
  "ledger_layout_record_set_id",
  "ledger_observed_measure_key",
  "ledger_semantic_fact_key",
  "ledger_source_record_id",
  "state_fips",
]);

function asObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.length ? value : null;
}

function normalizedSha256(value: unknown): string | null {
  const candidate = optionalString(value);
  return candidate && SHA256_RE.test(candidate) ? candidate.toLowerCase() : null;
}

function targetName(row: JsonObject): string {
  return String(row.name ?? row.target_name ?? "");
}

function targetValue(row: JsonObject): number | null {
  return finiteNumber(row.target) ?? finiteNumber(row.value);
}

function finalEstimate(row: JsonObject): number | null {
  return finiteNumber(row.final_estimate) ?? finiteNumber(row.estimate);
}

function targetMetadata(row: JsonObject): JsonObject {
  return asObject(row.metadata);
}

function targetFamily(row: JsonObject): string {
  const registryFamily = optionalString(asObject(row.registry).family);
  return registryFamily ?? optionalString(targetMetadata(row).target_role) ?? "unknown";
}

function isClose(
  left: number,
  right: number,
  tolerance: Pick<AttributionVerificationTolerance, "absolute" | "relative">,
): boolean {
  return Math.abs(left - right) <= Math.max(
    tolerance.absolute,
    tolerance.relative * Math.max(Math.abs(left), Math.abs(right)),
  );
}

function unavailable(
  finalLoss: number | null,
  reason: string,
  warnings: TargetLossDiagnosticWarning[],
): FinalTargetLossAttribution {
  return {
    status: "unavailable",
    aggregate: null,
    historical_final_loss: finalLoss,
    cap: null,
    basis_identifier: null,
    basis_hash: null,
    verification: null,
    producer_warnings: warnings,
    reason,
    targets: [],
  };
}

function stripAttributionFields(row: JsonObject): JsonObject {
  const clean = { ...row };
  for (const field of ATTRIBUTION_FIELDS) delete clean[field];
  return clean;
}

function rowsWithAttribution(
  rows: JsonObject[],
  targets: FinalTargetLossAttributionTarget[],
): JsonObject[] {
  return rows.map((row, index) => {
    const clean = stripAttributionFields(row);
    const target = targets[index];
    if (!target) return clean;
    return {
      ...clean,
      target_loss_weight: target.target_loss_weight,
      target_loss_weight_share: target.target_loss_weight_share,
      target_loss_scale: target.target_loss_scale,
      final_capped_scaled_error: target.final_capped_scaled_error,
      final_loss_contribution: target.final_loss_contribution,
    };
  });
}

export function targetLossBasisHash(
  rows: Array<Pick<FinalTargetLossAttributionTarget, "name" | "target_loss_weight" | "target_loss_scale">>,
): string {
  const hash = createHash("sha256");
  hash.update(Buffer.from("microcosm-target-loss-basis-v1\0", "utf8"));
  for (const row of rows) {
    const name = Buffer.from(row.name, "utf8");
    const nameLength = Buffer.allocUnsafe(4);
    nameLength.writeUInt32BE(name.length);
    const weight = Buffer.allocUnsafe(8);
    weight.writeDoubleBE(row.target_loss_weight);
    const scale = Buffer.allocUnsafe(8);
    scale.writeDoubleBE(row.target_loss_scale);
    hash.update(nameLength);
    hash.update(name);
    hash.update(weight);
    hash.update(scale);
  }
  return hash.digest("hex");
}

export function orderedTargetNamesHash(rows: JsonObject[]): string {
  const hash = createHash("sha256");
  hash.update(Buffer.from("microcosm-target-name-surface-v1\0", "utf8"));
  for (const row of rows) {
    const name = Buffer.from(targetName(row), "utf8");
    const nameLength = Buffer.allocUnsafe(4);
    nameLength.writeUInt32BE(name.length);
    hash.update(nameLength);
    hash.update(name);
  }
  return hash.digest("hex");
}

function diagnosticWarnings(diagnostics: JsonObject): TargetLossDiagnosticWarning[] {
  if (!Array.isArray(diagnostics.diagnostic_warnings)) return [];
  return diagnostics.diagnostic_warnings.flatMap((value) => {
    const warning = asObject(value);
    const code = optionalString(warning.code);
    const message = optionalString(warning.message);
    if (!code || !message) return [];
    return [{ code, severity: optionalString(warning.severity), message }];
  });
}

function reportedAttribution(
  diagnostics: JsonObject,
  rows: JsonObject[],
  finalLoss: number | null,
  warnings: TargetLossDiagnosticWarning[],
): FinalTargetLossAttribution {
  const producerWarning = warnings.find((warning) =>
    warning.code.startsWith(TARGET_LOSS_WARNING_PREFIX),
  );
  if (producerWarning) {
    return unavailable(
      finalLoss,
      `Microcosm withheld target-loss attribution: ${producerWarning.message}`,
      warnings,
    );
  }
  if (finalLoss == null) {
    return unavailable(finalLoss, "Schema-version-6 diagnostics have no finite final_loss.", warnings);
  }
  const basis = asObject(diagnostics.target_loss_basis);
  const cap = finiteNumber(basis.cap);
  const targetCount = finiteNumber(basis.target_count);
  const totalWeight = finiteNumber(basis.total_target_weight);
  const formula = optionalString(basis.formula);
  const weightKind = optionalString(basis.weight_kind);
  const scaleKind = optionalString(basis.scale_kind);
  const hashAlgorithm = optionalString(basis.hash_algorithm);
  const expectedHash = optionalString(basis.sha256);
  if (
    cap == null || cap <= 0 || targetCount !== rows.length || totalWeight == null ||
    totalWeight <= 0 || formula !== TARGET_LOSS_FORMULA || !weightKind || !scaleKind ||
    hashAlgorithm !== TARGET_LOSS_BASIS_HASH_ALGORITHM ||
    !expectedHash || !/^[0-9a-f]{64}$/.test(expectedHash)
  ) {
    return unavailable(
      finalLoss,
      "Schema-version-6 target_loss_basis is missing or inconsistent.",
      warnings,
    );
  }

  const targets: FinalTargetLossAttributionTarget[] = [];
  for (const [index, row] of rows.entries()) {
    const name = targetName(row);
    const target = targetValue(row);
    const estimate = finalEstimate(row);
    const weight = finiteNumber(row.target_loss_weight);
    const share = finiteNumber(row.target_loss_weight_share);
    const scale = finiteNumber(row.target_loss_scale);
    const cappedError = finiteNumber(row.final_capped_scaled_error);
    const contribution = finiteNumber(row.final_loss_contribution);
    if (
      !name || target == null || estimate == null || weight == null || weight < 0 ||
      share == null || share < 0 || scale == null || scale <= 0 ||
      cappedError == null || cappedError < 0 || cappedError > cap ||
      contribution == null || contribution < 0
    ) {
      return unavailable(
        finalLoss,
        `Schema-version-6 target attribution is partial or invalid at row ${index}.`,
        warnings,
      );
    }
    const recomputedError = Math.min(Math.abs(estimate - target) / scale, cap);
    if (
      !isClose(cappedError, recomputedError, {
        absolute: TARGET_LOSS_ATTRIBUTION_ABS_TOLERANCE,
        relative: TARGET_LOSS_ATTRIBUTION_REL_TOLERANCE,
      }) ||
      !isClose(contribution, share * cappedError, {
        absolute: TARGET_LOSS_ATTRIBUTION_ABS_TOLERANCE,
        relative: TARGET_LOSS_ATTRIBUTION_REL_TOLERANCE,
      })
    ) {
      return unavailable(
        finalLoss,
        `Schema-version-6 target attribution values disagree at row ${index}.`,
        warnings,
      );
    }
    targets.push({
      index,
      name,
      target_loss_weight: weight,
      target_loss_weight_share: share,
      target_loss_scale: scale,
      final_capped_scaled_error: cappedError,
      final_loss_contribution: contribution,
    });
  }

  const summedWeight = targets.reduce((sum, target) => sum + target.target_loss_weight, 0);
  const summedShares = targets.reduce((sum, target) => sum + target.target_loss_weight_share, 0);
  const aggregate = targets.reduce((sum, target) => sum + target.final_loss_contribution, 0);
  const tolerance = {
    absolute: TARGET_LOSS_ATTRIBUTION_ABS_TOLERANCE,
    relative: TARGET_LOSS_ATTRIBUTION_REL_TOLERANCE,
  };
  const rowsConsistent = targets.every((target) =>
    isClose(target.target_loss_weight_share, target.target_loss_weight / summedWeight, tolerance),
  );
  const actualHash = targetLossBasisHash(targets);
  if (
    !isClose(summedWeight, totalWeight, tolerance) ||
    !isClose(summedShares, 1, tolerance) ||
    !rowsConsistent ||
    !isClose(aggregate, finalLoss, tolerance) ||
    actualHash !== expectedHash
  ) {
    return unavailable(
      finalLoss,
      "Schema-version-6 target attribution fails its aggregate or basis-hash invariant.",
      warnings,
    );
  }

  return {
    status: "reported",
    aggregate,
    historical_final_loss: finalLoss,
    cap,
    basis_identifier: `${weightKind}:${scaleKind}`,
    basis_hash: actualHash,
    verification: {
      valid: true,
      difference: aggregate - finalLoss,
      tolerance: {
        kind: "floating_point",
        absolute: TARGET_LOSS_ATTRIBUTION_ABS_TOLERANCE,
        relative: TARGET_LOSS_ATTRIBUTION_REL_TOLERANCE,
      },
    },
    producer_warnings: warnings,
    reason: null,
    targets,
  };
}

type BasisClassifier = (row: JsonObject) => "amount" | "count";

function historicalBasis(row: JsonObject): "amount" | "count" {
  const metadata = targetMetadata(row);
  const mode = String(metadata.measure_mode ?? "");
  const sourceMeasure = String(metadata.source_measure_id ?? "");
  if (metadata.count === "true") return "count";
  if (["count", "positive_count", "less_than_count"].includes(mode)) return "count";
  if (sourceMeasure.includes("enrollment") || sourceMeasure.includes("recipients")) {
    return "count";
  }
  if (sourceMeasure.includes("return") && sourceMeasure.includes("count")) return "count";
  return "amount";
}

function newerBasis(row: JsonObject): "amount" | "count" {
  const metadata = targetMetadata(row);
  const mode = String(metadata.measure_mode ?? "");
  const sourceMeasure = String(metadata.source_measure_id ?? "");
  if (["indicator_sum", "less_than_indicator_sum"].includes(mode)) return "count";
  if (sourceMeasure.includes("enrollment") || sourceMeasure.includes("recipients")) {
    return "count";
  }
  if (sourceMeasure.includes("return") && sourceMeasure.includes("count")) return "count";
  return "amount";
}

// Versioned fallback for schema-v1/v2 slash-delimited rows after the artifact
// adapter has parsed their measure. It is deliberately confined to the six
// manifest-pinned legacy releases.
function legacyCurrentDoctrineBasis(row: JsonObject): "amount" | "count" {
  const modern = newerBasis(row);
  if (modern === "count") return modern;
  const measure = String(row.measure ?? "").toLowerCase();
  if (["count", "filers", "nonfilers"].includes(measure)) return "count";
  const nameTokens = targetName(row).toLowerCase().split(/[./]/);
  if (
    nameTokens.some((token) => ["count", "filers", "nonfilers"].includes(token)) ||
    nameTokens.some((token) => /_(returns|claims|count)$/.test(token))
  ) {
    return "count";
  }
  return "amount";
}

function squareRootWeights(rows: JsonObject[], basisOf: BasisClassifier): number[] | null {
  const weights = rows.map(() => 1);
  const bases = [...new Set(rows.map(basisOf))].sort();
  if (!bases.length) return null;
  for (const basis of bases) {
    const indices = rows.flatMap((row, index) => basisOf(row) === basis ? [index] : []);
    const raw = indices.map((index) => {
      const target = targetValue(rows[index]);
      return target == null ? Number.NaN : Math.sqrt(Math.max(Math.abs(target), 1));
    });
    if (!raw.every(Number.isFinite)) return null;
    const mean = raw.reduce((sum, value) => sum + value, 0) / raw.length;
    indices.forEach((index, offset) => { weights[index] = raw[offset] / mean; });
  }
  return weights;
}

function conceptKey(row: JsonObject, basisOf: BasisClassifier): string {
  const metadata = targetMetadata(row);
  if (metadata.ledger_geography_level !== "congressional_district") {
    return JSON.stringify([
      basisOf(row),
      row.entity,
      row.period,
      targetFamily(row),
      targetName(row),
    ]);
  }
  const semanticMetadata = Object.entries(metadata)
    .filter(([key]) => !CONCEPT_METADATA_EXCLUSIONS.has(key))
    .sort(([left], [right]) => left.localeCompare(right));
  return JSON.stringify([
    basisOf(row),
    row.entity,
    row.period,
    targetFamily(row),
    row.filter ?? "",
    metadata.state_fips ?? "",
    semanticMetadata,
  ]);
}

function reconstructedWeights(
  rows: JsonObject[],
  basisOf: BasisClassifier,
  conceptBudget: boolean,
  familyMultipliers: JsonObject | null,
): number[] | null {
  const weights = squareRootWeights(rows, basisOf);
  if (!weights) return null;
  if (conceptBudget) {
    const groups = new Map<string, number[]>();
    rows.forEach((row, index) => {
      const key = conceptKey(row, basisOf);
      const indices = groups.get(key) ?? [];
      indices.push(index);
      groups.set(key, indices);
    });
    for (const indices of groups.values()) {
      const total = indices.reduce((sum, index) => sum + weights[index], 0);
      const budget = Math.max(...indices.map((index) => weights[index]), 0);
      if (total > 0 && budget > 0) {
        for (const index of indices) weights[index] *= budget / total;
      }
    }
  }

  const bases = [...new Set(rows.map(basisOf))].sort();
  const basisTotal = rows.length / bases.length;
  for (const basis of bases) {
    const indices = rows.flatMap((row, index) => basisOf(row) === basis ? [index] : []);
    const total = indices.reduce((sum, index) => sum + weights[index], 0);
    if (total <= 0) return null;
    for (const index of indices) weights[index] *= basisTotal / total;
  }
  let mean = weights.reduce((sum, weight) => sum + weight, 0) / weights.length;
  if (!Number.isFinite(mean) || mean <= 0) return null;
  for (let index = 0; index < weights.length; index += 1) weights[index] /= mean;

  if (familyMultipliers) {
    const matchedFamilies = new Set<string>();
    for (let index = 0; index < rows.length; index += 1) {
      const family = targetFamily(rows[index]);
      const multiplier = finiteNumber(familyMultipliers[family]);
      if (multiplier != null) {
        if (multiplier <= 0) return null;
        weights[index] *= multiplier;
        matchedFamilies.add(family);
      }
    }
    if (Object.keys(familyMultipliers).some((family) => !matchedFamilies.has(family))) {
      return null;
    }
    mean = weights.reduce((sum, weight) => sum + weight, 0) / weights.length;
    if (!Number.isFinite(mean) || mean <= 0) return null;
    for (let index = 0; index < weights.length; index += 1) weights[index] /= mean;
  }
  return weights;
}

function lossCap(diagnostics: JsonObject): number | null {
  const options = asObject(diagnostics.options);
  const producer = asObject(diagnostics.build);
  return finiteNumber(asObject(options.target_loss_scales).cap)
    ?? finiteNumber(options.target_loss_cap)
    ?? finiteNumber(producer.target_loss_cap);
}

function supportContentFailure(
  support: HistoricalAttributionSupport,
  evidence: Pick<
    HistoricalAttributionEvidence,
    | "releaseFamily"
    | "diagnosticsSchema"
    | "targetCount"
    | "orderedTargetNamesSha256"
    | "producerTargetSurfaceSha256"
    | "weightingIdentifier"
    | "lossCap"
  >,
): string | null {
  if (support.diagnosticsSchema !== evidence.diagnosticsSchema) return "diagnostics schema mismatch";
  if (support.releaseFamily !== evidence.releaseFamily) return "release family mismatch";
  if (support.targetCount !== evidence.targetCount) return "target count mismatch";
  if (support.orderedTargetNamesSha256 !== evidence.orderedTargetNamesSha256) {
    return "ordered target surface mismatch";
  }
  if (
    support.producerTargetSurfaceSha256 &&
    evidence.producerTargetSurfaceSha256 !== support.producerTargetSurfaceSha256
  ) {
    return "diagnostics target-surface digest does not match the pinned parent digest";
  }
  if (support.weightingIdentifier !== evidence.weightingIdentifier) {
    return "weighting identifier mismatch";
  }
  if (support.lossCap != null && support.lossCap !== evidence.lossCap) {
    return "target-loss cap mismatch";
  }
  return null;
}

function supportEvidenceFailure(
  support: HistoricalAttributionSupport,
  evidence: HistoricalAttributionEvidence,
): string | null {
  if (support.buildId !== evidence.buildId) return "build identifier mismatch";
  if (support.buildSha && support.buildSha !== evidence.buildSha) {
    return "build SHA mismatch";
  }
  if (support.producerCommit && support.producerCommit !== evidence.producerCommit) {
    return "producer commit mismatch";
  }
  return supportContentFailure(support, evidence);
}

export function classifyHistoricalAttributionEvidence(
  evidence: HistoricalAttributionEvidence,
): HistoricalAttributionClassification {
  const support = HISTORICAL_ATTRIBUTION_SUPPORT_BY_RELEASE.get(evidence.releaseId);
  if (!support) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "No pinned historical attribution recipe matches this release.",
    };
  }
  const failure = supportEvidenceFailure(support, evidence);
  return failure
    ? { status: "unavailable", recipe: null, reason: failure }
    : { status: support.expectedStatus, recipe: support.recipe, reason: null };
}

export function classifyInheritedAttributionEvidence(
  evidence: InheritedAttributionEvidence,
): HistoricalAttributionClassification {
  if (!evidence.parentBuildId) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "inherited calibration does not declare a parent build identifier",
    };
  }
  const support = HISTORICAL_ATTRIBUTION_SUPPORT_BY_RELEASE.get(evidence.parentBuildId);
  if (!support || support.buildId !== evidence.parentBuildId) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "no pinned historical attribution recipe matches the declared parent build",
    };
  }
  if (
    evidence.declaredDiagnosticsSchema == null ||
    !Number.isInteger(evidence.declaredDiagnosticsSchema)
  ) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "inherited calibration does not declare an integer diagnostics schema version",
    };
  }
  if (evidence.declaredDiagnosticsSchema !== evidence.diagnosticsSchema) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "declared diagnostics schema does not match the downloaded diagnostics",
    };
  }
  const declaredDigest = normalizedSha256(evidence.declaredDiagnosticsSha256);
  if (!declaredDigest) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "inherited calibration does not declare a valid diagnostics SHA-256 digest",
    };
  }
  const observedDigest = normalizedSha256(evidence.observedDiagnosticsSha256);
  if (!observedDigest) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "the downloaded diagnostics SHA-256 digest is unavailable",
    };
  }
  if (observedDigest !== declaredDigest) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "downloaded diagnostics do not match the digest declared by the release",
    };
  }
  const parentDigest = normalizedSha256(support.diagnosticsSha256);
  if (!parentDigest) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "the declared parent has no pinned diagnostics digest",
    };
  }
  if (observedDigest !== parentDigest) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "downloaded diagnostics do not match the pinned parent artifact",
    };
  }
  if (!support.producerTargetSurfaceSha256) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "the declared parent has no pinned target-surface digest",
    };
  }
  if (!normalizedSha256(evidence.producerTargetSurfaceSha256)) {
    return {
      status: "unavailable",
      recipe: null,
      reason: "downloaded diagnostics do not declare a valid target-surface digest",
    };
  }
  const failure = supportContentFailure(support, evidence);
  return failure
    ? { status: "unavailable", recipe: null, reason: failure }
    : { status: support.expectedStatus, recipe: support.recipe, reason: null };
}

function targetSurfaceSha256(
  diagnostics: JsonObject,
  buildManifest: JsonObject,
): string | null {
  return optionalString(asObject(diagnostics.target_surface).sha256)
    ?? optionalString(asObject(asObject(buildManifest.calibration).target_surface).sha256);
}

function historicalEvidence(
  releaseId: string,
  diagnostics: JsonObject,
  rows: JsonObject[],
  buildManifest: JsonObject,
  releaseFamily: "national" | "local_area",
): HistoricalAttributionEvidence {
  return {
    releaseId,
    buildId: optionalString(buildManifest.build_id),
    buildSha: optionalString(buildManifest.build_sha),
    producerCommit: optionalString(asObject(buildManifest.code).git_commit),
    releaseFamily,
    diagnosticsSchema: finiteNumber(diagnostics.schema_version),
    targetCount: rows.length,
    orderedTargetNamesSha256: orderedTargetNamesHash(rows),
    producerTargetSurfaceSha256: targetSurfaceSha256(diagnostics, buildManifest),
    weightingIdentifier: optionalString(asObject(diagnostics.build).target_loss_weighting),
    lossCap: lossCap(diagnostics),
  };
}

export function reconstructTargetLossAttribution(
  support: HistoricalAttributionSupport,
  diagnostics: JsonObject,
  rows: JsonObject[],
  finalLoss: number | null,
  warnings: TargetLossDiagnosticWarning[],
): FinalTargetLossAttribution {
  if (finalLoss == null) return unavailable(finalLoss, "Historical diagnostics have no finite final_loss.", warnings);

  const recipe: HistoricalAttributionRecipe = support.recipe;
  const isDerived = recipe === "legacy_current_doctrine_v1";
  const cap = isDerived || recipe === "uniform_target_scale_cap_100pct_v1"
    ? 1
    : lossCap(diagnostics);
  if (cap == null || !Number.isFinite(cap) || cap <= 0) {
    return unavailable(finalLoss, "The supported recipe has no valid loss cap.", warnings);
  }
  const scaleSummary = asObject(asObject(diagnostics.options).target_loss_scales);
  const scaleKind = optionalString(scaleSummary.kind);
  if (!isDerived && recipe !== "uniform_target_scale_cap_100pct_v1" && scaleKind !== "default_target") {
    return unavailable(finalLoss, "The historical recipe requires default target-derived scales.", warnings);
  }
  const producer = asObject(diagnostics.build);
  const rawMultipliers = producer.target_loss_family_multipliers;
  const familyMultipliers = rawMultipliers == null ? null : asObject(rawMultipliers);
  if (rawMultipliers != null && familyMultipliers && !Object.keys(familyMultipliers).length) {
    return unavailable(finalLoss, "Declared target-family multipliers are incomplete.", warnings);
  }

  let weights: number[] | null;
  if (recipe === "uniform_target_scale_cap_100pct_v1") {
    weights = rows.map(() => 1);
  } else if (recipe === "historical_sqrt_value_50_50_v1") {
    weights = reconstructedWeights(rows, historicalBasis, false, familyMultipliers);
  } else if (recipe === "newer_sqrt_value_50_50_v2") {
    weights = reconstructedWeights(rows, newerBasis, false, familyMultipliers);
  } else if (recipe === "concept_budget_sqrt_value_50_50_v3") {
    weights = reconstructedWeights(rows, newerBasis, true, familyMultipliers);
  } else {
    weights = reconstructedWeights(rows, legacyCurrentDoctrineBasis, true, null);
  }
  if (!weights || weights.length !== rows.length) {
    return unavailable(finalLoss, "The supported weighting recipe could not reconstruct every target.", warnings);
  }
  const totalWeight = weights.reduce((sum, weight) => sum + weight, 0);
  if (!Number.isFinite(totalWeight) || totalWeight <= 0) {
    return unavailable(finalLoss, "The reconstructed target weights have no positive total.", warnings);
  }

  const targets: FinalTargetLossAttributionTarget[] = [];
  for (const [index, row] of rows.entries()) {
    const name = targetName(row);
    const target = targetValue(row);
    const estimate = finalEstimate(row);
    if (!name || target == null || estimate == null) {
      return unavailable(finalLoss, `Target row ${index} lacks a finite target or final estimate.`, warnings);
    }
    const scale = Math.max(Math.abs(target), 1);
    const weight = weights[index];
    const share = weight / totalWeight;
    const cappedError = Math.min(Math.abs(estimate - target) / scale, cap);
    targets.push({
      index,
      name,
      target_loss_weight: weight,
      target_loss_weight_share: share,
      target_loss_scale: scale,
      final_capped_scaled_error: cappedError,
      final_loss_contribution: share * cappedError,
    });
  }
  const aggregate = targets.reduce((sum, target) => sum + target.final_loss_contribution, 0);
  const difference = aggregate - finalLoss;
  if (!isDerived && !isClose(aggregate, finalLoss, support.tolerance)) {
    return unavailable(
      finalLoss,
      "The supported historical recipe does not reproduce final_loss within its pinned tolerance.",
      warnings,
    );
  }
  return {
    status: isDerived ? "derived" : "exact_reconstructed",
    aggregate,
    historical_final_loss: finalLoss,
    cap,
    basis_identifier: recipe,
    basis_hash: targetLossBasisHash(targets),
    verification: {
      valid: isDerived ? true : isClose(aggregate, finalLoss, support.tolerance),
      difference: isDerived ? null : difference,
      tolerance: isDerived ? NOT_APPLICABLE_TOLERANCE : support.tolerance,
    },
    producer_warnings: warnings,
    reason: isDerived
      ? "Derived under the pinned current-doctrine recipe; historical final_loss remains separate."
      : null,
    targets,
  };
}

export function normalizeTargetLossAttribution(
  input: NormalizeTargetLossAttributionInput,
): NormalizedTargetLossAttributionResult {
  const {
    diagnostics,
    releaseId,
    buildManifest,
    releaseFamily,
    diagnosticsSha256 = null,
  } = input;
  const rows = input.rows.map(stripAttributionFields);
  const finalLoss = finiteNumber(diagnostics.final_loss);
  const warnings = diagnosticWarnings(diagnostics);
  const schemaVersion = finiteNumber(diagnostics.schema_version);
  const calibration = asObject(buildManifest.calibration);
  const inherited = calibration.mode === "inherited";
  const parentBuildId = inherited ? optionalString(calibration.parent_build_id) : null;
  const inheritedClassification = inherited
    ? classifyInheritedAttributionEvidence({
        parentBuildId,
        declaredDiagnosticsSchema: finiteNumber(calibration.diagnostics_schema_version),
        declaredDiagnosticsSha256: optionalString(calibration.diagnostics_sha256),
        observedDiagnosticsSha256: diagnosticsSha256,
        releaseFamily,
        diagnosticsSchema: schemaVersion,
        targetCount: input.rows.length,
        orderedTargetNamesSha256: orderedTargetNamesHash(input.rows),
        producerTargetSurfaceSha256: targetSurfaceSha256(diagnostics, buildManifest),
        weightingIdentifier: optionalString(asObject(diagnostics.build).target_loss_weighting),
        lossCap: lossCap(diagnostics),
      })
    : null;

  let attribution: FinalTargetLossAttribution;
  if (inheritedClassification?.status === "unavailable") {
    attribution = unavailable(
      finalLoss,
      `Inherited calibration diagnostics rejected: ${inheritedClassification.reason}.`,
      warnings,
    );
  } else if (schemaVersion != null && schemaVersion >= 6) {
    attribution = reportedAttribution(diagnostics, input.rows, finalLoss, warnings);
  } else {
    const supportReleaseId = inherited ? parentBuildId : releaseId;
    const support = supportReleaseId
      ? HISTORICAL_ATTRIBUTION_SUPPORT_BY_RELEASE.get(supportReleaseId)
      : undefined;
    if (!support) {
      attribution = unavailable(finalLoss, "No pinned historical attribution recipe matches this release.", warnings);
    } else if (inherited) {
      attribution = reconstructTargetLossAttribution(
        support,
        diagnostics,
        input.rows,
        finalLoss,
        warnings,
      );
    } else {
      const classification = classifyHistoricalAttributionEvidence(
        historicalEvidence(releaseId, diagnostics, input.rows, buildManifest, releaseFamily),
      );
      attribution = classification.status === "unavailable"
        ? unavailable(
            finalLoss,
            `Historical attribution support rejected: ${classification.reason}.`,
            warnings,
          )
        : reconstructTargetLossAttribution(
            support,
            diagnostics,
            input.rows,
            finalLoss,
            warnings,
          );
    }
  }
  const inheritedValidationReason = inheritedClassification?.reason
    ?? (inherited && attribution.status === "unavailable" ? attribution.reason : null);
  const provenance: CalibrationProvenance = inherited
    ? {
        mode: "inherited",
        dataset_release_id: releaseId,
        calibration_source_id: parentBuildId,
        diagnostics_sha256: normalizedSha256(diagnosticsSha256),
        target_surface_sha256: normalizedSha256(
          targetSurfaceSha256(diagnostics, buildManifest),
        ),
        validation: {
          status: inheritedValidationReason ? "failed" : "verified",
          reason: inheritedValidationReason,
        },
      }
    : {
        mode: "direct",
        dataset_release_id: releaseId,
        calibration_source_id: releaseId,
        diagnostics_sha256: normalizedSha256(diagnosticsSha256),
        target_surface_sha256: normalizedSha256(
          targetSurfaceSha256(diagnostics, buildManifest),
        ),
        validation: { status: "not_applicable", reason: null },
      };
  return {
    attribution,
    provenance,
    rows: attribution.status === "unavailable"
      ? rows
      : rowsWithAttribution(rows, attribution.targets),
  };
}

export function targetLossAttributionSummary(attribution: FinalTargetLossAttribution) {
  return {
    status: attribution.status,
    aggregate: attribution.aggregate,
    historical_final_loss: attribution.historical_final_loss,
    cap: attribution.cap,
    basis_identifier: attribution.basis_identifier,
    basis_hash: attribution.basis_hash,
    verification: attribution.verification,
    producer_warnings: attribution.producer_warnings,
    reason: attribution.reason,
    target_count: attribution.targets.length,
  };
}
