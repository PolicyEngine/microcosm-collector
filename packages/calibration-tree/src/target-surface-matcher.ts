import type { Calibration } from "./latest-artifact";
import type {
  TargetRepresentation,
  TargetRowRepresentation,
} from "./target-representation";

type TargetRow = Calibration["rows"][number];

export type TargetMatchKind =
  | "base_name"
  | "chronicle_fact_key"
  | "structured_identity";
export type TargetSurfaceStatus = "shared" | "added" | "removed";

export interface TargetMatchingCounts {
  base_name: number;
  chronicle_fact_key: number;
  structured_identity: number;
}

export interface TargetMatchingSummary {
  current_representation: TargetRepresentation;
  candidate_representation: TargetRepresentation;
  matched_by: TargetMatchingCounts;
  ambiguous_key_groups: TargetMatchingCounts;
}

export interface TargetSurfaceMatch {
  comparison_id: string;
  comparison_status: TargetSurfaceStatus;
  match_kind: TargetMatchKind | null;
  current: TargetRow | null;
  candidate: TargetRow | null;
  current_name: string | null;
  candidate_name: string | null;
  current_representation: TargetRowRepresentation | null;
  candidate_representation: TargetRowRepresentation | null;
  current_target_ordinal: number | null;
  candidate_target_ordinal: number | null;
}

export interface TargetSurfaceMatchResult {
  matches: TargetSurfaceMatch[];
  matching: TargetMatchingSummary;
}

interface IndexedTarget {
  index: number;
  row: TargetRow;
}

interface MatchStrategy {
  kind: TargetMatchKind;
  key: (row: TargetRow) => string | null;
}

function asObject(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function normalizedTargetName(row: TargetRow): string | null {
  return nonEmptyString(row.base_name) ?? nonEmptyString(row.name);
}

function originalTargetName(row: TargetRow): string | null {
  return nonEmptyString(row.name) ?? nonEmptyString(row.base_name);
}

export function targetRowRepresentation(row: TargetRow): TargetRowRepresentation {
  if (row.target_representation === "hierarchy") return "hierarchy";
  if (row.target_representation === "structured") return "structured";
  if (row.target_representation === "legacy") return "legacy";
  return row.dimension_adapter === "structured" ? "structured" : "legacy";
}

function collectionRepresentation(calibration: Calibration): TargetRepresentation {
  const published = calibration.target_schema?.target_representation;
  if (
    published === "legacy" ||
    published === "structured" ||
    published === "hierarchy" ||
    published === "mixed" ||
    published === "unknown"
  ) {
    return published;
  }
  if (!calibration.rows.length) return "unknown";
  const representations = new Set(calibration.rows.map(targetRowRepresentation));
  if (representations.size > 1) return "mixed";
  if (representations.has("hierarchy")) return "hierarchy";
  return representations.has("structured") ? "structured" : "legacy";
}

export function chronicleFactKey(row: TargetRow): string | null {
  return nonEmptyString(asObject(row.chronicle).fact_key);
}

export function structuredIdentity(row: TargetRow): string | null {
  const representation = targetRowRepresentation(row);
  if (representation !== "structured" && representation !== "hierarchy") {
    return null;
  }
  const source = nonEmptyString(row.source);
  const variable = nonEmptyString(row.variable);
  if (!source || !variable) return null;
  const dimensions = representation === "hierarchy"
    ? (Array.isArray(row.target_dimensions) ? row.target_dimensions : [])
        .flatMap((dimension) => {
          const id = nonEmptyString(dimension.source_key ?? dimension.key);
          const rawValue = nonEmptyString(
            dimension.raw_value ?? dimension.value_id ?? dimension.value,
          );
          return id && rawValue ? [[id, rawValue] as const] : [];
        })
    : Object.entries(asObject(row.dimensions))
        .flatMap(([id, value]) => {
          const rawValue = nonEmptyString(value);
          return id && rawValue ? [[id, rawValue] as const] : [];
        })
        .sort(([left], [right]) => left.localeCompare(right));
  return JSON.stringify({
    source,
    variable,
    measure: nonEmptyString(row.measure),
    dimensions,
  });
}

function groupsByKey(
  rows: Map<number, IndexedTarget>,
  keyOf: MatchStrategy["key"],
): Map<string, IndexedTarget[]> {
  const groups = new Map<string, IndexedTarget[]>();
  for (const indexed of rows.values()) {
    const key = keyOf(indexed.row);
    if (!key) continue;
    const group = groups.get(key) ?? [];
    group.push(indexed);
    groups.set(key, group);
  }
  return groups;
}

function encoded(value: string): string {
  return encodeURIComponent(value);
}

function sharedMatch(
  kind: TargetMatchKind,
  key: string,
  current: IndexedTarget,
  candidate: IndexedTarget,
): TargetSurfaceMatch {
  return {
    comparison_id: `shared:${kind}:${encoded(key)}`,
    comparison_status: "shared",
    match_kind: kind,
    current: current.row,
    candidate: candidate.row,
    current_name: originalTargetName(current.row),
    candidate_name: originalTargetName(candidate.row),
    current_representation: targetRowRepresentation(current.row),
    candidate_representation: targetRowRepresentation(candidate.row),
    current_target_ordinal: current.index,
    candidate_target_ordinal: candidate.index,
  };
}

function sideOnlyMatch(
  side: "current" | "candidate",
  indexed: IndexedTarget,
): TargetSurfaceMatch {
  const row = indexed.row;
  const name = originalTargetName(row);
  return {
    comparison_id: `${side}:${indexed.index}:${encoded(normalizedTargetName(row) ?? name ?? "")}`,
    comparison_status: side === "current" ? "removed" : "added",
    match_kind: null,
    current: side === "current" ? row : null,
    candidate: side === "candidate" ? row : null,
    current_name: side === "current" ? name : null,
    candidate_name: side === "candidate" ? name : null,
    current_representation:
      side === "current" ? targetRowRepresentation(row) : null,
    candidate_representation:
      side === "candidate" ? targetRowRepresentation(row) : null,
    current_target_ordinal: side === "current" ? indexed.index : null,
    candidate_target_ordinal: side === "candidate" ? indexed.index : null,
  };
}

const MATCH_STRATEGIES: MatchStrategy[] = [
  { kind: "base_name", key: normalizedTargetName },
  { kind: "chronicle_fact_key", key: chronicleFactKey },
  { kind: "structured_identity", key: structuredIdentity },
];

function zeroCounts(): TargetMatchingCounts {
  return {
    base_name: 0,
    chronicle_fact_key: 0,
    structured_identity: 0,
  };
}

export function matchTargetSurfaces(
  currentCalibration: Calibration,
  candidateCalibration: Calibration,
): TargetSurfaceMatchResult {
  const current = new Map(
    currentCalibration.rows.map((row, index) => [index, { index, row }]),
  );
  const candidate = new Map(
    candidateCalibration.rows.map((row, index) => [index, { index, row }]),
  );
  const matchedBy = zeroCounts();
  const ambiguousKeyGroups = zeroCounts();
  const matches: TargetSurfaceMatch[] = [];

  for (const strategy of MATCH_STRATEGIES) {
    const currentGroups = groupsByKey(current, strategy.key);
    const candidateGroups = groupsByKey(candidate, strategy.key);
    const keys = [...new Set([...currentGroups.keys(), ...candidateGroups.keys()])]
      .sort((left, right) => left.localeCompare(right));
    for (const key of keys) {
      const currentGroup = currentGroups.get(key) ?? [];
      const candidateGroup = candidateGroups.get(key) ?? [];
      if (!currentGroup.length || !candidateGroup.length) continue;
      if (currentGroup.length !== 1 || candidateGroup.length !== 1) {
        ambiguousKeyGroups[strategy.kind] += 1;
        continue;
      }
      const currentTarget = currentGroup[0];
      const candidateTarget = candidateGroup[0];
      matches.push(sharedMatch(strategy.kind, key, currentTarget, candidateTarget));
      matchedBy[strategy.kind] += 1;
      current.delete(currentTarget.index);
      candidate.delete(candidateTarget.index);
    }
  }

  matches.push(
    ...[...current.values()]
      .sort((left, right) => left.index - right.index)
      .map((row) => sideOnlyMatch("current", row)),
    ...[...candidate.values()]
      .sort((left, right) => left.index - right.index)
      .map((row) => sideOnlyMatch("candidate", row)),
  );

  return {
    matches,
    matching: {
      current_representation: collectionRepresentation(currentCalibration),
      candidate_representation: collectionRepresentation(candidateCalibration),
      matched_by: matchedBy,
      ambiguous_key_groups: ambiguousKeyGroups,
    },
  };
}
