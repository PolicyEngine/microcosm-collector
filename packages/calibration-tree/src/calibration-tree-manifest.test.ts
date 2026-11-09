import { expect, test } from "bun:test";

import { CountryNotConfiguredError } from "./countries";
import {
  calibrationBuildsNewestFirst,
  calibrationTreeCountryManifest,
  calibrationTreeManifestEntry,
  emptyCalibrationTreeManifest,
  parseCalibrationTreeManifest,
  releaseDirectoryOf,
  serializeCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";

const entry: CalibrationTreeManifestEntry = {
  buildArtifactId: "a".repeat(64),
  kind: "release",
  sourceId: "microcosm-us-test-20260915",
  label: "microcosm-us-test-20260915",
  releaseId: "microcosm-us-test-20260915",
  stagingRunId: null,
  hfRepo: "policyengine/populace-us",
  hfCommitSha: "1234567890abcdef1234567890abcdef12345678",
  treeSchemaVersion: 6,
  indexSha256: "b".repeat(64),
  indexBytes: 1234,
  createdAt: "2026-09-15T12:00:00.000Z",
  updatedAt: "2026-09-15T12:00:00.000Z",
};

test("manifest updates one country without replacing other country entries", () => {
  const uk = {
    ...entry,
    buildArtifactId: "c".repeat(64),
    sourceId: "microcosm-uk-test-20260915",
    label: "microcosm-uk-test-20260915",
    releaseId: "microcosm-uk-test-20260915",
    hfRepo: "policyengine/populace-uk-private",
  };
  const manifest = withCalibrationTreeManifestEntry(
    withCalibrationTreeManifestEntry(emptyCalibrationTreeManifest(), "uk", uk),
    "us",
    entry,
  );
  expect(calibrationTreeManifestEntry(manifest, "us", {
    releaseId: entry.releaseId!,
  })).toEqual(entry);
  expect(calibrationTreeManifestEntry(manifest, "uk", {
    releaseId: uk.releaseId!,
  })).toEqual(uk);
  expect(serializeCalibrationTreeManifest(manifest)).toBe(
    serializeCalibrationTreeManifest(parseCalibrationTreeManifest(manifest)),
  );
});

function notConfiguredReason(read: () => unknown): string | null {
  try {
    read();
  } catch (error) {
    return error instanceof CountryNotConfiguredError ? error.reason : `other: ${String(error)}`;
  }
  return null;
}

test("a country without a published release is not configured, not another country's build", () => {
  // Belgium has no entry; the UK has a build that is not its latest release.
  const manifest = withCalibrationTreeManifestEntry(emptyCalibrationTreeManifest(), "uk", {
    ...entry,
    hfRepo: "policyengine/populace-uk-private",
  });
  expect(notConfiguredReason(() => calibrationTreeCountryManifest(manifest, "be"))).toBe(
    "no_published_release",
  );
  expect(
    notConfiguredReason(() => calibrationTreeManifestEntry(manifest, "be", { releaseId: "latest" })),
  ).toBe("no_published_release");
  expect(
    notConfiguredReason(() => calibrationTreeManifestEntry(manifest, "uk", { releaseId: "latest" })),
  ).toBe("no_published_release");
  expect(notConfiguredReason(() => calibrationTreeManifestEntry(manifest, "uk", {}))).toBe(
    "no_published_release",
  );
  // An exact release id still resolves, and an unknown one is a plain miss.
  expect(
    calibrationTreeManifestEntry(manifest, "uk", { releaseId: entry.releaseId! }).buildArtifactId,
  ).toBe(entry.buildArtifactId);
  expect(
    notConfiguredReason(() => calibrationTreeManifestEntry(manifest, "uk", { releaseId: "missing" })),
  ).toBe("other: Error: No matching calibration build exists for uk.");
});

test("manifest validation rejects mutable or malformed content identities", () => {
  expect(() => parseCalibrationTreeManifest({
    schemaVersion: 6,
    countries: {
      us: { latestReleaseBuildArtifactId: null, builds: [{ ...entry, hfCommitSha: "main" }] },
    },
  })).toThrow("invalid HF commit SHA");
  expect(() => parseCalibrationTreeManifest({
    schemaVersion: 6,
    countries: {
      us: { latestReleaseBuildArtifactId: null, builds: [{ ...entry, indexSha256: "short" }] },
    },
  })).toThrow("invalid index hash");
  expect(() => parseCalibrationTreeManifest({
    schemaVersion: 5,
    countries: {},
  })).toThrow("Unsupported calibration tree manifest schema 5");
});

test("manifest rejects two immutable builds for the same release or staging run", () => {
  expect(() => parseCalibrationTreeManifest({
    schemaVersion: 6,
    countries: {
      us: {
        latestReleaseBuildArtifactId: null,
        builds: [entry, { ...entry, buildArtifactId: "d".repeat(64) }],
      },
    },
  })).toThrow("repeats a source alias");

  const manifest = withCalibrationTreeManifestEntry(
    emptyCalibrationTreeManifest(),
    "us",
    entry,
  );
  expect(() => withCalibrationTreeManifestEntry(
    manifest,
    "us",
    { ...entry, buildArtifactId: "d".repeat(64) },
  )).toThrow("already resolves to build");
});

test("re-adding a build replaces its metadata and keeps its content identity immutable", () => {
  const placeholder = { ...entry, createdAt: null, updatedAt: "1970-01-01T00:00:00.000Z" };
  const stamped = { ...entry, createdAt: "2026-10-04T18:57:22.000Z", updatedAt: "2026-10-04T18:57:22.000Z" };
  const manifest = withCalibrationTreeManifestEntry(
    withCalibrationTreeManifestEntry(emptyCalibrationTreeManifest(), "us", placeholder, true),
    "us",
    stamped,
  );
  expect(calibrationTreeManifestEntry(manifest, "us", { releaseId: entry.releaseId! })).toEqual(stamped);
  expect(manifest.countries.us?.builds).toHaveLength(1);
  expect(manifest.countries.us?.latestReleaseBuildArtifactId).toBe(entry.buildArtifactId);
  expect(() =>
    withCalibrationTreeManifestEntry(manifest, "us", { ...stamped, indexSha256: "f".repeat(64) }),
  ).toThrow("immutable");
});

test("release cuts sit beside their line folder's build and keep their folder", () => {
  // The line folder's build (published before cuts were recognised) and one
  // build per cut tag: distinct aliases, so the country stays valid.
  const line = { ...entry, releaseId: "microcosm-zz-2024-national", sourceId: "microcosm-zz-2024-national", label: "microcosm-zz-2024-national" };
  const cutTag = "microcosm-zz-2024-national-20261019T083323Z-0a1b2c3d";
  const cut: CalibrationTreeManifestEntry = {
    ...entry,
    buildArtifactId: "e".repeat(64),
    sourceId: cutTag,
    label: cutTag,
    releaseId: cutTag,
    releaseDirectory: "microcosm-zz-2024-national",
  };
  const manifest = withCalibrationTreeManifestEntry(
    withCalibrationTreeManifestEntry(emptyCalibrationTreeManifest(), "uk", line),
    "uk",
    cut,
    true,
  );
  const parsed = parseCalibrationTreeManifest(
    JSON.parse(serializeCalibrationTreeManifest(manifest)),
  );
  expect(calibrationTreeManifestEntry(parsed, "uk", { releaseId: cutTag })).toEqual(cut);
  expect(calibrationTreeManifestEntry(parsed, "uk", {})).toEqual(cut);
  expect(releaseDirectoryOf(cut)).toBe("microcosm-zz-2024-national");
  expect(releaseDirectoryOf(line)).toBe("microcosm-zz-2024-national");

  expect(() => withCalibrationTreeManifestEntry(
    manifest,
    "uk",
    { ...cut, releaseDirectory: "microcosm-zz-2024-dense" },
  )).toThrow("immutable");
  expect(() => parseCalibrationTreeManifest({
    schemaVersion: 6,
    countries: {
      uk: { latestReleaseBuildArtifactId: null, builds: [{ ...cut, releaseDirectory: "../latest" }] },
    },
  })).toThrow("invalid release directory");
});

test("orders calibration builds by instant across RFC 3339 offsets", () => {
  const builds = [
    {
      buildArtifactId: "offset-earlier",
      createdAt: "2026-01-01T00:30:00+02:00",
      updatedAt: "2026-01-01T00:30:00+02:00",
    },
    {
      buildArtifactId: "utc-later",
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    },
    {
      buildArtifactId: "same-instant-b",
      createdAt: null,
      updatedAt: "2025-12-31T19:00:00-05:00",
    },
    {
      buildArtifactId: "same-instant-a",
      createdAt: "2026-01-01T00:00:00+00:00",
      updatedAt: "2026-01-01T00:00:00+00:00",
    },
  ];

  expect(
    calibrationBuildsNewestFirst(builds).map((build) => build.buildArtifactId),
  ).toEqual([
    "same-instant-a",
    "same-instant-b",
    "utc-later",
    "offset-earlier",
  ]);
  expect(builds.map((build) => build.buildArtifactId)).toEqual([
    "offset-earlier",
    "utc-later",
    "same-instant-b",
    "same-instant-a",
  ]);
});

test("an undated calibration build sorts after every dated one", () => {
  const builds = [
    { buildArtifactId: "undated-b", createdAt: null, updatedAt: null },
    {
      buildArtifactId: "dated",
      createdAt: "2026-10-04T18:57:22Z",
      updatedAt: "2026-10-04T18:57:22Z",
    },
    { buildArtifactId: "undated-a", createdAt: null, updatedAt: null },
  ];
  expect(
    calibrationBuildsNewestFirst(builds).map((build) => build.buildArtifactId),
  ).toEqual(["dated", "undated-a", "undated-b"]);
});
