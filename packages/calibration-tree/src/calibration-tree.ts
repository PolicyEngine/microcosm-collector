import type {
  CalibrationStatus,
  ComparisonFit,
  ExplorerNodeSelection,
  ExplorerState,
  FitBand,
} from "./calibration-explorer";
import { COMPARISON_FITS } from "./calibration-explorer";
import { canonicalLabel, programLabel } from "./program-label";
import { sourceLabel } from "./source-label";
import type { CalibrationProvenance } from "./target-loss-attribution";

const DEFAULT_GEOGRAPHY = "United States";
const DEFAULT_GEOGRAPHY_LEVEL = "national";

export interface CalibrationTreeDimension {
  key: string;
  label: string;
  value: string;
  value_id?: string;
  source_key?: string;
  raw_value?: string;
  rank?: number;
}

export interface CalibrationTreeTarget {
  name?: string | null;
  base_name?: string | null;
  comparison_id?: string | null;
  source?: string | null;
  source_label?: string | null;
  variable?: string | null;
  variable_label?: string | null;
  variable_key?: string | null;
  measure?: string | null;
  source_measure_id?: string | null;
  target_label?: string | null;
  target_representation?: string | null;
  level?: string | null;
  geography?: string | null;
  abs_relative_error?: number | null;
  target_loss_weight_share?: number | null;
  final_capped_scaled_error?: number | null;
  final_loss_contribution?: number | null;
  target_change?: number | null;
  comparison_status?: "shared" | "added" | "removed" | null;
  comparison_fit?: ComparisonFit | null;
  calibration_status?: CalibrationStatus | "not_materialized" | null;
  target_dimensions?: CalibrationTreeDimension[] | null;
  [key: string]: unknown;
}

export interface CalibrationTreeChangeMetrics {
  increasedError: number;
  reducedError: number;
  netChange: number;
  changedTargets: number;
  unchangedTargets: number;
  sharedTargets: number;
  addedTargets: number;
  removedTargets: number;
}

export interface CalibrationTreeMetrics {
  nTargets: number;
  scored: number;
  within10Pct: number;
  loss: number;
  targetLossWeightShare: number;
  weightedAverageCappedError: number | null;
  meanAbsRelativeError: number | null;
  medianAbsRelativeError: number | null;
  change?: CalibrationTreeChangeMetrics;
}

export interface CalibrationTreeMetricInput {
  absRelativeError: number | null;
  targetLossWeightShare: number | null;
  finalLossContribution: number | null;
  targetChange: number | null;
  comparisonStatus: "shared" | "added" | "removed" | null;
}

export interface CalibrationTreeNode {
  id: string;
  label: string;
  kind: ExplorerNodeSelection["kind"];
  selection: ExplorerNodeSelection;
  metrics: CalibrationTreeMetrics;
  target?: CalibrationTreeTarget;
  authored_label?: boolean;
}

export interface CalibrationTreeGroup {
  id: string;
  label: string;
  nodes: CalibrationTreeNode[];
  metrics: CalibrationTreeMetrics;
}

export interface CalibrationTreeResponse {
  releaseId?: string;
  calibrationProvenance?: CalibrationProvenance;
  lossAttributionAvailable: boolean;
  path: ExplorerState["path"];
  pathLabels: {
    source?: string;
    program?: string;
    geography?: string;
    dimensions?: string[];
  };
  currentLevel:
    | { kind: "overview"; label: string }
    | { kind: "geography"; label: string }
    | { kind: "dimension"; key: string; label: string }
    | { kind: "mixed"; label: string }
    | { kind: "target"; label: string };
  groups: CalibrationTreeGroup[];
  dimensionOrder: Array<{ key: string; label: string }>;
  filterOptions: {
    geographyLevels: string[];
    geographies: string[];
    fitBands: string[];
    comparisonFits: string[];
    calibrationStatuses: string[];
  };
  filteredMetrics: CalibrationTreeMetrics;
}

export type CalibrationTreeSizeMode = "targets" | "weight" | "loss";

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function finiteError(row: CalibrationTreeTarget): number | null {
  const value = row.abs_relative_error;
  return typeof value === "number" && Number.isFinite(value) ? Math.abs(value) : null;
}

function finiteLossContribution(row: CalibrationTreeTarget): number | null {
  const value = row.final_loss_contribution;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function finiteTargetLossWeightShare(row: CalibrationTreeTarget): number | null {
  const value = row.target_loss_weight_share;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function fitBandForTarget(row: CalibrationTreeTarget): FitBand {
  const error = finiteError(row);
  if (error == null) return "unscored";
  if (error <= 0.05) return "0_5";
  if (error <= 0.1) return "5_10";
  if (error <= 0.2) return "10_20";
  if (error <= 0.4) return "20_40";
  return "40_plus";
}

/**
 * Returns the rows that every selected filter keeps. The Geography filter
 * selects geography labels: pass the labeller of all the rows a tree is built
 * from when `rows` are only some of them, so that a row's label does not
 * depend on which other rows are filtered. Without one, the labeller of
 * `rows` is used.
 */
export function applyExplorerFilters(
  rows: CalibrationTreeTarget[],
  filters: ExplorerState["filters"],
  geographyLabel?: GeographyLabeller,
): CalibrationTreeTarget[] {
  const selectedGeographyLabel = filters.geographies.length
    ? geographyLabel ?? geographyLabeller(rows)
    : null;
  return rows.filter((row) => {
    const geographyLevel = String(row.level ?? "").trim() || DEFAULT_GEOGRAPHY_LEVEL;
    const status =
      row.calibration_status === "not_materialized"
        ? "skipped"
        : row.calibration_status ?? null;
    return (
      (!filters.geographyLevels.length || filters.geographyLevels.includes(geographyLevel)) &&
      (selectedGeographyLabel == null ||
        filters.geographies.includes(selectedGeographyLabel(row))) &&
      (!filters.fitBands.length || filters.fitBands.includes(fitBandForTarget(row))) &&
      (!filters.comparisonFits.length ||
        (row.comparison_fit != null && filters.comparisonFits.includes(row.comparison_fit))) &&
      (!filters.calibrationStatuses.length ||
        (status != null && filters.calibrationStatuses.includes(status)))
    );
  });
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function calibrationTreeMetricsFromInputs(
  inputs: CalibrationTreeMetricInput[],
): CalibrationTreeMetrics {
  const errors = inputs
    .map((input) => input.absRelativeError)
    .filter((value): value is number => value != null);
  const loss = inputs.reduce(
    (sum, input) => sum + (input.finalLossContribution ?? 0),
    0,
  );
  const targetLossWeightShare = inputs.reduce(
    (sum, input) => sum + (input.targetLossWeightShare ?? 0),
    0,
  );
  const changes = inputs
    .map((input) => input.targetChange)
    .filter((value): value is number => value != null);
  const increasedError = changes.reduce((sum, value) => sum + Math.max(value, 0), 0);
  const reducedError = changes.reduce((sum, value) => sum + Math.max(-value, 0), 0);
  const change = changes.length
    ? {
        increasedError,
        reducedError,
        netChange: changes.reduce((sum, value) => sum + value, 0),
        changedTargets: changes.filter((value) => Math.abs(value) > 1e-12).length,
        unchangedTargets: changes.filter((value) => Math.abs(value) <= 1e-12).length,
        sharedTargets: inputs.filter((input) => input.comparisonStatus === "shared").length,
        addedTargets: inputs.filter((input) => input.comparisonStatus === "added").length,
        removedTargets: inputs.filter((input) => input.comparisonStatus === "removed").length,
      }
    : undefined;
  return {
    nTargets: inputs.length,
    scored: errors.length,
    within10Pct: errors.filter((error) => error <= 0.1).length,
    loss,
    targetLossWeightShare,
    weightedAverageCappedError: targetLossWeightShare > 0
      ? loss / targetLossWeightShare
      : null,
    meanAbsRelativeError: errors.length
      ? errors.reduce((sum, error) => sum + error, 0) / errors.length
      : null,
    medianAbsRelativeError: median(errors),
    change,
  };
}

export function calibrationTreeMetrics(
  rows: CalibrationTreeTarget[],
): CalibrationTreeMetrics {
  return calibrationTreeMetricsFromInputs(rows.map((row) => ({
    absRelativeError: finiteError(row),
    targetLossWeightShare: finiteTargetLossWeightShare(row),
    finalLossContribution: finiteLossContribution(row),
    targetChange: finiteNumber(row.target_change),
    comparisonStatus:
      row.comparison_status === "shared" ||
      row.comparison_status === "added" ||
      row.comparison_status === "removed"
        ? row.comparison_status
        : null,
  })));
}

function humanize(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function programId(row: CalibrationTreeTarget): string {
  const variable = String(row.variable ?? "").trim();
  if (variable) return variable;
  const source = String(row.source ?? "").trim();
  const variableKey = String(row.variable_key ?? "").trim();
  if (variableKey) {
    const prefix = source ? `${source} / ` : "";
    return (prefix && variableKey.startsWith(prefix)
      ? variableKey.slice(prefix.length)
      : variableKey
    ).replace(/\s+·\s+(count|total|amount|mean)$/i, "");
  }
  return String(row.name ?? row.base_name ?? "unknown");
}

interface BreakdownDimension {
  key: string;
  label: string;
}

// These concepts are already represented elsewhere in the explorer or were
// explicitly rejected as navigation levels. Match exact semantic keys so a
// legitimate dimension such as `bd_amount_basis` remains explorable.
const NON_NAVIGABLE_BREAKDOWN_DIMENSIONS = new Set([
  "bd_amount",
  "bd_count",
  "bd_geography",
  "bd_geography_level",
  "bd_level",
  "bd_measure",
  "bd_state",
  "bd_state_abbreviation",
]);

function isNavigableBreakdownDimension(key: string): boolean {
  return /^bd_[a-z0-9_]+$/.test(key) && !NON_NAVIGABLE_BREAKDOWN_DIMENSIONS.has(key);
}

function rowDimensionValue(
  row: CalibrationTreeTarget,
  key: string,
): string | null {
  const values = [
    ...new Set(
      (row.target_dimensions ?? [])
        .filter((dimension) => dimension.key === key)
        .map((dimension) => String(dimension.value ?? "").trim())
        .filter(Boolean),
    ),
  ];
  return values.length === 1 ? values[0] : null;
}

export function orderedBreakdownDimensions(
  rows: CalibrationTreeTarget[],
): Array<{ key: string; label: string }> {
  const labelsByKey = new Map<string, Map<string, number>>();
  for (const row of rows) {
    for (const dimension of row.target_dimensions ?? []) {
      if (!isNavigableBreakdownDimension(dimension.key)) continue;
      const labels = labelsByKey.get(dimension.key) ?? new Map<string, number>();
      const label = String(dimension.label ?? "").trim();
      if (label) labels.set(label, (labels.get(label) ?? 0) + 1);
      labelsByKey.set(dimension.key, labels);
    }
  }

  return [...labelsByKey.entries()]
    .map(([key, labels]) => ({
      key,
      label: canonicalLabel(
        [...labels.entries()].sort(
          ([leftLabel, leftCount], [rightLabel, rightCount]) =>
            rightCount - leftCount || leftLabel.localeCompare(rightLabel),
        )[0]?.[0] ?? key.slice(3),
      ),
      coverage: rows.filter((row) => rowDimensionValue(row, key) != null).length,
      distinctValues: new Set(
        rows
          .map((row) => rowDimensionValue(row, key))
          .filter((value): value is string => value != null),
      ).size,
    }))
    .sort((left, right) => {
      return (
        Number(right.distinctValues > 1) - Number(left.distinctValues > 1) ||
        right.coverage - left.coverage ||
        left.distinctValues - right.distinctValues ||
        left.label.localeCompare(right.label) ||
        left.key.localeCompare(right.key)
      );
    })
    .map(({ key, label }) => ({ key, label }));
}

function rowGeographyLabel(row: CalibrationTreeTarget): string {
  return String(row.geography ?? "").trim() || DEFAULT_GEOGRAPHY;
}

// A row names its geography by level and `geography_id`, and spells the
// geography's label in `geography`. Rows from different publishers can spell
// one geography differently (a state's full name on one target, its postal
// code on another), and the reader (validateHierarchyTargets) reports the
// spellings without rewriting the rows. So that the tree lists a geography
// once, every row of a geography ID takes the first spelling of that ID in
// row order, which is the spelling the reader keeps; when the rows spell it
// one way, it is that spelling, so trees built from rows without variants
// do not change. Rows reach the builder after buildCalibration has added the
// level to a label that another geography level also uses
// (levelQualifiedGeographyLabels), so the kept spelling can carry its level,
// such as "Wrexham (local authority)" where the reader reports "Wrexham".
//
// The label is read from every row the tree is built from, before filters,
// because it is also the geography's selection value: a geography must have
// the same label in both breakdowns, at every level and under any filter, as
// in a published tree, which is compiled without filters. Distinct geography
// IDs that share a label still form one node, as before.
//
// The Geography filter uses the same label: it offers one option per node
// label (filterOptions), and an option selects every row with that label
// (applyExplorerFilters), whichever way the row spells the geography. The
// published filter index stores one posting per label (compiledTarget in
// calibration-tree-artifact.ts). A spelling that is not a label, such as
// "ME", selects no row.
//
// Rows without a geography ID (comparison rows carry none) keep their own
// spelling.
export interface GeographyLabeller {
  (row: CalibrationTreeTarget): string;
  /** The labels of all the rows, one per geography node. */
  readonly labels: ReadonlySet<string>;
}

function geographyKey(row: CalibrationTreeTarget): string | null {
  const geographyIdentifier = row.geography_id;
  const trimmedIdentifier =
    typeof geographyIdentifier === "string" ? geographyIdentifier.trim() : "";
  if (!trimmedIdentifier) return null;
  return `${String(row.level ?? "").trim()}\0${trimmedIdentifier}`;
}

// Labels every row in one pass. A compile builds one labeller and every level
// of the tree from the same rows (buildCalibrationTree's
// allRowsGeographyLabel), so the label of each of those rows is stored and
// looked up by the row instead of being worked out again at every level.
export function geographyLabeller(rows: CalibrationTreeTarget[]): GeographyLabeller {
  const firstSpellings = new Map<string, string>();
  const rowLabels = new Map<CalibrationTreeTarget, string>();
  const labels = new Set<string>();
  for (const row of rows) {
    const key = geographyKey(row);
    let label = key == null ? undefined : firstSpellings.get(key);
    if (label === undefined) {
      label = rowGeographyLabel(row);
      if (key != null) firstSpellings.set(key, label);
      labels.add(label);
    }
    rowLabels.set(row, label);
  }
  const labelOf = (row: CalibrationTreeTarget) => {
    const rowLabel = rowLabels.get(row);
    if (rowLabel !== undefined) return rowLabel;
    const key = geographyKey(row);
    return (key == null ? undefined : firstSpellings.get(key)) ??
      rowGeographyLabel(row);
  };
  return Object.assign(labelOf, { labels });
}

function targetLabel(row: CalibrationTreeTarget): string {
  if (row.target_representation === "hierarchy") {
    return String(row.target_label);
  }
  const sourceMeasureId = String(row.source_measure_id ?? "").trim();
  if (sourceMeasureId) return humanize(sourceMeasureId);
  const id = String(row.name ?? row.base_name ?? "Target");
  const leaf = id.replace(/@[^@]+$/, "").split(/[./]/).filter(Boolean).at(-1);
  return humanize(leaf ?? id);
}

interface DimensionPartition {
  dimension: BreakdownDimension;
  rows: CalibrationTreeTarget[];
}

interface HierarchyEntry {
  row: CalibrationTreeTarget;
  cursor: number;
}

interface HierarchyDimensionPartition {
  dimension: BreakdownDimension;
  entries: HierarchyEntry[];
}

function hierarchyDimension(
  entry: HierarchyEntry,
): CalibrationTreeDimension | null {
  return entry.row.target_dimensions?.[entry.cursor] ?? null;
}

function hierarchyDimensionValueId(
  dimension: CalibrationTreeDimension,
): string {
  return String(
    dimension.value_id ?? dimension.raw_value ?? dimension.value,
  ).trim();
}

// The reader (validateHierarchyTargets) keeps the first spelling, in row
// order across the whole file, of a label that rows spell more than one way,
// and reports the others without rewriting the rows, so each row reaches the
// builder with its own spelling. The builder labels a dimension key from the
// rows of the program and geography being built (hierarchyDimensionOrder),
// and a dimension value from the rows of its node (hierarchyValueLabel):
// - when those rows spell it one way, it keeps that spelling, even if the
//   reader keeps another one. Trees built before label variants were
//   accepted did so, and they must rebuild unchanged.
// - otherwise, where the builder used to refuse, it uses the spelling the
//   reader keeps (readerKeptLabels).
interface ReaderKeptLabels {
  dimensions: ReadonlyMap<string, string>;
  values: ReadonlyMap<string, string>;
}

function hierarchyValueKey(key: string, valueId: string): string {
  return `${key}\0${valueId}`;
}

function readerKeptLabels(rows: CalibrationTreeTarget[]): ReaderKeptLabels {
  const dimensions = new Map<string, string>();
  const values = new Map<string, string>();
  for (const row of rows) {
    if (row.target_representation !== "hierarchy") continue;
    for (const dimension of row.target_dimensions ?? []) {
      if (!dimensions.has(dimension.key)) {
        dimensions.set(dimension.key, dimension.label);
      }
      const valueKey = hierarchyValueKey(
        dimension.key,
        hierarchyDimensionValueId(dimension),
      );
      if (!values.has(valueKey)) values.set(valueKey, dimension.value);
    }
  }
  return { dimensions, values };
}

function hierarchyValueLabel(
  entries: HierarchyEntry[],
  keptLabels: () => ReaderKeptLabels,
): string {
  const dimension = hierarchyDimension(entries[0])!;
  const spellings = new Set(
    entries.map((entry) => hierarchyDimension(entry)!.value),
  );
  if (spellings.size === 1) return dimension.value;
  return keptLabels().values.get(
    hierarchyValueKey(dimension.key, hierarchyDimensionValueId(dimension)),
  ) ?? dimension.value;
}

function entriesByValueId(
  entries: HierarchyEntry[],
): Map<string, HierarchyEntry[]> {
  const byValue = new Map<string, HierarchyEntry[]>();
  for (const entry of entries) {
    const valueId = hierarchyDimensionValueId(hierarchyDimension(entry)!);
    const valueEntries = byValue.get(valueId);
    if (valueEntries) valueEntries.push(entry);
    else byValue.set(valueId, [entry]);
  }
  return byValue;
}

function mergeHierarchyPartitions(
  partitions: HierarchyDimensionPartition[],
): HierarchyDimensionPartition[] {
  const merged = new Map<string, HierarchyDimensionPartition>();
  for (const partition of partitions) {
    const existing = merged.get(partition.dimension.key);
    if (existing) {
      existing.entries.push(...partition.entries);
    } else {
      merged.set(partition.dimension.key, {
        dimension: partition.dimension,
        entries: [...partition.entries],
      });
    }
  }
  return [...merged.values()];
}

function hierarchyPartitions(
  entries: HierarchyEntry[],
  dimensionLabels: ReadonlyMap<string, string>,
): {
  dimensions: HierarchyDimensionPartition[];
  targets: HierarchyEntry[];
} {
  const targets: HierarchyEntry[] = [];
  const grouped = new Map<string, HierarchyDimensionPartition>();
  for (const entry of entries) {
    const dimension = hierarchyDimension(entry);
    if (!dimension) {
      targets.push(entry);
      continue;
    }
    const existing = grouped.get(dimension.key);
    if (existing) {
      existing.entries.push(entry);
    } else {
      grouped.set(dimension.key, {
        dimension: {
          key: dimension.key,
          label: dimensionLabels.get(dimension.key) ?? dimension.label,
        },
        entries: [entry],
      });
    }
  }

  const visible: HierarchyDimensionPartition[] = [];
  for (const partition of grouped.values()) {
    const values = new Set(
      partition.entries.map((entry) =>
        hierarchyDimensionValueId(hierarchyDimension(entry)!),
      ),
    );
    if (values.size > 1) {
      visible.push(partition);
      continue;
    }
    const nested = hierarchyPartitions(
      partition.entries.map((entry) => ({ ...entry, cursor: entry.cursor + 1 })),
      dimensionLabels,
    );
    visible.push(...nested.dimensions);
    targets.push(...nested.targets);
  }
  return {
    dimensions: mergeHierarchyPartitions(visible),
    targets,
  };
}

function hierarchyDimensionOrder(
  rows: CalibrationTreeTarget[],
  keptLabels: () => ReaderKeptLabels,
): BreakdownDimension[] {
  const order = new Map<string, BreakdownDimension>();
  const spelledMoreThanOneWay = new Set<string>();
  for (const row of rows) {
    for (const dimension of row.target_dimensions ?? []) {
      const existing = order.get(dimension.key);
      if (!existing) {
        order.set(dimension.key, {
          key: dimension.key,
          label: dimension.label,
        });
      } else if (existing.label !== dimension.label) {
        spelledMoreThanOneWay.add(dimension.key);
      }
    }
  }
  for (const key of spelledMoreThanOneWay) {
    const dimension = order.get(key)!;
    dimension.label = keptLabels().dimensions.get(key) ?? dimension.label;
  }
  return [...order.values()];
}

function partitionRowsByDimension(
  rows: CalibrationTreeTarget[],
  selectedKeys: Set<string>,
): { dimensions: DimensionPartition[]; targets: CalibrationTreeTarget[] } {
  const remaining = new Set(rows);
  const rankedDimensions = orderedBreakdownDimensions(rows).filter(
    (dimension) => !selectedKeys.has(dimension.key),
  );
  const displayRank = new Map(
    rankedDimensions.map((dimension, index) => [dimension.key, index]),
  );
  const candidates = rankedDimensions.flatMap((dimension) => {
    const eligibleRows = rows.filter(
      (row) => rowDimensionValue(row, dimension.key) != null,
    );
    const values = new Set(
      eligibleRows
        .map((row) => rowDimensionValue(row, dimension.key))
        .filter((value): value is string => value != null),
    );
    return values.size > 1 ? [{ dimension, rows: eligibleRows }] : [];
  });

  // Allocate narrower sparse dimensions first so a broader overlapping
  // dimension cannot consume every one of their rows. Display order remains
  // independently ranked by branch coverage and cardinality.
  const partitionsByKey = new Map<string, DimensionPartition>();
  for (const candidate of [...candidates].sort(
    (left, right) =>
      left.rows.length - right.rows.length ||
      (displayRank.get(left.dimension.key) ?? 0) -
        (displayRank.get(right.dimension.key) ?? 0),
  )) {
    const assigned = candidate.rows.filter((row) => remaining.has(row));
    const values = new Set(
      assigned
        .map((row) => rowDimensionValue(row, candidate.dimension.key))
        .filter((value): value is string => value != null),
    );
    if (values.size <= 1) continue;
    partitionsByKey.set(candidate.dimension.key, {
      dimension: candidate.dimension,
      rows: assigned,
    });
    for (const row of assigned) remaining.delete(row);
  }

  return {
    dimensions: rankedDimensions.flatMap((dimension) => {
      const partition = partitionsByKey.get(dimension.key);
      return partition ? [partition] : [];
    }),
    targets: [...remaining],
  };
}

function groupRows(
  rows: CalibrationTreeTarget[],
  keyOf: (row: CalibrationTreeTarget) => string,
): Map<string, CalibrationTreeTarget[]> {
  const groups = new Map<string, CalibrationTreeTarget[]>();
  for (const row of rows) {
    const key = keyOf(row);
    const group = groups.get(key) ?? [];
    group.push(row);
    groups.set(key, group);
  }
  return groups;
}

function node(
  id: string,
  label: string,
  kind: CalibrationTreeNode["kind"],
  selection: ExplorerNodeSelection,
  rows: CalibrationTreeTarget[],
  target?: CalibrationTreeTarget,
  authoredLabel = false,
): CalibrationTreeNode {
  return {
    id,
    label,
    kind,
    selection,
    metrics: calibrationTreeMetrics(rows),
    target,
    authored_label: authoredLabel || undefined,
  };
}

function sortNodes(nodes: CalibrationTreeNode[]): CalibrationTreeNode[] {
  return nodes.sort(
    (left, right) =>
      right.metrics.nTargets - left.metrics.nTargets ||
      left.label.localeCompare(right.label),
  );
}

function filterOptions(
  rows: CalibrationTreeTarget[],
  geographyLabel: GeographyLabeller,
) {
  const unique = (values: Iterable<string>) => [...new Set(values)].sort((left, right) =>
    left.localeCompare(right),
  );
  return {
    geographyLevels: unique(rows.map(
      (row) => String(row.level ?? "").trim() || DEFAULT_GEOGRAPHY_LEVEL,
    )),
    geographies: unique(geographyLabel.labels),
    fitBands: ["0_5", "5_10", "10_20", "20_40", "40_plus", "unscored"],
    comparisonFits: rows.some((row) => row.comparison_fit != null)
      ? [...COMPARISON_FITS]
      : [],
    calibrationStatuses: ["included", "skipped"],
  };
}

function normalizeChartCalibrationStatus(
  row: CalibrationTreeTarget,
): CalibrationTreeTarget {
  if (row.calibration_status !== "not_materialized") return row;
  return {
    ...row,
    calibration_status: "skipped",
    calibration_status_label: "Skipped",
  };
}

export function normalizeCalibrationTreeTargets(
  rows: CalibrationTreeTarget[],
): CalibrationTreeTarget[] {
  return rows.map(normalizeChartCalibrationStatus);
}

function programGroups(rows: CalibrationTreeTarget[]): CalibrationTreeGroup[] {
  const bySource = groupRows(
    rows,
    (row) => String(row.source ?? "other") || "other",
  );
  return [...bySource.entries()]
    .map(([source, sourceRows]) => {
      const artifactSourceLabel = sourceRows
        .map((row) => row.source_label?.trim())
        .find((label): label is string => Boolean(label));
      const byProgram = groupRows(sourceRows, programId);
      const nodes = sortNodes(
        [...byProgram.entries()].map(([program, programRows]) => {
          const labels = [
            ...new Set(
              programRows
                .map((row) => row.variable_label?.trim())
                .filter((label): label is string => Boolean(label)),
            ),
          ];
          return node(
            program,
            programLabel(program, labels.length === 1 ? labels[0] : null),
            "program",
            { kind: "program", source, value: program },
            programRows,
          );
        }),
      );
      return {
        id: source,
        label: artifactSourceLabel ?? sourceLabel(source),
        nodes,
        metrics: calibrationTreeMetrics(sourceRows),
      };
    })
    .sort(
      (left, right) =>
        right.metrics.nTargets - left.metrics.nTargets ||
        left.id.localeCompare(right.id),
    );
}

function pathLabels(
  rows: CalibrationTreeTarget[],
  path: ExplorerState["path"],
  geographyLabel: GeographyLabeller,
): CalibrationTreeResponse["pathLabels"] {
  const labels: CalibrationTreeResponse["pathLabels"] = {};
  if (path.geography) {
    const geographyLabels = [
      ...new Set(
        rows
          .filter(
            (row) =>
              row.target_representation === "hierarchy" &&
              Boolean(row.geography?.trim()) &&
              geographyLabel(row) === path.geography,
          )
          .map(geographyLabel),
      ),
    ];
    if (geographyLabels.length === 1) labels.geography = geographyLabels[0];
  }
  if (!path.source) return labels;
  const sourceRows = rows.filter(
    (row) => String(row.source ?? "other") === path.source,
  );
  const artifactSourceLabel = sourceRows
    .map((row) => row.source_label?.trim())
    .find((label): label is string => Boolean(label));
  labels.source = artifactSourceLabel ?? sourceLabel(path.source);
  if (!path.program) return labels;
  const programRows = sourceRows.filter((row) => programId(row) === path.program);
  const artifactProgramLabels = [
    ...new Set(
      programRows
        .map((row) => row.variable_label?.trim())
        .filter((label): label is string => Boolean(label)),
    ),
  ];
  labels.program = programLabel(
    path.program,
    artifactProgramLabels.length === 1 ? artifactProgramLabels[0] : null,
  );
  return labels;
}

function geographyNodes(
  rows: CalibrationTreeTarget[],
  geographyLabel: GeographyLabeller,
): CalibrationTreeNode[] {
  const byGeography = groupRows(rows, geographyLabel);
  return sortNodes(
    [...byGeography.entries()].map(([geography, geographyRows]) =>
      node(
        geography,
        geography,
        "geography",
        { kind: "geography", value: geography },
        geographyRows,
      ),
    ),
  );
}

function hierarchyDimensionTree(
  allRows: CalibrationTreeTarget[],
  geographyRows: CalibrationTreeTarget[],
  state: ExplorerState,
  releaseId: string | undefined,
  lossAttributionAvailable: boolean,
  calibrationProvenance: CalibrationProvenance | undefined,
  selectedPathLabels: CalibrationTreeResponse["pathLabels"],
  options: CalibrationTreeResponse["filterOptions"],
  geographyLabel: GeographyLabeller,
): CalibrationTreeResponse {
  // Only rows that spell a label more than one way need the reader's
  // spellings, so they are read from all rows only then.
  let labelsKeptByReader: ReaderKeptLabels | undefined;
  const keptLabels = () => (labelsKeptByReader ??= readerKeptLabels(allRows));
  const dimensionOrder = hierarchyDimensionOrder(geographyRows, keptLabels);
  const dimensionLabels = new Map(
    dimensionOrder.map(({ key, label }) => [key, label]),
  );
  let entries: HierarchyEntry[] = geographyRows.map((row) => ({
    row,
    cursor: 0,
  }));
  const selectedValueLabels: string[] = [];
  for (const selection of state.path.dimensions) {
    const partition = hierarchyPartitions(entries, dimensionLabels).dimensions.find(
      ({ dimension }) => dimension.key === selection.key,
    );
    if (!partition) {
      entries = [];
      break;
    }
    const matching = partition.entries.filter((entry) => {
      const dimension = hierarchyDimension(entry);
      return (
        dimension != null &&
        hierarchyDimensionValueId(dimension) === selection.value
      );
    });
    selectedValueLabels.push(
      matching.length
        ? hierarchyValueLabel(matching, keptLabels)
        : selection.value,
    );
    entries = matching.map((entry) => ({
      ...entry,
      cursor: entry.cursor + 1,
    }));
  }

  const scopedRows = entries.map((entry) => entry.row);
  const filteredScopedRows = applyExplorerFilters(
    scopedRows,
    state.filters,
    geographyLabel,
  );
  const visibleRows = new Set(filteredScopedRows);
  const partition = hierarchyPartitions(entries, dimensionLabels);
  const groups: CalibrationTreeGroup[] = partition.dimensions.flatMap(
    ({ dimension, entries: dimensionEntries }) => {
      const visibleEntries = dimensionEntries.filter((entry) =>
        visibleRows.has(entry.row),
      );
      if (!visibleEntries.length) return [];
      // A value is labelled from all of its rows, not only the visible ones,
      // so filters do not change its label: its selected path and the
      // published tree, which is compiled without filters, use all its rows.
      const allEntriesByValue = entriesByValueId(dimensionEntries);
      const byValue = entriesByValueId(visibleEntries);
      const nodes = sortNodes(
        [...byValue.entries()].map(([valueId, valueEntries]) =>
          node(
            valueId,
            hierarchyValueLabel(allEntriesByValue.get(valueId)!, keptLabels),
            "dimension_value",
            {
              kind: "dimension_value",
              key: dimension.key,
              label: dimension.label,
              value: valueId,
            },
            valueEntries.map((entry) => entry.row),
            undefined,
            true,
          ),
        ),
      );
      return [{
        id: dimension.key,
        label: dimension.label,
        nodes,
        metrics: calibrationTreeMetrics(visibleEntries.map((entry) => entry.row)),
      }];
    },
  );

  const visibleTargets = partition.targets
    .map((entry) => entry.row)
    .filter((row) => visibleRows.has(row));
  const targetNodes = visibleTargets
    .map((row, index) => {
      const id = String(
        row.comparison_id ?? row.name ?? row.base_name ?? `target-${index}`,
      );
      return node(
        id,
        targetLabel(row),
        "target",
        { kind: "target", value: id },
        [row],
        row,
      );
    })
    .sort(
      (left, right) =>
        left.label.localeCompare(right.label) || left.id.localeCompare(right.id),
    );
  if (targetNodes.length) {
    groups.push({
      id: "targets",
      label: "Targets",
      nodes: targetNodes,
      metrics: calibrationTreeMetrics(visibleTargets),
    });
  }

  const currentLevel =
    partition.dimensions.length === 1 && partition.targets.length === 0
      ? { kind: "dimension" as const, ...partition.dimensions[0].dimension }
      : partition.dimensions.length === 0
        ? { kind: "target" as const, label: "Targets" }
        : { kind: "mixed" as const, label: "Breakdowns and targets" };
  return {
    releaseId,
    calibrationProvenance,
    lossAttributionAvailable,
    path: state.path,
    pathLabels: {
      ...selectedPathLabels,
      dimensions: selectedValueLabels,
    },
    currentLevel,
    groups,
    dimensionOrder,
    filterOptions: options,
    filteredMetrics: calibrationTreeMetrics(filteredScopedRows),
  };
}

/**
 * Builds one level of the calibration tree from all of its rows.
 *
 * `allRowsGeographyLabel` is `geographyLabeller(allRows)`, for a caller that
 * builds many levels from the same rows (compileCalibrationTreeBundleDraft):
 * it is then built once rather than at every level. Without it, the labeller
 * of `allRows` is built here.
 */
export function buildCalibrationTree(
  allRows: CalibrationTreeTarget[],
  state: ExplorerState,
  releaseId?: string,
  lossAttributionAvailable =
    allRows.length > 0 && allRows.every((row) => finiteLossContribution(row) != null),
  calibrationProvenance?: CalibrationProvenance,
  allRowsGeographyLabel?: GeographyLabeller,
): CalibrationTreeResponse {
  const chartRows = normalizeCalibrationTreeTargets(allRows);
  const { path } = state;
  const geographyLabel = allRowsGeographyLabel ?? geographyLabeller(chartRows);
  const selectedPathLabels = pathLabels(chartRows, path, geographyLabel);
  const options = filterOptions(chartRows, geographyLabel);
  const filteredRows = applyExplorerFilters(
    chartRows,
    state.filters,
    geographyLabel,
  );

  if (state.breakdown === "geography" && !path.geography) {
    const nodes = geographyNodes(filteredRows, geographyLabel);
    return {
      releaseId,
      calibrationProvenance,
      lossAttributionAvailable,
      path,
      pathLabels: selectedPathLabels,
      currentLevel: { kind: "geography", label: "Geography" },
      groups: [{
        id: "geography",
        label: "Geography",
        nodes,
        metrics: calibrationTreeMetrics(filteredRows),
      }],
      dimensionOrder: [],
      filterOptions: options,
      filteredMetrics: calibrationTreeMetrics(filteredRows),
    };
  }

  if (state.breakdown === "geography" && (!path.source || !path.program)) {
    const geographyRows = chartRows.filter(
      (row) => geographyLabel(row) === path.geography,
    );
    const filteredGeographyRows = applyExplorerFilters(
      geographyRows,
      state.filters,
      geographyLabel,
    );
    return {
      releaseId,
      calibrationProvenance,
      lossAttributionAvailable,
      path,
      pathLabels: selectedPathLabels,
      currentLevel: { kind: "overview", label: "Programs" },
      groups: programGroups(filteredGeographyRows),
      dimensionOrder: [],
      filterOptions: options,
      filteredMetrics: calibrationTreeMetrics(filteredGeographyRows),
    };
  }

  if (!path.source || !path.program) {
    return {
      releaseId,
      calibrationProvenance,
      lossAttributionAvailable,
      path,
      pathLabels: selectedPathLabels,
      currentLevel: { kind: "overview", label: "Programs" },
      groups: programGroups(filteredRows),
      dimensionOrder: [],
      filterOptions: options,
      filteredMetrics: calibrationTreeMetrics(filteredRows),
    };
  }

  const programRows = chartRows.filter(
    (row) =>
      String(row.source ?? "other") === path.source &&
      programId(row) === path.program,
  );
  const filteredProgramRows = applyExplorerFilters(
    programRows,
    state.filters,
    geographyLabel,
  );
  if (!path.geography) {
    const nodes = geographyNodes(filteredProgramRows, geographyLabel);
    return {
      releaseId,
      calibrationProvenance,
      lossAttributionAvailable,
      path,
      pathLabels: selectedPathLabels,
      currentLevel: { kind: "geography", label: "Geography" },
      groups: [{
        id: path.program,
        label: selectedPathLabels.program ?? programLabel(path.program),
        nodes,
        metrics: calibrationTreeMetrics(filteredProgramRows),
      }],
      dimensionOrder: [],
      filterOptions: options,
      filteredMetrics: calibrationTreeMetrics(filteredProgramRows),
    };
  }

  const geographyRows = path.geography
    ? programRows.filter((row) => geographyLabel(row) === path.geography)
    : programRows;
  if (
    geographyRows.length > 0 &&
    geographyRows.every((row) => row.target_representation === "hierarchy")
  ) {
    return hierarchyDimensionTree(
      chartRows,
      geographyRows,
      state,
      releaseId,
      lossAttributionAvailable,
      calibrationProvenance,
      selectedPathLabels,
      options,
      geographyLabel,
    );
  }
  const dimensionOrder = orderedBreakdownDimensions(geographyRows);
  let scopedRows = geographyRows;
  const selectedKeys = new Set<string>();
  for (const selection of path.dimensions) {
    const selectedPartition = partitionRowsByDimension(scopedRows, selectedKeys).dimensions.find(
      ({ dimension }) => dimension.key === selection.key,
    );
    if (!selectedPartition) {
      scopedRows = [];
      break;
    }
    scopedRows = selectedPartition.rows.filter(
      (row) => rowDimensionValue(row, selection.key) === selection.value,
    );
    selectedKeys.add(selection.key);
  }
  const filteredScopedRows = applyExplorerFilters(
    scopedRows,
    state.filters,
    geographyLabel,
  );
  const visibleRows = new Set(filteredScopedRows);
  const partition = partitionRowsByDimension(scopedRows, selectedKeys);

  const groups: CalibrationTreeGroup[] = partition.dimensions.flatMap(
    ({ dimension, rows: dimensionRows }) => {
      const visibleDimensionRows = dimensionRows.filter((row) => visibleRows.has(row));
      if (!visibleDimensionRows.length) return [];
      const byValue = groupRows(
        visibleDimensionRows,
        (row) => rowDimensionValue(row, dimension.key) ?? "",
      );
      const nodes = sortNodes(
        [...byValue.entries()].map(([value, rows]) =>
          node(
            value,
            value,
            "dimension_value",
            {
              kind: "dimension_value",
              key: dimension.key,
              label: dimension.label,
              value,
            },
            rows,
          ),
        ),
      );
      return [{
        id: dimension.key,
        label: dimension.label,
        nodes,
        metrics: calibrationTreeMetrics(visibleDimensionRows),
      }];
    },
  );

  const visibleTargets = partition.targets.filter((row) => visibleRows.has(row));
  const targetNodes = visibleTargets
    .map((row, index) => {
      const id = String(row.comparison_id ?? row.name ?? row.base_name ?? `target-${index}`);
      return node(
        id,
        targetLabel(row),
        "target",
        { kind: "target", value: id },
        [row],
        row,
      );
    })
    .sort((left, right) => left.label.localeCompare(right.label) || left.id.localeCompare(right.id));
  if (targetNodes.length) {
    groups.push({
      id: "targets",
      label: "Targets",
      nodes: targetNodes,
      metrics: calibrationTreeMetrics(visibleTargets),
    });
  }

  const currentLevel =
    partition.dimensions.length === 1 && partition.targets.length === 0
      ? { kind: "dimension" as const, ...partition.dimensions[0].dimension }
      : partition.dimensions.length === 0
        ? { kind: "target" as const, label: "Targets" }
        : { kind: "mixed" as const, label: "Breakdowns and targets" };
  return {
    releaseId,
    calibrationProvenance,
    lossAttributionAvailable,
    path,
    pathLabels: selectedPathLabels,
    currentLevel,
    groups,
    dimensionOrder,
    filterOptions: options,
    filteredMetrics: calibrationTreeMetrics(filteredScopedRows),
  };
}

export function effectiveNodeMetric(
  nodes: CalibrationTreeNode[],
  mode: CalibrationTreeSizeMode,
): number[] {
  const values = nodes.map((item) => {
    if (mode === "targets") return item.metrics.nTargets;
    if (mode === "weight") return item.metrics.targetLossWeightShare;
    return item.metrics.loss;
  });
  return values.some((value) => value > 0)
    ? values
    : nodes.map((item) => item.metrics.nTargets);
}
