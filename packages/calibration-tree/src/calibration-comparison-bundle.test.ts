import { expect, test } from "bun:test";

import { buildCalibrationComparisonBundles } from "./calibration-comparison-bundle";
import {
  calibrationTreeTargetsFromSummaries,
  type CalibrationTreeTargetSummaryArtifact,
} from "./calibration-tree-artifact";
import { buildCalibrationTreeBundle } from "./calibration-tree-bundle";

function source(buildArtifactId: string, releaseId: string, contribution: number) {
  const bundle = buildCalibrationTreeBundle({
    country: "us",
    buildArtifactId,
    releaseId,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: buildArtifactId.slice(0, 40),
    sourceArtifacts: {
      calibrationDiagnostics: {
        path: `releases/${releaseId}/calibration_diagnostics.json`,
        sha256: buildArtifactId,
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    rows: [{
      name: "snap|national",
      base_name: "snap|national",
      source: "chronicle",
      variable: "snap",
      target_representation: "hierarchy",
      target_dimensions: [],
      target: 100,
      final_estimate: 100 + contribution * 100,
      abs_relative_error: contribution,
      target_loss_weight: 1,
      target_loss_weight_share: 1,
      target_loss_scale: 100,
      final_capped_scaled_error: contribution,
      final_loss_contribution: contribution,
      calibration_status: "included",
    }],
    comparison: {
      releaseId,
      status: "reported",
      aggregate: contribution,
      cap: 1,
      basisIdentifier: "basis",
      targetRepresentation: "hierarchy",
    },
  });
  const summaries = bundle.files.flatMap((file) =>
    file.part.startsWith("target-summary-")
      ? [file.artifact as CalibrationTreeTargetSummaryArtifact]
      : [],
  );
  return {
    index: bundle.index,
    indexSha256: bundle.files.find((file) => file.part === "index")!.sha256,
    targetSummaries: calibrationTreeTargetsFromSummaries(bundle.index, summaries),
  };
}

test("comparison bundles are deterministic and contain both tree modes", () => {
  const current = source("a".repeat(64), "release-a", 0.1);
  const candidate = source("b".repeat(64), "release-b", 0.05);
  const first = buildCalibrationComparisonBundles(current, candidate);
  const second = buildCalibrationComparisonBundles(current, candidate);

  expect(second.pairArtifactId).toBe(first.pairArtifactId);
  expect(second.reported.files.map((file) => file.serialized)).toEqual(
    first.reported.files.map((file) => file.serialized),
  );
  expect(first.reported.index.comparison).toMatchObject({
    mode: "reported",
    available: true,
    currentBuildArtifactId: "a".repeat(64),
    candidateBuildArtifactId: "b".repeat(64),
    summary: { netChange: -0.05 },
  });
  expect(first.shared.index.comparison).toMatchObject({
    mode: "shared",
    available: true,
  });
  expect(first.reported.index.build.kind).toBe("comparison");
  expect(first.reported.index.parts.targetDetailStrategy).toBe("source-targets");
  expect(first.reported.index.parts.targetDetails).toEqual([]);
  expect(first.reported.files.some((file) =>
    file.part.startsWith("target-details-"),
  )).toBe(false);
  expect(first.shared.index.parts.targetDetailStrategy).toBe("source-targets");
  expect(first.shared.index.parts.targetDetails).toEqual([]);
  expect(first.reported.index.build.sourceArtifacts).toMatchObject({
    calibrationDiagnostics: null,
    comparisonCurrentIndex: {
      path: `calibration-trees/us/${current.index.buildArtifactId}/index.json.gz`,
    },
    comparisonCandidateIndex: {
      path: `calibration-trees/us/${candidate.index.buildArtifactId}/index.json.gz`,
    },
  });
});
