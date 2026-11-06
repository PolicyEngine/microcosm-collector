import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";

import {
  CALIBRATION_TREE_SCHEMA_VERSION,
  CALIBRATION_TREE_TARGET_DETAIL_MAX_RAW_BYTES,
  CALIBRATION_TREE_TARGET_SUMMARY_MAX_RAW_BYTES,
  calibrationTreeFilterIndexFromDraft,
  calibrationTreePartPath,
  calibrationTreeTargetDetailsFromDraft,
  calibrationTreeTargetSummariesFromDraft,
  calibrationTreeTiersFromDraft,
  compileCalibrationTreeBundleDraft,
  parseCalibrationTreeIndex,
  parseCalibrationTreePart,
  serializeCalibrationTreePart,
  type CalibrationTreeArtifactPart,
  type CalibrationTreeFilterIndexArtifact,
  type CalibrationTreeIndexArtifact,
  type CalibrationTreePart,
  type CalibrationTreePartDescriptor,
  type CalibrationTreeTargetDetailsArtifact,
  type CalibrationTreeTargetDetailsDescriptor,
  type CalibrationTreeTargetSummaryArtifact,
  type CalibrationTreeTargetSummaryDescriptor,
  type CalibrationTreeTierArtifact,
  type CalibrationTreeTierDescriptor,
  type CompileCalibrationTreeInput,
  type CompiledCalibrationLevel,
} from "./calibration-tree-artifact";
import {
  fitBandForTarget,
  geographyLabeller,
  type CalibrationTreeTarget,
} from "./calibration-tree";

export interface CalibrationTreeBundleFile {
  part: CalibrationTreePart;
  path: string;
  artifact: CalibrationTreeArtifactPart;
  serialized: string;
  compressed: Uint8Array;
  sha256: string;
  rawBytes: number;
  gzipBytes: number;
}

export interface CalibrationTreeBundle {
  index: CalibrationTreeIndexArtifact;
  files: CalibrationTreeBundleFile[];
}

export function calibrationTreeBuildArtifactId(
  input: CompileCalibrationTreeInput,
): string {
  const sourceHashes = Object.fromEntries(
    Object.entries(input.sourceArtifacts)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([name, artifact]) => [
        name,
        artifact == null
          ? null
          : {
              sha256: artifact.sha256,
              ...(artifact.hfRepo ? { hfRepo: artifact.hfRepo } : {}),
              ...(artifact.hfCommitSha
                ? { hfCommitSha: artifact.hfCommitSha }
                : {}),
            },
      ]),
  );
  return sha256(JSON.stringify({
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    country: input.country,
    kind: input.buildKind ?? "release",
    sourceId: input.sourceId ?? input.releaseId,
    sourceHashes,
  }));
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function describePart(artifact: CalibrationTreeArtifactPart): CalibrationTreeBundleFile {
  const serialized = serializeCalibrationTreePart(artifact);
  const compressed = gzipSync(serialized, { level: 9 });
  return {
    part: artifact.part,
    path: calibrationTreePartPath(
      artifact.country,
      artifact.buildArtifactId,
      artifact.part,
    ),
    artifact,
    serialized,
    compressed,
    sha256: sha256(serialized),
    rawBytes: Buffer.byteLength(serialized, "utf8"),
    gzipBytes: compressed.byteLength,
  };
}

function descriptor(file: CalibrationTreeBundleFile): CalibrationTreePartDescriptor {
  if (file.part === "index") throw new Error("The bundle index cannot describe itself.");
  return {
    part: file.part,
    path: file.path,
    sha256: file.sha256,
    rawBytes: file.rawBytes,
    gzipBytes: file.gzipBytes,
  };
}

export function buildCalibrationTreeBundle(
  input: CompileCalibrationTreeInput,
): CalibrationTreeBundle {
  const draft = compileCalibrationTreeBundleDraft({
    ...input,
    buildArtifactId:
      input.buildArtifactId ?? calibrationTreeBuildArtifactId(input),
  });
  const targetDetails = input.buildKind === "comparison"
    ? { artifacts: [] }
    : calibrationTreeTargetDetailsFromDraft(draft);
  const targetDetailsFiles = targetDetails.artifacts.map(describePart);
  const targetSummaries = calibrationTreeTargetSummariesFromDraft(draft);
  const targetSummaryFiles = targetSummaries.artifacts.map(describePart);
  const filterIndexFile = describePart(calibrationTreeFilterIndexFromDraft(draft));
  const tierFiles = calibrationTreeTiersFromDraft(draft).map(describePart);
  const rootLevels = Object.fromEntries(
    Object.entries(draft.levels).filter(
      ([levelId]) => draft.levelDepths[levelId] === 0,
    ),
  );
  const tierDescriptors = tierFiles.map((file, index): CalibrationTreeTierDescriptor => ({
    ...descriptor(file),
    part: file.part as `tier-${number}`,
    depth: index + 1,
    levelIds: Object.keys(
      (file.artifact as CalibrationTreeTierArtifact).levels,
    ).sort(),
  }));
  const index: CalibrationTreeIndexArtifact = {
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    country: draft.country,
    buildArtifactId: draft.build.buildArtifactId,
    part: "index",
    build: draft.build,
    targetComparison: draft.comparison,
    comparison: draft.comparisonResult,
    calibrationProvenance: draft.calibrationProvenance,
    lossAttributionAvailable: draft.lossAttributionAvailable,
    filterOptions: draft.filterOptions,
    targetCount: draft.targets.length,
    maxDepth: tierFiles.length,
    roots: draft.roots,
    levels: rootLevels,
    parts: {
      tiers: tierDescriptors,
      filterIndex: {
        ...descriptor(filterIndexFile),
        part: "filter-index",
      },
      targetSummaries: targetSummaryFiles.map((file, shardIndex) => ({
        ...descriptor(file),
        part: file.part as CalibrationTreeTargetSummaryDescriptor["part"],
        startTargetOrdinal:
          targetSummaries.artifacts[shardIndex].startTargetOrdinal,
        endTargetOrdinalExclusive:
          targetSummaries.artifacts[shardIndex].endTargetOrdinalExclusive,
      })),
      targetDetailStrategy:
        input.buildKind === "comparison" ? "source-targets" : "shards",
      targetDetails: targetDetailsFiles.map((file, shardIndex) => ({
        ...descriptor(file),
        part: file.part as CalibrationTreeTargetDetailsDescriptor["part"],
        startTargetOrdinal: targetDetails.artifacts[shardIndex].startTargetOrdinal,
        endTargetOrdinalExclusive:
          targetDetails.artifacts[shardIndex].endTargetOrdinalExclusive,
      })),
    },
  };
  const indexFile = describePart(index);
  const bundle = {
    index,
    files: [
      filterIndexFile,
      ...targetSummaryFiles,
      ...targetDetailsFiles,
      ...tierFiles,
      indexFile,
    ],
  };
  validateCalibrationTreeBundle(bundle);
  return bundle;
}

function sameIndices(left: number[], right: number[]): boolean {
  return left.length === right.length &&
    left.every((value, index) => value === right[index]);
}

type FilterPostingKey = keyof CalibrationTreeFilterIndexArtifact["postings"];

function expectedFacet(
  row: CalibrationTreeTarget,
  key: Exclude<FilterPostingKey, "geographies">,
): string | null {
  if (key === "geographyLevels") return String(row.level ?? "").trim() || "national";
  if (key === "fitBands") return fitBandForTarget(row);
  if (key === "comparisonFits") return row.comparison_fit ?? null;
  if (row.calibration_status === "included") return "included";
  if (row.calibration_status === "skipped" || row.calibration_status === "not_materialized") {
    return "skipped";
  }
  return null;
}

// The target ordinals of each facet value, with rows in target order.
function rowPostings(
  rows: CalibrationTreeTarget[],
  facetValue: (row: CalibrationTreeTarget) => string | null,
): Map<string | null, number[]> {
  const postings = new Map<string | null, number[]>();
  rows.forEach((row, targetOrdinal) => {
    const value = facetValue(row);
    const targetOrdinals = postings.get(value) ?? [];
    targetOrdinals.push(targetOrdinal);
    postings.set(value, targetOrdinals);
  });
  return postings;
}

function samePostings(
  actual: Map<string | null, number[]>,
  expected: Map<string | null, number[]>,
): boolean {
  return actual.size === expected.size &&
    [...expected].every(([value, targetOrdinals]) =>
      sameIndices(actual.get(value) ?? [], targetOrdinals),
    );
}

// The Geography filter offers and selects the labels of the tree's geography
// nodes, so each geography posting must hold exactly the targets of the
// geography root's node with that label. The compact target rows cannot give
// those postings: they carry each row's own spelling and no geography ID, and
// a node groups every spelling of one geography ID (geographyLabeller in
// calibration-tree.ts). Trees published before nodes were grouped by
// geography ID grouped both their nodes and their postings by each row's own
// spelling, so they meet this condition too.
//
// The node and the posting come from the same labeller, so they are also
// checked against the target rows (geographyPostingsFollowRows).
function geographyNodePostings(
  bundle: CalibrationTreeBundle,
): Map<string | null, number[]> {
  const root = bundle.index.levels[bundle.index.roots.geography];
  if (!root) throw new Error("Calibration tree geography root is missing.");
  const postings = new Map<string | null, number[]>();
  for (const treeNode of root.groups.flatMap((group) => group.nodes)) {
    if (
      treeNode.selection.kind !== "geography" ||
      postings.has(treeNode.selection.value)
    ) {
      throw new Error("Calibration tree geography root must list each geography once.");
    }
    postings.set(treeNode.selection.value, treeNode.targetOrdinals);
  }
  return postings;
}

// Each geography posting must hold the targets of one label worked out from
// the target rows. A tree built with nodes grouped by geography ID has one
// posting per label that geographyLabeller gives its target detail rows,
// which carry the geography IDs; a comparison tree has no detail rows, and
// its compact rows carry no geography ID, so each row keeps its own
// spelling. A tree published before nodes were grouped by geography ID has
// one posting per row's own spelling. Where every geography ID is spelled
// one way, both rules give the same postings.
function geographyPostingsFollowRows(
  actual: Map<string | null, number[]>,
  summaryRows: CalibrationTreeTarget[],
  detailRows: CalibrationTreeTarget[],
): boolean {
  const labelledRows = detailRows.length ? detailRows : summaryRows;
  return (
    samePostings(actual, rowPostings(labelledRows, geographyLabeller(labelledRows))) ||
    samePostings(
      actual,
      rowPostings(
        summaryRows,
        (row) => String(row.geography ?? "").trim() || "United States",
      ),
    )
  );
}

function validatePostingCoverage(
  bundle: CalibrationTreeBundle,
  filterIndexFile: CalibrationTreeBundleFile,
  targetSummaryFiles: CalibrationTreeBundleFile[],
  targetDetailsFiles: CalibrationTreeBundleFile[],
): void {
  const filterIndex = filterIndexFile.artifact;
  const targetSummaries = targetSummaryFiles.map((file) => file.artifact);
  const targetDetails = targetDetailsFiles.map((file) => file.artifact);
  if (
    filterIndex.part !== "filter-index" ||
    targetSummaries.some((artifact) => !artifact.part.startsWith("target-summary-")) ||
    targetDetails.some((artifact) => !artifact.part.startsWith("target-details-"))
  ) {
    throw new Error("Calibration tree target files have incorrect part identities.");
  }
  const detailRows = targetDetails.flatMap(
    (artifact) => (artifact as CalibrationTreeTargetDetailsArtifact).targets,
  );
  const summaryTargets = targetSummaries.flatMap(
    (artifact) => (artifact as CalibrationTreeTargetSummaryArtifact).targets,
  );
  const summaryRows = summaryTargets.map((target) => target.comparison.row);
  if (
    filterIndex.targetCount !== bundle.index.targetCount ||
    summaryTargets.length !== bundle.index.targetCount ||
    (bundle.index.parts.targetDetailStrategy === "shards" &&
      detailRows.length !== bundle.index.targetCount) ||
    (bundle.index.parts.targetDetailStrategy === "source-targets" &&
      detailRows.length !== 0)
  ) {
    throw new Error("Calibration tree target files do not match the declared target count.");
  }
  const ids = new Set(summaryTargets.map((target) => target.id));
  if (ids.size !== summaryTargets.length) {
    throw new Error("Calibration tree target summaries repeat a target id.");
  }
  if (
    bundle.index.parts.targetDetailStrategy === "source-targets" &&
    summaryTargets.some((target) => target.detailSource == null)
  ) {
    throw new Error("Comparison target summaries must identify their source targets.");
  }
  for (const key of [
    "geographyLevels",
    "geographies",
    "fitBands",
    "comparisonFits",
    "calibrationStatuses",
  ] as const) {
    const actual = new Map<string | null, number[]>(
      (filterIndex as CalibrationTreeFilterIndexArtifact).postings[key].map(
        (posting) => [posting.value, posting.targetOrdinals],
      ),
    );
    const expected = key === "geographies"
      ? geographyNodePostings(bundle)
      : rowPostings(summaryRows, (row) => expectedFacet(row, key));
    if (actual.size !== expected.size) {
      throw new Error(`Calibration tree ${key} postings do not cover every facet value.`);
    }
    for (const [value, targetOrdinals] of expected) {
      if (!sameIndices(actual.get(value) ?? [], targetOrdinals)) {
        throw new Error(`Calibration tree ${key} posting ${String(value)} is incorrect.`);
      }
    }
    if (
      key === "geographies" &&
      !geographyPostingsFollowRows(actual, summaryRows, detailRows)
    ) {
      throw new Error("Calibration tree geographies postings do not follow the target rows.");
    }
  }
}

function allLevels(bundle: CalibrationTreeBundle): Record<string, CompiledCalibrationLevel> {
  return Object.assign(
    {},
    bundle.index.levels,
    ...bundle.files.flatMap((file) =>
      file.artifact.part.startsWith("tier-")
        ? [(file.artifact as CalibrationTreeTierArtifact).levels]
        : [],
    ),
  );
}

function validateGraph(bundle: CalibrationTreeBundle): void {
  const levels = allLevels(bundle);
  const expectedTargetOrdinals = Array.from(
    { length: bundle.index.targetCount },
    (_, targetOrdinal) => targetOrdinal,
  );
  for (const rootName of ["program", "geography"] as const) {
    const root = levels[bundle.index.roots[rootName]];
    if (!root || !sameIndices(root.targetOrdinals, expectedTargetOrdinals)) {
      throw new Error(`Calibration tree ${rootName} root does not cover every target.`);
    }
  }

  const depths = new Map<string, number>();
  const queue = [
    { levelId: bundle.index.roots.program, depth: 0 },
    { levelId: bundle.index.roots.geography, depth: 0 },
  ];
  while (queue.length) {
    const { levelId, depth } = queue.shift()!;
    const previousDepth = depths.get(levelId);
    if (previousDepth != null) {
      if (previousDepth !== depth) {
        throw new Error(`Calibration tree level ${levelId} appears at multiple depths.`);
      }
      continue;
    }
    const level = levels[levelId];
    if (!level) throw new Error(`Calibration tree level ${levelId} is missing.`);
    depths.set(levelId, depth);
    for (const node of level.groups.flatMap((group) => group.nodes)) {
      if (node.kind === "target") continue;
      const child = levels[node.nextLevelId];
      if (!child) throw new Error(`Calibration tree level ${node.nextLevelId} is missing.`);
      if (!sameIndices(node.targetOrdinals, child.targetOrdinals)) {
        throw new Error(`Calibration tree branch ${node.id} has inconsistent membership.`);
      }
      queue.push({ levelId: node.nextLevelId, depth: depth + 1 });
    }
  }
  if (depths.size !== Object.keys(levels).length) {
    throw new Error("Calibration tree contains an unreachable level.");
  }

  const expectedDepths = new Map<string, number>();
  Object.keys(bundle.index.levels).forEach((levelId) => expectedDepths.set(levelId, 0));
  for (const descriptor of bundle.index.parts.tiers) {
    descriptor.levelIds.forEach((levelId) => expectedDepths.set(levelId, descriptor.depth));
  }
  if (expectedDepths.size !== Object.keys(levels).length) {
    throw new Error("Calibration tree tier descriptors omit or duplicate levels.");
  }
  for (const [levelId, depth] of depths) {
    if (expectedDepths.get(levelId) !== depth) {
      throw new Error(`Calibration tree level ${levelId} is stored in the wrong tier.`);
    }
  }
}

export function validateCalibrationTreeBundle(bundle: CalibrationTreeBundle): void {
  parseCalibrationTreeIndex(bundle.index);
  const expectedParts: CalibrationTreePart[] = [
    "filter-index",
    ...bundle.index.parts.targetSummaries.map((descriptor) => descriptor.part),
    ...bundle.index.parts.targetDetails.map((descriptor) => descriptor.part),
    ...bundle.index.parts.tiers.map((tier) => tier.part),
    "index",
  ];
  if (
    bundle.files.length !== expectedParts.length ||
    bundle.files.some((file, index) => file.part !== expectedParts[index])
  ) {
    throw new Error("Calibration tree bundle has missing, additional, or misordered files.");
  }
  const seenPaths = new Set<string>();
  for (const file of bundle.files) {
    if (seenPaths.has(file.path)) {
      throw new Error(`Calibration tree bundle repeats ${file.path}.`);
    }
    seenPaths.add(file.path);
    const expectedPath = calibrationTreePartPath(
      bundle.index.country,
      bundle.index.buildArtifactId,
      file.part,
    );
    if (file.path !== expectedPath) {
      throw new Error(`Calibration tree part ${file.part} has the wrong path.`);
    }
    if (serializeCalibrationTreePart(file.artifact) !== file.serialized) {
      throw new Error(`Calibration tree part ${file.part} artifact and bytes differ.`);
    }
    const parsed = parseCalibrationTreePart(JSON.parse(file.serialized), file.part);
    if (
      parsed.country !== bundle.index.country ||
      parsed.buildArtifactId !== bundle.index.buildArtifactId
    ) {
      throw new Error(`Calibration tree part ${file.part} has inconsistent identity.`);
    }
    if (
      sha256(file.serialized) !== file.sha256 ||
      Buffer.byteLength(file.serialized, "utf8") !== file.rawBytes ||
      gzipSync(file.serialized, { level: 9 }).byteLength !== file.gzipBytes ||
      !Buffer.from(file.compressed).equals(gzipSync(file.serialized, { level: 9 }))
    ) {
      throw new Error(`Calibration tree part ${file.part} has incorrect content metadata.`);
    }
    if (
      file.part.startsWith("target-summary-") &&
      file.rawBytes > CALIBRATION_TREE_TARGET_SUMMARY_MAX_RAW_BYTES
    ) {
      throw new Error(`Calibration tree part ${file.part} exceeds the target-summary limit.`);
    }
    if (
      file.part.startsWith("target-details-") &&
      file.rawBytes > CALIBRATION_TREE_TARGET_DETAIL_MAX_RAW_BYTES
    ) {
      throw new Error(`Calibration tree part ${file.part} exceeds the target-detail limit.`);
    }
  }
  const fileByPart = new Map(bundle.files.map((file) => [file.part, file]));
  if (serializeCalibrationTreePart(bundle.index) !== fileByPart.get("index")?.serialized) {
    throw new Error("Calibration tree bundle index does not match index.json.gz.");
  }
  for (const partDescriptor of [
    ...bundle.index.parts.tiers,
    bundle.index.parts.filterIndex,
    ...bundle.index.parts.targetSummaries,
    ...bundle.index.parts.targetDetails,
  ]) {
    const file = fileByPart.get(partDescriptor.part);
    if (
      !file ||
      file.path !== partDescriptor.path ||
      file.sha256 !== partDescriptor.sha256 ||
      file.rawBytes !== partDescriptor.rawBytes ||
      file.gzipBytes !== partDescriptor.gzipBytes
    ) {
      throw new Error(`Calibration tree descriptor ${partDescriptor.part} is incorrect.`);
    }
    if (
      partDescriptor.part.startsWith("target-summary-") ||
      partDescriptor.part.startsWith("target-details-")
    ) {
      const artifact = file.artifact as
        | CalibrationTreeTargetSummaryArtifact
        | CalibrationTreeTargetDetailsArtifact;
      const rangeDescriptor = partDescriptor as
        | CalibrationTreeTargetSummaryDescriptor
        | CalibrationTreeTargetDetailsDescriptor;
      if (
        artifact.startTargetOrdinal !== rangeDescriptor.startTargetOrdinal ||
        artifact.endTargetOrdinalExclusive !== rangeDescriptor.endTargetOrdinalExclusive
      ) {
        throw new Error(
          `Calibration tree descriptor ${partDescriptor.part} has an inconsistent target range.`,
        );
      }
    }
  }
  const filterIndexFile = fileByPart.get("filter-index")!;
  const targetSummaryFiles = bundle.index.parts.targetSummaries.map(
    (descriptor) => fileByPart.get(descriptor.part)!,
  );
  const targetDetailsFiles = bundle.index.parts.targetDetails.map(
    (descriptor) => fileByPart.get(descriptor.part)!,
  );
  validatePostingCoverage(
    bundle,
    filterIndexFile,
    targetSummaryFiles,
    targetDetailsFiles,
  );
  validateGraph(bundle);
}

export function calibrationTreeBundleFile(
  bundle: CalibrationTreeBundle,
  part: CalibrationTreePart,
): CalibrationTreeBundleFile {
  const file = bundle.files.find((candidate) => candidate.part === part);
  if (!file) throw new Error(`Calibration tree bundle is missing ${part}.`);
  return file;
}
