import { expect, test } from "bun:test";
import { gzipSync } from "node:zlib";

import {
  loadCalibrationComparisonSource,
  loadCalibrationComparisonTargetDetail,
} from "./calibration-comparison-blob";
import { buildCalibrationComparisonBundles } from "./calibration-comparison-bundle";
import type { CalibrationTreeTargetSummaryArtifact } from "./calibration-tree-artifact";
import { getCalibrationTreeBlob } from "./calibration-tree-blob";
import { buildCalibrationTreeBundle } from "./calibration-tree-bundle";

const BUILD = "a".repeat(64);

function bundle(
  buildArtifactId = BUILD,
  releaseId = "release-a",
  target = 100,
) {
  return buildCalibrationTreeBundle({
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
      target,
      final_estimate: 105,
      abs_relative_error: 0.05,
      target_loss_weight: 1,
      target_loss_weight_share: 1,
      target_loss_scale: 100,
      final_capped_scaled_error: 0.05,
      final_loss_contribution: 0.05,
      calibration_status: "included",
    }],
    comparison: {
      releaseId,
      status: "reported",
      aggregate: 0.05,
      cap: 1,
      basisIdentifier: "basis",
      targetRepresentation: "hierarchy",
    },
  });
}

function blobResult(pathname: string, body: string) {
  const compressed = gzipSync(body);
  return {
    statusCode: 200 as const,
    stream: new Blob([compressed]).stream(),
    headers: new Headers(),
    blob: {
      url: `https://blob.example/${pathname}`,
      downloadUrl: `https://blob.example/${pathname}?download=1`,
      pathname,
      contentDisposition: "inline",
      cacheControl: "public, max-age=31536000",
      uploadedAt: new Date("2026-09-18T00:00:00.000Z"),
      etag: '"etag"',
      contentType: "application/gzip",
      size: compressed.byteLength,
    },
  };
}

test("comparison source loading reads only the index and target summaries", async () => {
  const built = bundle();
  const requested: string[] = [];
  const getBlob = (async (options: Parameters<typeof getCalibrationTreeBlob>[0]) => {
    requested.push(options.part);
    const file = built.files.find((candidate) => candidate.part === options.part);
    return file ? blobResult(file.path, file.serialized) : null;
  }) as typeof getCalibrationTreeBlob;

  const source = await loadCalibrationComparisonSource("us", BUILD, getBlob);

  expect(source.targetSummaries).toHaveLength(1);
  expect(requested).toEqual(["index", "target-summary-0001"]);
  expect(requested).not.toContain("filter-index");
  expect(requested.some((part) => part.startsWith("target-details-"))).toBe(false);
});

test("comparison source loading rejects a target-summary digest mismatch", async () => {
  const built = bundle();
  const getBlob = (async (options: Parameters<typeof getCalibrationTreeBlob>[0]) => {
    const file = built.files.find((candidate) => candidate.part === options.part);
    if (!file) return null;
    const body = options.part.startsWith("target-summary-")
      ? `${file.serialized} `
      : file.serialized;
    return blobResult(file.path, body);
  }) as typeof getCalibrationTreeBlob;

  expect(loadCalibrationComparisonSource("us", BUILD, getBlob)).rejects.toThrow(
    "digest is invalid",
  );
});

test("comparison target detail reads only the selected source detail shards", async () => {
  const current = bundle("a".repeat(64), "release-current", 100);
  const candidate = bundle("b".repeat(64), "release-candidate", 101);
  const source = (built: typeof current) => ({
    index: built.index,
    indexSha256: built.files.find((file) => file.part === "index")!.sha256,
    targetSummaries: built.files.flatMap((file) =>
      file.part.startsWith("target-summary-")
        ? (file.artifact as CalibrationTreeTargetSummaryArtifact).targets
        : [],
    ),
  });
  const comparison = buildCalibrationComparisonBundles(
    source(current),
    source(candidate),
  ).reported;
  const bundles = [current, candidate, comparison];
  const requested: Array<{ buildArtifactId: string; part: string }> = [];
  const getBlob = (async (options: Parameters<typeof getCalibrationTreeBlob>[0]) => {
    requested.push({
      buildArtifactId: options.buildArtifactId,
      part: options.part,
    });
    const built = bundles.find(
      (candidateBundle) => candidateBundle.index.buildArtifactId === options.buildArtifactId,
    );
    const file = built?.files.find((candidateFile) => candidateFile.part === options.part);
    return file ? blobResult(file.path, file.serialized) : null;
  }) as typeof getCalibrationTreeBlob;

  const result = await loadCalibrationComparisonTargetDetail({
    country: "us",
    buildArtifactId: comparison.index.buildArtifactId,
    targetOrdinal: 0,
    getBlob,
  });

  expect(result.target.currentDetail?.target).toBe(100);
  expect(result.target.candidateDetail?.target).toBe(101);
  expect(requested).toContainEqual({
    buildArtifactId: comparison.index.buildArtifactId,
    part: "index",
  });
  expect(requested).toContainEqual({
    buildArtifactId: comparison.index.buildArtifactId,
    part: "target-summary-0001",
  });
  expect(requested.filter((request) => request.part.startsWith("target-details-")))
    .toHaveLength(2);
  expect(requested.some((request) =>
    request.buildArtifactId === comparison.index.buildArtifactId &&
    request.part.startsWith("target-details-"),
  )).toBe(false);
});
