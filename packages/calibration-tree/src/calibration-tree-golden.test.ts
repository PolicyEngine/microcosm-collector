import { expect, test } from "bun:test";

import recordedGoldens from "./fixtures/calibration-tree-golden.json";
import {
  GOLDEN_COMPARISON_CASES,
  GOLDEN_SOURCE_CASES,
  GZIP_HEADER_WITHOUT_OS,
  goldenComparisonRecord,
  goldenManifestRecord,
  goldenSourceBuild,
  goldenSourceRecord,
  type CalibrationTreeGoldenRecord,
  type GoldenComparisonCaseName,
  type GoldenSourceCaseName,
} from "./test-support/calibration-tree-golden-cases";

// The record was produced by the builder before it moved into its own
// package. Every case must reproduce it exactly: paths, raw JSON digests and
// sizes, gzip sizes, deflate streams, build ids, and reader responses.
const recorded = recordedGoldens as CalibrationTreeGoldenRecord;

// The US release has 5,659 targets; building and validating its bundle
// gzips about 40 MB three times. The limit is generous so the suite also
// passes on a heavily loaded machine.
const BUILD_TIMEOUT_MS = 300_000;

const SOURCE_DESCRIPTIONS: Record<GoldenSourceCaseName, string> = {
  "us-release": "a US legacy release",
  "uk-release": "a UK schema 8 hierarchy release",
  "uk-release-loss-attribution": "a UK release with target loss attribution",
  "be-release": "a Belgium release",
  "us-staging": "a US version 1 staging run",
  "uk-staging": "a UK version 2 staging run",
  "uk-staging-staged-bundle": "a UK staging run read from its staged dataset bundle",
};

const COMPARISON_DESCRIPTIONS: Record<GoldenComparisonCaseName, string> = {
  "us-comparison": "a US release-to-staging comparison",
  "uk-comparison": "a UK release-to-staging comparison",
};

for (const name of GOLDEN_SOURCE_CASES) {
  test(
    `${SOURCE_DESCRIPTIONS[name]} reproduces its recorded tree bundle and reader responses`,
    async () => {
      expect(await goldenSourceRecord(name)).toEqual(recorded.sources[name]);
    },
    BUILD_TIMEOUT_MS,
  );
}

for (const name of GOLDEN_COMPARISON_CASES) {
  test(
    `${COMPARISON_DESCRIPTIONS[name]} reproduces its recorded bundles and target detail`,
    async () => {
      expect(await goldenComparisonRecord(name)).toEqual(recorded.comparisons[name]);
    },
    BUILD_TIMEOUT_MS,
  );
}

test(
  "the tree manifest for every recorded build reproduces its recorded bytes",
  async () => {
    expect(await goldenManifestRecord()).toEqual(recorded.manifest);
  },
  BUILD_TIMEOUT_MS,
);

test(
  "every recorded part is gzip with the fixed header ahead of its recorded deflate stream",
  async () => {
    for (const name of GOLDEN_SOURCE_CASES) {
      const { bundle } = await goldenSourceBuild(name);
      for (const file of bundle.files) {
        expect([...file.compressed.subarray(0, GZIP_HEADER_WITHOUT_OS.length)])
          .toEqual(GZIP_HEADER_WITHOUT_OS);
      }
    }
  },
  BUILD_TIMEOUT_MS,
);

test("the recorded US release spans several target-summary and target-detail shards", () => {
  const parts = recorded.sources["us-release"].files.map((file) => file.part);
  expect(parts.filter((part) => part.startsWith("target-summary-")).length)
    .toBeGreaterThan(1);
  expect(parts.filter((part) => part.startsWith("target-details-")).length)
    .toBeGreaterThan(1);
  expect(parts.filter((part) => part.startsWith("tier-")).length).toBeGreaterThan(1);
});
