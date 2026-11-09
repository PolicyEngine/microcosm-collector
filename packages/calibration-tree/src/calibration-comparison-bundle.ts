import { createHash } from "node:crypto";

import { buildTargetChangeDatasetFromSummaries } from "./calibration-build-comparison";
import {
  CALIBRATION_TREE_SCHEMA_VERSION,
  calibrationTreePartPath,
  type CalibrationTreeIndexArtifact,
  type CalibrationTreeTargetSummary,
} from "./calibration-tree-artifact";
import type { CalibrationTreeTarget } from "./calibration-tree";
import {
  buildCalibrationTreeBundle,
  type CalibrationTreeBundle,
} from "./calibration-tree-bundle";
import { targetChangeForMode, type TargetChangeMode } from "./target-change";

export interface ComparisonBundleSource {
  index: CalibrationTreeIndexArtifact;
  indexSha256: string;
  targetSummaries: CalibrationTreeTargetSummary[];
}

export interface CalibrationComparisonBundles {
  pairArtifactId: string;
  reported: CalibrationTreeBundle;
  shared: CalibrationTreeBundle;
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function calibrationComparisonPairArtifactId(
  country: string,
  currentBuildArtifactId: string,
  candidateBuildArtifactId: string,
): string {
  return sha256(JSON.stringify({
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    country,
    currentBuildArtifactId,
    candidateBuildArtifactId,
  }));
}

export function calibrationComparisonBuildArtifactId(
  pairArtifactId: string,
  mode: TargetChangeMode,
): string {
  return sha256(JSON.stringify({
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    pairArtifactId,
    mode,
  }));
}

function rowsForMode(
  rows: ReturnType<typeof buildTargetChangeDatasetFromSummaries>["rows"],
  mode: TargetChangeMode,
): CalibrationTreeTarget[] {
  return rows.flatMap((row) => {
    if (mode === "shared" && row.comparison_status !== "shared") return [];
    const change = targetChangeForMode(row, mode);
    return change == null ? [] : [{ ...row, target_change: change }];
  });
}

export function buildCalibrationComparisonBundles(
  current: ComparisonBundleSource,
  candidate: ComparisonBundleSource,
): CalibrationComparisonBundles {
  if (current.index.country !== candidate.index.country) {
    throw new Error("Calibration builds from different countries cannot be compared.");
  }
  const country = current.index.country;
  const currentBuildArtifactId = current.index.buildArtifactId;
  const candidateBuildArtifactId = candidate.index.buildArtifactId;
  const pairArtifactId = calibrationComparisonPairArtifactId(
    country,
    currentBuildArtifactId,
    candidateBuildArtifactId,
  );
  const dataset = buildTargetChangeDatasetFromSummaries(
    {
      country,
      comparison: current.index.targetComparison,
      targets: current.targetSummaries,
    },
    {
      country,
      comparison: candidate.index.targetComparison,
      targets: candidate.targetSummaries,
    },
  );
  const buildForMode = (mode: TargetChangeMode): CalibrationTreeBundle => {
    const buildArtifactId = calibrationComparisonBuildArtifactId(
      pairArtifactId,
      mode,
    );
    return buildCalibrationTreeBundle({
      country,
      buildArtifactId,
      buildKind: "comparison",
      sourceId: `${currentBuildArtifactId}:${candidateBuildArtifactId}:${mode}`,
      label: `${dataset.current.releaseId} → ${dataset.candidate.releaseId}`,
      createdAt: candidate.index.build.createdAt,
      releaseId: dataset.candidate.releaseId,
      hfRepo: null,
      hfCommitSha: null,
      sourceArtifacts: {
        calibrationDiagnostics: null,
        buildManifest: null,
        releaseManifest: null,
        demographics: null,
        comparisonCurrentIndex: {
          path: calibrationTreePartPath(country, currentBuildArtifactId, "index"),
          sha256: current.indexSha256,
        },
        comparisonCandidateIndex: {
          path: calibrationTreePartPath(country, candidateBuildArtifactId, "index"),
          sha256: candidate.indexSha256,
        },
      },
      rows: rowsForMode(dataset.rows, mode),
      calibrationProvenance: dataset.candidate.calibrationProvenance,
      lossAttributionAvailable: dataset.available,
      comparison: candidate.index.targetComparison,
      comparisonResult: {
        pairArtifactId,
        currentBuildArtifactId,
        candidateBuildArtifactId,
        mode,
        available: dataset.available && dataset.summaries[mode] != null,
        reason: dataset.reason ?? dataset.modeReasons[mode],
        current: dataset.current,
        candidate: dataset.candidate,
        methodology: dataset.methodology,
        matching: dataset.matching,
        summary: dataset.summaries[mode],
      },
    });
  };

  return {
    pairArtifactId,
    reported: buildForMode("reported"),
    shared: buildForMode("shared"),
  };
}
