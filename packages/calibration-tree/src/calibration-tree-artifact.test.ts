import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { gunzipSync, gzipSync } from "node:zlib";

import {
  calibrationTreeTargetDetailsFromDraft,
  calibrationTreeResponseFromBundle,
  calibrationTreeTargetDetailSelection,
  calibrationTreeTargetsFromSummaries,
  calibrationTreeTargetSummariesFromDraft,
  compileCalibrationTreeBundleDraft,
  parseCalibrationTreeIndex,
  parseCalibrationTreePart,
  serializeCalibrationTreePart,
  type CalibrationTreeArtifactPart,
  type CalibrationTreeFilterIndexArtifact,
  type CalibrationTreePosting,
  type CalibrationTreeTargetDetailsArtifact,
  type CalibrationTreeTargetSummaryArtifact,
  type CalibrationTreeTierArtifact,
} from "./calibration-tree-artifact";
import {
  buildCalibrationTreeBundle,
  validateCalibrationTreeBundle,
  type CalibrationTreeBundle,
} from "./calibration-tree-bundle";
import {
  createExplorerState,
  selectExplorerNode,
  type ExplorerState,
} from "./calibration-explorer";
import {
  buildCalibrationTree,
  type CalibrationTreeTarget,
} from "./calibration-tree";
import maineRelease from "./fixtures/us-766b7c7-maine-targets.json";

const COMMIT = "1234567890abcdef1234567890abcdef12345678";
const SOURCE_HASH = "a".repeat(64);
const RELEASE = "microcosm-us-test-20260915";

const rows: CalibrationTreeTarget[] = [
  ["snap-total", "United States", "national", "All", 0.02, "included"],
  ["snap-ca-children", "California", "state", "Children", 0.12, "included"],
  ["snap-ca-adults", "California", "state", "Adults", 0.3, "not_materialized"],
  ["ctc-total", "United States", "national", "All", 0.04, "included"],
].map(([id, geography, level, householdType, error, status]) => ({
  comparison_id: id as string,
  name: id as string,
  target_label: id as string,
  target_representation: "hierarchy",
  source: "chronicle",
  source_label: "Chronicle",
  variable: String(id).startsWith("ctc") ? "ctc" : "snap",
  variable_label: String(id).startsWith("ctc") ? "Child Tax Credit" : "SNAP",
  geography: geography as string,
  level: level as string,
  abs_relative_error: error as number,
  final_loss_contribution: (error as number) / 10,
  target_loss_weight_share: 0.25,
  calibration_status: status as "included" | "not_materialized",
  target_dimensions: householdType === "All"
    ? []
    : [{
        key: "household_type",
        label: "Household type",
        value: householdType as string,
        value_id: String(householdType).toLowerCase(),
      }],
}));

function bundle(bundleRows = rows): CalibrationTreeBundle {
  return buildCalibrationTreeBundle({
    country: "us",
    releaseId: RELEASE,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: COMMIT,
    sourceArtifacts: {
      calibrationDiagnostics: {
        path: `releases/${RELEASE}/calibration_diagnostics.json`,
        sha256: SOURCE_HASH,
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    rows: bundleRows,
    lossAttributionAvailable: true,
  });
}

function loaded(built = bundle()) {
  return {
    index: built.index,
    tiers: built.files.flatMap((file) =>
      file.part.startsWith("tier-")
        ? [file.artifact as CalibrationTreeTierArtifact]
        : [],
    ),
    filterIndex: built.files.find((file) => file.part === "filter-index")!
      .artifact as CalibrationTreeFilterIndexArtifact,
    targetSummaries: calibrationTreeTargetsFromSummaries(
      built.index,
      built.files.flatMap((file) =>
        file.part.startsWith("target-summary-")
          ? [file.artifact as CalibrationTreeTargetSummaryArtifact]
          : [],
      ),
    ),
    targetDetailShard: built.files.find((file) =>
      file.part.startsWith("target-details-"),
    )!
      .artifact as CalibrationTreeTargetDetailsArtifact,
  };
}

test("partitioned bundle returns the same response at every navigable tree level", () => {
  const compiled = loaded();
  const queue: ExplorerState[] = [
    createExplorerState(),
    { ...createExplorerState(), breakdown: "geography" },
  ];
  const visited = new Set<string>();

  while (queue.length) {
    const state = queue.shift()!;
    const key = JSON.stringify({ breakdown: state.breakdown, path: state.path });
    if (visited.has(key)) continue;
    visited.add(key);
    const direct = buildCalibrationTree(rows, state, RELEASE, true);
    expect(calibrationTreeResponseFromBundle(compiled, state)).toEqual(direct);
    for (const node of direct.groups.flatMap((group) => group.nodes)) {
      if (node.kind !== "target") queue.push(selectExplorerNode(state, node.selection));
    }
  }

  expect(visited.size).toBeGreaterThan(4);
});

test("posting-list filters union within categories and intersect across them", () => {
  const state: ExplorerState = {
    ...createExplorerState(),
    filters: {
      geographyLevels: ["state"],
      geographies: ["California", "New York"],
      fitBands: ["10_20", "20_40"],
      comparisonFits: [],
      calibrationStatuses: ["included"],
    },
  };
  expect(calibrationTreeResponseFromBundle(loaded(), state)).toEqual(
    buildCalibrationTree(rows, state, RELEASE, true),
  );
});

// Release populace-us-2024-buildo-acs-local-766b7c7-20261017T030317Z names
// state 0400000US23 "Maine" on its SNAP and Medicaid targets and "ME" on its
// later state population target.
test("published and live trees list a geography ID that rows spell two ways as one node", () => {
  function maineTarget(
    name: string,
    source: string,
    geography: string,
    error: number,
  ): CalibrationTreeTarget {
    return {
      comparison_id: name,
      name,
      target_label: name,
      target_representation: "hierarchy",
      source,
      source_label: source,
      variable: `${source}.total`,
      variable_label: source,
      geography,
      geography_id: "0400000US23",
      level: "state",
      abs_relative_error: error,
      final_loss_contribution: error / 10,
      target_loss_weight_share: 0.25,
      calibration_status: "included",
      target_dimensions: [],
    };
  }
  const maineRows = [
    maineTarget("usda_snap.fy2024.state_benefits.nero.me.total_benefits@2024", "usda_snap", "Maine", 0.02),
    maineTarget(
      "cms_medicaid.month2024_12.state_enrollment.me.total_medicaid_enrollment@2024",
      "cms_medicaid",
      "Maine",
      0.03,
    ),
    maineTarget("pop_state_23@2024", "census_population", "ME", 0.5),
  ];
  const compiled = loaded(bundle(maineRows));
  const unfiltered: ExplorerState = { ...createExplorerState(), breakdown: "geography" };
  const poorlyFitOnly: ExplorerState = {
    ...unfiltered,
    filters: { ...unfiltered.filters, fitBands: ["40_plus"] },
  };

  const geographyNodes = (state: ExplorerState) => {
    const published = calibrationTreeResponseFromBundle(compiled, state);
    expect(published).toEqual(buildCalibrationTree(maineRows, state, RELEASE, true));
    return published!.groups
      .flatMap((group) => group.nodes)
      .map((treeNode) => [treeNode.label, treeNode.metrics.nTargets]);
  };
  expect(geographyNodes(unfiltered)).toEqual([["Maine", 3]]);
  expect(geographyNodes(poorlyFitOnly)).toEqual([["Maine", 1]]);

  const maine = selectExplorerNode(unfiltered, { kind: "geography", value: "Maine" });
  for (const programState of [maine, { ...maine, filters: poorlyFitOnly.filters }]) {
    const programs = calibrationTreeResponseFromBundle(compiled, programState);
    expect(programs).toEqual(buildCalibrationTree(maineRows, programState, RELEASE, true));
    expect(programs!.pathLabels.geography).toBe("Maine");
  }
  expect(
    calibrationTreeResponseFromBundle(compiled, maine)!.groups.map((group) => group.id),
  ).toEqual(["census_population", "cms_medicaid", "usda_snap"]);
});

// Every row of state 0400000US23 and of Maine's two congressional districts in
// release populace-us-2024-buildo-acs-local-766b7c7-20261017T030317Z, in
// release order (the fixture records how it was made). 84 of the state's 85
// targets spell it "Maine"; its state population target spells it "ME".
const MAINE_RELEASE_ROWS = maineRelease.rows as unknown as CalibrationTreeTarget[];

// Replaces one part of a built bundle and rewrites the digest and sizes that
// its descriptor and the index record, as a builder that wrote the part would.
function withReplacedPart(
  built: CalibrationTreeBundle,
  artifact: CalibrationTreeArtifactPart,
): CalibrationTreeBundle {
  const content = (part: CalibrationTreeArtifactPart) => {
    const serialized = serializeCalibrationTreePart(part);
    const compressed = gzipSync(serialized, { level: 9 });
    return {
      serialized,
      compressed,
      sha256: createHash("sha256").update(serialized, "utf8").digest("hex"),
      rawBytes: Buffer.byteLength(serialized, "utf8"),
      gzipBytes: compressed.byteLength,
    };
  };
  const replaced = structuredClone(built);
  const partContent = content(artifact);
  Object.assign(
    replaced.files.find((file) => file.part === artifact.part)!,
    { artifact, ...partContent },
  );
  const { parts } = replaced.index;
  Object.assign(
    [
      parts.filterIndex,
      ...parts.targetSummaries,
      ...parts.targetDetails,
      ...parts.tiers,
    ].find((descriptor) => descriptor.part === artifact.part)!,
    {
      sha256: partContent.sha256,
      rawBytes: partContent.rawBytes,
      gzipBytes: partContent.gzipBytes,
    },
  );
  Object.assign(
    replaced.files.find((file) => file.part === "index")!,
    { artifact: replaced.index, ...content(replaced.index) },
  );
  return replaced;
}

function withGeographyPostings(
  built: CalibrationTreeBundle,
  geographies: CalibrationTreePosting[],
): CalibrationTreeBundle {
  const filterIndex = built.files.find((file) => file.part === "filter-index")!
    .artifact as CalibrationTreeFilterIndexArtifact;
  return withReplacedPart(built, {
    ...filterIndex,
    postings: { ...filterIndex.postings, geographies },
  });
}

test("published and live Geography filters offer one option per geography and select all of its targets", () => {
  const built = bundle(MAINE_RELEASE_ROWS);
  const compiled = loaded(built);
  const geographyRoot: ExplorerState = {
    ...createExplorerState(),
    breakdown: "geography",
  };
  const filteredBy = (
    treeState: ExplorerState,
    geographies: string[],
  ): ExplorerState => ({
    ...treeState,
    filters: { ...treeState.filters, geographies },
  });

  const geographies = [
    "Maine",
    "ME congressional district 1",
    "ME congressional district 2",
  ];
  expect(built.index.filterOptions.geographies).toEqual(geographies);
  expect(
    buildCalibrationTree(MAINE_RELEASE_ROWS, geographyRoot, RELEASE, true)
      .filterOptions.geographies,
  ).toEqual(geographies);
  expect(
    compiled.filterIndex.postings.geographies.map((posting) => [
      posting.value,
      posting.targetOrdinals.length,
    ]),
  ).toEqual([
    ["Maine", 85],
    ["ME congressional district 1", 1],
    ["ME congressional district 2", 1],
  ]);

  const maine = selectExplorerNode(geographyRoot, {
    kind: "geography",
    value: "Maine",
  });
  const population = selectExplorerNode(createExplorerState(), {
    kind: "program",
    source: "census_population",
    value: "census_population.resident_population",
  });
  for (const treeState of [geographyRoot, maine, population]) {
    const filtered = filteredBy(treeState, ["Maine"]);
    expect(calibrationTreeResponseFromBundle(compiled, filtered)).toEqual(
      buildCalibrationTree(MAINE_RELEASE_ROWS, filtered, RELEASE, true),
    );
  }
  expect(
    calibrationTreeResponseFromBundle(compiled, filteredBy(geographyRoot, ["Maine"]))!
      .groups.flatMap((group) => group.nodes)
      .map((treeNode) => [treeNode.label, treeNode.metrics.nTargets]),
  ).toEqual([["Maine", 85]]);

  // The program breakdown lists the same programs and counts; the published
  // reader keeps the compiled node order under a filter.
  const programs = filteredBy(createExplorerState(), ["Maine"]);
  const programCounts = (response: ReturnType<typeof buildCalibrationTree>) =>
    response.groups
      .map((group) => [group.id, group.metrics.nTargets] as const)
      .sort(([left], [right]) => left.localeCompare(right));
  const publishedPrograms = calibrationTreeResponseFromBundle(compiled, programs)!;
  const livePrograms = buildCalibrationTree(MAINE_RELEASE_ROWS, programs, RELEASE, true);
  expect(programCounts(publishedPrograms)).toEqual(programCounts(livePrograms));
  expect(publishedPrograms.filteredMetrics.nTargets).toBe(85);
  expect(livePrograms.filteredMetrics.nTargets).toBe(85);

  // A spelling that is not an option selects no target in either tree.
  const spelledME = filteredBy(geographyRoot, ["ME"]);
  for (const response of [
    calibrationTreeResponseFromBundle(compiled, spelledME)!,
    buildCalibrationTree(MAINE_RELEASE_ROWS, spelledME, RELEASE, true),
  ]) {
    expect(response.groups.flatMap((group) => group.nodes)).toEqual([]);
    expect(response.filteredMetrics.nTargets).toBe(0);
  }
});

test("bundle validation requires one geography posting per geography node", () => {
  const built = bundle(MAINE_RELEASE_ROWS);
  const geographyRoot = built.index.levels[built.index.roots.geography];
  const nodePostings = geographyRoot.groups
    .flatMap((group) => group.nodes)
    .map((treeNode) => ({
      value: treeNode.selection.value,
      targetOrdinals: treeNode.targetOrdinals,
    }))
    .sort((left, right) => left.value.localeCompare(right.value));
  expect(nodePostings.map((posting) => [posting.value, posting.targetOrdinals.length]))
    .toEqual([
      ["Maine", 85],
      ["ME congressional district 1", 1],
      ["ME congressional district 2", 1],
    ]);
  expect(() =>
    validateCalibrationTreeBundle(withGeographyPostings(built, nodePostings)),
  ).not.toThrow();

  // Postings by each row's own spelling split the Maine node: the option
  // "Maine" would select 84 of its 85 targets.
  const spellingPostings = new Map<string, number[]>();
  MAINE_RELEASE_ROWS.forEach((row, targetOrdinal) => {
    const spelling = String(row.geography);
    spellingPostings.set(spelling, [
      ...(spellingPostings.get(spelling) ?? []),
      targetOrdinal,
    ]);
  });
  expect(() =>
    validateCalibrationTreeBundle(
      withGeographyPostings(
        built,
        [...spellingPostings]
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([value, targetOrdinals]) => ({ value, targetOrdinals })),
      ),
    ),
  ).toThrow(/geographies posting/);
});

test("bundle validation still accepts a tree published with a node per spelling", () => {
  // Earlier builders ignored geography IDs: they grouped geography nodes and
  // postings by each row's own spelling, and stored the rows, IDs included,
  // as target details. Five published UK staging trees spell a geography ID
  // two ways and were built that way. Building from the rows without their
  // IDs gives the same nodes and postings; the IDs are then put back into
  // the target details, as those trees store them.
  const rowsWithoutIds = MAINE_RELEASE_ROWS.map(
    ({ geography_id: _geographyId, ...row }) => row,
  );
  let published = bundle(rowsWithoutIds);
  expect(
    loaded(published).filterIndex.postings.geographies.map((posting) => [
      posting.value,
      posting.targetOrdinals.length,
    ]),
  ).toEqual([
    ["Maine", 84],
    ["ME", 1],
    ["ME congressional district 1", 1],
    ["ME congressional district 2", 1],
  ]);
  for (const file of published.files) {
    if (!file.part.startsWith("target-details-")) continue;
    const details = file.artifact as CalibrationTreeTargetDetailsArtifact;
    published = withReplacedPart(published, {
      ...details,
      targets: details.targets.map((row, offset) => ({
        ...row,
        geography_id:
          MAINE_RELEASE_ROWS[details.startTargetOrdinal + offset].geography_id,
      })),
    });
  }

  expect(() => validateCalibrationTreeBundle(published)).not.toThrow();
});

test("bundle validation refuses geography nodes and postings that agree with each other but not with the target rows", () => {
  const built = bundle(MAINE_RELEASE_ROWS);
  // Renames one geography in the geography root's node and in its posting
  // alike, as a builder that labelled a geography wrongly would write both.
  const withGeographyRenamed = (from: string, to: string) => {
    const renamed = structuredClone(built);
    const geographyRoot = renamed.index.levels[renamed.index.roots.geography];
    const treeNode = geographyRoot.groups
      .flatMap((group) => group.nodes)
      .find((candidate) => candidate.selection.value === from)!;
    treeNode.label = to;
    treeNode.selection = { kind: "geography", value: to };
    return withGeographyPostings(
      renamed,
      loaded(built).filterIndex.postings.geographies
        .map((posting) => posting.value === from ? { ...posting, value: to } : posting)
        .sort((left, right) => left.value.localeCompare(right.value)),
    );
  };

  expect(() =>
    validateCalibrationTreeBundle(withGeographyRenamed("Maine", "Maine")),
  ).not.toThrow();
  // A label that no row spells.
  expect(() =>
    validateCalibrationTreeBundle(
      withGeographyRenamed("ME congressional district 1", "Kennebec"),
    ),
  ).toThrow(/geographies postings do not follow the target rows/);
  // The state's other spelling for all 85 targets: the rows spell it "Maine"
  // first, and only the population target spells it "ME".
  expect(() =>
    validateCalibrationTreeBundle(withGeographyRenamed("Maine", "ME")),
  ).toThrow(/geographies postings do not follow the target rows/);
});

test("a compile reads each row's geography ID once, however many levels the tree has", () => {
  // A compile builds every level of the tree from the same rows, so it labels
  // each row's geography once instead of once per level: the largest
  // published trees have more than 12,000 levels.
  const geographyIdReads = MAINE_RELEASE_ROWS.map(() => 0);
  const countedRows = MAINE_RELEASE_ROWS.map(
    ({ geography_id: geographyId, ...row }, rowIndex) =>
      Object.defineProperty({ ...row }, "geography_id", {
        enumerable: true,
        get() {
          geographyIdReads[rowIndex] += 1;
          return geographyId;
        },
      }) as CalibrationTreeTarget,
  );

  const draft = compileCalibrationTreeBundleDraft({
    country: "us",
    releaseId: RELEASE,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: COMMIT,
    buildArtifactId: SOURCE_HASH,
    sourceArtifacts: {
      calibrationDiagnostics: null,
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    rows: countedRows,
    lossAttributionAvailable: true,
  });

  expect(Object.keys(draft.levels).length).toBeGreaterThan(50);
  expect(geographyIdReads).toEqual(MAINE_RELEASE_ROWS.map(() => 1));
});

test("the index renders without later parts and deeper navigation waits for its tier", () => {
  const built = bundle();
  const root = calibrationTreeResponseFromBundle(
    { index: built.index, tiers: [] },
    createExplorerState(),
  );
  expect(root?.currentLevel.kind).toBe("overview");
  const program = root!.groups.flatMap((group) => group.nodes)
    .find((node) => node.kind === "program")!;
  const childState = selectExplorerNode(createExplorerState(), program.selection);
  expect(calibrationTreeResponseFromBundle(
    { index: built.index, tiers: [] },
    childState,
  )).toBeNull();
});

test("bundle serialization and folder paths are deterministic", async () => {
  const first = bundle();
  const second = bundle();
  expect(second.files.map((file) => file.serialized)).toEqual(
    first.files.map((file) => file.serialized),
  );
  const buildArtifactId = first.index.buildArtifactId;
  expect(first.files.map((file) => file.path)).toEqual([
    `calibration-trees/us/${buildArtifactId}/filter-index.json.gz`,
    ...first.index.parts.targetSummaries.map((_, index) =>
      `calibration-trees/us/${buildArtifactId}/target-summary-${String(index + 1).padStart(4, "0")}.json.gz`,
    ),
    `calibration-trees/us/${buildArtifactId}/target-details-0001.json.gz`,
    ...first.index.parts.tiers.map((_, index) =>
      `calibration-trees/us/${buildArtifactId}/tier-${index + 1}.json.gz`,
    ),
    `calibration-trees/us/${buildArtifactId}/index.json.gz`,
  ]);
  first.files.forEach((file) => {
    expect(gunzipSync(file.compressed).toString("utf8")).toBe(file.serialized);
    expect(file.compressed.byteLength).toBe(file.gzipBytes);
  });

  const directory = await mkdtemp(join(tmpdir(), "calibration-tree-bundle-"));
  try {
    for (const file of first.files) {
      const destination = join(directory, file.path);
      await mkdir(dirname(destination), { recursive: true });
      await writeFile(destination, file.compressed);
    }
    const materialized = first.files.map((file) =>
      relative(directory, join(directory, file.path)),
    );
    expect(materialized).toEqual(first.files.map((file) => file.path));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("target details use deterministic contiguous ordinal ranges", () => {
  const input = {
    country: "us" as const,
    releaseId: RELEASE,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: COMMIT,
    sourceArtifacts: {
      calibrationDiagnostics: {
        path: `releases/${RELEASE}/calibration_diagnostics.json`,
        sha256: SOURCE_HASH,
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    rows,
    lossAttributionAvailable: true,
  };
  const draft = compileCalibrationTreeBundleDraft(input);
  const first = calibrationTreeTargetDetailsFromDraft(draft, 1_000);
  const second = calibrationTreeTargetDetailsFromDraft(draft, 1_000);

  expect(first.artifacts.length).toBeGreaterThan(1);
  expect(first).toEqual(second);
  expect(first.artifacts.map((artifact) => artifact.part)).toEqual(
    first.artifacts.map((_, index) =>
      `target-details-${String(index + 1).padStart(4, "0")}` as const,
    ),
  );
  expect(first.artifacts[0].startTargetOrdinal).toBe(0);
  first.artifacts.forEach((artifact, shardIndex) => {
    const next = first.artifacts[shardIndex + 1];
    if (next) expect(artifact.endTargetOrdinalExclusive).toBe(next.startTargetOrdinal);
    artifact.targets.forEach((target, offset) => {
      expect(target).toEqual(
        draft.targets[artifact.startTargetOrdinal + offset].detail,
      );
    });
  });
  expect(first.artifacts.at(-1)?.endTargetOrdinalExclusive).toBe(rows.length);
});

test("target summaries use deterministic byte-bounded shards and reconstruct ordinal order", () => {
  const draft = compileCalibrationTreeBundleDraft({
    country: "us",
    releaseId: RELEASE,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: COMMIT,
    sourceArtifacts: {
      calibrationDiagnostics: {
        path: `releases/${RELEASE}/calibration_diagnostics.json`,
        sha256: SOURCE_HASH,
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    rows,
    lossAttributionAvailable: true,
  });
  const first = calibrationTreeTargetSummariesFromDraft(draft, 1_500);
  const second = calibrationTreeTargetSummariesFromDraft(draft, 1_500);
  expect(first).toEqual(second);
  expect(first.artifacts.length).toBeGreaterThan(1);
  first.artifacts.forEach((artifact) => {
    expect(Buffer.byteLength(serializeCalibrationTreePart(artifact), "utf8"))
      .toBeLessThanOrEqual(1_500);
  });

  const built = bundle();
  const summaries = built.files.flatMap((file) =>
    file.part.startsWith("target-summary-")
      ? [file.artifact as CalibrationTreeTargetSummaryArtifact]
      : [],
  );
  expect(calibrationTreeTargetsFromSummaries(built.index, summaries))
    .toHaveLength(rows.length);
  const corruptRange = structuredClone(summaries);
  corruptRange[0].startTargetOrdinal += 1;
  corruptRange[0].endTargetOrdinalExclusive += 1;
  expect(() => calibrationTreeTargetsFromSummaries(built.index, corruptRange)).toThrow(
    "unexpected range or identity",
  );
});

test("target-detail shard limits use exact UTF-8 bytes", () => {
  const unicodeRows = [{
    ...rows[0],
    comparison_id: "unicode-target",
    name: "unicode-target",
    target_label: "Café 🌍",
    description: "é🌍".repeat(50),
  }];
  const draft = compileCalibrationTreeBundleDraft({
    country: "us",
    releaseId: RELEASE,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: COMMIT,
    sourceArtifacts: {
      calibrationDiagnostics: {
        path: `releases/${RELEASE}/calibration_diagnostics.json`,
        sha256: SOURCE_HASH,
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    rows: unicodeRows,
    lossAttributionAvailable: true,
  });
  const initial = calibrationTreeTargetDetailsFromDraft(draft, 10_000);
  const serialized = serializeCalibrationTreePart(initial.artifacts[0]);
  const exactBytes = Buffer.byteLength(serialized, "utf8");
  expect(exactBytes).toBeGreaterThan(serialized.length);
  expect(calibrationTreeTargetDetailsFromDraft(draft, exactBytes).artifacts).toHaveLength(1);
  expect(() => calibrationTreeTargetDetailsFromDraft(draft, exactBytes - 1)).toThrow(
    "exceeds",
  );
});

test("calibration tree schema 5 artifacts are rejected", () => {
  const legacy = structuredClone(bundle().index) as unknown as Record<string, unknown>;
  legacy.schemaVersion = 5;
  expect(() => parseCalibrationTreeIndex(legacy)).toThrow(
    "Unsupported calibration tree schema 5",
  );
});

test("target detail selection resolves ordinal ranges", () => {
  const built = bundle();
  Array.from({ length: built.index.targetCount }, (_, targetOrdinal) => {
    const selection = calibrationTreeTargetDetailSelection(
      built.index,
      targetOrdinal,
    );
    expect(selection.descriptor.startTargetOrdinal + selection.offset).toBe(
      targetOrdinal,
    );
  });

  expect(() => calibrationTreeTargetDetailSelection(
    built.index,
    built.index.targetCount,
  )).toThrow(
    "not covered by a detail shard",
  );
});

test("bundle validation rejects corrupt metadata and postings", () => {
  const built = bundle();
  const corruptPath = structuredClone(built);
  corruptPath.index.parts.tiers[0].path = "calibration-trees/v2/bad.json";
  expect(() => validateCalibrationTreeBundle(corruptPath)).toThrow("wrong path");

  const filterIndexFile = built.files.find((file) => file.part === "filter-index")!;
  const parsed = parseCalibrationTreePart(
    JSON.parse(filterIndexFile.serialized),
    "filter-index",
  ) as CalibrationTreeFilterIndexArtifact;
  parsed.postings.geographies[0].targetOrdinals = [];
  const corruptPosting = structuredClone(built);
  const corruptFile = corruptPosting.files.find((file) => file.part === "filter-index")!;
  corruptFile.artifact = parsed;
  corruptFile.serialized = `${JSON.stringify(parsed)}\n`;
  expect(() => validateCalibrationTreeBundle(corruptPosting)).toThrow();

  const corruptRange = structuredClone(built);
  corruptRange.index.parts.targetSummaries[0].startTargetOrdinal = 1;
  expect(() => validateCalibrationTreeBundle(corruptRange)).toThrow("invalid target range");
});
