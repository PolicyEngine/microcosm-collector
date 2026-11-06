import { createHash } from "node:crypto";

import {
  buildCalibrationComparisonBundles,
  calibrationComparisonBuildArtifactId,
  calibrationComparisonPairArtifactId,
  type ComparisonBundleSource,
} from "./calibration-comparison-bundle";
import {
  CALIBRATION_TREE_SCHEMA_VERSION,
  calibrationTreeTargetDetailSelection,
  calibrationTreeTargetsFromSummaries,
  parseCalibrationTreeIndex,
  parseCalibrationTreeTargetDetails,
  parseCalibrationTreeTargetSummary,
  type CalibrationTreeComparisonTargetDetailResponse,
  type CalibrationTreeIndexArtifact,
  type CalibrationTreeTargetDetailsArtifact,
} from "./calibration-tree-artifact";
import {
  calibrationTreeBlobText,
  getCalibrationTreeBlob,
  uploadCalibrationTreeBundle,
} from "./calibration-tree-blob";
import type { MicrocosmCountry } from "./countries";
import type { TargetChangeRow } from "./target-change";

interface ComparisonBuildIds {
  pairArtifactId: string;
  reportedBuildArtifactId: string;
  sharedBuildArtifactId: string;
}

const activeBuilds = new Map<string, Promise<ComparisonBuildIds>>();
const ACTIVE_BUILD_LIMIT = 8;

function ids(
  country: MicrocosmCountry,
  currentBuildArtifactId: string,
  candidateBuildArtifactId: string,
): ComparisonBuildIds {
  const pairArtifactId = calibrationComparisonPairArtifactId(
    country,
    currentBuildArtifactId,
    candidateBuildArtifactId,
  );
  return {
    pairArtifactId,
    reportedBuildArtifactId: calibrationComparisonBuildArtifactId(
      pairArtifactId,
      "reported",
    ),
    sharedBuildArtifactId: calibrationComparisonBuildArtifactId(
      pairArtifactId,
      "shared",
    ),
  };
}

async function textFromBlob(
  result: Awaited<ReturnType<typeof getCalibrationTreeBlob>>,
  label: string,
): Promise<string> {
  if (!result || result.statusCode !== 200 || !result.stream) {
    throw new Error(`${label} is unavailable.`);
  }
  return calibrationTreeBlobText(result.stream);
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export async function loadCalibrationComparisonSource(
  country: MicrocosmCountry,
  buildArtifactId: string,
  getBlob: typeof getCalibrationTreeBlob = getCalibrationTreeBlob,
): Promise<ComparisonBundleSource> {
  const indexText = await textFromBlob(
    await getBlob({
      country,
      buildArtifactId,
      part: "index",
      consistent: true,
    }),
    `Calibration build ${buildArtifactId} index`,
  );
  const index = parseCalibrationTreeIndex(JSON.parse(indexText));
  if (index.country !== country || index.buildArtifactId !== buildArtifactId) {
    throw new Error(`Calibration build ${buildArtifactId} index identity is inconsistent.`);
  }
  if (index.build.kind === "comparison") {
    throw new Error("A derived comparison cannot be used as a source build.");
  }
  const targetSummaries = await Promise.all(
    index.parts.targetSummaries.map(async (descriptor) => {
      const text = await textFromBlob(
        await getBlob({
          country,
          buildArtifactId,
          part: descriptor.part,
          consistent: true,
        }),
        `Calibration build ${buildArtifactId} ${descriptor.part}`,
      );
      if (sha256(text) !== descriptor.sha256) {
        throw new Error(
          `Calibration build ${buildArtifactId} ${descriptor.part} digest is invalid.`,
        );
      }
      return parseCalibrationTreeTargetSummary(JSON.parse(text), descriptor.part);
    }),
  );
  return {
    index,
    indexSha256: sha256(indexText),
    targetSummaries: calibrationTreeTargetsFromSummaries(index, targetSummaries),
  };
}

async function loadIndex(options: {
  country: MicrocosmCountry;
  buildArtifactId: string;
  expectedSha256?: string;
  getBlob: typeof getCalibrationTreeBlob;
}): Promise<CalibrationTreeIndexArtifact> {
  const text = await textFromBlob(
    await options.getBlob({
      country: options.country,
      buildArtifactId: options.buildArtifactId,
      part: "index",
      consistent: true,
    }),
    `Calibration build ${options.buildArtifactId} index`,
  );
  if (options.expectedSha256 && sha256(text) !== options.expectedSha256) {
    throw new Error(`Calibration build ${options.buildArtifactId} index digest is invalid.`);
  }
  const index = parseCalibrationTreeIndex(JSON.parse(text));
  if (
    index.country !== options.country ||
    index.buildArtifactId !== options.buildArtifactId
  ) {
    throw new Error(
      `Calibration build ${options.buildArtifactId} index identity is inconsistent.`,
    );
  }
  return index;
}

export async function loadCalibrationComparisonTargetDetail(options: {
  country: MicrocosmCountry;
  buildArtifactId: string;
  targetOrdinal: number;
  getBlob?: typeof getCalibrationTreeBlob;
}): Promise<CalibrationTreeComparisonTargetDetailResponse> {
  const getBlob = options.getBlob ?? getCalibrationTreeBlob;
  const comparisonIndex = await loadIndex({
    country: options.country,
    buildArtifactId: options.buildArtifactId,
    getBlob,
  });
  if (
    comparisonIndex.build.kind !== "comparison" ||
    comparisonIndex.parts.targetDetailStrategy !== "source-targets" ||
    !comparisonIndex.comparison
  ) {
    throw new Error("The requested build does not use source-backed comparison details.");
  }
  if (
    !Number.isSafeInteger(options.targetOrdinal) ||
    options.targetOrdinal < 0 ||
    options.targetOrdinal >= comparisonIndex.targetCount
  ) {
    throw new Error("The requested comparison target ordinal is out of range.");
  }
  const summaryDescriptor = comparisonIndex.parts.targetSummaries.find(
    (descriptor) =>
      options.targetOrdinal >= descriptor.startTargetOrdinal &&
      options.targetOrdinal < descriptor.endTargetOrdinalExclusive,
  );
  if (!summaryDescriptor) {
    throw new Error("The requested comparison target has no summary shard.");
  }
  const summaryText = await textFromBlob(
    await getBlob({
      country: options.country,
      buildArtifactId: options.buildArtifactId,
      part: summaryDescriptor.part,
      consistent: true,
    }),
    `Calibration comparison ${options.buildArtifactId} ${summaryDescriptor.part}`,
  );
  if (sha256(summaryText) !== summaryDescriptor.sha256) {
    throw new Error("The comparison target-summary digest is invalid.");
  }
  const summaryArtifact = parseCalibrationTreeTargetSummary(
    JSON.parse(summaryText),
    summaryDescriptor.part,
  );
  const summary = summaryArtifact.targets[
    options.targetOrdinal - summaryDescriptor.startTargetOrdinal
  ];
  if (!summary?.detailSource) {
    throw new Error("The comparison target does not identify its source targets.");
  }

  const sourceIndexes = new Map<string, Promise<CalibrationTreeIndexArtifact>>();
  const sourceDetails = new Map<string, Promise<CalibrationTreeTargetDetailsArtifact>>();
  const sourceIndex = (
    buildArtifactId: string,
    expectedSha256: string,
  ): Promise<CalibrationTreeIndexArtifact> => {
    const existing = sourceIndexes.get(buildArtifactId);
    if (existing) return existing;
    const loading = loadIndex({
      country: options.country,
      buildArtifactId,
      expectedSha256,
      getBlob,
    });
    sourceIndexes.set(buildArtifactId, loading);
    return loading;
  };
  const sourceDetail = async (
    buildArtifactId: string,
    expectedIndexSha256: string,
    targetOrdinal: number | null,
  ) => {
    if (targetOrdinal == null) return null;
    const index = await sourceIndex(buildArtifactId, expectedIndexSha256);
    if (index.build.kind === "comparison") {
      throw new Error("Comparison target details must resolve to source builds.");
    }
    const selection = calibrationTreeTargetDetailSelection(index, targetOrdinal);
    const key = `${buildArtifactId}:${selection.descriptor.part}`;
    let loading = sourceDetails.get(key);
    if (!loading) {
      loading = (async () => {
        const text = await textFromBlob(
          await getBlob({
            country: options.country,
            buildArtifactId,
            part: selection.descriptor.part,
            consistent: true,
          }),
          `Calibration build ${buildArtifactId} ${selection.descriptor.part}`,
        );
        if (sha256(text) !== selection.descriptor.sha256) {
          throw new Error(
            `Calibration build ${buildArtifactId} ${selection.descriptor.part} digest is invalid.`,
          );
        }
        return parseCalibrationTreeTargetDetails(
          JSON.parse(text),
          selection.descriptor.part,
        );
      })();
      sourceDetails.set(key, loading);
    }
    const artifact = await loading;
    return artifact.targets[selection.offset] ?? null;
  };

  const currentSource = comparisonIndex.build.sourceArtifacts.comparisonCurrentIndex;
  const candidateSource = comparisonIndex.build.sourceArtifacts.comparisonCandidateIndex;
  if (!currentSource || !candidateSource) {
    throw new Error("The comparison build does not identify both source indexes.");
  }
  const { currentBuildArtifactId, candidateBuildArtifactId } = comparisonIndex.comparison;
  const [currentDetail, candidateDetail] = await Promise.all([
    sourceDetail(
      currentBuildArtifactId,
      currentSource.sha256,
      summary.detailSource.currentTargetOrdinal,
    ),
    sourceDetail(
      candidateBuildArtifactId,
      candidateSource.sha256,
      summary.detailSource.candidateTargetOrdinal,
    ),
  ]);
  const compact = summary.comparison.row as TargetChangeRow;
  return {
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    country: options.country,
    buildArtifactId: options.buildArtifactId,
    targetOrdinal: options.targetOrdinal,
    targetId: summary.id,
    target: {
      ...compact,
      currentDetail,
      candidateDetail,
    },
  };
}

async function comparisonExists(
  country: MicrocosmCountry,
  buildArtifactId: string,
): Promise<boolean> {
  return Boolean(await getCalibrationTreeBlob({
    country,
    buildArtifactId,
    part: "index",
    consistent: true,
  }));
}

async function buildAndPublishComparison(options: {
  country: MicrocosmCountry;
  currentBuildArtifactId: string;
  candidateBuildArtifactId: string;
  token: string;
}): Promise<ComparisonBuildIds> {
  const result = ids(
    options.country,
    options.currentBuildArtifactId,
    options.candidateBuildArtifactId,
  );
  const [reportedExists, sharedExists] = await Promise.all([
    comparisonExists(options.country, result.reportedBuildArtifactId),
    comparisonExists(options.country, result.sharedBuildArtifactId),
  ]);
  if (reportedExists && sharedExists) return result;

  const [current, candidate] = await Promise.all([
    loadCalibrationComparisonSource(options.country, options.currentBuildArtifactId),
    loadCalibrationComparisonSource(options.country, options.candidateBuildArtifactId),
  ]);
  const bundles = buildCalibrationComparisonBundles(current, candidate);
  if (bundles.pairArtifactId !== result.pairArtifactId) {
    throw new Error("Calibration comparison identity is inconsistent.");
  }
  await Promise.all([
    uploadCalibrationTreeBundle({ bundle: bundles.reported, token: options.token }),
    uploadCalibrationTreeBundle({ bundle: bundles.shared, token: options.token }),
  ]);
  return result;
}

export function ensureCalibrationComparison(options: {
  country: MicrocosmCountry;
  currentBuildArtifactId: string;
  candidateBuildArtifactId: string;
  token: string;
}): Promise<ComparisonBuildIds> {
  const key = [
    options.country,
    options.currentBuildArtifactId,
    options.candidateBuildArtifactId,
  ].join(":");
  const existing = activeBuilds.get(key);
  if (existing) return existing;
  const promise = buildAndPublishComparison(options);
  activeBuilds.set(key, promise);
  while (activeBuilds.size > ACTIVE_BUILD_LIMIT) {
    const oldest = activeBuilds.keys().next().value;
    if (typeof oldest !== "string") break;
    activeBuilds.delete(oldest);
  }
  void promise.then(
    () => activeBuilds.delete(key),
    () => activeBuilds.delete(key),
  );
  return promise;
}
