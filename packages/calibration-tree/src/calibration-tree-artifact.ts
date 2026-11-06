import {
  buildCalibrationTree,
  calibrationTreeMetricsFromInputs,
  fitBandForTarget,
  geographyLabeller,
  normalizeCalibrationTreeTargets,
  type CalibrationTreeGroup,
  type CalibrationTreeMetricInput,
  type CalibrationTreeMetrics,
  type CalibrationTreeNode,
  type CalibrationTreeResponse,
  type CalibrationTreeTarget,
  type GeographyLabeller,
} from "./calibration-tree";
import {
  createExplorerState,
  selectExplorerNode,
  type CalibrationStatus,
  type ComparisonFit,
  type ExplorerNodeSelection,
  type ExplorerState,
  type FitBand,
} from "./calibration-explorer";
import { isCountry, type MicrocosmCountry } from "./countries";
import {
  chronicleFactKey,
  normalizedTargetName,
  structuredIdentity,
  targetRowRepresentation,
} from "./target-surface-matcher";
import type {
  CalibrationProvenance,
  TargetLossAttributionStatus,
} from "./target-loss-attribution";
import type {
  TargetRepresentation,
  TargetRowRepresentation,
} from "./target-representation";
import type {
  TargetChangeAttributionSide,
  TargetChangeMethodology,
  TargetChangeMode,
  TargetChangeRow,
  TargetChangeSummary,
} from "./target-change";
import type { TargetMatchingSummary } from "./target-surface-matcher";

export const CALIBRATION_TREE_SCHEMA_VERSION = 6 as const;
export const CALIBRATION_TREE_TARGET_SUMMARY_MAX_RAW_BYTES = 8_000_000;
export const CALIBRATION_TREE_TARGET_DETAIL_MAX_RAW_BYTES = 4_000_000;

export type CalibrationTreeTierPart = `tier-${number}`;
export type CalibrationTreeTargetSummaryPart = `target-summary-${string}`;
export type CalibrationTreeTargetDetailsPart = `target-details-${string}`;
export type CalibrationTreePart =
  | "index"
  | CalibrationTreeTierPart
  | "filter-index"
  | CalibrationTreeTargetSummaryPart
  | CalibrationTreeTargetDetailsPart;

export interface CalibrationTreeSourceArtifact {
  path: string;
  sha256: string;
  hfRepo?: string;
  hfCommitSha?: string;
}

export interface CalibrationTreeSourceArtifacts {
  calibrationDiagnostics: CalibrationTreeSourceArtifact | null;
  buildManifest: CalibrationTreeSourceArtifact | null;
  releaseManifest: CalibrationTreeSourceArtifact | null;
  demographics: CalibrationTreeSourceArtifact | null;
  stagingRunManifest?: CalibrationTreeSourceArtifact | null;
  stagingProgress?: CalibrationTreeSourceArtifact | null;
  stagedDatasetReceipt?: CalibrationTreeSourceArtifact | null;
  comparisonCurrentIndex?: CalibrationTreeSourceArtifact | null;
  comparisonCandidateIndex?: CalibrationTreeSourceArtifact | null;
}

export type CalibrationTreeBuildKind = "release" | "staging" | "comparison";

export interface CalibrationTreeBuild {
  buildArtifactId: string;
  kind: CalibrationTreeBuildKind;
  sourceId: string;
  label: string;
  releaseId: string | null;
  hfRepo: string | null;
  hfCommitSha: string | null;
  createdAt: string | null;
  sourceArtifacts: CalibrationTreeSourceArtifacts;
}

export interface CalibrationTreeTargetSummary {
  id: string;
  label: string;
  metricInputs: CalibrationTreeMetricInput;
  comparison: CalibrationTreeComparisonTarget;
  detailSource?: {
    currentTargetOrdinal: number | null;
    candidateTargetOrdinal: number | null;
  };
}

export interface CalibrationTreeComparisonTarget {
  normalizedBaseName: string | null;
  chronicleFactKey: string | null;
  structuredIdentity: string | null;
  representation: TargetRowRepresentation;
  row: CalibrationTreeTarget;
}

export interface CalibrationTreeComparisonMetadata {
  releaseId: string;
  calibrationProvenance?: CalibrationProvenance;
  status: TargetLossAttributionStatus;
  aggregate: number | null;
  cap: number | null;
  basisIdentifier: string | null;
  targetRepresentation: TargetRepresentation;
}

export interface CalibrationTreeComparisonResultMetadata {
  pairArtifactId: string;
  currentBuildArtifactId: string;
  candidateBuildArtifactId: string;
  mode: TargetChangeMode;
  available: boolean;
  reason: string | null;
  current: TargetChangeAttributionSide;
  candidate: TargetChangeAttributionSide;
  methodology: TargetChangeMethodology;
  matching: TargetMatchingSummary;
  summary: TargetChangeSummary | null;
}

export interface CalibrationTreePosting<T extends string | null = string> {
  value: T;
  targetOrdinals: number[];
}

export interface CalibrationTreeFilterPostings {
  geographyLevels: CalibrationTreePosting[];
  geographies: CalibrationTreePosting[];
  fitBands: CalibrationTreePosting<FitBand>[];
  comparisonFits: CalibrationTreePosting<ComparisonFit | null>[];
  calibrationStatuses: CalibrationTreePosting<CalibrationStatus | null>[];
}

interface CompiledCalibrationNodeBase {
  id: string;
  label: string;
  selection: ExplorerNodeSelection;
  targetOrdinals: number[];
  metrics: CalibrationTreeMetrics;
  authoredLabel?: boolean;
}

export interface CompiledCalibrationBranchNode
  extends CompiledCalibrationNodeBase {
  kind: Exclude<ExplorerNodeSelection["kind"], "target">;
  nextLevelId: string;
}

export interface CompiledCalibrationTargetNode
  extends CompiledCalibrationNodeBase {
  kind: "target";
  targetOrdinal: number;
}

export type CompiledCalibrationNode =
  | CompiledCalibrationBranchNode
  | CompiledCalibrationTargetNode;

export interface CompiledCalibrationGroup {
  id: string;
  label: string;
  targetOrdinals: number[];
  metrics: CalibrationTreeMetrics;
  nodes: CompiledCalibrationNode[];
}

export interface CompiledCalibrationLevel {
  currentLevel: CalibrationTreeResponse["currentLevel"];
  pathLabels: CalibrationTreeResponse["pathLabels"];
  dimensionOrder: CalibrationTreeResponse["dimensionOrder"];
  targetOrdinals: number[];
  metrics: CalibrationTreeMetrics;
  groups: CompiledCalibrationGroup[];
}

interface CalibrationTreePartIdentity {
  schemaVersion: typeof CALIBRATION_TREE_SCHEMA_VERSION;
  country: MicrocosmCountry;
  buildArtifactId: string;
  part: CalibrationTreePart;
}

export interface CalibrationTreePartDescriptor {
  part: Exclude<CalibrationTreePart, "index">;
  path: string;
  sha256: string;
  rawBytes: number;
  gzipBytes: number;
}

export interface CalibrationTreeTierDescriptor
  extends CalibrationTreePartDescriptor {
  part: CalibrationTreeTierPart;
  depth: number;
  levelIds: string[];
}

export interface CalibrationTreeTargetDetailsDescriptor
  extends CalibrationTreePartDescriptor {
  part: CalibrationTreeTargetDetailsPart;
  startTargetOrdinal: number;
  endTargetOrdinalExclusive: number;
}

export interface CalibrationTreeTargetSummaryDescriptor
  extends CalibrationTreePartDescriptor {
  part: CalibrationTreeTargetSummaryPart;
  startTargetOrdinal: number;
  endTargetOrdinalExclusive: number;
}

export interface CalibrationTreeIndexArtifact
  extends CalibrationTreePartIdentity {
  part: "index";
  build: CalibrationTreeBuild;
  targetComparison: CalibrationTreeComparisonMetadata;
  comparison?: CalibrationTreeComparisonResultMetadata;
  calibrationProvenance?: CalibrationProvenance;
  lossAttributionAvailable: boolean;
  filterOptions: CalibrationTreeResponse["filterOptions"];
  targetCount: number;
  maxDepth: number;
  roots: {
    program: string;
    geography: string;
  };
  levels: Record<string, CompiledCalibrationLevel>;
  parts: {
    tiers: CalibrationTreeTierDescriptor[];
    filterIndex: CalibrationTreePartDescriptor & { part: "filter-index" };
    targetSummaries: CalibrationTreeTargetSummaryDescriptor[];
    targetDetailStrategy: "shards" | "source-targets";
    targetDetails: CalibrationTreeTargetDetailsDescriptor[];
  };
}

export interface CalibrationTreeTierArtifact
  extends CalibrationTreePartIdentity {
  part: CalibrationTreeTierPart;
  depth: number;
  targetCount: number;
  levels: Record<string, CompiledCalibrationLevel>;
}

export interface CalibrationTreeFilterIndexArtifact
  extends CalibrationTreePartIdentity {
  part: "filter-index";
  targetCount: number;
  postings: CalibrationTreeFilterPostings;
}

export interface CalibrationTreeTargetSummaryArtifact
  extends CalibrationTreePartIdentity {
  part: CalibrationTreeTargetSummaryPart;
  startTargetOrdinal: number;
  endTargetOrdinalExclusive: number;
  targets: CalibrationTreeTargetSummary[];
}

export interface CalibrationTreeTargetDetailsArtifact
  extends CalibrationTreePartIdentity {
  part: CalibrationTreeTargetDetailsPart;
  startTargetOrdinal: number;
  endTargetOrdinalExclusive: number;
  targets: CalibrationTreeTarget[];
}

export type CalibrationTreeArtifactPart =
  | CalibrationTreeIndexArtifact
  | CalibrationTreeTierArtifact
  | CalibrationTreeFilterIndexArtifact
  | CalibrationTreeTargetSummaryArtifact
  | CalibrationTreeTargetDetailsArtifact;

export interface CalibrationTreeBundleDraft {
  country: MicrocosmCountry;
  build: CalibrationTreeBuild;
  comparison: CalibrationTreeComparisonMetadata;
  comparisonResult?: CalibrationTreeComparisonResultMetadata;
  calibrationProvenance?: CalibrationProvenance;
  lossAttributionAvailable: boolean;
  filterOptions: CalibrationTreeResponse["filterOptions"];
  roots: CalibrationTreeIndexArtifact["roots"];
  levels: Record<string, CompiledCalibrationLevel>;
  levelDepths: Record<string, number>;
  targets: Array<CalibrationTreeTargetSummary & {
    facets: {
      geographyLevel: string;
      geography: string;
      fitBand: FitBand;
      comparisonFit: ComparisonFit | null;
      calibrationStatus: CalibrationStatus | null;
    };
    detail: CalibrationTreeTarget;
  }>;
}

export interface LoadedCalibrationTreeBundle {
  index: CalibrationTreeIndexArtifact;
  tiers: CalibrationTreeTierArtifact[];
  filterIndex?: CalibrationTreeFilterIndexArtifact;
  targetSummaries?: CalibrationTreeTargetSummary[];
  targetDetailShard?: CalibrationTreeTargetDetailsArtifact;
  selectedTargetDetail?: {
    targetOrdinal: number;
    target: CalibrationTreeTarget;
  };
}

export interface CalibrationTreeTargetDetailsPlan {
  artifacts: CalibrationTreeTargetDetailsArtifact[];
}

export interface CalibrationTreeTargetSummariesPlan {
  artifacts: CalibrationTreeTargetSummaryArtifact[];
}

export interface CalibrationTreeTargetDetailSelection {
  descriptor: CalibrationTreeTargetDetailsDescriptor;
  offset: number;
  targetOrdinal: number;
}

export interface CalibrationTreeComparisonTargetDetailResponse {
  schemaVersion: typeof CALIBRATION_TREE_SCHEMA_VERSION;
  country: MicrocosmCountry;
  buildArtifactId: string;
  targetOrdinal: number;
  targetId: string;
  target: TargetChangeRow;
}

export interface CompileCalibrationTreeInput {
  country: MicrocosmCountry;
  buildArtifactId?: string;
  buildKind?: CalibrationTreeBuildKind;
  sourceId?: string;
  label?: string;
  createdAt?: string | null;
  releaseId: string;
  hfRepo: string | null;
  hfCommitSha: string | null;
  sourceArtifacts: CalibrationTreeSourceArtifacts;
  rows: CalibrationTreeTarget[];
  calibrationProvenance?: CalibrationProvenance;
  lossAttributionAvailable?: boolean;
  comparison?: CalibrationTreeComparisonMetadata;
  comparisonResult?: CalibrationTreeComparisonResultMetadata;
}

const DEFAULT_GEOGRAPHY_LEVEL = "national";
const SHA256_RE = /^[0-9a-f]{64}$/;
const HF_COMMIT_RE = /^[0-9a-f]{40,64}$/;
const TIER_PART_RE = /^tier-([1-9][0-9]*)$/;
const TARGET_SUMMARY_PART_RE = /^target-summary-([0-9]{4})$/;
const TARGET_DETAILS_PART_RE = /^target-details-([0-9]{4})$/;

function targetSummaryPart(shardIndex: number): CalibrationTreeTargetSummaryPart {
  const number = shardIndex + 1;
  if (!Number.isSafeInteger(number) || number < 1 || number > 9_999) {
    throw new Error("Calibration tree target-summary shard number is out of range.");
  }
  return `target-summary-${String(number).padStart(4, "0")}`;
}

function targetDetailsPart(shardIndex: number): CalibrationTreeTargetDetailsPart {
  const number = shardIndex + 1;
  if (!Number.isSafeInteger(number) || number < 1 || number > 9_999) {
    throw new Error("Calibration tree target-detail shard number is out of range.");
  }
  return `target-details-${String(number).padStart(4, "0")}`;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function nonnegativeNumber(value: unknown): number | null {
  const result = finiteNumber(value);
  return result != null && result >= 0 ? result : null;
}

function targetIdentifier(row: CalibrationTreeTarget, index: number): string {
  return String(row.comparison_id ?? row.name ?? row.base_name ?? `target-${index}`);
}

function normalizeStatus(
  value: CalibrationTreeTarget["calibration_status"],
): CalibrationStatus | null {
  if (value === "included") return "included";
  if (value === "skipped" || value === "not_materialized") return "skipped";
  return null;
}

const COMPARISON_ROW_FIELDS = [
  "name",
  "base_name",
  "target",
  "value",
  "final_estimate",
  "estimate",
  "source",
  "source_label",
  "variable",
  "variable_label",
  "variable_key",
  "measure",
  "source_measure_id",
  "target_label",
  "target_representation",
  "dimension_adapter",
  "dimensions",
  "target_dimensions",
  "chronicle",
  "level",
  "geography",
  "family",
  "breakdown",
  "dims",
  "calibration_status",
  "comparison_fit",
  "comparison_id",
  "match_kind",
  "current_name",
  "candidate_name",
  "current_representation",
  "candidate_representation",
  "comparison_status",
  "current",
  "candidate",
  "reported_change",
  "pooled_weight_share",
  "shared_current_contribution",
  "shared_candidate_contribution",
  "shared_change",
  "current_target_ordinal",
  "candidate_target_ordinal",
  "abs_relative_error",
  "target_loss_weight",
  "target_loss_weight_share",
  "target_loss_scale",
  "final_capped_scaled_error",
  "final_loss_contribution",
] as const;

function comparisonReadyTarget(
  row: CalibrationTreeTarget,
): CalibrationTreeComparisonTarget {
  const compactRow = Object.fromEntries(
    COMPARISON_ROW_FIELDS.flatMap((field) =>
      row[field] === undefined ? [] : [[field, row[field]]],
    ),
  ) as CalibrationTreeTarget;
  const matchingRow = row as Parameters<typeof normalizedTargetName>[0];
  return {
    normalizedBaseName: normalizedTargetName(matchingRow),
    chronicleFactKey: chronicleFactKey(matchingRow),
    structuredIdentity: structuredIdentity(matchingRow),
    representation: targetRowRepresentation(matchingRow),
    row: compactRow,
  };
}

function compiledTarget(
  row: CalibrationTreeTarget,
  index: number,
  geographyLabel: GeographyLabeller,
): CalibrationTreeBundleDraft["targets"][number] {
  const error = finiteNumber(row.abs_relative_error);
  const hasDetailSource =
    row.current_target_ordinal !== undefined ||
    row.candidate_target_ordinal !== undefined;
  const sourceOrdinal = (value: unknown, label: string): number | null => {
    if (value == null) return null;
    if (!Number.isSafeInteger(value) || (value as number) < 0) {
      throw new Error(`${label} must be a non-negative integer or null.`);
    }
    return value as number;
  };
  return {
    id: targetIdentifier(row, index),
    label: targetIdentifier(row, index),
    facets: {
      geographyLevel:
        String(row.level ?? "").trim() || DEFAULT_GEOGRAPHY_LEVEL,
      // The geography's tree label, which the Geography filter offers and
      // selects, so a geography that rows spell more than one way has one
      // posting (see geographyLabeller).
      geography: geographyLabel(row),
      fitBand: fitBandForTarget(row),
      comparisonFit:
        row.comparison_fit === "improved" ||
        row.comparison_fit === "regressed" ||
        row.comparison_fit === "unchanged" ||
        row.comparison_fit === "not_applicable"
          ? row.comparison_fit
          : null,
      calibrationStatus: normalizeStatus(row.calibration_status),
    },
    metricInputs: {
      absRelativeError: error == null ? null : Math.abs(error),
      targetLossWeightShare: nonnegativeNumber(row.target_loss_weight_share),
      finalLossContribution: nonnegativeNumber(row.final_loss_contribution),
      targetChange: finiteNumber(row.target_change),
      comparisonStatus:
        row.comparison_status === "shared" ||
        row.comparison_status === "added" ||
        row.comparison_status === "removed"
          ? row.comparison_status
          : null,
    },
    comparison: comparisonReadyTarget(row),
    ...(hasDetailSource
      ? {
          detailSource: {
            currentTargetOrdinal: sourceOrdinal(
              row.current_target_ordinal,
              "Current comparison target ordinal",
            ),
            candidateTargetOrdinal: sourceOrdinal(
              row.candidate_target_ordinal,
              "Candidate comparison target ordinal",
            ),
          },
        }
      : {}),
    detail: row,
  };
}

function uniqueSorted(indices: number[]): number[] {
  return [...new Set(indices)].sort((left, right) => left - right);
}

function sameIndices(left: number[], right: number[]): boolean {
  return left.length === right.length &&
    left.every((value, index) => value === right[index]);
}

function semanticStateKey(state: ExplorerState): string {
  const path = {
    source: state.path.source,
    program: state.path.program,
    geography: state.path.geography,
    dimensions: state.path.dimensions.map(({ key, label, value }) => ({
      key,
      label,
      value,
    })),
  };
  return path.source && path.program && path.geography
    ? JSON.stringify(path)
    : JSON.stringify({ breakdown: state.breakdown, path });
}

function targetOrdinalFor(
  target: CalibrationTreeTarget | undefined,
  targetOrdinals: Map<CalibrationTreeTarget, number>,
): number {
  if (!target) throw new Error("Compiled target node is missing target details.");
  const ordinal = targetOrdinals.get(target);
  if (ordinal == null) {
    throw new Error(`Compiled target ${String(target.name ?? target.base_name)} is unknown.`);
  }
  return ordinal;
}

function inferredTargetRepresentation(
  targets: CalibrationTreeTarget[],
): TargetRepresentation {
  if (!targets.length) return "unknown";
  const values = new Set(
    targets.map((row) =>
      targetRowRepresentation(
        row as Parameters<typeof targetRowRepresentation>[0],
      ),
    ),
  );
  if (values.size > 1) return "mixed";
  if (values.has("hierarchy")) return "hierarchy";
  return values.has("structured") ? "structured" : "legacy";
}

export function compileCalibrationTreeBundleDraft(
  input: CompileCalibrationTreeInput,
): CalibrationTreeBundleDraft {
  if (
    input.buildKind !== "comparison" &&
    (input.hfCommitSha == null || !HF_COMMIT_RE.test(input.hfCommitSha))
  ) {
    throw new Error(`Invalid Hugging Face commit SHA: ${input.hfCommitSha}`);
  }
  const buildArtifactId = input.buildArtifactId ??
    input.sourceArtifacts.calibrationDiagnostics?.sha256;
  if (!buildArtifactId) {
    throw new Error("A calibration build artifact id is required.");
  }
  if (!SHA256_RE.test(buildArtifactId)) {
    throw new Error(`Invalid calibration build artifact id: ${buildArtifactId}`);
  }
  const rows = normalizeCalibrationTreeTargets(input.rows);
  const lossAttributionAvailable =
    input.lossAttributionAvailable ??
    (rows.length > 0 &&
      rows.every((row) => nonnegativeNumber(row.final_loss_contribution) != null));
  // One labeller for the geography postings and for every level of the tree.
  const geographyLabel = geographyLabeller(rows);
  const targets = rows.map((row, index) =>
    compiledTarget(row, index, geographyLabel),
  );
  const targetOrdinals = new Map(rows.map((row, ordinal) => [row, ordinal]));
  const levels: Record<string, CompiledCalibrationLevel> = {};
  const stateLevels = new Map<string, string>();
  let nextLevelNumber = 0;
  let filterOptions: CalibrationTreeResponse["filterOptions"] | null = null;

  const compileLevel = (state: ExplorerState): string => {
    const stateKey = semanticStateKey(state);
    const existing = stateLevels.get(stateKey);
    if (existing) return existing;

    const levelId = `level-${String(nextLevelNumber).padStart(6, "0")}`;
    nextLevelNumber += 1;
    stateLevels.set(stateKey, levelId);

    const response = buildCalibrationTree(
      rows,
      state,
      input.releaseId,
      lossAttributionAvailable,
      undefined,
      geographyLabel,
    );
    filterOptions ??= response.filterOptions;

    const groups = response.groups.map((group): CompiledCalibrationGroup => {
      const nodes = group.nodes.map((treeNode): CompiledCalibrationNode => {
        if (treeNode.kind === "target") {
          const targetOrdinal = targetOrdinalFor(treeNode.target, targetOrdinals);
          targets[targetOrdinal].id = treeNode.id;
          targets[targetOrdinal].label = treeNode.label;
          return {
            id: treeNode.id,
            label: treeNode.label,
            kind: "target",
            selection: treeNode.selection,
            targetOrdinals: [targetOrdinal],
            targetOrdinal,
            metrics: treeNode.metrics,
            authoredLabel: treeNode.authored_label,
          };
        }

        const childState = selectExplorerNode(state, treeNode.selection);
        if (semanticStateKey(childState) === stateKey) {
          throw new Error(`Selection ${treeNode.id} did not advance the calibration tree.`);
        }
        const nextLevelId = compileLevel(childState);
        return {
          id: treeNode.id,
          label: treeNode.label,
          kind: treeNode.kind,
          selection: treeNode.selection,
          targetOrdinals: levels[nextLevelId].targetOrdinals,
          nextLevelId,
          metrics: treeNode.metrics,
          authoredLabel: treeNode.authored_label,
        };
      });
      return {
        id: group.id,
        label: group.label,
        nodes,
        targetOrdinals: uniqueSorted(nodes.flatMap((node) => node.targetOrdinals)),
        metrics: group.metrics,
      };
    });

    levels[levelId] = {
      currentLevel: response.currentLevel,
      pathLabels: response.pathLabels,
      dimensionOrder: response.dimensionOrder,
      groups,
      targetOrdinals: uniqueSorted(groups.flatMap((group) => group.targetOrdinals)),
      metrics: response.filteredMetrics,
    };
    return levelId;
  };

  const roots = {
    program: compileLevel(createExplorerState()),
    geography: compileLevel({ ...createExplorerState(), breakdown: "geography" }),
  };
  const levelDepths: Record<string, number> = {};
  const queue = [
    { levelId: roots.program, depth: 0 },
    { levelId: roots.geography, depth: 0 },
  ];
  while (queue.length) {
    const { levelId, depth } = queue.shift()!;
    const previousDepth = levelDepths[levelId];
    if (previousDepth != null) {
      if (previousDepth !== depth) {
        throw new Error(`Calibration tree level ${levelId} is reachable at multiple depths.`);
      }
      continue;
    }
    levelDepths[levelId] = depth;
    for (const node of levels[levelId].groups.flatMap((group) => group.nodes)) {
      if (node.kind !== "target") {
        queue.push({ levelId: node.nextLevelId, depth: depth + 1 });
      }
    }
  }
  if (Object.keys(levelDepths).length !== Object.keys(levels).length) {
    throw new Error("Calibration tree contains an unreachable level.");
  }

  return {
    country: input.country,
    build: {
      buildArtifactId,
      kind: input.buildKind ?? "release",
      sourceId: input.sourceId ?? input.releaseId,
      label: input.label ?? input.releaseId,
      releaseId: input.releaseId,
      hfRepo: input.hfRepo,
      hfCommitSha: input.hfCommitSha,
      createdAt: input.createdAt ?? null,
      sourceArtifacts: input.sourceArtifacts,
    },
    comparison: input.comparison ?? {
      releaseId: input.releaseId,
      calibrationProvenance: input.calibrationProvenance,
      status: lossAttributionAvailable ? "derived" : "unavailable",
      aggregate: lossAttributionAvailable
        ? targets.reduce(
            (sum, target) =>
              sum + (target.metricInputs.finalLossContribution ?? 0),
            0,
          )
        : null,
      cap: null,
      basisIdentifier: null,
      targetRepresentation: inferredTargetRepresentation(rows),
    },
    comparisonResult: input.comparisonResult,
    calibrationProvenance: input.calibrationProvenance,
    lossAttributionAvailable,
    filterOptions: filterOptions ?? {
      geographyLevels: [],
      geographies: [],
      fitBands: [],
      comparisonFits: [],
      calibrationStatuses: [],
    },
    roots,
    levels,
    levelDepths,
    targets,
  };
}

function postingValues<T extends string | null>(values: T[]): CalibrationTreePosting<T>[] {
  const postings = new Map<T, number[]>();
  values.forEach((value, index) => {
    const indices = postings.get(value) ?? [];
    indices.push(index);
    postings.set(value, indices);
  });
  return [...postings.entries()]
    .sort(([left], [right]) => String(left).localeCompare(String(right)))
    .map(([value, targetOrdinals]) => ({ value, targetOrdinals }));
}

export function calibrationTreeFilterIndexFromDraft(
  draft: CalibrationTreeBundleDraft,
): CalibrationTreeFilterIndexArtifact {
  return {
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    country: draft.country,
    buildArtifactId: draft.build.buildArtifactId,
    part: "filter-index",
    targetCount: draft.targets.length,
    postings: {
      geographyLevels: postingValues(
        draft.targets.map((target) => target.facets.geographyLevel),
      ),
      geographies: postingValues(
        draft.targets.map((target) => target.facets.geography),
      ),
      fitBands: postingValues(
        draft.targets.map((target) => target.facets.fitBand),
      ),
      comparisonFits: postingValues(
        draft.targets.map((target) => target.facets.comparisonFit),
      ),
      calibrationStatuses: postingValues(
        draft.targets.map((target) => target.facets.calibrationStatus),
      ),
    },
  };
}

export function calibrationTreeTargetSummariesFromDraft(
  draft: CalibrationTreeBundleDraft,
  maxRawBytes = CALIBRATION_TREE_TARGET_SUMMARY_MAX_RAW_BYTES,
): CalibrationTreeTargetSummariesPlan {
  if (!Number.isSafeInteger(maxRawBytes) || maxRawBytes <= 0) {
    throw new Error("Calibration tree target-summary shard size must be a positive integer.");
  }
  const targets = draft.targets.map(({ id, label, metricInputs, comparison, detailSource }) => ({
    id,
    label,
    metricInputs,
    comparison,
    ...(detailSource ? { detailSource } : {}),
  }));
  const serializedTargets = targets.map(stableJson);
  const targetBytes = serializedTargets.map(utf8Bytes);
  const artifacts: CalibrationTreeTargetSummaryArtifact[] = [];
  let startTargetOrdinal = 0;

  while (startTargetOrdinal < targets.length) {
    const shardIndex = artifacts.length;
    if (shardIndex >= 9_999) {
      throw new Error("Calibration tree target summaries require more than 9,999 shards.");
    }
    const part = targetSummaryPart(shardIndex);
    let endTargetOrdinalExclusive = startTargetOrdinal;
    let payloadBytes = 0;

    while (endTargetOrdinalExclusive < targets.length) {
      const nextPayloadBytes =
        payloadBytes +
        targetBytes[endTargetOrdinalExclusive] +
        (endTargetOrdinalExclusive === startTargetOrdinal ? 0 : 1);
      const emptyArtifact: CalibrationTreeTargetSummaryArtifact = {
        schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
        country: draft.country,
        buildArtifactId: draft.build.buildArtifactId,
        part,
        startTargetOrdinal,
        endTargetOrdinalExclusive: endTargetOrdinalExclusive + 1,
        targets: [],
      };
      const candidateBytes = utf8Bytes(`${stableJson(emptyArtifact)}\n`) + nextPayloadBytes;
      if (candidateBytes > maxRawBytes) break;
      payloadBytes = nextPayloadBytes;
      endTargetOrdinalExclusive += 1;
    }

    if (endTargetOrdinalExclusive === startTargetOrdinal) {
      const targetBytesWithEnvelope = utf8Bytes(`${stableJson({
        schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
        country: draft.country,
        buildArtifactId: draft.build.buildArtifactId,
        part,
        startTargetOrdinal,
        endTargetOrdinalExclusive: startTargetOrdinal + 1,
        targets: [targets[startTargetOrdinal]],
      })}\n`);
      throw new Error(
        `Calibration target summary ordinal ${startTargetOrdinal} requires ` +
        `${targetBytesWithEnvelope} bytes, which exceeds the ${maxRawBytes}-byte shard limit.`,
      );
    }

    const artifact: CalibrationTreeTargetSummaryArtifact = {
      schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
      country: draft.country,
      buildArtifactId: draft.build.buildArtifactId,
      part,
      startTargetOrdinal,
      endTargetOrdinalExclusive,
      targets: targets.slice(startTargetOrdinal, endTargetOrdinalExclusive),
    };
    if (utf8Bytes(serializeCalibrationTreePart(artifact)) > maxRawBytes) {
      throw new Error(`Calibration tree shard ${part} exceeded its planned byte limit.`);
    }
    artifacts.push(artifact);
    startTargetOrdinal = endTargetOrdinalExclusive;
  }

  return { artifacts };
}

export function calibrationTreeTargetDetailsFromDraft(
  draft: CalibrationTreeBundleDraft,
  maxRawBytes = CALIBRATION_TREE_TARGET_DETAIL_MAX_RAW_BYTES,
): CalibrationTreeTargetDetailsPlan {
  if (!Number.isSafeInteger(maxRawBytes) || maxRawBytes <= 0) {
    throw new Error("Calibration tree target-detail shard size must be a positive integer.");
  }
  const serializedTargets = draft.targets.map((target) => stableJson(target.detail));
  const targetBytes = serializedTargets.map(utf8Bytes);
  const artifacts: CalibrationTreeTargetDetailsArtifact[] = [];
  let startTargetOrdinal = 0;

  while (startTargetOrdinal < draft.targets.length) {
    const shardIndex = artifacts.length;
    if (shardIndex >= 9_999) {
      throw new Error("Calibration tree target details require more than 9,999 shards.");
    }
    const part = targetDetailsPart(shardIndex);
    let endTargetOrdinalExclusive = startTargetOrdinal;
    let payloadBytes = 0;

    while (endTargetOrdinalExclusive < draft.targets.length) {
      const nextPayloadBytes =
        payloadBytes +
        targetBytes[endTargetOrdinalExclusive] +
        (endTargetOrdinalExclusive === startTargetOrdinal ? 0 : 1);
      const emptyArtifact: CalibrationTreeTargetDetailsArtifact = {
        schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
        country: draft.country,
        buildArtifactId: draft.build.buildArtifactId,
        part,
        startTargetOrdinal,
        endTargetOrdinalExclusive: endTargetOrdinalExclusive + 1,
        targets: [],
      };
      const candidateBytes = utf8Bytes(`${stableJson(emptyArtifact)}\n`) + nextPayloadBytes;
      if (candidateBytes > maxRawBytes) break;
      payloadBytes = nextPayloadBytes;
      endTargetOrdinalExclusive += 1;
    }

    if (endTargetOrdinalExclusive === startTargetOrdinal) {
      const targetBytesWithEnvelope = utf8Bytes(`${stableJson({
        schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
        country: draft.country,
        buildArtifactId: draft.build.buildArtifactId,
        part,
        startTargetOrdinal,
        endTargetOrdinalExclusive: startTargetOrdinal + 1,
        targets: [draft.targets[startTargetOrdinal].detail],
      })}\n`);
      throw new Error(
        `Calibration target ordinal ${startTargetOrdinal} requires ${targetBytesWithEnvelope} bytes, ` +
        `which exceeds the ${maxRawBytes}-byte shard limit.`,
      );
    }

    const artifact: CalibrationTreeTargetDetailsArtifact = {
      schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
      country: draft.country,
      buildArtifactId: draft.build.buildArtifactId,
      part,
      startTargetOrdinal,
      endTargetOrdinalExclusive,
      targets: draft.targets
        .slice(startTargetOrdinal, endTargetOrdinalExclusive)
        .map((target) => target.detail),
    };
    const serialized = serializeCalibrationTreePart(artifact);
    if (utf8Bytes(serialized) > maxRawBytes) {
      throw new Error(`Calibration tree shard ${part} exceeded its planned byte limit.`);
    }
    artifacts.push(artifact);
    startTargetOrdinal = endTargetOrdinalExclusive;
  }

  return { artifacts };
}

export function calibrationTreeTargetDetailSelection(
  index: CalibrationTreeIndexArtifact,
  targetOrdinal: number,
): CalibrationTreeTargetDetailSelection {
  if (index.parts.targetDetailStrategy !== "shards") {
    throw new Error("Calibration comparison target details resolve through source targets.");
  }
  const descriptor = index.parts.targetDetails.find(
    (candidate) =>
      targetOrdinal >= candidate.startTargetOrdinal &&
      targetOrdinal < candidate.endTargetOrdinalExclusive,
  );
  if (
    !Number.isSafeInteger(targetOrdinal) ||
    targetOrdinal < 0 ||
    !descriptor
  ) {
    throw new Error(
      `Calibration target ordinal ${targetOrdinal} is not covered by a detail shard.`,
    );
  }
  const offset = targetOrdinal - descriptor.startTargetOrdinal;
  return { descriptor, offset, targetOrdinal };
}

export function calibrationTreeTiersFromDraft(
  draft: CalibrationTreeBundleDraft,
): CalibrationTreeTierArtifact[] {
  const maxDepth = Math.max(0, ...Object.values(draft.levelDepths));
  return Array.from({ length: maxDepth }, (_, index) => index + 1).map((depth) => ({
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    country: draft.country,
    buildArtifactId: draft.build.buildArtifactId,
    part: `tier-${depth}`,
    depth,
    targetCount: draft.targets.length,
    levels: Object.fromEntries(
      Object.entries(draft.levels).filter(
        ([levelId]) => draft.levelDepths[levelId] === depth,
      ),
    ),
  }));
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([, item]) => item !== undefined)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableValue(item)]),
    );
  }
  if (typeof value === "number" && !Number.isFinite(value)) return null;
  return value;
}

function stableJson(value: unknown): string {
  return JSON.stringify(stableValue(value));
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

export function serializeCalibrationTreePart(part: CalibrationTreeArtifactPart): string {
  parseCalibrationTreePart(part, part.part);
  return `${stableJson(part)}\n`;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function validateIndices(value: unknown, targetCount: number, label: string): number[] {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array.`);
  let previous = -1;
  for (const item of value) {
    if (!Number.isInteger(item) || item < 0 || item >= targetCount) {
      throw new Error(`${label} contains an invalid target index.`);
    }
    if (item <= previous) throw new Error(`${label} must be sorted and unique.`);
    previous = item;
  }
  return value as number[];
}

function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${label} must be an array of strings.`);
  }
  return value as string[];
}

function validateIdentity(
  value: unknown,
  expectedPart?: CalibrationTreePart,
): Record<string, unknown> {
  const part = record(value, "Calibration tree part");
  if (part.schemaVersion !== CALIBRATION_TREE_SCHEMA_VERSION) {
    throw new Error(`Unsupported calibration tree schema ${String(part.schemaVersion)}.`);
  }
  if (typeof part.country !== "string" || !isCountry(part.country)) {
    throw new Error("Calibration tree part has an unknown country.");
  }
  if (
    typeof part.buildArtifactId !== "string" ||
    !SHA256_RE.test(part.buildArtifactId)
  ) {
    throw new Error("Calibration tree part has an invalid build artifact id.");
  }
  if (typeof part.part !== "string" || !isCalibrationTreePart(part.part)) {
    throw new Error("Calibration tree part name is invalid.");
  }
  if (expectedPart && part.part !== expectedPart) {
    throw new Error(`Expected calibration tree part ${expectedPart}, received ${part.part}.`);
  }
  return part;
}

function validateLevelRecords(
  value: unknown,
  targetCount: number,
  label: string,
): Record<string, CompiledCalibrationLevel> {
  const levels = record(value, label);
  for (const [levelId, item] of Object.entries(levels)) {
    const level = record(item, `${label}.${levelId}`);
    const levelIndices = validateIndices(
      level.targetOrdinals,
      targetCount,
      `${levelId}.targetOrdinals`,
    );
    if (!Array.isArray(level.groups)) throw new Error(`${levelId}.groups must be an array.`);
    const indicesFromGroups: number[] = [];
    for (const [groupIndex, groupItem] of level.groups.entries()) {
      const group = record(groupItem, `${levelId}.groups[${groupIndex}]`);
      const groupIndices = validateIndices(
        group.targetOrdinals,
        targetCount,
        `${levelId}.groups[${groupIndex}].targetOrdinals`,
      );
      if (!Array.isArray(group.nodes)) {
        throw new Error(`${levelId}.groups[${groupIndex}].nodes must be an array.`);
      }
      const indicesFromNodes: number[] = [];
      for (const [nodeIndex, nodeItem] of group.nodes.entries()) {
        const node = record(nodeItem, `${levelId}.groups[${groupIndex}].nodes[${nodeIndex}]`);
        const indices = validateIndices(
          node.targetOrdinals,
          targetCount,
          `${levelId}.groups[${groupIndex}].nodes[${nodeIndex}].targetOrdinals`,
        );
        indicesFromNodes.push(...indices);
        if (node.kind === "target") {
          if (
            indices.length !== 1 ||
            !Number.isInteger(node.targetOrdinal) ||
            indices[0] !== node.targetOrdinal
          ) {
            throw new Error(`Target node ${String(node.id)} has an invalid target ordinal.`);
          }
        } else if (typeof node.nextLevelId !== "string" || !node.nextLevelId) {
          throw new Error(`Branch node ${String(node.id)} has an invalid next level.`);
        }
      }
      if (!sameIndices(groupIndices, uniqueSorted(indicesFromNodes))) {
        throw new Error(`${levelId}.groups[${groupIndex}] has inconsistent membership.`);
      }
      indicesFromGroups.push(...groupIndices);
    }
    if (!sameIndices(levelIndices, uniqueSorted(indicesFromGroups))) {
      throw new Error(`${levelId} has inconsistent membership.`);
    }
  }
  return levels as Record<string, CompiledCalibrationLevel>;
}

function validateDescriptor(
  value: unknown,
  expectedPart: Exclude<CalibrationTreePart, "index">,
  country: MicrocosmCountry,
  buildArtifactId: string,
): CalibrationTreePartDescriptor {
  const descriptor = record(value, `Calibration tree descriptor ${expectedPart}`);
  if (descriptor.part !== expectedPart) {
    throw new Error(`Calibration tree descriptor ${expectedPart} has the wrong part name.`);
  }
  if (
    descriptor.path !==
      calibrationTreePartPath(country, buildArtifactId, expectedPart)
  ) {
    throw new Error(`Calibration tree descriptor ${expectedPart} has the wrong path.`);
  }
  if (typeof descriptor.sha256 !== "string" || !SHA256_RE.test(descriptor.sha256)) {
    throw new Error(`Calibration tree descriptor ${expectedPart} has an invalid hash.`);
  }
  for (const field of ["rawBytes", "gzipBytes"] as const) {
    if (!Number.isSafeInteger(descriptor[field]) || (descriptor[field] as number) <= 0) {
      throw new Error(`Calibration tree descriptor ${expectedPart} has invalid ${field}.`);
    }
  }
  return descriptor as unknown as CalibrationTreePartDescriptor;
}

function validateTargetShardDescriptors(options: {
  value: unknown;
  targetCount: number;
  country: MicrocosmCountry;
  buildArtifactId: string;
  label: "target-summary" | "target-detail";
  maxRawBytes: number;
  partForIndex: (
    shardIndex: number,
  ) => CalibrationTreeTargetSummaryPart | CalibrationTreeTargetDetailsPart;
}): void {
  if (!Array.isArray(options.value)) {
    throw new Error(`Calibration tree ${options.label} descriptors must be an array.`);
  }
  let nextTargetOrdinal = 0;
  options.value.forEach((item, shardIndex) => {
    const expectedPart = options.partForIndex(shardIndex);
    const descriptor = validateDescriptor(
      item,
      expectedPart,
      options.country,
      options.buildArtifactId,
    ) as CalibrationTreeTargetSummaryDescriptor | CalibrationTreeTargetDetailsDescriptor;
    if (descriptor.rawBytes > options.maxRawBytes) {
      throw new Error(`Calibration tree descriptor ${expectedPart} exceeds the byte limit.`);
    }
    if (
      descriptor.startTargetOrdinal !== nextTargetOrdinal ||
      !Number.isSafeInteger(descriptor.endTargetOrdinalExclusive) ||
      descriptor.endTargetOrdinalExclusive <= descriptor.startTargetOrdinal ||
      descriptor.endTargetOrdinalExclusive > options.targetCount
    ) {
      throw new Error(`Calibration tree descriptor ${expectedPart} has an invalid target range.`);
    }
    nextTargetOrdinal = descriptor.endTargetOrdinalExclusive;
  });
  if (nextTargetOrdinal !== options.targetCount) {
    throw new Error(
      `Calibration tree ${options.label} descriptors do not cover every target.`,
    );
  }
}

function validateBuild(
  value: unknown,
  country: MicrocosmCountry,
  buildArtifactId: string,
): void {
  const build = record(value, "Calibration tree build");
  if (build.buildArtifactId !== buildArtifactId) {
    throw new Error("Calibration tree build identity does not match its parts.");
  }
  if (!["release", "staging", "comparison"].includes(String(build.kind))) {
    throw new Error("Calibration tree build kind is invalid.");
  }
  for (const key of ["sourceId", "label"] as const) {
    if (typeof build[key] !== "string" || !build[key]) {
      throw new Error(`Calibration tree build ${key} is missing.`);
    }
  }
  if (build.releaseId != null && typeof build.releaseId !== "string") {
    throw new Error("Calibration tree build release id is invalid.");
  }
  if (build.hfRepo != null && (typeof build.hfRepo !== "string" || !build.hfRepo)) {
    throw new Error("Calibration tree build repository is invalid.");
  }
  if (
    build.hfCommitSha != null &&
    (typeof build.hfCommitSha !== "string" || !HF_COMMIT_RE.test(build.hfCommitSha))
  ) {
    throw new Error("Calibration tree build commit is invalid.");
  }
  if (
    build.createdAt != null &&
    (typeof build.createdAt !== "string" || !Number.isFinite(Date.parse(build.createdAt)))
  ) {
    throw new Error("Calibration tree build creation time is invalid.");
  }
  const sources = record(build.sourceArtifacts, "Calibration tree source artifacts");
  for (const key of [
    "calibrationDiagnostics",
    "buildManifest",
    "releaseManifest",
    "demographics",
    "stagingRunManifest",
    "stagingProgress",
    "stagedDatasetReceipt",
    "comparisonCurrentIndex",
    "comparisonCandidateIndex",
  ]) {
    if (sources[key] == null) {
      if (key === "calibrationDiagnostics" && build.kind !== "comparison") {
        throw new Error("Calibration diagnostics source is missing.");
      }
      if (
        build.kind === "comparison" &&
        (key === "comparisonCurrentIndex" ||
          key === "comparisonCandidateIndex")
      ) {
        throw new Error(`Calibration comparison source ${key} is missing.`);
      }
      continue;
    }
    const source = record(sources[key], `Calibration tree source ${key}`);
    if (
      typeof source.path !== "string" ||
      !source.path ||
      source.path.startsWith("/") ||
      source.path.split("/").includes("..")
    ) {
      throw new Error(`Calibration tree source ${key} has an invalid path.`);
    }
    if (typeof source.sha256 !== "string" || !SHA256_RE.test(source.sha256)) {
      throw new Error(`Calibration tree source ${key} has an invalid hash.`);
    }
    if (
      source.hfRepo != null &&
      (typeof source.hfRepo !== "string" || !source.hfRepo)
    ) {
      throw new Error(`Calibration tree source ${key} has an invalid repository.`);
    }
    if (
      source.hfCommitSha != null &&
      (typeof source.hfCommitSha !== "string" || !HF_COMMIT_RE.test(source.hfCommitSha))
    ) {
      throw new Error(`Calibration tree source ${key} has an invalid commit.`);
    }
  }
  if (!isCountry(country)) throw new Error("Calibration tree build country is invalid.");
}

export function parseCalibrationTreeIndex(value: unknown): CalibrationTreeIndexArtifact {
  const index = validateIdentity(value, "index");
  const country = index.country as MicrocosmCountry;
  const buildArtifactId = index.buildArtifactId as string;
  validateBuild(index.build, country, buildArtifactId);
  validateComparisonMetadata(index.targetComparison);
  if (index.comparison != null) {
    const comparison = record(index.comparison, "Calibration tree comparison result");
    for (const key of [
      "pairArtifactId",
      "currentBuildArtifactId",
      "candidateBuildArtifactId",
    ] as const) {
      if (typeof comparison[key] !== "string" || !SHA256_RE.test(comparison[key])) {
        throw new Error(`Calibration tree comparison ${key} is invalid.`);
      }
    }
    if (comparison.mode !== "reported" && comparison.mode !== "shared") {
      throw new Error("Calibration tree comparison mode is invalid.");
    }
    if (typeof comparison.available !== "boolean") {
      throw new Error("Calibration tree comparison availability is invalid.");
    }
  }
  if (typeof index.lossAttributionAvailable !== "boolean") {
    throw new Error("Calibration tree loss-attribution availability must be boolean.");
  }
  if (!Number.isSafeInteger(index.targetCount) || (index.targetCount as number) < 0) {
    throw new Error("Calibration tree target count is invalid.");
  }
  const targetCount = index.targetCount as number;
  if (!Number.isSafeInteger(index.maxDepth) || (index.maxDepth as number) < 0) {
    throw new Error("Calibration tree maximum depth is invalid.");
  }
  const filters = record(index.filterOptions, "Calibration tree filter options");
  for (const key of [
    "geographyLevels",
    "geographies",
    "fitBands",
    "comparisonFits",
    "calibrationStatuses",
  ]) {
    stringArray(filters[key], `Calibration tree filter option ${key}`);
  }
  const levels = validateLevelRecords(
    index.levels,
    index.targetCount as number,
    "Calibration tree root levels",
  );
  const roots = record(index.roots, "Calibration tree roots");
  for (const rootName of ["program", "geography"] as const) {
    if (typeof roots[rootName] !== "string" || !levels[roots[rootName] as string]) {
      throw new Error(`Calibration tree ${rootName} root is invalid.`);
    }
  }
  const parts = record(index.parts, "Calibration tree part descriptors");
  if (!Array.isArray(parts.tiers)) {
    throw new Error("Calibration tree tier descriptors must be an array.");
  }
  const maxDepth = index.maxDepth as number;
  if (parts.tiers.length !== maxDepth) {
    throw new Error("Calibration tree tier descriptors are not contiguous.");
  }
  parts.tiers.forEach((item, tierIndex) => {
    const expectedDepth = tierIndex + 1;
    const expectedPart = `tier-${expectedDepth}` as CalibrationTreeTierPart;
    const descriptor = validateDescriptor(
      item,
      expectedPart,
      country,
      buildArtifactId,
    );
    const tier = descriptor as CalibrationTreeTierDescriptor;
    if (tier.depth !== expectedDepth) {
      throw new Error(`Calibration tree descriptor ${expectedPart} has the wrong depth.`);
    }
    stringArray(tier.levelIds, `Calibration tree descriptor ${expectedPart} levelIds`);
  });
  validateDescriptor(parts.filterIndex, "filter-index", country, buildArtifactId);
  validateTargetShardDescriptors({
    value: parts.targetSummaries,
    targetCount,
    country,
    buildArtifactId,
    label: "target-summary",
    maxRawBytes: CALIBRATION_TREE_TARGET_SUMMARY_MAX_RAW_BYTES,
    partForIndex: targetSummaryPart,
  });
  const expectedDetailStrategy =
    (index.build as CalibrationTreeBuild).kind === "comparison"
      ? "source-targets"
      : "shards";
  if (parts.targetDetailStrategy !== expectedDetailStrategy) {
    throw new Error("Calibration tree target-detail strategy is inconsistent with its build.");
  }
  if (expectedDetailStrategy === "source-targets") {
    if (!Array.isArray(parts.targetDetails) || parts.targetDetails.length !== 0) {
      throw new Error("Comparison trees cannot publish target-detail shards.");
    }
  } else {
    validateTargetShardDescriptors({
      value: parts.targetDetails,
      targetCount,
      country,
      buildArtifactId,
      label: "target-detail",
      maxRawBytes: CALIBRATION_TREE_TARGET_DETAIL_MAX_RAW_BYTES,
      partForIndex: targetDetailsPart,
    });
  }
  return value as CalibrationTreeIndexArtifact;
}

export function parseCalibrationTreeTier(
  value: unknown,
  expectedPart?: CalibrationTreeTierPart,
): CalibrationTreeTierArtifact {
  const tier = validateIdentity(value, expectedPart);
  const match = String(tier.part).match(TIER_PART_RE);
  if (!match) throw new Error("Calibration tree tier part name is invalid.");
  const depth = Number(match[1]);
  if (tier.depth !== depth) throw new Error(`Calibration tree tier-${depth} has the wrong depth.`);
  if (!Number.isSafeInteger(tier.targetCount) || (tier.targetCount as number) < 0) {
    throw new Error(`Calibration tree tier-${depth} has an invalid target count.`);
  }
  validateLevelRecords(
    tier.levels,
    tier.targetCount as number,
    `Calibration tree tier-${depth} levels`,
  );
  return value as CalibrationTreeTierArtifact;
}

function validateMetricInputs(value: unknown, label: string): void {
  const inputs = record(value, label);
  for (const key of [
    "absRelativeError",
    "targetLossWeightShare",
    "finalLossContribution",
    "targetChange",
  ]) {
    if (inputs[key] != null &&
        (typeof inputs[key] !== "number" || !Number.isFinite(inputs[key]))) {
      throw new Error(`${label}.${key} must be a finite number or null.`);
    }
  }
  if (
    inputs.comparisonStatus != null &&
    !["shared", "added", "removed"].includes(String(inputs.comparisonStatus))
  ) {
    throw new Error(`${label}.comparisonStatus is invalid.`);
  }
}

function validateComparisonMetadata(value: unknown): void {
  const comparison = record(value, "Calibration tree comparison metadata");
  if (typeof comparison.releaseId !== "string" || !comparison.releaseId) {
    throw new Error("Calibration tree comparison release id is missing.");
  }
  if (
    !["reported", "exact_reconstructed", "derived", "unavailable"].includes(
      String(comparison.status),
    )
  ) {
    throw new Error("Calibration tree comparison attribution status is invalid.");
  }
  for (const key of ["aggregate", "cap"] as const) {
    if (
      comparison[key] != null &&
      (typeof comparison[key] !== "number" || !Number.isFinite(comparison[key]))
    ) {
      throw new Error(`Calibration tree comparison ${key} is invalid.`);
    }
  }
  if (
    comparison.basisIdentifier != null &&
    typeof comparison.basisIdentifier !== "string"
  ) {
    throw new Error("Calibration tree comparison basis identifier is invalid.");
  }
  if (
    !["legacy", "structured", "hierarchy", "mixed", "unknown"].includes(
      String(comparison.targetRepresentation),
    )
  ) {
    throw new Error("Calibration tree comparison target representation is invalid.");
  }
}

function validatePostings(
  value: unknown,
  targetCount: number,
  label: string,
): void {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array.`);
  const seen = new Set<string>();
  for (const [index, item] of value.entries()) {
    const posting = record(item, `${label}[${index}]`);
    if (posting.value !== null && typeof posting.value !== "string") {
      throw new Error(`${label}[${index}].value must be a string or null.`);
    }
    const key = posting.value === null ? "null" : `string:${posting.value}`;
    if (seen.has(key)) throw new Error(`${label} contains a duplicate value.`);
    seen.add(key);
    validateIndices(posting.targetOrdinals, targetCount, `${label}[${index}].targetOrdinals`);
  }
}

export function parseCalibrationTreeFilterIndex(
  value: unknown,
): CalibrationTreeFilterIndexArtifact {
  const part = validateIdentity(value, "filter-index");
  if (!Number.isSafeInteger(part.targetCount) || (part.targetCount as number) < 0) {
    throw new Error("Calibration tree filter index has an invalid target count.");
  }
  const postings = record(part.postings, "Calibration tree filter postings");
  for (const key of [
    "geographyLevels",
    "geographies",
    "fitBands",
    "comparisonFits",
    "calibrationStatuses",
  ]) {
    validatePostings(
      postings[key],
      part.targetCount as number,
      `Calibration tree filter postings ${key}`,
    );
  }
  return value as CalibrationTreeFilterIndexArtifact;
}

export function parseCalibrationTreeTargetSummary(
  value: unknown,
  expectedPart?: CalibrationTreeTargetSummaryPart,
): CalibrationTreeTargetSummaryArtifact {
  const part = validateIdentity(value, expectedPart);
  if (typeof part.part !== "string" || !TARGET_SUMMARY_PART_RE.test(part.part)) {
    throw new Error("Calibration tree target-summary part name is invalid.");
  }
  if (
    !Number.isSafeInteger(part.startTargetOrdinal) ||
    (part.startTargetOrdinal as number) < 0 ||
    !Number.isSafeInteger(part.endTargetOrdinalExclusive) ||
    (part.endTargetOrdinalExclusive as number) <= (part.startTargetOrdinal as number)
  ) {
    throw new Error("Calibration tree target-summary range is invalid.");
  }
  if (!Array.isArray(part.targets)) {
    throw new Error("Calibration tree target summaries must be an array.");
  }
  if (
    part.targets.length !==
      (part.endTargetOrdinalExclusive as number) - (part.startTargetOrdinal as number)
  ) {
    throw new Error("Calibration tree target-summary count does not match its range.");
  }
  const ids = new Set<string>();
  part.targets.forEach((item, index) => {
    const target = record(item, `Calibration tree target summary ${index}`);
    if (typeof target.id !== "string" || !target.id || typeof target.label !== "string") {
      throw new Error(`Calibration tree target summary ${index} has invalid identity fields.`);
    }
    if (ids.has(target.id)) throw new Error(`Calibration target id ${target.id} is duplicated.`);
    ids.add(target.id);
    validateMetricInputs(target.metricInputs, `Calibration tree target summary ${index}`);
    const comparison = record(
      target.comparison,
      `Calibration tree target summary ${index} comparison`,
    );
    for (const key of [
      "normalizedBaseName",
      "chronicleFactKey",
      "structuredIdentity",
    ] as const) {
      if (comparison[key] != null && typeof comparison[key] !== "string") {
        throw new Error(
          `Calibration tree target summary ${index} comparison ${key} is invalid.`,
        );
      }
    }
    if (!['legacy', 'structured', 'hierarchy'].includes(String(comparison.representation))) {
      throw new Error(
        `Calibration tree target summary ${index} comparison representation is invalid.`,
      );
    }
    record(
      comparison.row,
      `Calibration tree target summary ${index} comparison row`,
    );
    if (target.detailSource != null) {
      const source = record(
        target.detailSource,
        `Calibration tree target summary ${index} detail source`,
      );
      for (const key of ["currentTargetOrdinal", "candidateTargetOrdinal"] as const) {
        if (
          source[key] != null &&
          (!Number.isSafeInteger(source[key]) || (source[key] as number) < 0)
        ) {
          throw new Error(
            `Calibration tree target summary ${index} detail source ${key} is invalid.`,
          );
        }
      }
    }
  });
  return value as CalibrationTreeTargetSummaryArtifact;
}

export function parseCalibrationTreeTargetDetails(
  value: unknown,
  expectedPart?: CalibrationTreeTargetDetailsPart,
): CalibrationTreeTargetDetailsArtifact {
  const part = validateIdentity(value, expectedPart);
  if (typeof part.part !== "string" || !TARGET_DETAILS_PART_RE.test(part.part)) {
    throw new Error("Calibration tree target-detail part name is invalid.");
  }
  if (
    !Number.isSafeInteger(part.startTargetOrdinal) ||
    (part.startTargetOrdinal as number) < 0 ||
    !Number.isSafeInteger(part.endTargetOrdinalExclusive) ||
    (part.endTargetOrdinalExclusive as number) <= (part.startTargetOrdinal as number)
  ) {
    throw new Error("Calibration tree target-detail range is invalid.");
  }
  if (!Array.isArray(part.targets)) {
    throw new Error("Calibration tree target details must be an array.");
  }
  if (
    part.targets.length !==
      (part.endTargetOrdinalExclusive as number) - (part.startTargetOrdinal as number)
  ) {
    throw new Error("Calibration tree target-detail count does not match its range.");
  }
  part.targets.forEach((item, index) => {
    record(item, `Calibration tree target detail ${index}`);
  });
  return value as CalibrationTreeTargetDetailsArtifact;
}

export function parseCalibrationTreeComparisonTargetDetail(
  value: unknown,
): CalibrationTreeComparisonTargetDetailResponse {
  const response = record(value, "Calibration comparison target detail");
  if (response.schemaVersion !== CALIBRATION_TREE_SCHEMA_VERSION) {
    throw new Error(
      `Unsupported calibration tree schema ${String(response.schemaVersion)}.`,
    );
  }
  if (typeof response.country !== "string" || !isCountry(response.country)) {
    throw new Error("Calibration comparison target detail has an unknown country.");
  }
  if (
    typeof response.buildArtifactId !== "string" ||
    !SHA256_RE.test(response.buildArtifactId)
  ) {
    throw new Error("Calibration comparison target detail has an invalid build id.");
  }
  if (!Number.isSafeInteger(response.targetOrdinal) || (response.targetOrdinal as number) < 0) {
    throw new Error("Calibration comparison target detail has an invalid target ordinal.");
  }
  if (typeof response.targetId !== "string" || !response.targetId) {
    throw new Error("Calibration comparison target detail has an invalid target id.");
  }
  record(response.target, "Calibration comparison target detail target");
  return value as CalibrationTreeComparisonTargetDetailResponse;
}

export function parseCalibrationTreePart(
  value: unknown,
  expectedPart?: CalibrationTreePart,
): CalibrationTreeArtifactPart {
  const identity = validateIdentity(value, expectedPart);
  if (identity.part === "index") return parseCalibrationTreeIndex(value);
  if (identity.part === "filter-index") return parseCalibrationTreeFilterIndex(value);
  if (TARGET_SUMMARY_PART_RE.test(String(identity.part))) {
    return parseCalibrationTreeTargetSummary(
      value,
      identity.part as CalibrationTreeTargetSummaryPart,
    );
  }
  if (TARGET_DETAILS_PART_RE.test(String(identity.part))) {
    return parseCalibrationTreeTargetDetails(
      value,
      identity.part as CalibrationTreeTargetDetailsPart,
    );
  }
  return parseCalibrationTreeTier(value, identity.part as CalibrationTreeTierPart);
}

function sameSelection(
  node: CompiledCalibrationNode,
  selection: ExplorerNodeSelection,
): boolean {
  if (node.kind !== selection.kind) return false;
  if (selection.kind === "program") {
    return (
      node.selection.kind === "program" &&
      node.selection.source === selection.source &&
      node.selection.value === selection.value
    );
  }
  if (selection.kind === "dimension_value") {
    return (
      node.selection.kind === "dimension_value" &&
      node.selection.key === selection.key &&
      node.selection.value === selection.value
    );
  }
  return node.selection.value === selection.value;
}

function selectedPath(state: ExplorerState): ExplorerNodeSelection[] {
  const selections: ExplorerNodeSelection[] = [];
  if (state.breakdown === "geography" && state.path.geography) {
    selections.push({ kind: "geography", value: state.path.geography });
  }
  if (state.path.source && state.path.program) {
    selections.push({
      kind: "program",
      source: state.path.source,
      value: state.path.program,
    });
  }
  if (state.breakdown === "program" && state.path.geography) {
    selections.push({ kind: "geography", value: state.path.geography });
  }
  selections.push(
    ...state.path.dimensions.map((dimension) => ({
      kind: "dimension_value" as const,
      key: dimension.key,
      label: dimension.label ?? dimension.key,
      value: dimension.value,
    })),
  );
  return selections;
}

function membershipForFilters(
  filterIndex: CalibrationTreeFilterIndexArtifact,
  filters: ExplorerState["filters"],
): Uint8Array {
  const membership = new Uint8Array(filterIndex.targetCount);
  membership.fill(1);
  for (const [key, selected] of Object.entries(filters) as Array<
    [keyof ExplorerState["filters"], string[]]
  >) {
    if (!selected.length) continue;
    const category = new Uint8Array(filterIndex.targetCount);
    const postings = filterIndex.postings[key] as CalibrationTreePosting[];
    for (const posting of postings) {
      if (posting.value != null && selected.includes(posting.value)) {
        for (const targetOrdinal of posting.targetOrdinals) category[targetOrdinal] = 1;
      }
    }
    for (let index = 0; index < membership.length; index += 1) {
      membership[index] &= category[index];
    }
  }
  return membership;
}

export function calibrationTreeTargetsFromSummaries(
  index: CalibrationTreeIndexArtifact,
  summaries: CalibrationTreeTargetSummaryArtifact[],
): CalibrationTreeTargetSummary[] {
  if (summaries.length !== index.parts.targetSummaries.length) {
    throw new Error("Calibration tree target-summary shards are incomplete.");
  }
  const targets: CalibrationTreeTargetSummary[] = [];
  const ids = new Set<string>();
  summaries.forEach((summary, shardIndex) => {
    const descriptor = index.parts.targetSummaries[shardIndex];
    if (
      !descriptor ||
      summary.part !== descriptor.part ||
      summary.country !== index.country ||
      summary.buildArtifactId !== index.buildArtifactId ||
      summary.startTargetOrdinal !== descriptor.startTargetOrdinal ||
      summary.endTargetOrdinalExclusive !== descriptor.endTargetOrdinalExclusive
    ) {
      throw new Error(
        `Calibration tree target-summary shard ${shardIndex + 1} has an unexpected range or identity.`,
      );
    }
    for (const target of summary.targets) {
      if (ids.has(target.id)) {
        throw new Error(`Calibration target id ${target.id} is duplicated across summary shards.`);
      }
      ids.add(target.id);
      targets.push(target);
    }
  });
  if (targets.length !== index.targetCount) {
    throw new Error("Calibration tree target-summary shards do not match the target count.");
  }
  return targets;
}

function targetDetailForOrdinal(
  shard: CalibrationTreeTargetDetailsArtifact | undefined,
  selected: LoadedCalibrationTreeBundle["selectedTargetDetail"],
  targetOrdinal: number,
): CalibrationTreeTarget | undefined {
  if (selected?.targetOrdinal === targetOrdinal) return selected.target;
  if (
    !shard ||
    targetOrdinal < shard.startTargetOrdinal ||
    targetOrdinal >= shard.endTargetOrdinalExclusive
  ) {
    return undefined;
  }
  return shard.targets[targetOrdinal - shard.startTargetOrdinal];
}

function loadedLevels(bundle: LoadedCalibrationTreeBundle) {
  return Object.assign(
    {},
    bundle.index.levels,
    ...bundle.tiers.map((tier) => tier.levels),
  ) as Record<string, CompiledCalibrationLevel>;
}

export function calibrationTreeResponseFromBundle(
  bundle: LoadedCalibrationTreeBundle,
  state: ExplorerState,
): CalibrationTreeResponse | null {
  const levels = loadedLevels(bundle);
  let levelId = bundle.index.roots[state.breakdown];
  for (const selection of selectedPath(state)) {
    const level = levels[levelId];
    if (!level) return null;
    const node = level.groups
      .flatMap((group) => group.nodes)
      .find((candidate) => sameSelection(candidate, selection));
    if (!node || node.kind === "target") {
      throw new Error(`Calibration tree does not contain the selected ${selection.kind}.`);
    }
    levelId = node.nextLevelId;
  }
  const level = levels[levelId];
  if (!level) return null;

  const hasFilters = Object.values(state.filters).some((values) => values.length > 0);
  if (
    bundle.filterIndex &&
    bundle.filterIndex.targetCount !== bundle.index.targetCount
  ) {
    throw new Error("Calibration tree filter index does not match the target count.");
  }
  const targets = bundle.targetSummaries;
  if (hasFilters && (!bundle.filterIndex || !targets)) return null;
  const membership = bundle.filterIndex
    ? membershipForFilters(bundle.filterIndex, state.filters)
    : null;
  const filterIndices = (indices: number[]) =>
    membership ? indices.filter((index) => membership[index] === 1) : indices;
  const metrics = (indices: number[], fallback: CalibrationTreeMetrics) =>
    hasFilters && targets
      ? calibrationTreeMetricsFromInputs(
          indices.map((index) => targets[index].metricInputs),
        )
      : fallback;

  const groups: CalibrationTreeGroup[] = level.groups.flatMap((group) => {
    const groupIndices = filterIndices(group.targetOrdinals);
    if (!groupIndices.length) return [];
    const nodes: CalibrationTreeNode[] = group.nodes.flatMap((node) => {
      const nodeIndices = filterIndices(node.targetOrdinals);
      if (!nodeIndices.length) return [];
      return [{
        id: node.id,
        label: node.label,
        kind: node.kind,
        selection: node.selection,
        metrics: metrics(nodeIndices, node.metrics),
        target:
          node.kind === "target"
            ? targetDetailForOrdinal(
                bundle.targetDetailShard,
                bundle.selectedTargetDetail,
                node.targetOrdinal,
              )
            : undefined,
        authored_label: node.authoredLabel,
      }];
    });
    if (!nodes.length) return [];
    return [{
      id: group.id,
      label: group.label,
      nodes,
      metrics: metrics(groupIndices, group.metrics),
    }];
  });
  const levelIndices = filterIndices(level.targetOrdinals);

  return {
    releaseId: bundle.index.build.releaseId ?? bundle.index.build.sourceId,
    calibrationProvenance: bundle.index.calibrationProvenance,
    lossAttributionAvailable: bundle.index.lossAttributionAvailable,
    path: state.path,
    pathLabels: level.pathLabels,
    currentLevel: level.currentLevel,
    groups,
    dimensionOrder: level.dimensionOrder,
    filterOptions: bundle.index.filterOptions,
    filteredMetrics: metrics(levelIndices, level.metrics),
  };
}

export function isCalibrationTreePart(value: string): value is CalibrationTreePart {
  return value === "index" ||
    value === "filter-index" ||
    (TARGET_SUMMARY_PART_RE.test(value) && value !== "target-summary-0000") ||
    (TARGET_DETAILS_PART_RE.test(value) && value !== "target-details-0000") ||
    TIER_PART_RE.test(value);
}

export function calibrationTreePartDepth(part: CalibrationTreePart): number | null {
  const match = part.match(TIER_PART_RE);
  return match ? Number(match[1]) : null;
}

export function calibrationTreeBundlePrefix(
  country: MicrocosmCountry,
  buildArtifactId: string,
): string {
  if (!SHA256_RE.test(buildArtifactId)) {
    throw new Error("Invalid calibration build artifact id.");
  }
  return `calibration-trees/${country}/${buildArtifactId}`;
}

export function calibrationTreePartPath(
  country: MicrocosmCountry,
  buildArtifactId: string,
  part: CalibrationTreePart,
): string {
  if (!isCalibrationTreePart(part)) throw new Error("Invalid calibration tree part.");
  return `${calibrationTreeBundlePrefix(country, buildArtifactId)}/${part}.json.gz`;
}

export function isSha256(value: string): boolean {
  return SHA256_RE.test(value);
}

export function isHfCommitSha(value: string): boolean {
  return HF_COMMIT_RE.test(value);
}
